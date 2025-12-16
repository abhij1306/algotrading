"""
Orchestrator Agent - Central coordinator for the multi-agent system
"""
import asyncio
import threading
from typing import Dict, Any, Optional
from datetime import datetime, time, timedelta
import pytz

from .config import config
from .stock_scanner import StockScannerAgent
from .options_scanner import OptionsScannerAgent
from .decision_agent import DecisionAgent
from .risk_agent import RiskAgent
from .execution_agent import ExecutionAgent
from .journal_agent import JournalAgent
from .groq_client import get_groq_client
from .utils import is_market_open, is_market_hours

# LLM Agents
from .llm_signal_agent import LLMSignalAgent
from .llm_risk_agent import LLMRiskAgent
from .llm_orchestrator import LLMDecisionOrchestrator


class OrchestratorAgent:
    """
    Central coordinator that manages the multi-agent lifecycle
    Handles scheduling, state management, and agent coordination
    """
    
    def __init__(self, custom_config: Optional[Dict[str, Any]] = None):
        # Load configuration
        self.config_obj = config if custom_config is None else custom_config
        self.config = self.config_obj.config if hasattr(self.config_obj, 'config') else self.config_obj
        
        # Initialize core agents
        self.journal_agent = JournalAgent(self.config)
        self.stock_scanner = StockScannerAgent(self.config)
        self.options_scanner = OptionsScannerAgent(self.config)
        self.decision_agent = DecisionAgent(self.config)
        self.risk_agent = RiskAgent(self.config, journal_agent=self.journal_agent)
        self.execution_agent = ExecutionAgent(self.config, journal_agent=self.journal_agent)
        self.groq_client = get_groq_client(self.config.get('groq', {}))
        
        # Initialize LLM agents (if enabled in config)
        llm_config = self.config.get('llm', {})
        self.llm_enabled = llm_config.get('signal_enhancement', False)
        
        if self.llm_enabled:
            try:
                provider = llm_config.get('provider', 'groq')
                self.llm_signal_agent = LLMSignalAgent(provider=provider)
                self.llm_risk_agent = LLMRiskAgent(provider=provider)
                self.llm_decision_orchestrator = LLMDecisionOrchestrator(provider=provider)
                print("[ORCHESTRATOR] LLM agents initialized successfully")
            except Exception as e:
                print(f"[ORCHESTRATOR] Failed to initialize LLM agents: {e}")
                self.llm_enabled = False
                self.llm_signal_agent = None
                self.llm_risk_agent = None
                self.llm_decision_orchestrator = None
        else:
            self.llm_signal_agent = None
            self.llm_risk_agent = None
            self.llm_decision_orchestrator = None
        
        # State management
        self.is_running = False
        self.last_scan_time = None
        self.current_signals = []
        self.scan_interval = self.config.get('scan_interval_sec', 300)
        
        # Threading
        self.scanner_thread = None
        self.position_monitor_thread = None
        
    async def start_market_session(self):
        """Start the trading session"""
        if self.is_running:
            return {'status': 'already_running', 'message': 'Smart Trader is already running'}
        
        # For now, allow starting even outside market hours for testing
        # In production, uncomment the market hours check below
        # if not is_market_open(self.config.get('market', {})):
        #     return {
        #         'status': 'market_closed',
        #         'message': 'Market is currently closed. Smart Trader starts during market hours (09:15-15:30 IST)'
        #     }
        
        self.is_running = True
        
        # Start scanner thread (scans every 5 minutes)
        self.scanner_thread = threading.Thread(target=self._scanner_loop, daemon=True)
        self.scanner_thread.start()
        
        # Start position monitor thread
        self.position_monitor_thread = threading.Thread(target=self._position_monitor_loop, daemon=True)
        self.position_monitor_thread.start()
        
        return {
            'status': 'started',
            'message': 'Smart Trader started successfully - auto-scanning every 5 minutes',
            'scan_interval': self.scan_interval,
            'next_scan': (datetime.now() + timedelta(seconds=self.scan_interval)).isoformat()
        }
    
    def stop_market_session(self):
        """Stop the trading session"""
        if not self.is_running:
            return {'status': 'not_running', 'message': 'Smart Trader is not running'}
        
        self.is_running = False
        
        # Close all open positions
        # TODO: Get current prices and close positions
        
        return {
            'status': 'stopped',
            'message': 'Smart Trader stopped successfully'
        }
    
    def clear_signals(self):
        """Clear all current signals (useful for starting fresh)"""
        count = len(self.current_signals)
        self.current_signals = []
        return {
            'status': 'cleared',
            'message': f'Cleared {count} signals',
            'signals_cleared': count
        }
    
    def _scanner_loop(self):
        """Main scanner loop (runs in separate thread)"""
        while self.is_running:
            try:
                # Check if still in market hours
                if not is_market_hours(self.config.get('market', {})):
                    print("Market hours ended. Stopping scanner...")
                    self.is_running = False
                    break
                
                # Run scan cycle
                self.trigger_scan_cycle()
                
                # Wait for next scan interval
                threading.Event().wait(self.scan_interval)
                
            except Exception as e:
                print(f"Error in scanner loop: {e}")
                threading.Event().wait(10)  # Wait 10 seconds before retry
    
    def _position_monitor_loop(self):
        """Position monitoring loop (checks for SL/Target hits)"""
        while self.is_running:
            try:
                # Get open positions
                open_positions = self.execution_agent.get_open_positions()
                
                if open_positions:
                    # TODO: Fetch current prices for all symbols
                    # current_prices = self._fetch_current_prices([p['symbol'] for p in open_positions])
                    # self.execution_agent.update_positions(current_prices)
                    pass
                
                # Check every 30 seconds
                threading.Event().wait(30)
                
            except Exception as e:
                print(f"Error in position monitor loop: {e}")
                threading.Event().wait(10)
    
    def _scan_cycle(self):
        """Execute a scan cycle - scan stocks/options and rank signals"""
        try:
            # Scan stocks (NSE F&O)
            print(f"[ORCHESTRATOR] Scanning stocks...")
            stock_signals = self.stock_scanner.scan()
            print(f"[ORCHESTRATOR] Found {len(stock_signals)} stock signals")
            
            # Disable options scanner for now (no historical data available)
            # options_signals = self.options_scanner.scan()
            options_signals = []
            print(f"[ORCHESTRATOR] Found {len(options_signals)} options signals (disabled)")
            
            # 3. Merge and rank signals
            print("  → Processing signals...")
            all_signals = self.decision_agent.process_signals(stock_signals, options_signals)
            
            # 4. Get top candidates
            top_signals = self.decision_agent.get_top_candidates(all_signals, limit=10)
            
            # 5. LLM Enhancement (if enabled)
            if self.llm_enabled and self.llm_signal_agent:
                print("  → Enhancing signals with AI...")
                try:
                    # Run LLM analysis asynchronously
                    import asyncio
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    
                    enhanced_signals = loop.run_until_complete(
                        self.llm_signal_agent.batch_analyze(
                            signals=top_signals,
                            market_data=None  # TODO: Add market context
                        )
                    )
                    
                    loop.close()
                    
                    # Replace signals with enhanced versions
                    top_signals = [s['original_signal'] for s in enhanced_signals]
                    
                    # Add LLM analysis to each signal
                    for i, signal in enumerate(top_signals):
                        if i < len(enhanced_signals) and enhanced_signals[i].get('llm_analysis'):
                            signal['llm_analysis'] = enhanced_signals[i]['llm_analysis']
                            signal['llm_enhanced'] = True
                    
                    print(f"  ✓ AI enhancement complete")
                except Exception as e:
                    print(f"  ⚠ AI enhancement failed: {e}")
            
            # 6. Format for display
            new_signals = [
                self.decision_agent.format_for_display(signal)
                for signal in top_signals
            ]
            
            # 7. Append new signals to existing ones (don't replace!)
            # Deduplicate based on symbol + direction to avoid duplicates
            existing_keys = {(s['symbol'], s['direction']) for s in self.current_signals}
            for signal in new_signals:
                key = (signal['symbol'], signal['direction'])
                if key not in existing_keys:
                    self.current_signals.append(signal)
                    existing_keys.add(key)
            
            self.last_scan_time = datetime.now()
            
            # 8. Save ALL signals to history (not just new ones)
            try:
                from .signal_history import get_signal_history_service
                history_service = get_signal_history_service()
                history_service.save_signals(new_signals)  # Save only new signals to history
            except Exception as e:
                print(f"Error saving signal history: {e}")
            
            print(f"  ✓ Scan complete. {len(new_signals)} new signals, {len(self.current_signals)} total signals")
            
            # Optional: Get AI summary
            if self.groq_client and self.groq_client.api_key:
                try:
                    summary = self.groq_client.summarize_market({
                        'stock_signals': stock_signals,
                        'options_signals': options_signals,
                        'stocks_scanned': len(self.stock_scanner.universe)
                    })
                    if summary:
                        print(f"\n  AI Summary: {summary}\n")
                except:
                    pass
            
        except Exception as e:
            print(f"Error in scan cycle: {e}")
            import traceback
            traceback.print_exc()
    
    def trigger_scan_cycle(self):
        """Execute one complete scan cycle - public method to trigger scanning"""
        self._scan_cycle()
    
    def get_system_state(self) -> Dict[str, Any]:
        """Get current system state"""
        return {
            'is_running': self.is_running,
            'market_open': is_market_open(self.config.get('market', {})),
            'last_scan_time': self.last_scan_time.isoformat() if self.last_scan_time else None,
            'active_signals': len(self.current_signals),
            'open_positions': len(self.execution_agent.get_open_positions()),
            'today_trades': self.journal_agent.get_today_trades_count(),
            'today_pnl': round(self.journal_agent.get_today_pnl(), 2),
            'current_capital': round(self.journal_agent.current_capital, 2)
        }
    
    def get_current_signals(self) -> list:
        """Get current ranked signals (sorted by timestamp, latest first)"""
        # Sort by timestamp in descending order (latest first)
        sorted_signals = sorted(
            self.current_signals,
            key=lambda x: x.get('timestamp', ''),
            reverse=True
        )
        return sorted_signals
    
    def execute_trade(self, signal_id: str, quantity: Optional[int] = None, trade_type: str = "SPOT") -> Dict[str, Any]:
        """
        Execute a paper trade for the selected signal
        
        Args:
            signal_id: Signal ID
            quantity: Optional quantity override
            trade_type: SPOT, FUTURES, or OPTIONS
            
        Returns:
            Trade execution result
        """
        # Find signal
        signal = None
        for s in self.current_signals:
            if s['id'] == signal_id:
                signal = s.copy()  # Make a copy to modify
                break
        
        if not signal:
            return {
                'status': 'error',
                'message': 'Signal not found'
            }
        
        # Override instrument_type based on trade_type from UI
        if trade_type == "FUTURES":
            signal['instrument_type'] = 'FUTURE'
        elif trade_type == "OPTIONS":
            signal['instrument_type'] = 'OPTION'
        # SPOT keeps original 'STOCK' type
        
        # Validate with risk agent
        risk_approval = self.risk_agent.validate_trade(
            signal,
            self.journal_agent.current_capital
        )
        
        if not risk_approval['approved']:
            return {
                'status': 'rejected',
                'message': risk_approval['rejection_reason'],
                'risk_check': risk_approval
            }
        
        # Execute trade
        result = self.execution_agent.execute_trade(signal, risk_approval, quantity)
        
        # Record trade for cooldown
        if result['status'] == 'FILLED':
            self.risk_agent.record_trade(signal['symbol'])
        
        return result
    
    def get_open_positions(self) -> list:
        """Get all open positions"""
        return self.execution_agent.get_open_positions()
    
    def get_pnl_summary(self) -> Dict[str, Any]:
        """Get P&L summary"""
        return self.journal_agent.get_pnl_summary()
    
    def get_tradebook(self, limit: Optional[int] = None) -> list:
        """Get trade history"""
        return self.journal_agent.get_tradebook(limit)
    
    def close_position(self, trade_id: str, exit_reason: str = 'Manual Close'):
        """Manually close a position"""
        # Get position
        positions = self.execution_agent.get_open_positions()
        position = next((p for p in positions if p['trade_id'] == trade_id), None)
        
        if not position:
            return {'status': 'error', 'message': 'Position not found'}
        
        # TODO: Fetch current price
        current_price = position['entry_price']  # Placeholder
        
        self.execution_agent.close_position(trade_id, current_price, exit_reason)
        
        return {'status': 'success', 'message': f'Position {trade_id} closed'}


# Global orchestrator instance
_orchestrator_instance = None

def get_orchestrator(config: Optional[Dict[str, Any]] = None) -> OrchestratorAgent:
    """Get or create global orchestrator instance"""
    global _orchestrator_instance
    
    if _orchestrator_instance is None:
        _orchestrator_instance = OrchestratorAgent(config)
    
    return _orchestrator_instance
