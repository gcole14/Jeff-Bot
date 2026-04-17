import os
from dotenv import load_dotenv

load_dotenv()

# --- Discord ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))  # The channel to post in

# --- Riot API ---
RIOT_API_KEY = os.getenv("RIOT_API_KEY")
REGION = os.getenv("REGION", "na1")           # na1, euw1, kr, etc.
ROUTING = os.getenv("ROUTING", "americas")    # americas, europe, asia (for match-v5)

# --- Your friend group (Riot IDs: "Name#TAG") ---
SUMMONERS = [
    "WarrenGetty#Oil",
    "31Sec#LCPM",
    "SwimboSlice#pizza",
    "H1ph0pp3r#8008",
]

# --- Schedule ---
TIMEZONE = "America/Denver"   # Utah timezone

DAILY_HOUR = 8       # 8 AM daily recap
DAILY_MINUTE = 0

WEEKLY_DAY = "mon"   # Monday morning weekly digest
WEEKLY_HOUR = 9
WEEKLY_MINUTE = 0

# --- Stats config ---
DAILY_MATCH_LOOKBACK_HOURS = 24    # How far back to look for daily stats
WEEKLY_MATCH_LOOKBACK_DAYS = 7     # How far back for weekly stats
MAX_MATCHES_PER_PLAYER = 20        # Max matches to fetch per player per period
