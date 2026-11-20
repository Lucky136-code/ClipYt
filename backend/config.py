import os
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"

# Use local data/temp if writable (localhost), else /tmp/clipyt (cloud containers like HF Spaces)
try:
    DATA_DIR = BASE_DIR / "data"
    TEMP_DIR = DATA_DIR / "temp"
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    _probe = TEMP_DIR / ".write_test"
    _probe.write_text("ok", encoding="utf-8")
    _probe.unlink(missing_ok=True)
except Exception:
    DATA_DIR = Path(tempfile.gettempdir()) / "clipyt"
    TEMP_DIR = DATA_DIR / "temp"
    TEMP_DIR.mkdir(parents=True, exist_ok=True)

EXPORTS_DIR = TEMP_DIR

YTDLP_CACHE_DIR = TEMP_DIR / "ytdlp_cache"
YTDLP_CACHE_DIR.mkdir(parents=True, exist_ok=True)

YTDLP_BASE_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "no_color": True,
    "source_address": "0.0.0.0",
    "cachedir": str(YTDLP_CACHE_DIR),
    "js_runtimes": {"deno": {}, "node": {}},
    "remote_components": ["ejs:github"],
    "nopart": True,
    "overwrites": True,
    "concurrent_fragment_downloads": 4,
    "buffersize": 8 * 1024 * 1024,
    "socket_timeout": 12,
    "retries": 3,
    "fragment_retries": 3,
}

# Optional YouTube cookies support for cloud hosting (e.g. Hugging Face Spaces Secret: YOUTUBE_COOKIES)
_ENV_COOKIES = os.environ.get("YOUTUBE_COOKIES", "").strip()
_COOKIES_FILE = TEMP_DIR / "yt_cookies.txt"


def ensure_fresh_cookiefile() -> str:
    """
    Re-writes yt_cookies.txt from the immutable YOUTUBE_COOKIES env secret so
    yt-dlp's automatic cookiejar save can never overwrite or strip auth cookies!
    """
    if _ENV_COOKIES:
        _COOKIES_FILE.write_text(_ENV_COOKIES, encoding="utf-8")
        return str(_COOKIES_FILE)
    return ""


if _ENV_COOKIES:
    YTDLP_BASE_OPTS["cookiefile"] = ensure_fresh_cookiefile()
else:
    _LOCAL_COOKIE_FILE = DATA_DIR / "cookies.txt"
    _ROOT_COOKIE_FILE = BASE_DIR / "cookies.txt"
    if _LOCAL_COOKIE_FILE.exists() and _LOCAL_COOKIE_FILE.stat().st_size > 100:
        YTDLP_BASE_OPTS["cookiefile"] = str(_LOCAL_COOKIE_FILE)
    elif _ROOT_COOKIE_FILE.exists() and _ROOT_COOKIE_FILE.stat().st_size > 100:
        YTDLP_BASE_OPTS["cookiefile"] = str(_ROOT_COOKIE_FILE)

ASPECT_RATIOS = {
    "9:16": {"width": 1080, "height": 1920, "label": "9:16 Vertical (Shorts / Reels / TikTok)"},
    "1:1": {"width": 1080, "height": 1080, "label": "1:1 Square (Instagram / LinkedIn / X)"},
    "4:5": {"width": 1080, "height": 1350, "label": "4:5 Portrait (Instagram Feed)"},
    "16:9": {"width": 1920, "height": 1080, "label": "16:9 Landscape (Original)"},
}

ASPECT_RATIOS_4K = {
    "9:16": {"width": 2160, "height": 3840, "label": "9:16 Vertical (4K)"},
    "1:1": {"width": 2160, "height": 2160, "label": "1:1 Square (4K)"},
    "4:5": {"width": 2160, "height": 2700, "label": "4:5 Portrait (4K)"},
    "16:9": {"width": 3840, "height": 2160, "label": "16:9 Landscape (4K)"},
}

ASPECT_RATIOS_720 = {
    "9:16": {"width": 720, "height": 1280},
    "1:1": {"width": 720, "height": 720},
    "4:5": {"width": 720, "height": 900},
    "16:9": {"width": 1280, "height": 720},
}

# ==============================================================================
# Telegram 6-Hour Access Key System for Live YouTube Studio
# ==============================================================================
TELEGRAM_CHANNEL_URL = "https://t.me/+N0hxuYHqK5kyY2U1"
LIVE_KEYS_FILE = DATA_DIR / "live_keys.json"
DEFAULT_LIVE_KEYS = [
    "LUCKY6H",
    "CLIP6H",
    "GTA6LIVE",
    "STREAM2026",
    "VIRAL2026",
    "TELEGRAM6H",
    "LIVE6H",
]


def get_rolling_6h_slot(offset: int = 0) -> int:
    """Returns the current 6-hour time slot index."""
    import time
    return int((time.time() // (6 * 3600)) + offset)


def get_rolling_6h_key(offset: int = 0) -> str:
    """Deterministic 6-hour rolling code for Telegram channel announcements (e.g. CLIP4891)."""
    slot = get_rolling_6h_slot(offset)
    code = (abs(hash((slot, "clipyt_telegram_key_2026"))) % 90000) + 10000
    return f"CLIP{code}"


def get_all_valid_live_keys() -> list[str]:
    """Retrieves all currently active valid keys (admin list + custom JSON + rolling 6h keys)."""
    import json
    keys = set(k.upper().strip() for k in DEFAULT_LIVE_KEYS)

    # Add rolling 6h key for current slot and previous slot (grace period)
    keys.add(get_rolling_6h_key(0).upper())
    keys.add(get_rolling_6h_key(-1).upper())

    # Check custom keys file
    if LIVE_KEYS_FILE.exists():
        try:
            custom_data = json.loads(LIVE_KEYS_FILE.read_text(encoding="utf-8"))
            if isinstance(custom_data, list):
                for k in custom_data:
                    if isinstance(k, str) and k.strip():
                        keys.add(k.upper().strip())
            elif isinstance(custom_data, dict):
                for k in custom_data.get("keys", []):
                    if isinstance(k, str) and k.strip():
                        keys.add(k.upper().strip())
        except Exception:
            pass

    return sorted(list(keys))


def verify_live_key(candidate: str) -> tuple[bool, str, int]:
    """
    Validates a submitted key.
    Returns (is_valid, message, expires_in_sec).
    Valid keys grant 6 hours (21600 seconds) of access.
    """
    if not candidate:
        return False, "Please enter the 6-hour access key from our Telegram channel.", 0

    clean = candidate.strip().upper()
    valid_keys = get_all_valid_live_keys()

    if clean in valid_keys:
        return True, "Key verified! Full access unlocked for 6 hours.", 6 * 3600

    return False, "Invalid or expired key. Please get the latest key on our Telegram channel.", 0

