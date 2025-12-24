"""
Refactored Orchestrator - New signal generation flow
"""
import asyncio
import threading
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta, time
import pytz
import pandas as pd
from sqlalchemy import desc

from .config import config
from .models import MarketSnapshot, CompositeSignal, TradeSetup, ConfidenceLevel
from .generators import (
    MomentumGenerator,
    VolumeAnomalyGenerator,
    RangeExpansionGenerator,
    ReversalGenerator,
    IndexAlignmentGenerator
)
from .aggregator import SignalAggregator
from .agents import (
    LLMSignalAnalyst,
    LLMTradeReviewer,
    ConfidenceEngine,
    TradeConstructionAgent
)
from .snapshot_builder import SnapshotBuilder
from .risk_agent import RiskAgent
from .execution_agent import ExecutionAgent
from .stock_scanner import StockScannerAgent
from ..database import SessionLocal, IntradayCandle, SmartTraderSignal, get_or_create_company

# Import Consolidated Fyers client
from ..services.fyers_client import get_fyers_client

def is_market_open_ist() -> bool:
    """Check if current time is within NSE market hours (9:15 AM - 3:30 PM IST)"""
    tz = pytz.timezone('Asia/Kolkata')
    now = datetime.now(tz)

    # Weekends check
    if now.weekday() >= 5: # Sat=5, Sun=6
        return False

    market_start = time(9, 15)
    market_end = time(15, 30)
    current_time = now.time()

    return market_start <= current_time <= market_end

class NewOrchestratorAgent:
    """
    Refactored orchestrator with deterministic signal generation + LLM enhancement.
    """
    
    def __init__(self, custom_config: Optional[Dict[str, Any]] = None):
        self.config = custom_config or config
        
        # Initialize generators
        self.generators = [
            MomentumGenerator(self.config),
            VolumeAnomalyGenerator(self.config),
            RangeExpansionGenerator(self.config),
            ReversalGenerator(self.config),
            IndexAlignmentGenerator(self.config)
        ]
        
        # Initialize aggregator
        self.aggregator = SignalAggregator()
        
        # Initialize agents
        self.llm_analyst = LLMSignalAnalyst(self.config)
        self.llm_reviewer = LLMTradeReviewer(self.config)
        self.confidence_engine = ConfidenceEngine(self.config)
        self.trade_constructor = TradeConstructionAgent(self.config)
        self.risk_agent = RiskAgent(self.config)
        self.execution_agent = ExecutionAgent(self.config)
        
        # Stock scanner for getting symbols
        self.stock_scanner = StockScannerAgent(self.config)
        
        # State
        self.current_signals: List[CompositeSignal] = []
        self.is_running = False
        self.scanner_thread = None
        
    def start_market_session(self):
        """Start the trading session"""
        if self.is_running:
            print("Session already running")
            return
        
        self.is_running = True
        self.scanner_thread = threading.Thread(target=self._scanner_loop, daemon=True)
        self.scanner_thread.start()
        print("✅ Market session started")
    
    def stop_market_session(self):
        """Stop the trading session"""
        self.is_running = False
        if self.scanner_thread:
            self.scanner_thread.join(timeout=5)
        print("🛑 Market session stopped")
    
    def _scanner_loop(self):
        """Main scanner loop"""
        import time
        
        while self.is_running:
            try:
                # Market Hours Guard
                if not is_market_open_ist():
                   print("💤 Market closed. Sleeping...")
                   time.sleep(60)
                   continue

                self._scan_cycle()
                time.sleep(self.config.get('scan_interval', 60))
            except Exception as e:
                print(f"Error in scanner loop: {e}")
                time.sleep(10)
    
    def _scan_cycle(self):
        """Execute one scan cycle"""
        print(f"\n{'='*60}")
        print(f"🔍 Scan Cycle Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'='*60}")
        
        # Get symbols to scan
        symbols = self._get_scan_universe()
        print(f"📊 Scanning {len(symbols)} symbols...")
        
        all_composite_signals = []
        
        for symbol in symbols:
            try:
                # Step 1: Create MarketSnapshot with Incremental Fetch
                snapshot = self._create_snapshot(symbol)
                if not snapshot:
                    continue
                
                # Step 2: Generate signals deterministically
                raw_signals = []
                for generator in self.generators:
                    try:
                        signals = generator.generate(snapshot)
                        raw_signals.extend(signals)
                    except Exception as e:
                        print(f"Generator {generator.__class__.__name__} error for {symbol}: {e}")
                
                if not raw_signals:
                    continue
                
                print(f"  {symbol}: Generated {len(raw_signals)} raw signals")
                
                # Step 3: Aggregate signals
                composite_signals = self.aggregator.aggregate(raw_signals)
                
                for composite in composite_signals:
                    # Step 4: LLM Analysis (optional, with timeout)
                    try:
                        llm_analysis = self.llm_analyst.analyze(composite, snapshot)
                        composite.llm_analysis = llm_analysis
                    except Exception as e:
                        print(f"LLM Analysis failed for {symbol}: {e}")
                        composite.llm_analysis = None
                    
                    # Step 5: Confidence Scoring
                    final_confidence, confidence_level = self.confidence_engine.compute_confidence(
                        composite,
                        composite.llm_analysis
                    )
                    composite.final_confidence_score = final_confidence
                    composite.confidence_level = confidence_level
                    
                    print(f"    → {composite.direction.value} signal: {confidence_level.value} confidence ({final_confidence:.2f})")
                    
                    all_composite_signals.append(composite)

                    # Auto-execute if High Confidence (Paper Mode)
                    if confidence_level.value == "HIGH":
                       self.execute_trade_from_signal(composite)
            
            except Exception as e:
                print(f"Error processing {symbol}: {e}")
        
        # Update current signals
        self.current_signals = all_composite_signals
        
        # Persist Signals to DB
        self._persist_signals(all_composite_signals)

        print(f"\n✅ Scan Complete: {len(all_composite_signals)} composite signals generated")
        print(f"{'='*60}\n")
    
    def _get_scan_universe(self) -> List[str]:
        """Get list of symbols to scan"""
        # Use stock scanner to get universe
        from .utils import get_nse_fo_universe
        universe = get_nse_fo_universe()
        # Limit to top 20 for testing
        return universe[:20] if len(universe) > 20 else universe
    
    def _create_snapshot(self, symbol: str) -> Optional[MarketSnapshot]:
        """
        Create MarketSnapshot for symbol using Fyers 5m intraday data.
        Implements INCREMENTAL FETCH + DB CACHE.
        """
        db = SessionLocal()
        try:
            # 1. Get Company ID
            company = get_or_create_company(db, symbol)

            # 2. Check last cached candle
            last_candle = db.query(IntradayCandle).filter(
                IntradayCandle.company_id == company.id,
                IntradayCandle.timeframe == 5
            ).order_by(desc(IntradayCandle.timestamp)).first()

            to_date = datetime.now()
            
            # Determine range to fetch
            if last_candle:
                # Fetch from last candle timestamp
                from_date = last_candle.timestamp
                # If last fetch was very recent (within 5 mins), skip fetch unless forcing
                if (to_date - from_date).total_seconds() < 300:
                    # Just load from DB
                    pass
            else:
                # First time: fetch 7 days
                from_date = to_date - timedelta(days=7)

            # Fetch new data only if needed
            range_from = from_date.strftime("%Y-%m-%d")
            range_to = to_date.strftime("%Y-%m-%d")
            
            # Only fetch if dates are different or forced
            if range_from != range_to or not last_candle:
                 self._fetch_and_cache_candles(db, company, symbol, range_from, range_to)

            # 3. Load full 7 days from DB
            cutoff = to_date - timedelta(days=7)
            db_candles = db.query(IntradayCandle).filter(
                IntradayCandle.company_id == company.id,
                IntradayCandle.timeframe == 5,
                IntradayCandle.timestamp >= cutoff
            ).order_by(IntradayCandle.timestamp).all()
            
            if not db_candles or len(db_candles) < 50:
                 # Fallback: Try fetch again if DB is empty
                 self._fetch_and_cache_candles(db, company, symbol, range_from, range_to)
                 db_candles = db.query(IntradayCandle).filter(
                    IntradayCandle.company_id == company.id,
                    IntradayCandle.timeframe == 5,
                    IntradayCandle.timestamp >= cutoff
                 ).order_by(IntradayCandle.timestamp).all()
            
            if not db_candles:
                return None

            # Convert to DataFrame
            data = []
            for c in db_candles:
                data.append({
                    'timestamp': c.timestamp,
                    'open': c.open,
                    'high': c.high,
                    'low': c.low,
                    'close': c.close,
                    'volume': c.volume
                })
            
            df = pd.DataFrame(data)
            df.set_index('timestamp', inplace=True)
            
            # Build snapshot
            snapshot = SnapshotBuilder.from_dataframe(
                symbol=symbol,
                df=df,
                timeframe="5m"
            )
            return snapshot

        except Exception as e:
            print(f"Error creating snapshot for {symbol}: {e}")
            return None
        finally:
            db.close()

    def _fetch_and_cache_candles(self, db, company, symbol: str, range_from, range_to):
        """Fetch from Fyers and save to DB"""
        fyers = get_fyers_client()
        fyers_symbol = f"NSE:{symbol}-EQ"

        # print(f"  Fetching {symbol} {range_from} to {range_to}")
        data = fyers.get_historical_data(fyers_symbol, "5", range_from, range_to)

        if data and 'candles' in data:
            candles = data['candles']
            new_records = 0
            for c in candles:
                # Fyers returns [timestamp_epoch, open, high, low, close, volume]
                ts = datetime.fromtimestamp(c[0])

                # Check exist (or use upsert logic if supported, but here plain check)
                exists = db.query(IntradayCandle).filter(
                    IntradayCandle.company_id == company.id,
                    IntradayCandle.timeframe == 5,
                    IntradayCandle.timestamp == ts
                ).first()

                if not exists:
                    rec = IntradayCandle(
                        company_id=company.id,
                        timestamp=ts,
                        timeframe=5,
                        open=c[1], high=c[2], low=c[3], close=c[4], volume=c[5]
                    )
                    db.add(rec)
                    new_records += 1

            if new_records > 0:
                db.commit()
                # print(f"  Saved {new_records} candles for {symbol}")

    def get_current_signals(self) -> List[Dict[str, Any]]:
        """Get current signals (sorted by confidence)"""
        # Sort by confidence level and score
        sorted_signals = sorted(
            self.current_signals,
            key=lambda x: (
                {"HIGH": 3, "MEDIUM": 2, "LOW": 1}.get(x.confidence_level.value, 0),
                x.final_confidence_score
            ),
            reverse=True
        )
        
        # Convert to dict format
        return [self._signal_to_dict(s) for s in sorted_signals]
    
    def _signal_to_dict(self, signal: CompositeSignal) -> Dict[str, Any]:
        """Convert CompositeSignal to dictionary"""
        return {
            "id": signal.composite_id,
            "symbol": signal.symbol,
            "direction": signal.direction.value,
            "timeframe": signal.timeframe,
            "confluence_count": signal.confluence_count,
            "aggregate_strength": signal.aggregate_strength,
            "confidence_level": signal.confidence_level.value,
            "final_confidence": signal.final_confidence_score,
            "signal_families": [f.value for f in signal.signal_families],
            "signal_names": signal.signal_names,
            "reasons": signal.merged_reasons,
            "llm_narrative": signal.llm_analysis.narrative_reasoning if signal.llm_analysis else None,
            "risk_flags": signal.llm_analysis.risk_flags if signal.llm_analysis else [],
            "timestamp": signal.last_signal_time.isoformat()
        }
    
    def execute_trade(self, signal_id: str) -> Dict[str, Any]:
        """Execute trade for a signal"""
        # Find signal
        signal = next((s for s in self.current_signals if s.composite_id == signal_id), None)
        if not signal:
            return {"success": False, "error": "Signal not found"}
        
        try:
            # Get fresh snapshot
            snapshot = self._create_snapshot(signal.symbol)
            if not snapshot:
                return {"success": False, "error": "Could not fetch current price"}
            
            # Step 6: Construct trade
            trade_setup = self.trade_constructor.construct_trade(
                signal,
                snapshot,
                signal.confidence_level
            )
            
            # Step 7: LLM Review (optional)
            try:
                review = self.llm_reviewer.review(trade_setup)
                trade_setup.llm_review = review
                
                if review.recommendation == "WAIT":
                    return {"success": False, "error": f"LLM suggests WAIT: {review.reasoning}"}
            except Exception as e:
                print(f"LLM Review failed: {e}")
            
            # Step 8: Risk Check
            risk_check = self.risk_agent.check_trade(trade_setup)
            if not risk_check.approved:
                return {"success": False, "error": f"Risk check failed: {', '.join(risk_check.reasons)}"}
            
            # Step 9: Execute
            result = self.execution_agent.execute_paper_trade(trade_setup)
            
            if result.get('status') == 'FILLED':
                return {
                    "success": True,
                    "message": result.get('message', 'Trade executed'),
                    "trade_id": result.get('trade_id'),
                    "position": result.get('position')
                }
            else:
                return {
                    "success": False,
                    "error": result.get('message', 'Trade execution failed')
                }
        
        except Exception as e:
            print(f"Execute trade error: {e}")
            import traceback
            traceback.print_exc()
            return {"success": False, "error": str(e)}

    def _persist_signals(self, signals: List[CompositeSignal]):
        """Save signals to database"""
        db = SessionLocal()
        try:
            for s in signals:
                # Check if exists
                exists = db.query(SmartTraderSignal).filter_by(composite_id=s.composite_id).first()
                if exists: continue

                db_signal = SmartTraderSignal(
                    composite_id=s.composite_id,
                    symbol=s.symbol,
                    timeframe=s.timeframe,
                    direction=s.direction.value,
                    signal_families=str([f.value for f in s.signal_families]), # Simple stringify
                    signal_names=str(s.signal_names),
                    confluence_count=s.confluence_count,
                    aggregate_strength=s.aggregate_strength,
                    base_confidence=0.0, # Not strictly tracked in CompositeSignal yet
                    final_confidence=s.final_confidence_score,
                    confidence_level=s.confidence_level.value,
                    first_signal_time=s.last_signal_time,
                    last_signal_time=s.last_signal_time
                )
                db.add(db_signal)
            db.commit()
        except Exception as e:
            print(f"Error saving signals to DB: {e}")
        finally:
            db.close()

    def execute_trade_from_signal(self, signal: CompositeSignal):
         """Helper to execute trade from internal loop (auto-trade)"""
         # This method is used when auto-trading High confidence signals
         try:
            snapshot = self._create_snapshot(signal.symbol)
            trade_setup = self.trade_constructor.construct_trade(signal, snapshot, signal.confidence_level)
            risk_check = self.risk_agent.check_trade(trade_setup)
            if risk_check.approved:
                self.execution_agent.execute_paper_trade(trade_setup)
         except Exception as e:
             print(f"Auto-trade failed for {signal.symbol}: {e}")

# Global instance
_orchestrator_instance = None

def get_orchestrator(config: Optional[Dict[str, Any]] = None):
    """Get or create global orchestrator instance"""
    global _orchestrator_instance
    if _orchestrator_instance is None:
        _orchestrator_instance = NewOrchestratorAgent(config)
    return _orchestrator_instance
