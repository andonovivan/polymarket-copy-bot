"""One-time script to generate or retrieve Polymarket API credentials.

Usage:
    1. Set PRIVATE_KEY in your .env file (or pass it as an env var).
    2. Run: python generate_api_creds.py
    3. Copy the output into your .env file.
"""

import os

from dotenv import load_dotenv
from py_clob_client.client import ClobClient

load_dotenv()

HOST = "https://clob.polymarket.com"
CHAIN_ID = 137

private_key = os.getenv("PRIVATE_KEY", "")
if not private_key:
    print("Error: Set PRIVATE_KEY in .env first.")
    raise SystemExit(1)

client = ClobClient(host=HOST, chain_id=CHAIN_ID, key=private_key)
creds = client.create_or_derive_api_creds()

print("\nAdd these to your .env file:\n")
print(f"POLYMARKET_API_KEY={creds.api_key}")
print(f"POLYMARKET_API_SECRET={creds.api_secret}")
print(f"POLYMARKET_API_PASSPHRASE={creds.api_passphrase}")
