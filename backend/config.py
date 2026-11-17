import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
TEMP_DIR = DATA_DIR / "temp"
EXPORTS_DIR = BASE_DIR / "exports"
FRONTEND_DIR = BASE_DIR / "frontend"

# Ensure required directories exist
TEMP_DIR.mkdir(parents=True, exist_ok=True)
EXPORTS_DIR.mkdir(parents=True, exist_ok=True)

YTDLP_BASE_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "no_color": True,
    "source_address": "0.0.0.0",
    "js_runtimes": {"node": {}},
    "remote_components": ["ejs:github"],
}

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
