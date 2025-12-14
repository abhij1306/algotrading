"""
Zerodha Kite Connect Login Script
Generates access token for API authentication
"""
import os
import sys
import json
from kiteconnect import KiteConnect

sys.path.append(os.path.dirname(__file__))
from kite_client import load_api_credentials, save_access_token


def login():
    """Perform Kite Connect OAuth login"""
    
    print("="*70)
    print("ZERODHA KITE CONNECT - LOGIN")
    print("="*70)
    
    # Load API credentials
    try:
        creds = load_api_credentials()
        api_key = creds['api_key']
        api_secret = creds['api_secret']
    except Exception as e:
        print(f"\n❌ Error: {e}")
        return
    
    # Initialize KiteConnect
    kite = KiteConnect(api_key=api_key)
    
    # Generate login URL
    login_url = kite.login_url()
    
    print(f"\n📋 Step 1: Open this URL in your browser:")
    print(f"\n{login_url}\n")
    print("="*70)
    
    print("\n📋 Step 2: Login with your Zerodha credentials")
    print("📋 Step 3: After login, you'll be redirected to a URL")
    print("📋 Step 4: Copy the 'request_token' from the redirected URL")
    print("\nExample URL: https://127.0.0.1/?request_token=ABC123&action=login&status=success")
    print("Copy: ABC123")
    print("="*70)
    
    # Get request token from user
    request_token = input("\n🔑 Enter the request_token: ").strip()
    
    if not request_token:
        print("❌ No request token provided!")
        return
    
    try:
        # Generate session
        data = kite.generate_session(request_token, api_secret=api_secret)
        
        access_token = data['access_token']
        
        # Save access token
        save_access_token(access_token, api_key)
        
        print("\n✅ SUCCESS! Access token generated and saved")
        print(f"✅ Token file: zerodha/access_token.json")
        print("\n📊 You can now fetch historical data using kite_client.py")
        print("="*70)
        
    except Exception as e:
        print(f"\n❌ Failed to generate access token: {e}")
        print("\nPlease check:")
        print("1. Request token is correct")
        print("2. API secret is correct")
        print("3. Request token hasn't expired (valid for few minutes only)")


if __name__ == "__main__":
    login()
