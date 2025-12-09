# AlgoTrading - Broker Integration

This project provides integration with Fyers and Zerodha brokers for algorithmic trading.

## 📁 Project Structure

```
AlgoTrading/
├── fyers/                       # Fyers broker integration
│   ├── fyers_client.py          # Fyers API client for REST operations
│   ├── data_feed.py             # WebSocket data feed (real-time)
│   ├── fyers_login.py           # OAuth authentication
│   ├── utils.py                 # Utility functions
│   └── config/
│       ├── keys.env             # Fyers API credentials
│       └── access_token.json    # Fyers access token
├── zerodha/                     # Zerodha broker integration
│   ├── kite_client.py           # Kite Connect API client
│   ├── zerodha_login.py         # OAuth authentication
│   ├── api_credentials.json     # Zerodha API credentials
│   └── access_token.json        # Zerodha access token
├── backtest_config.json         # Backtest configuration
├── requirements.txt             # Python dependencies
└── README.md                    # This file
```

## 🚀 Quick Start

### 1. Setup Authentication

#### Fyers Broker Setup

```bash
python fyers/fyers_login.py
```

This will:
- Open a browser window for Fyers login
- Generate and save your access token
- Store credentials in `fyers/config/access_token.json`

#### Zerodha Broker Setup

```bash
python zerodha/zerodha_login.py
```

This will:
- Open a browser window for Zerodha login
- Generate and save your access token
- Store credentials in `zerodha/access_token.json`

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

## 🔧 Troubleshooting

### Authentication Issues
```bash
# Fyers - Regenerate access token
python fyers/fyers_login.py

# Zerodha - Regenerate access token
python zerodha/zerodha_login.py
```

### Missing Data
```bash
# Force refresh from API
engine.load_historical_data(days=30, force_refresh=True)
```

### Import Errors
```bash
# Install dependencies
pip install -r requirements.txt
```

## 📞 Support

For issues and questions:
1. Check the logs in `logs/` directory
2. Verify your API credentials in `fyers/config/keys.env` or `zerodha/api_credentials.json`
3. Ensure you have internet connectivity for API calls

## 📄 License

This project is for educational purposes. Please comply with broker API terms of service and applicable regulations.