# Zerodha Kite Connect - Troubleshooting Guide

## "Request method not allowed" Error

This error occurs when the **Redirect URL** in your Kite Connect app doesn't match the login flow.

### Solution

1. **Go to Kite Connect Dashboard**
   - Visit: https://developers.kite.trade/apps
   - Login with your Zerodha credentials
   - Click on your app

2. **Check Redirect URL Setting**
   
   Your app's **Redirect URL** should be set to:
   ```
   http://127.0.0.1
   ```
   
   OR
   ```
   https://127.0.0.1
   ```

3. **Update if Needed**
   - Click "Edit" on your app
   - Set Redirect URL to `http://127.0.0.1`
   - Save changes

4. **Try Login Again**
   ```bash
   python zerodha/zerodha_login.py
   ```

## Alternative: Manual Token Generation

If the redirect URL issue persists, you can manually generate the access token:

### Step 1: Get Request Token Manually

Open this URL in browser (replace YOUR_API_KEY with your actual API key):
```
https://kite.zerodha.com/connect/login?v=3&api_key=YOUR_API_KEY
```

After login, you'll be redirected to:
```
http://127.0.0.1/?request_token=XXXXXX&action=login&status=success
```

Copy the `request_token` value.

### Step 2: Generate Access Token Manually

Run this Python command (replace placeholders with your actual credentials):

```python
from kiteconnect import KiteConnect
import json

api_key = "YOUR_API_KEY_HERE"
api_secret = "YOUR_API_SECRET_HERE"
request_token = "YOUR_REQUEST_TOKEN_HERE"

kite = KiteConnect(api_key=api_key)
data = kite.generate_session(request_token, api_secret=api_secret)

# Save access token
with open("zerodha/access_token.json", "w") as f:
    json.dump({
        "access_token": data["access_token"],
        "api_key": api_key,
        "timestamp": "manual"
    }, f, indent=4)

print("✅ Access token saved!")
```

## Common Issues

### Issue 1: Redirect URL Mismatch
**Error**: "Request method not allowed"
**Fix**: Set redirect URL to `http://127.0.0.1` in Kite app settings

### Issue 2: Request Token Expired
**Error**: "Invalid request token"
**Fix**: Request tokens expire in 5 minutes - get a new one

### Issue 3: Wrong API Secret
**Error**: "Invalid API credentials"
**Fix**: Double-check your API secret in `api_credentials.json`

## Need Help?

Check Kite Connect documentation: https://kite.trade/docs/connect/v3/
