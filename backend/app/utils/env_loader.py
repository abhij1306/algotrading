import os

def load_dotenv(path: str = ".env"):
    """
    Manually load .env file into os.environ.
    Replaces python-dotenv dependency to fix PyInstaller build issues.
    """
    if not os.path.exists(path):
        # Try looking up one level if not found (development convenience)
        if os.path.exists(os.path.join("..", path)):
            path = os.path.join("..", path)
        else:
            return

    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                
                if "=" in line:
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip()
                    
                    # Remove quotes if present
                    if value.startswith('"') and value.endswith('"'):
                        value = value[1:-1]
                    elif value.startswith("'") and value.endswith("'"):
                        value = value[1:-1]
                        
                    os.environ[key] = value
    except Exception as e:
        print(f"Warning: Failed to load .env file: {e}")
