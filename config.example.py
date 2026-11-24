"""
Configuration Template for Cloud Trigger Board
Copy this file to config.py and fill in your credentials.
"""

# Google Drive OAuth2 Credentials
GDRIVE_CLIENT_ID = "YOUR_GOOGLE_CLIENT_ID.apps.googleusercontent.com"
GDRIVE_CLIENT_SECRET = "GOCSPX-YOUR_CLIENT_SECRET"
GDRIVE_REFRESH_TOKEN = "1//YOUR_REFRESH_TOKEN"

# Optional Residential Proxy (e.g., Oxylabs / Bright Data)
# Leave empty or None to connect directly without a proxy
RESIDENTIAL_PROXY_URL = None
# Example Oxylabs format:
# RESIDENTIAL_PROXY_URL = "http://customer-YOUR_USER-cc-us:YOUR_PASS@pr.oxylabs.io:7777"

# Server Port
PORT = 8899
HOST = "0.0.0.0"
