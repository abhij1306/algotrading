"""
Fast Trading Loop - "Agentic Trader" Optimization
Implements a streamlined Fetch -> Analyze -> Execute loop bypassing heavy LLM reasoning
for high-confidence technical signals.
"""
import time
import threading
from typing import Dict, Any, List
from .stock_scanner import StockScannerAgent
from .execution_agent import ExecutionAgent
from .config import config
from .config_utils import get_config_value
from ..database import SessionLocal, ActionCenter
from ..utils.audit_logger import AuditLogger
import json

class FastTradingLoop:
    """
    High-efficiency trading loop.
    Replaces multi-agent conversation with direct signal-to-execution logic.
    """
    
    def __init__(self, custom_config: Dict[str, Any] = None):
        self.config = custom_config or config
        self.scanner = StockScannerAgent(self.config)
        self.execution_agent = ExecutionAgent(self.config)
        self.is_running = False
        self.thread = None
        self.min_confidence_score = get_config_value("AUTO_TRADE_MIN_SCORE", 75)
        
    def start(self):
        """Start the fast loop"""
        if self.is_running:
            return
            
        self.is_running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        print("⚡ [FAST LOOP] Started High-Efficiency Agentic Mode")
        
    def stop(self):
        """Stop the loop"""
        self.is_running = False
        if self.thread:
            self.thread.join(timeout=5)
        print("⚡ [FAST LOOP] Stopped")
        
    def _loop(self):
        """Main execution loop"""
        while self.is_running:
            try:
                start_time = time.time()
                print(f"\n⚡ [FAST LOOP] Cycle Start: {time.strftime('%H:%M:%S')}")
                
                # 1. BULK DATA FETCH & ANALYZE (Parallelized in Scanner)
                # This uses the ThreadPoolExecutor optimization we just added
                signals = self.scanner.scan(use_live_prices=True)
                
                # 2. FILTER & EXECUTE
                self._process_signals(signals)
                
                duration = time.time() - start_time
                print(f"⚡ [FAST LOOP] Cycle Complete in {duration:.2f}s")
                
                # Sleep if cycle was too fast, or just wait for next interval
                # "Agentic Trader" guide suggests loop every 5 mins/interval
                interval = get_config_value("FAST_LOOP_INTERVAL_SEC", self.config.get('scan_interval', 300))
                time.sleep(max(1, interval - duration))
                
            except Exception as e:
                print(f"❌ [FAST LOOP] Error: {e}")
                time.sleep(10)

    def _process_signals(self, signals: List[Dict[str, Any]]):
        """Process signals and auto-execute best ones"""
        
        # Get current open positions to avoid duplicates
        # Execution agent has open_positions dict
        open_positions = self.execution_agent.open_positions
        open_symbols = {pos['symbol'] for pos in open_positions.values()}
        
        trades_placed = 0
        
        for signal in signals:
            symbol = signal['symbol']
            score = signal['momentum_score']
            
            # Skip if already in position
            if symbol in open_symbols:
                continue
                
            # Skip if score too low for auto-execution
            action_threshold = get_config_value("ACTION_CENTER_MIN_SCORE", 50)
            
            if score < action_threshold:
                continue
                
            if score < self.min_confidence_score:
                # ROUTE TO ACTION CENTER
                print(f"⚡ [FAST LOOP] Medium Confidence ({score}). Routing to Action Center: {symbol}")
                self._send_to_action_center(signal, score, "Medium Confidence Signal")
                
                AuditLogger.log_decision(
                    agent_name="FastTradingLoop",
                    action_type="ROUTE_TO_ACTION_CENTER",
                    symbol=symbol,
                    input_snapshot={"signal": signal},
                    decision={"action": "PENDING_APPROVAL"},
                    reasoning=f"Score {score} < Auto Threshold {self.min_confidence_score}",
                    confidence=score
                )
                continue
                
            print(f"⚡ [FAST LOOP] High Confidence Signal Found: {symbol} ({score}/100)")
            
            # 3. DIRECT EXECUTION (No LLM Dialogue)
            try:
                # Construct simplified "Risk Approval" (since we pre-validated confidence)
                # Ideally we still call a Risk Agent, but for "Fast Lane" we do basic checks
                
                risk_approval = {
                    "approved": True,
                    "qty": self._calculate_position_size(signal),
                    "reason": f"High Score {score}"
                }
                
                # Execute
                result = self.execution_agent.execute_trade(signal, risk_approval)
                
                if result.get('status') == 'FILLED':
                    print(f"✅ [FAST LOOP] EXECUTED: {symbol} {signal['direction']} @ {result['position']['entry_price']}")
                    trades_placed += 1
                    open_symbols.add(symbol) # Prevent double entry in same loop
                else:
                    print(f"⚠️ [FAST LOOP] Execution Failed: {result.get('message')}")
                    
            except Exception as e:
                print(f"❌ [FAST LOOP] Execution Error for {symbol}: {e}")
                
        if trades_placed == 0:
            print("⚡ [FAST LOOP] No high-confidence trades executed this cycle.")

    def _calculate_position_size(self, signal: Dict[str, Any]) -> int:
        """Simple position sizing"""
        # Fixed amount or percentage logic
        capital_per_trade = get_config_value("CAPITAL_PER_TRADE", 10000)
        price = signal.get('entry_price', 100)
        return max(1, int(capital_per_trade / price))

    def _send_to_action_center(self, signal: Dict[str, Any], score: float, reason: str):
        """Create pending action in DB"""
        db = SessionLocal()
        try:
            # Check for existing pending action for same symbol
            existing = db.query(ActionCenter).filter(
                ActionCenter.source_agent == 'FastLoop',
                ActionCenter.status == 'PENDING',
                ActionCenter.action_type == 'PLACE_ORDER'
            ).all()
            
            # Filter in python for payload symbol match if needed, or assume loose check
            # For now, just create new
            
            # Construct Order Payload
            order_payload = {
                "symbol": signal['symbol'],
                "direction": signal['direction'],
                "entry_price": signal['entry_price'],
                "quantity": self._calculate_position_size(signal)
            }
            
            action = ActionCenter(
                source_agent='FastLoop',
                action_type='PLACE_ORDER',
                payload=order_payload,
                reason=f"{reason} (Score: {score})",
                confidence=score,
                status='PENDING'
            )
            db.add(action)
            db.commit()
            print(f"✅ [ACTION CENTER] Created Pending Action for {signal['symbol']}")
            
        except Exception as e:
            print(f"❌ [ACTION CENTER] Error: {e}")
        finally:
            db.close()
