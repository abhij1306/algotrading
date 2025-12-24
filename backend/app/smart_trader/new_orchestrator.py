"""
Refactored Orchestrator - New signal generation flow
"""
import asyncio
import threading
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
import pytz
import pandas as pd

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
from .options_scanner import OptionsScannerAgent
from .models.signals import SignalFamily, Direction, ConfidenceLevel as Clvl

# Import Fyers client for live data
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
from fyers.fyers_client import get_historical_data


class NewOrchestratorAgent:
    """
    Refactored orchestrator with deterministic signal generation + LLM enhancement.
    
    Flow:
    1. MarketSnapshot Creation
    2. Signal Generation (deterministic)
    3. Aggregation
    4. LLM Analysis (optional)
    5. Confidence Scoring
    6. Trade Construction
    7. LLM Review (optional)
    8. Risk Check
    9. Execution
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
        self.options_scanner = OptionsScannerAgent(self.config)
        
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
        
        # --- Stocks & Futures Scan ---
        for symbol in symbols:
            try:
                # Step 1: Create MarketSnapshot
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
            
            except Exception as e:
                print(f"Error processing {symbol}: {e}")
        
        # --- Options Scan ---
        try:
            print(f"📊 Scanning Options (NIFTY/BANKNIFTY)...")
            option_signals = self.options_scanner.scan()
            print(f"  Found {len(option_signals)} option signals")

            for osig in option_signals:
                # Convert option dict to CompositeSignal
                import uuid
                # Map confidence string to Enum
                conf_map = {"HIGH": Clvl.HIGH, "MEDIUM": Clvl.MEDIUM, "LOW": Clvl.LOW}
                c_level = conf_map.get(osig.get('confidence', 'LOW'), Clvl.LOW)

                # Only include HIGH/MEDIUM options
                if c_level not in [Clvl.HIGH, Clvl.MEDIUM]:
                     continue

                composite = CompositeSignal(
                    composite_id=str(uuid.uuid4()),
                    symbol=osig['symbol'],
                    timeframe="5m", # Options scanner usually runs on 5m or 15m context
                    direction=Direction.LONG, # Buying the option is always LONG direction for the trade
                    confluence_count=2, # Base
                    aggregate_strength=osig['momentum_score'] / 100.0,
                    signal_families=[SignalFamily.OPTIONS, SignalFamily.MOMENTUM],
                    signal_names=["OPTION_MOMENTUM"],
                    merged_reasons=osig['reasons'],
                    merged_features=osig, # Store full option details here
                    first_signal_time=datetime.now(),
                    last_signal_time=datetime.now(),
                    confidence_level=c_level,
                    final_confidence_score=osig['momentum_score'] / 100.0
                )
                all_composite_signals.append(composite)

        except Exception as e:
            print(f"Error in Options Scan: {e}")
            import traceback
            traceback.print_exc()

        # Update current signals
        self.current_signals = all_composite_signals
        
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
        """Create MarketSnapshot for symbol using Fyers 5m intraday data"""
        try:
            # Calculate date range (last 7 days)
            to_date = datetime.now()
            from_date = to_date - timedelta(days=7)
            
            # Format dates for Fyers API
            range_from = from_date.strftime("%Y-%m-%d")
            range_to = to_date.strftime("%Y-%m-%d")
            
            # Convert symbol to Fyers format (e.g., RELIANCE -> NSE:RELIANCE-EQ)
            fyers_symbol = f"NSE:{symbol}-EQ"
            
            # Fetch 5m intraday data from Fyers
            print(f"📡 Fetching 5m data for {symbol} from Fyers...")
            data = get_historical_data(
                symbol=fyers_symbol,
                timeframe="5",  # 5 minutes
                range_from=range_from,
                range_to=range_to
            )
            
            if not data or 'candles' not in data or not data['candles']:
                print(f"⚠️ {symbol}: No data returned from Fyers")
                return None
            
            # Convert to DataFrame
            candles = data['candles']
            df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='s')
            df.set_index('timestamp', inplace=True)
            
            if df.empty or len(df) < 50:
                print(f"⚠️ {symbol}: Insufficient candles ({len(df)})")
                return None
            
            print(f"✅ {symbol}: Fetched {len(df)} candles")
            
            # Build snapshot with computed indicators
            snapshot = SnapshotBuilder.from_dataframe(
                symbol=symbol,
                df=df,
                timeframe="5m"
            )
            
            return snapshot
        
        except Exception as e:
            print(f"❌ {symbol}: Error creating snapshot: {e}")
            import traceback
            traceback.print_exc()
            return None
    
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
        base = {
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

        # Enrich with option details if present
        if SignalFamily.OPTIONS in signal.signal_families:
             base['option_details'] = signal.merged_features # We stored the full option dict here

        return base
    
    def execute_trade(self, signal_id: str) -> Dict[str, Any]:
        """Execute trade for a signal"""
        # Find signal
        signal = next((s for s in self.current_signals if s.composite_id == signal_id), None)
        if not signal:
            return {"success": False, "error": "Signal not found"}
        
        try:
            # Handle Options differently (no snapshot needed for simplified execution, or we need Option Snapshot)
            is_option = SignalFamily.OPTIONS in signal.signal_families
            
            if is_option:
                # Direct construction for options
                from .models import TradeSetup
                import uuid
                
                opt_details = signal.merged_features
                trade_setup = TradeSetup(
                    trade_id=str(uuid.uuid4()),
                    composite_signal=signal,
                    entry_price=opt_details.get('premium', 0),
                    stop_loss=opt_details.get('stop_loss', 0),
                    target=opt_details.get('target', 0),
                    quantity=opt_details.get('quantity', 50), # Default lot size
                    risk_amount=0, # Calc later
                    reward_amount=0,
                    risk_reward_ratio=1.5,
                    setup_method="OPTION_SCANNER",
                    created_at=datetime.now()
                )

                # Skip LLM/Snapshot for Options currently (as it requires Option Chain data not in SnapshotBuilder)
                # Just do Risk Check

            else:
                # Standard Equity Flow
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


# Global instance
_orchestrator_instance = None

def get_orchestrator(config: Optional[Dict[str, Any]] = None):
    """Get or create global orchestrator instance"""
    global _orchestrator_instance
    if _orchestrator_instance is None:
        _orchestrator_instance = NewOrchestratorAgent(config)
    return _orchestrator_instance
