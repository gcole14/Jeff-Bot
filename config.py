import os
from pathlib import Path
from dotenv import load_dotenv

try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
except ImportError:
    pass

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

load_dotenv(ENV_PATH)

# --- Discord ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID = int(os.getenv("GUILD_ID", "0") or "0")        # Optional: instant slash-command sync to your server
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0") or "0")    # Target channel for scheduled posts & alerts

# --- Riot API ---
RIOT_API_KEY = os.getenv("RIOT_API_KEY", "").strip()
REGION = os.getenv("REGION", "na1").strip().lower()           # na1, euw1, kr, etc.
ROUTING = os.getenv("ROUTING", "americas").strip().lower()    # americas, europe, asia, sea

# --- DeepSeek API (optional — enables AI coaching, VOD reviews, scouting, roasts) ---
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip().rstrip("/")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip()
AI_ENABLED = bool(DEEPSEEK_API_KEY) and os.getenv("AI_ENABLED", "true").strip().lower() not in ("0", "false", "no")

# --- Default friend group (Riot IDs: "Name#TAG") ---
# Can also be overridden via comma-separated SUMMONERS in .env, or managed live with /roster.
_DEFAULT_SUMMONERS = [
    "Cookmysock#cook",
    "31Sec#LCPM",
    "SwimboSlice#pizza",
    "H1ph0pp3r#8008",
]

_env_summoners = os.getenv("SUMMONERS", "").strip()
SUMMONERS = (
    [s.strip() for s in _env_summoners.split(",") if s.strip()]
    if _env_summoners
    else list(_DEFAULT_SUMMONERS)
)

# --- Schedule ---
TIMEZONE = os.getenv("TIMEZONE", "America/Denver").strip()

DAILY_HOUR = int(os.getenv("DAILY_HOUR", "8"))       # 8 AM daily recap
DAILY_MINUTE = int(os.getenv("DAILY_MINUTE", "0"))

WEEKLY_DAY = os.getenv("WEEKLY_DAY", "mon").strip().lower()   # Monday morning weekly digest
WEEKLY_HOUR = int(os.getenv("WEEKLY_HOUR", "9"))
WEEKLY_MINUTE = int(os.getenv("WEEKLY_MINUTE", "0"))

# --- Stats config ---
DAILY_MATCH_LOOKBACK_HOURS = int(os.getenv("DAILY_MATCH_LOOKBACK_HOURS", "24"))
WEEKLY_MATCH_LOOKBACK_DAYS = int(os.getenv("WEEKLY_MATCH_LOOKBACK_DAYS", "7"))
MAX_MATCHES_PER_PLAYER = int(os.getenv("MAX_MATCHES_PER_PLAYER", "60"))
IGNORE_REMAKES = os.getenv("IGNORE_REMAKES", "true").strip().lower() not in ("0", "false", "no")
REMAKE_MAX_SECONDS = int(os.getenv("REMAKE_MAX_SECONDS", "300"))

# --- Background trackers ---
RANK_CHECK_INTERVAL_HOURS = int(os.getenv("RANK_CHECK_INTERVAL_HOURS", "2"))
ANNOUNCE_DEMOTIONS = os.getenv("ANNOUNCE_DEMOTIONS", "true").strip().lower() not in ("0", "false", "no")
SQUAD_DEBRIEF_ENABLED = os.getenv("SQUAD_DEBRIEF_ENABLED", "true").strip().lower() not in ("0", "false", "no")
SQUAD_CHECK_INTERVAL_MINUTES = int(os.getenv("SQUAD_CHECK_INTERVAL_MINUTES", "10"))
PATCH_CHECK_ENABLED = os.getenv("PATCH_CHECK_ENABLED", "true").strip().lower() not in ("0", "false", "no")
PATCH_CHECK_INTERVAL_HOURS = int(os.getenv("PATCH_CHECK_INTERVAL_HOURS", "4"))

# --- Storage ---
DB_PATH = os.getenv("DB_PATH", str(BASE_DIR / "jeff_bot.db"))
RANK_HISTORY_FILE = os.getenv("RANK_HISTORY_FILE", str(BASE_DIR / "rank_history.json"))

# --- Hybrid Voice / TTS (Desktop 5070 Ti Chatterbox Turbo -> CM3588 Kokoro -> ElevenLabs) ---
VOICE_ENABLED = os.getenv("VOICE_ENABLED", "true").strip().lower() not in ("0", "false", "no")
VOICE_WORKER_URL = os.getenv("VOICE_WORKER_URL", "").strip().rstrip("/")          # e.g. http://192.168.1.50:8100
VOICE_WORKER_TOKEN = os.getenv("VOICE_WORKER_TOKEN", "").strip()
VOICE_DEFAULT_VOICE = os.getenv("VOICE_DEFAULT_VOICE", "jeff").strip()
VOICE_FALLBACK_TTS_URL = os.getenv("VOICE_FALLBACK_TTS_URL", "").strip().rstrip("/")  # e.g. http://kokoro:8880
VOICE_FALLBACK_VOICE = os.getenv("VOICE_FALLBACK_VOICE", "am_adam").strip()
VOICE_MAX_CHARS = int(os.getenv("VOICE_MAX_CHARS", "450"))
VOICE_IDLE_MINUTES = int(os.getenv("VOICE_IDLE_MINUTES", "15"))

# Optional cloud TTS fallback (ElevenLabs v4 Turbo)
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "").strip()
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "").strip()
ELEVENLABS_MODEL_ID = os.getenv("ELEVENLABS_MODEL_ID", "eleven_v4_turbo").strip()

# --- Data Dragon ---
DDRAGON_VERSIONS_URL = "https://ddragon.leagueoflegends.com/api/versions.json"
DDRAGON_CHAMPION_URL = "https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion.json"
DDRAGON_ITEM_URL = "https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/item.json"
