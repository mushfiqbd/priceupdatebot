import requests
import sqlite3
import time
from datetime import datetime, timedelta, timezone
import threading
import json
import os

# Telegram credentials
BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_ID = os.getenv('ADMIN_ID', '').split(',')  # Split comma-separated admin IDs
TELEGRAM_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

# Database setup
conn = sqlite3.connect("tokens.db", check_same_thread=False)
cursor = conn.cursor()

# Drop and recreate table with updated schema
cursor.execute("DROP TABLE IF EXISTS tokens")
cursor.execute("""
CREATE TABLE IF NOT EXISTS tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    address TEXT,
    chain_id TEXT,
    start_market_cap REAL,
    last_market_cap REAL,  -- Store the last checked market cap
    start_time TEXT,
    launch_time TEXT,
    notified_100_10 INTEGER DEFAULT 0,   -- Only track 100% growth within 10 mins
    source TEXT                         -- Track API source (DexScreener or GeckoTerminal)
)
""")
conn.commit()

# Test Telegram connection
def test_telegram_connection():
    try:
        test_msg = "🤖 Bot is online and ready to send notifications!"
        success = True
        for admin_id in ADMIN_ID:
            response = requests.post(TELEGRAM_URL, data={
                "chat_id": admin_id,
                "text": test_msg,
                "parse_mode": "Markdown"
            })
            if response.status_code == 200:
                print(f"✅ Telegram connection successful for admin {admin_id}!")
            else:
                print(f"❌ Telegram connection failed for admin {admin_id}: {response.status_code}")
                print(f"Response: {response.text}")
                success = False
        return success
    except Exception as e:
        print(f"❌ Telegram connection error: {e}")
        return False

# Helper function to fetch the latest market cap for a token
def fetch_latest_market_cap(token):
    if token['source'] == "DexScreener":
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token['address']}"
    else:  # GeckoTerminal
        url = f"https://api.geckoterminal.com/api/v2/networks/{token['chain_id']}/pools/{token['address']}"
    
    try:
        res = requests.get(url, timeout=10, headers={'accept': 'application/json'})
        if res.status_code == 200:
            if token['source'] == "DexScreener":
                data = res.json().get("pairs", [])
                market_cap = float(data[0].get('fdv') or 0) if data else 0
                print(f"Debug: Fetched market cap for {token['address']} on {token['chain_id']} (DexScreener): ${market_cap:,.0f}")
                print(f"Debug: Raw API response: {json.dumps(data, indent=2)}")
            else:  # GeckoTerminal
                data = res.json().get('data', {}).get('attributes', {})
                market_cap = float(data.get('fdv_usd', '0') or '0')
                print(f"Debug: Fetched market cap for {token['address']} on {token['chain_id']} (GeckoTerminal): ${market_cap:,.0f}")
                print(f"Debug: Raw API response: {json.dumps(data, indent=2)}")
            return market_cap
        else:
            print(f"❌ Non-200 response fetching market cap for {token['address']}: {res.status_code}")
            return token['market_cap']  # Fallback to stored value
    except Exception as e:
        print(f"❌ Error fetching market cap for {token['address']}: {e}")
        return token['market_cap']  # Fallback to stored value

# Fetch new tokens from DexScreener
def fetch_new_tokens_dexscreener():
    profiles_url = "https://api.dexscreener.com/token-profiles/latest/v1"
    try:
        print("\n🔍 Fetching new token profiles from DexScreener...")
        res = requests.get(profiles_url, timeout=10)
        print(f"[DexScreener fetch profiles] Status Code: {res.status_code}")

        if res.status_code == 200:
            try:
                profiles_data = res.json()
                print(f"✅ Found {len(profiles_data)} token profiles from DexScreener")
            except Exception as json_err:
                print("❌ Failed to parse DexScreener profiles JSON:", json_err)
                print("Response:", res.text[:300])
                return []

            tokens = []
            for profile in profiles_data:
                token_address = profile.get('tokenAddress')
                chain_id = profile.get('chainId')

                if not token_address or not chain_id:
                    print("❌ Missing token address or chain ID in DexScreener profile, skipping...")
                    continue

                pair_url = f"https://api.dexscreener.com/token-pairs/v1/{chain_id}/{token_address}"
                try:
                    print(f"   🔍 Fetching pair data for {token_address} on {chain_id} (DexScreener)...")
                    pair_res = requests.get(pair_url, timeout=5)
                    if pair_res.status_code == 200:
                        pair_data = pair_res.json()
                        if pair_data:
                            for pair in pair_data:
                                if not isinstance(pair, dict):
                                    print(f"   ⚠️ Skipping unexpected item in DexScreener pair data: {pair}")
                                    continue
                                token_name = pair.get('baseToken', {}).get('name', 'Unknown Token')
                                pair_chain_id = pair.get('chainId', chain_id)
                                pair_address = pair.get('pairAddress', token_address)
                                launch_time_iso = datetime.now(timezone.utc).isoformat()
                                if 'pairCreatedAt' in pair:
                                    try:
                                        timestamp = pair['pairCreatedAt']
                                        if timestamp > 1000000000000:
                                            timestamp /= 1000
                                        launch_time_iso = datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
                                    except Exception as e:
                                        print(f"   ⚠️ Could not parse DexScreener launch time: {e}")
                                market_cap = float(pair.get('fdv') or 0)
                                tokens.append({
                                    "name": token_name,
                                    "address": pair_address,
                                    "chain_id": pair_chain_id,
                                    "market_cap": market_cap,
                                    "launch_time": launch_time_iso,
                                    "source": "DexScreener"
                                })
                        else:
                            print(f"   ❌ No pair data found for {token_address} on {chain_id} (DexScreener)")
                    else:
                        print(f"   ❌ Non-200 response for DexScreener pair data: {pair_res.status_code}")
                except Exception as e:
                    print(f"   ❌ Error fetching DexScreener pair data: {e}")
        else:
            print("❌ Non-200 response from DexScreener profiles API:", res.status_code)
            return []
        return tokens
    except Exception as e:
        print("❌ Error fetching DexScreener profiles:", e)
        return []

# Fetch new tokens from GeckoTerminal
def fetch_new_tokens_geckoterminal():
    networks_url = "https://api.geckoterminal.com/api/v2/networks?page=1"
    try:
        print("\n🔍 Fetching supported networks from GeckoTerminal...")
        res = requests.get(networks_url, timeout=10, headers={'accept': 'application/json'})
        print(f"[GeckoTerminal fetch networks] Status Code: {res.status_code}")

        if res.status_code == 200:
            try:
                networks_data = res.json().get('data', [])
                print(f"✅ Found {len(networks_data)} networks from GeckoTerminal")
            except Exception as json_err:
                print("❌ Failed to parse GeckoTerminal networks JSON:", json_err)
                print("Response:", res.text[:300])
                return []

            tokens = []
            for network in networks_data:
                network_id = network.get('id')
                if not network_id:
                    print("❌ Missing network ID in GeckoTerminal response, skipping...")
                    continue

                new_pools_url = f"https://api.geckoterminal.com/api/v2/networks/{network_id}/new_pools?page=1"
                try:
                    print(f"   🔍 Fetching new pools for {network_id} (GeckoTerminal)...")
                    pool_res = requests.get(new_pools_url, timeout=5, headers={'accept': 'application/json'})
                    if pool_res.status_code == 200:
                        pool_data = pool_res.json().get('data', [])
                        for pool in pool_data:
                            if not isinstance(pool, dict):
                                print(f"   ⚠️ Skipping unexpected item in GeckoTerminal pool data: {pool}")
                                continue
                            attributes = pool.get('attributes', {})
                            token_name = attributes.get('name', 'Unknown Token')
                            pair_address = attributes.get('address')
                            chain_id = network_id
                            market_cap = float(attributes.get('fdv_usd', '0') or '0')
                            launch_time_str = attributes.get('pool_created_at')
                            launch_time_iso = datetime.now(timezone.utc).isoformat()
                            if launch_time_str:
                                try:
                                    launch_time = datetime.fromisoformat(launch_time_str.replace('Z', '+00:00'))
                                    launch_time_iso = launch_time.isoformat()
                                except Exception as e:
                                    print(f"   ⚠️ Could not parse GeckoTerminal pool creation time: {e}")
                            tokens.append({
                                "name": token_name,
                                "address": pair_address,
                                "chain_id": chain_id,
                                "market_cap": market_cap,
                                "launch_time": launch_time_iso,
                                "source": "GeckoTerminal"
                            })
                    else:
                        print(f"   ❌ Non-200 response for GeckoTerminal new pools: {pool_res.status_code}")
                except Exception as e:
                    print(f"   ❌ Error fetching GeckoTerminal new pools for {network_id}: {e}")
        else:
            print("❌ Non-200 response from GeckoTerminal networks API:", res.status_code)
            return []
        return tokens
    except Exception as e:
        print("❌ Error fetching GeckoTerminal networks:", e)
        return []

# Combined fetch new tokens function
def fetch_new_tokens():
    tokens = []
    tokens.extend(fetch_new_tokens_dexscreener())
    tokens.extend(fetch_new_tokens_geckoterminal())

    for token in tokens:
        # Only process Solana tokens
        if token['chain_id'].lower() != 'solana':
            continue

        print(f"\n📊 Token Information ({token['source']}):")
        print(f"   Name: {token['name']}")
        print(f"   Chain: {token['chain_id']}")
        print(f"   Address: {token['address']}")
        print(f"   Launch Time: {token['launch_time']}")
        print(f"   💰 Initial Market Cap: ${token['market_cap']:,.0f}")

        launch_datetime = datetime.fromisoformat(token['launch_time']).replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - launch_datetime
        age_minutes = int(age.total_seconds() / 60)
        print(f"   🕒 Age: {age_minutes} minutes ago")

        if age.total_seconds() <= 60 * 60:  # Only process tokens within 60 minutes of launch
            cursor.execute("SELECT id FROM tokens WHERE address = ? AND chain_id = ? AND source = ?",
                          (token['address'], token['chain_id'], token['source']))
            existing_token = cursor.fetchone()

            if existing_token is None and token['market_cap'] > 0:
                print(f"   ✅ New Solana token from {token['source']}! Saving to database...")
                cursor.execute("""
                    INSERT INTO tokens (name, address, chain_id, start_market_cap, last_market_cap, start_time, launch_time, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    token['name'], token['address'], token['chain_id'], token['market_cap'],
                    token['market_cap'],  # Set last_market_cap same as start_market_cap initially
                    datetime.now(timezone.utc).isoformat(), token['launch_time'], token['source']
                ))
                conn.commit()
                print(f"   💾 Saved initial market cap: ${token['market_cap']:,.0f}")
            else:
                print(f"   ℹ️ Token already tracked from {token['source']} (Address: {token['address']})")

# Check tokens in DB for updates
def fetch_market_cap(token, max_retries=3, retry_delay=2):
    """Fetch market cap with retry mechanism for rate limiting"""
    for attempt in range(max_retries):
        try:
            if token['source'] == "DexScreener":
                url = f"https://api.dexscreener.com/latest/dex/tokens/{token['address']}"
            else:  # GeckoTerminal
                url = f"https://api.geckoterminal.com/api/v2/networks/{token['chain_id']}/pools/{token['address']}"

            res = requests.get(url, timeout=10, headers={'accept': 'application/json'})
            
            # Handle rate limiting
            if res.status_code == 429:
                if attempt < max_retries - 1:
                    wait_time = retry_delay * (attempt + 1)  # Exponential backoff
                    print(f"   ⏳ Rate limited. Waiting {wait_time} seconds before retry {attempt + 1}/{max_retries}...")
                    time.sleep(wait_time)
                    continue
                else:
                    print(f"   ❌ Rate limit exceeded after {max_retries} retries")
                    return 0

            if res.status_code == 200:
                if token['source'] == "DexScreener":
                    data = res.json().get("pairs", [])
                    if data and len(data) > 0:
                        # Sort pairs by liquidity to get the most liquid pair
                        sorted_pairs = sorted(data, key=lambda x: float(x.get('liquidity', {}).get('usd', 0) or 0), reverse=True)
                        pair_data = sorted_pairs[0]
                        
                        print(f"Debug: DexScreener raw data for {token['name']}:")
                        print(json.dumps(pair_data, indent=2))
                        
                        current_cap = 0
                        # Try different market cap fields in order of preference
                        if 'fdv' in pair_data and pair_data['fdv'] and float(pair_data['fdv']) > 0:
                            current_cap = float(pair_data['fdv'])
                            print(f"   📊 Using FDV: ${current_cap:,.0f}")
                        elif 'marketCap' in pair_data and pair_data['marketCap'] and float(pair_data['marketCap']) > 0:
                            current_cap = float(pair_data['marketCap'])
                            print(f"   📊 Using Market Cap: ${current_cap:,.0f}")
                        elif 'liquidity' in pair_data and 'usd' in pair_data['liquidity'] and float(pair_data['liquidity']['usd']) > 0:
                            current_cap = float(pair_data['liquidity']['usd'])
                            print(f"   📊 Using Liquidity: ${current_cap:,.0f}")
                        
                        if current_cap > 0:
                            return current_cap
                else:  # GeckoTerminal
                    data = res.json().get('data', {})
                    if data:
                        attributes = data.get('attributes', {})
                        
                        print(f"Debug: GeckoTerminal raw data for {token['name']}:")
                        print(json.dumps(attributes, indent=2))
                        
                        current_cap = 0
                        # Try different market cap fields in order of preference
                        if 'fdv_usd' in attributes and attributes['fdv_usd'] and float(attributes['fdv_usd']) > 0:
                            current_cap = float(attributes['fdv_usd'])
                            print(f"   📊 Using FDV: ${current_cap:,.0f}")
                        elif 'market_cap_usd' in attributes and attributes['market_cap_usd'] and float(attributes['market_cap_usd']) > 0:
                            current_cap = float(attributes['market_cap_usd'])
                            print(f"   📊 Using Market Cap: ${current_cap:,.0f}")
                        elif 'liquidity_usd' in attributes and attributes['liquidity_usd'] and float(attributes['liquidity_usd']) > 0:
                            current_cap = float(attributes['liquidity_usd'])
                            print(f"   📊 Using Liquidity: ${current_cap:,.0f}")
                        
                        if current_cap > 0:
                            return current_cap
            
            if attempt < max_retries - 1:
                print(f"   ⏳ Retry {attempt + 1}/{max_retries} for {token['name']}...")
                time.sleep(retry_delay)
        except Exception as e:
            print(f"   ❌ Error fetching market cap (attempt {attempt + 1}): {e}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
    
    return 0  # Return 0 if all retries failed

def check_tracked_tokens():
    cursor.execute("""
        SELECT id, name, address, chain_id, start_market_cap, last_market_cap, start_time, launch_time, notified_100_10, source 
        FROM tokens 
        WHERE chain_id = 'solana' 
        AND datetime(launch_time) >= datetime('now', '-10 minutes')
    """)
    rows = cursor.fetchall()
    
    if not rows:
        print("\nℹ️ No Solana tokens to check (under 10 minutes old)")
        return

    print(f"\n🔍 Checking {len(rows)} Solana tokens...")
    
    for row in rows:
        token = {
            "id": row[0],
            "name": row[1],
            "address": row[2],
            "chain_id": row[3],
            "start_market_cap": row[4],
            "last_market_cap": row[5],
            "start_time": datetime.fromisoformat(row[6]).replace(tzinfo=timezone.utc),
            "launch_time": datetime.fromisoformat(row[7]).replace(tzinfo=timezone.utc),
            "notified_100_10": row[8],
            "source": row[9]
        }

        now = datetime.now(timezone.utc)
        age = now - token['launch_time']
        age_minutes = int(age.total_seconds() / 60)

        # Skip if token is older than 10 minutes (Double check, although DB query should handle this)
        if age.total_seconds() > 10 * 60:
            print(f"   ⏭️ Skipping {token['name']} - older than 10 minutes")
            continue

        try:
            # Fetch current market cap with retry mechanism
            current_cap = fetch_market_cap(token)

            print(f"\n📊 Market Cap Update for {token['name']} ({token['source']}):")
            print(f"   📉 Initial Market Cap: ${token['start_market_cap']:,.0f}")
            print(f"   📈 Last Checked Market Cap: ${token['last_market_cap']:,.0f}")
            print(f"   💰 Current Market Cap: ${current_cap:,.0f}")
            print(f"   🕒 Age: {age_minutes} minutes")

            if current_cap > 0 and token['start_market_cap'] > 0:
                # Calculate total growth from initial market cap
                total_growth = ((current_cap - token['start_market_cap']) / token['start_market_cap']) * 100
                print(f"   📈 Total growth from initial: {total_growth:+.1f}%")

                # Only send notification if market cap has doubled (100% growth) within 10 minutes
                if total_growth >= 100 and token['notified_100_10'] == 0:
                    print(f"   💥 100% growth milestone reached within 10 minutes! Sending notification...")
                    # Pass current_cap for the notification, but keep last_market_cap in DB for next check if needed
                    send_notification(token, current_cap, total_growth, f"{age_minutes} mins")
                    cursor.execute("UPDATE tokens SET notified_100_10 = 1, last_market_cap = ? WHERE id = ?", 
                                 (current_cap, token['id']))
                    conn.commit()
                    print(f"   ✅ Updated notified_100_10 and last_market_cap for {token['name']}")
                else:
                     # Update last_market_cap even if 100% growth is not reached
                    if current_cap > token['last_market_cap']:
                         cursor.execute("UPDATE tokens SET last_market_cap = ? WHERE id = ?", 
                                      (current_cap, token['id']))
                         conn.commit()
                         print(f"   ✅ Updated last_market_cap to ${current_cap:,.0f} for {token['name']}")
                    print(f"   ℹ️ No 100% growth milestone reached yet for {token['name']}")
            else:
                print(f"   ⚠️ Initial or Current market cap is 0 for {token['name']}, skipping growth check.")
        except Exception as e:
            print(f"   ❌ Error checking {token['source']} token {token['address']}: {e}")

# Send price update notification
def send_notification(token, current_cap, growth_percentage, time_frame):
    try:
        # Ensure launch_time is in string format
        if isinstance(token['launch_time'], datetime):
            launch_time = token['launch_time']
        else:
            launch_time = datetime.fromisoformat(str(token['launch_time']).replace('Z', '+00:00'))
        
        age = datetime.now(timezone.utc) - launch_time
        age_minutes = int(age.total_seconds() / 60)
        
        print(f"🚀 Token update found ({token['source']})!")
        print(f"   Token: {token['name']}")
        print(f"   Growth: {growth_percentage:.1f}%")
        print(f"   Time Frame: {time_frame}")
        print(f"   Age: {age_minutes} minutes ago")
        
        # Construct link
        if token['source'] == "GeckoTerminal":
            link = f"https://www.geckoterminal.com/{token['chain_id']}/pools/{token['address']}"
        else:  # DexScreener
            link = f"https://dexscreener.com/{token['chain_id']}/{token['address']}"
        
        # Message format for 100% growth within 10 minutes
        msg = (
            f"🚀 *100% Growth Alert ({token['source']})!*\n\n"
            f"🌐 Token: {token['name']}\n"
            f"🔗 Chain: {token['chain_id']}\n"
            f"📬 Address: `{token['address']}`\n"
            f"📉 Initial Market Cap: ${token['start_market_cap']:,.0f}\n"
            f"📈 Current Market Cap: ${current_cap:,.0f}\n"
            f"🚀 Growth: *{growth_percentage:.1f}%* in {time_frame}\n"
            f"⏰ Launch Time: {launch_time.strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
            f"🕒 Age: {age_minutes} minutes ago\n\n"
            f"[📊 View on {token['source']}]({link})"
        )
        
        for admin_id in ADMIN_ID:
            try:
                response = requests.post(TELEGRAM_URL, data={
                    "chat_id": admin_id,
                    "text": msg,
                    "parse_mode": "Markdown"
                })
                if response.status_code == 200:
                    print(f"✅ Price update notification sent to admin {admin_id} ({token['source']})")
                else:
                    print(f"❌ Failed to send price update notification to admin {admin_id} ({token['source']}): {response.status_code}")
                    print(f"Response: {response.text}")
            except Exception as e:
                print(f"❌ Failed to send price update notification to admin {admin_id} ({token['source']}): {e}")
    except Exception as e:
        print(f"❌ Error in send_notification: {e}")
        print(f"Token data: {token}")

# Main loop
def main_loop():
    print("\n🚀 Starting Solana Token Tracker...")
    print("   - Only tracking Solana tokens")
    print("   - Only tokens under 60 minutes old")
    print("   - Checking every 60 seconds")
    
    while True:
        try:
            fetch_new_tokens()
            check_tracked_tokens()
            print("\n⏳ Waiting 60 seconds before next check...")
            time.sleep(60)
        except Exception as e:
            print(f"❌ Error in main loop: {e}")
            time.sleep(60)  # Still wait 60 seconds before retrying

# Run thread
if __name__ == "__main__":
    print("🚀 Token Tracker Bot Starting...")
    if test_telegram_connection():
        print("Starting main loop...")
        threading.Thread(target=main_loop).start()
    else:
        print("❌ Failed to start bot due to Telegram connection issues")