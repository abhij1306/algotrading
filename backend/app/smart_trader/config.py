"""
Configuration for Smart Trader Multi-Agent System
"""
import os
import yaml
from pathlib import Path
from typing import Dict, Any
from dotenv import load_dotenv

load_dotenv()


class SmartTraderConfig:
    """Configuration management for Smart Trader"""
    
    def __init__(self, config_path: str = None):
        self.config_path = config_path or self._get_default_config_path()
        self.config = self._load_config()
        
    def _get_default_config_path(self) -> str:
        """Get default config file path"""
        return str(Path(__file__).parent / "config.yaml")
    
    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from YAML file"""
        try:
            with open(self.config_path, 'r') as f:
                config = yaml.safe_load(f)
                
            # Replace environment variables
            self._substitute_env_vars(config)
            return config
        except FileNotFoundError:
            # Return default configuration if file doesn't exist
            return self._get_default_config()
    
    def _substitute_env_vars(self, config: Dict[str, Any]):
        """Recursively substitute environment variables in config"""
        for key, value in config.items():
            if isinstance(value, dict):
                self._substitute_env_vars(value)
            elif isinstance(value, str) and value.startswith('${') and value.endswith('}'):
                env_var = value[2:-1]
                config[key] = os.getenv(env_var, '')
    
    def _get_default_config(self) -> Dict[str, Any]:
        """Return default configuration"""
        return {
            'mode': 'polling',
            'timeframe': '5m',
            'scan_interval_sec': 300,
            'market': {
                'start': '09:15',
                'end': '15:30',
                'timezone': 'Asia/Kolkata'
            },
            'universe': {
                'stocks': 'nse_fo',
                'indices': ['NIFTY', 'BANKNIFTY']
            },
            'risk': {
                'max_trades_per_day': 5,
                'max_risk_per_trade_pct': 2,
                'max_daily_loss_pct': 5,
                'symbol_cooldown_minutes': 30,
                'min_risk_reward_ratio': 1.5
            },
            'groq': {
                'api_key': os.getenv('GROQ_API_KEY', ''),
                'model': 'llama-3.3-70b-versatile',
                'timeout': 30
            },
            'paper_trading': {
                'initial_capital': 100000,
                'slippage_pct': 0.05,
                'commission_per_trade': 20
            },
            'scanner': {
                'min_volume_percentile': 70,
                'min_atr_pct': 1.5,
                'min_momentum_score': 60
            }
        }
    
    def get(self, key: str, default: Any = None) -> Any:
        """Get configuration value by key (supports dot notation)"""
        keys = key.split('.')
        value = self.config
        
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default
        
        return value
    
    def save_config(self, path: str = None):
        """Save current configuration to file"""
        save_path = path or self.config_path
        
        with open(save_path, 'w') as f:
            yaml.dump(self.config, f, default_flow_style=False)


# Global config instance
config = SmartTraderConfig()
