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

ASPECT_RATIOS = {
    "9:16": {"width": 1080, "height": 1920, "label": "9:16 Vertical (Shorts / Reels / TikTok)"},
    "1:1": {"width": 1080, "height": 1080, "label": "1:1 Square (Instagram / LinkedIn / X)"},
    "4:5": {"width": 1080, "height": 1350, "label": "4:5 Portrait (Instagram Feed)"},
    "16:9": {"width": 1920, "height": 1080, "label": "16:9 Landscape (Original)"},
}

ASPECT_RATIOS_720 = {
    "9:16": {"width": 720, "height": 1280},
    "1:1": {"width": 720, "height": 720},
    "4:5": {"width": 720, "height": 900},
    "16:9": {"width": 1280, "height": 720},
}
