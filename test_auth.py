#!/usr/bin/env python3
"""Quick auth test — run inside container: python3 test_auth.py"""
import os
import pmxt
from dotenv import load_dotenv

load_dotenv()

private_key = os.getenv("POLYMARKET_PRIVATE_KEY")
proxy_address = os.getenv("POLYMARKET_PROXY_ADDRESS")

print(f"Private key length : {len(private_key) if private_key else 'NOT SET'}")
print(f"Proxy address      : {proxy_address[:8]}...{proxy_address[-4:] if proxy_address else 'NOT SET'}")
print()

print("1. Connecting...")
poly = pmxt.Polymarket(private_key=private_key, proxy_address=proxy_address)
print("   OK")

print("2. Fetching balance (requires auth)...")
try:
    balance = poly.fetch_balance()
    print(f"   OK — balance: {balance}")
except Exception as e:
    print(f"   FAILED: {e}")

print("3. Fetching our positions (requires auth)...")
try:
    positions = poly.fetch_positions()
    print(f"   OK — {len(positions)} positions")
except Exception as e:
    print(f"   FAILED: {e}")

print("4. Fetching a market (no auth needed)...")
try:
    markets = poly.fetch_markets(slug="will-bitcoin-reach-80000-in-2025")
    print(f"   OK — found {len(markets)} markets")
except Exception as e:
    print(f"   FAILED: {e}")
