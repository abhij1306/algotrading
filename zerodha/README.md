# Zerodha Kite Connect Integration

This folder contains scripts to fetch historical data from Zerodha Kite Connect API.

## Setup

### 1. Install kiteconnect library
```bash
pip install kiteconnect
```

### 2. Get Kite Connect API Credentials

1. Go to https://developers.kite.trade/
2. Login with your Zerodha account
3. Create a new app
4. Note down your **API Key** and **API Secret**

### 3. Configure Credentials

Create `api_credentials.json`:
```json
{
  "api_key": "your_api_key_here",
  "api_secret": "your_api_secret_here"
}
```

### 4. Generate Access Token

Run the login script:
```bash
python zerodha/zerodha_login.py
```

Follow the instructions:
1. Open the provided URL in browser
2. Login with Zerodha credentials
3. Copy the `request_token` from redirected URL
4. Paste it in the terminal

This will generate `access_token.json`.

## Files

- `kite_client.py` - Kite Connect client wrapper
- `zerodha_login.py` - OAuth login script
- `fetch_november_data.py` - Data extraction script
- `api_credentials.json` - Your API credentials (create this)
- `access_token.json` - Generated access token (auto-created)

## Notes

- Access tokens are valid for 24 hours
- Re-run `zerodha_login.py` daily to refresh token
- Historical data is available for expired contracts via Zerodha
