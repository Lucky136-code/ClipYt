import io
import re
import json
import uuid
import asyncio
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple
import requests
from PIL import Image, ImageFilter, ImageEnhance
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from backend.config import (
    BASE_DIR,
    TEMP_DIR,
    FRONTEND_DIR,
    TELEGRAM_CHANNEL_URL,
    verify_live_key,
    get_rolling_6h_key,
)
from backend.analyzer import analyze_youtube_video, extract_video_id
from backend.subtitles import fetch_youtube_transcript
from backend.video_processor import (
    process_clip_pipeline,
    prewarm_video_streams,
    detect_best_encoder,
    cleanup_old_temp_files,
)
from backend.pro_studio import (
    inspect_pro_source,
    fetch_pro_boundary_frames,
    process_pro_split_pipeline,
)
from backend.live_radar import poll_live_chat_radar
from backend.chat_hype_scanner import scan_live_viral_moments
from backend.auto_frame import compute_auto_boxes, get_fallback_boxes

app = FastAPI(title="ClipYT - YouTube Shorts Creator", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory background task registry, transcript cache, & heatmap cache
TASKS: Dict[str, Dict[str, Any]] = {}
TRANSCRIPT_CACHE: Dict[str, Any] = {}
HEATMAP_CACHE: Dict[str, Any] = {}
ANALYSIS_CACHE: Dict[str, Any] = {}

# Production Concurrency Guard: Up to 3 simultaneous NVENC GPU renders
import threading
import time
import concurrent.futures
RENDER_SEMAPHORE = threading.Semaphore(3)
RENDER_LOCK = threading.Lock()
ACTIVE_RENDERS = 0
WAITING_QUEUE = 0

# =========================================================================
# RATE LIMITING: 3 YOUTUBE LINK PASTES/ANALYSES PER 4 HOURS (14,400s) PER CLIENT IP
# Users can export and download generated shorts unlimited times from their analyzed videos!
# =========================================================================
RATE_LIMIT_MAX_LINKS = 999999
RATE_LIMIT_MAX_CLIPS = 999999
RATE_LIMIT_WINDOW_SECONDS = 0
PASTE_RATE_LIMITS: Dict[str, List[float]] = {}
RATE_LIMIT_LOCK = threading.Lock()


def get_client_ip(request: Request) -> str:
    """Extract real client IP even behind Cloudflare Tunnel, Vercel, or proxy."""
    cf_ip = request.headers.get("CF-Connecting-IP")
    if cf_ip:
        return cf_ip.strip()
    x_forwarded = request.headers.get("X-Forwarded-For")
    if x_forwarded:
        return x_forwarded.split(",")[0].strip()
    x_real_ip = request.headers.get("X-Real-IP")
    if x_real_ip:
        return x_real_ip.strip()
    if request.client and request.client.host:
        return request.client.host.strip()
    return "127.0.0.1"


def check_link_rate_limit(client_id: str) -> Tuple[bool, int, float]:
    """Unlimited link pastes and clip generations for all users."""
    return True, 999999, 0.0


# Alias for backward compatibility
check_rate_limit = check_link_rate_limit


def record_link_paste(client_id: str):
    """Record a YouTube link paste/analysis timestamp for rate limiting."""
    now = time.time()
    cutoff = now - RATE_LIMIT_WINDOW_SECONDS
    with RATE_LIMIT_LOCK:
        timestamps = PASTE_RATE_LIMITS.get(client_id, [])
        valid_ts = [t for t in timestamps if t > cutoff]
        valid_ts.append(now)
        PASTE_RATE_LIMITS[client_id] = valid_ts


# Alias for backward compatibility
record_clip_export = record_link_paste


def get_current_tunnel_url() -> Optional[str]:
    """Retrieve active Cloudflare Tunnel URL from vercel.json to bypass Vercel bandwidth limits."""
    try:
        vercel_cfg = BASE_DIR / "vercel.json"
        if vercel_cfg.exists():
            content = json.loads(vercel_cfg.read_text(encoding="utf-8"))
            for rule in content.get("rewrites", []):
                if rule.get("source") == "/api/:path*":
                    dest = rule.get("destination", "")
                    m = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", dest)
                    if m:
                        return m.group(0)
    except Exception:
        pass
    return None


async def _periodic_disk_cleaner():
    """Wakes up every 60 seconds and auto-deletes any temporary video or fragment older than 5 minutes (300s)."""
    while True:
        try:
            await asyncio.sleep(60)
            await asyncio.to_thread(cleanup_old_temp_files, 300)
            now = time.time()
            for tmp in BASE_DIR.glob("*.tmp*"):
                if tmp.is_file() and (now - tmp.stat().st_mtime) > 300:
                    try:
                        tmp.unlink(missing_ok=True)
                    except Exception:
                        pass
        except Exception:
            pass


def _prune_expired_tasks_and_caches():
    """Purge tasks and caches older than 30 minutes to prevent RAM creep under high traffic."""
    now = time.time()
    expired = [tid for tid, t in list(TASKS.items()) if (now - t.get("created_at", now)) > 1800]
    for tid in expired:
        TASKS.pop(tid, None)
    if len(TRANSCRIPT_CACHE) > 40:
        TRANSCRIPT_CACHE.clear()
    if len(HEATMAP_CACHE) > 40:
        HEATMAP_CACHE.clear()
    if len(ANALYSIS_CACHE) > 50:
        ANALYSIS_CACHE.clear()


@app.on_event("startup")
async def on_startup():
    """Probe GPU encoder once on startup, launch 5-min auto disk cleaner, and scale thread pool."""
    try:
        loop = asyncio.get_running_loop()
        loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=64))
    except Exception:
        pass
    asyncio.create_task(_periodic_disk_cleaner())
    await asyncio.to_thread(detect_best_encoder)


class AnalyzeRequest(BaseModel):
    url: str
    clip_duration: float = Field(default=30.0, ge=10.0, le=120.0)
    max_clips: int = Field(default=5, ge=1, le=15)
    access_key: Optional[str] = None


class GenerateRequest(BaseModel):
    url: str
    video_id: str
    clip_id: str = "clip_1"
    start_time: float
    end_time: float
    aspect_ratio: str = "9:16"
    framing_mode: str = "blur"
    burn_subtitles: bool = True
    subtitle_style: str = "viral_pop"
    subtitle_position: str = "bottom"
    resolution: str = "1080p"
    climax_hook: bool = True
    peak_time: Optional[float] = None
    access_key: Optional[str] = None


class BoxModel(BaseModel):
    x: float = 0.0
    y: float = 0.0
    w: float = 0.5
    h: float = 0.5

    @field_validator("x", "y", mode="before")
    def clamp_coord(cls, v):
        try:
            return max(0.0, min(1.0, float(v)))
        except Exception:
            return 0.0

    @field_validator("w", "h", mode="before")
    def clamp_size(cls, v):
        try:
            return max(0.05, min(1.0, float(v)))
        except Exception:
            return 0.5


class LiveKeyVerifyRequest(BaseModel):
    key: str


class ProInspectRequest(BaseModel):
    url: str
    force_refresh: bool = False
    preview_offset_sec: Optional[float] = Field(default=None, ge=0.0)
    access_key: Optional[str] = None


class ProFramesRequest(BaseModel):
    url: str
    video_id: str
    is_live: bool = False
    start_utc_ts: Optional[float] = None
    end_utc_ts: Optional[float] = None
    start_time: float = Field(default=0.0, ge=0.0)
    end_time: float = Field(default=35.0, ge=0.0)
    access_key: Optional[str] = None


class ProGenerateRequest(BaseModel):
    url: str
    video_id: str
    is_live: bool = False
    duration_sec: float = Field(default=35.0, ge=5.0, le=120.0)
    rewind_sec: float = Field(default=0.0, ge=0.0, le=43200.0)
    start_utc_ts: Optional[float] = None
    end_utc_ts: Optional[float] = None
    start_time: float = Field(default=0.0, ge=0.0)
    end_time: float = Field(default=35.0, ge=5.0)
    cam_box: BoxModel
    stream_box: BoxModel
    cam_ratio: float = Field(default=0.40, ge=0.25, le=0.65)
    cam_position: str = "top"
    resolution: str = "1080p"
    burn_subtitles: bool = True
    subtitle_style: str = "viral_pop"
    subtitle_position: str = "seam"
    access_key: Optional[str] = None


class AutoViralScanRequest(BaseModel):
    url: str
    max_peaks: int = Field(default=5, ge=1, le=10)
    clip_duration: float = Field(default=30.0, ge=10.0, le=90.0)
    hook_duration: float = Field(default=3.0, ge=2.0, le=6.0)
    access_key: Optional[str] = None


class AutoViralGenerateRequest(BaseModel):
    url: str
    video_id: str
    is_live: bool = True
    peak_offset_sec: float = 0.0
    clip_start_utc_ts: Optional[float] = None
    clip_end_utc_ts: Optional[float] = None
    hook_start_utc_ts: Optional[float] = None
    hook_end_utc_ts: Optional[float] = None
    start_time: float = Field(default=0.0, ge=0.0)
    end_time: float = Field(default=30.0, ge=5.0)
    cam_box: Optional[BoxModel] = None
    stream_box: Optional[BoxModel] = None
    cam_ratio: float = Field(default=0.40, ge=0.25, le=0.65)
    cam_position: str = "bottom"
    resolution: str = "1080p"
    burn_subtitles: bool = True
    subtitle_style: str = "viral_pop"
    subtitle_position: str = "seam"
    climax_hook: bool = True
    hook_duration: float = Field(default=3.0, ge=2.0, le=6.0)
    access_key: Optional[str] = None


def _require_access_key(key: Optional[str]):
    """Strictly enforce valid 6-hour Telegram access key before any generation or analysis."""
    is_valid, msg, _ = verify_live_key(key or "")
    if not is_valid:
        raise HTTPException(
            status_code=403,
            detail={
                "message": msg or "Access key required. Get the active 6-hour key from our Telegram group!",
                "telegram_url": TELEGRAM_CHANNEL_URL,
            },
        )


@app.get("/api/diag")
async def api_diag(url: str = "https://www.youtube.com/watch?v=B2YhlGTGXpM"):
    """Safe diagnostic endpoint (never exposes secrets) to inspect yt-dlp formats on cloud hosts."""
    import shutil
    import yt_dlp
    from backend.config import YTDLP_BASE_OPTS

    cookie_path = YTDLP_BASE_OPTS.get("cookiefile")
    cookie_lines = 0
    if cookie_path and Path(cookie_path).exists():
        cookie_lines = sum(
            1 for ln in Path(cookie_path).read_text(encoding="utf-8", errors="ignore").splitlines()
            if ln.strip() and not ln.startswith("#")
        )

    def _probe():
        report = {}
        for name, extra, use_ck in [
            ("visionos_nock", {"extractor_args": {"youtube": {"player_client": ["visionos"]}}}, False),
            ("android_nock", {"extractor_args": {"youtube": {"player_client": ["android", "mweb"]}}}, False),
            ("ios_nock", {"extractor_args": {"youtube": {"player_client": ["ios"]}}}, False),
            ("web_creator_nock", {"extractor_args": {"youtube": {"player_client": ["web_creator"]}}}, False),
            ("web_embedded_nock", {"extractor_args": {"youtube": {"player_client": ["web_embedded"]}}}, False),
            ("default_ck", {}, True),
            ("web_safari_ck", {"extractor_args": {"youtube": {"player_client": ["web_safari"]}}}, True),
            ("tv_ck", {"extractor_args": {"youtube": {"player_client": ["tv"]}}}, True),
            ("mweb_ck", {"extractor_args": {"youtube": {"player_client": ["mweb"]}}}, True),
            ("android_vr_nock", {"extractor_args": {"youtube": {"player_client": ["android_vr"]}}}, False),
        ]:
            warns = []

            class _DiagLogger:
                def debug(self, msg): pass
                def info(self, msg): pass
                def warning(self, msg): warns.append(str(msg)[:160])
                def error(self, msg): warns.append("ERR: " + str(msg)[:160])

            opts = dict(YTDLP_BASE_OPTS)
            if not use_ck:
                opts.pop("cookiefile", None)
            opts.update(extra)
            opts["ignore_no_formats_error"] = True
            opts["format"] = None
            opts["no_warnings"] = False
            opts["logger"] = _DiagLogger()
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                fmts = [
                    f"{f.get('format_id')}:{f.get('height')}p:{f.get('protocol')}"
                    for f in (info.get("formats") or [])
                    if f.get("vcodec") not in (None, "none")
                ]
                report[name] = {"video_count": len(fmts), "sample": fmts[-6:], "warnings": warns[:4]}
            except Exception as e:
                report[name] = {"error": re.sub(r"\x1b\[[0-9;]*m", "", str(e))[:220], "warnings": warns[:4]}
        return report

    from backend.whisper_captions import get_whisper_status
    clients = await asyncio.to_thread(_probe)
    return {
        "yt_dlp_version": yt_dlp.version.__version__,
        "deno_path": shutil.which("deno"),
        "node_path": shutil.which("node"),
        "ffmpeg_path": shutil.which("ffmpeg"),
        "cookie_entries": cookie_lines,
        "whisper": get_whisper_status(),
        "clients": clients,
    }


@app.get("/api/preview")
async def api_preview(url: str):
    """Instant YouTube thumbnail & title preview as soon as a user pastes a link."""
    vid = extract_video_id(url)
    if not vid:
        raise HTTPException(status_code=400, detail="Invalid YouTube link")
    clean_url = f"https://www.youtube.com/watch?v={vid}"
    title = "YouTube Video"
    author = ""
    thumbnail = f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg"
    try:
        resp = await asyncio.to_thread(
            requests.get,
            "https://www.youtube.com/oembed",
            params={"url": clean_url, "format": "json"},
            timeout=5,
        )
        if resp.status_code == 200:
            j = resp.json()
            title = j.get("title", title)
            author = j.get("author_name", "")
            thumbnail = j.get("thumbnail_url", thumbnail)
            if thumbnail and "hqdefault.jpg" in thumbnail:
                thumbnail = thumbnail.replace("hqdefault.jpg", "maxresdefault.jpg")
    except Exception:
        pass
    return {
        "success": True,
        "video_id": vid,
        "title": title,
        "author": author,
        "thumbnail": thumbnail,
    }


@app.post("/api/analyze")
async def api_analyze(req: AnalyzeRequest, request: Request, background_tasks: BackgroundTasks):
    """Analyze YouTube link, extract engagement map & frame pictures, with 3-link-pastes per 4h rate limit."""
    _require_access_key(req.access_key)
    client_ip = get_client_ip(request)

    # 0. Instant Cache: If already analyzed recently, return in 0.001s!
    cache_k = f"{req.url}_{req.clip_duration}_{req.max_clips}"
    cached = ANALYSIS_CACHE.get(cache_k)
    if cached and (time.time() - cached.get("ts", 0)) < 900:
        return {"success": True, "data": cached["data"], "remaining_links": 999999}

    allowed, remaining, reset_in = check_link_rate_limit(client_ip)
    if not allowed:
        hrs = int(reset_in // 3600)
        mins = int((reset_in % 3600) // 60)
        time_msg = f"{hrs}h {mins}m" if hrs > 0 else f"{mins}m"
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit reached: Free tier allows pasting up to {RATE_LIMIT_MAX_LINKS} YouTube links per 4 hours. Next link unlocks in {time_msg}. (You can still generate and download unlimited shorts from your analyzed videos!)",
        )

    try:
        data = await asyncio.to_thread(
            analyze_youtube_video,
            url=req.url,
            clip_duration=req.clip_duration,
            max_clips=req.max_clips,
        )
        vid = data["video_id"]
        caption_info = data.pop("_caption_info", None)
        info_obj = data.pop("_info", None)
        prefetched = data.pop("_prefetched_transcript", None)
        transcript = (
            prefetched
            if prefetched is not None
            else await asyncio.to_thread(fetch_youtube_transcript, vid, info_obj or caption_info)
        )
        data["has_transcript"] = bool(transcript and len(transcript) > 0)
        TRANSCRIPT_CACHE[vid] = transcript
        HEATMAP_CACHE[vid] = data.get("heatmap") or []
        ANALYSIS_CACHE[cache_k] = {"data": data, "ts": time.time()}

        # Successfully analyzed valid YouTube link -> count 1 link paste
        record_link_paste(client_ip)

        # Pre-warm HD stream URLs in background while user picks a moment!
        background_tasks.add_task(prewarm_video_streams, req.url, vid)

        return {"success": True, "data": data, "remaining_links": max(0, remaining - 1)}
    except HTTPException:
        raise
    except Exception as e:
        clean_err = re.sub(r"\x1b\[[0-9;]*m", "", str(e))
        raise HTTPException(status_code=400, detail=clean_err)


def _run_generate_task(task_id: str, req: GenerateRequest):
    """Background task runner for fast GPU Short creation with multi-user live queue tracking."""
    global ACTIVE_RENDERS, WAITING_QUEUE
    def update_progress(message: str, progress: int):
        if task_id in TASKS:
            TASKS[task_id]["message"] = message
            TASKS[task_id]["progress"] = progress

    with RENDER_LOCK:
        WAITING_QUEUE += 1
        pos = WAITING_QUEUE

    try:
        if ACTIVE_RENDERS >= 3 or pos > 1:
            eta = pos * 5
            update_progress(f"In queue (Position #{pos} — wait ~{eta}s)...", 6)
        else:
            update_progress("Starting GPU render slot...", 8)

        with RENDER_SEMAPHORE:
            with RENDER_LOCK:
                WAITING_QUEUE = max(0, WAITING_QUEUE - 1)
                ACTIVE_RENDERS += 1

            try:
                cached_transcript = TRANSCRIPT_CACHE.get(req.video_id)
                cached_heatmap = HEATMAP_CACHE.get(req.video_id)
                result = process_clip_pipeline(
                    url=req.url,
                    video_id=req.video_id,
                    clip_id=req.clip_id,
                    start_time=req.start_time,
                    end_time=req.end_time,
                    aspect_ratio=req.aspect_ratio,
                    framing_mode=req.framing_mode,
                    burn_subtitles=req.burn_subtitles,
                    subtitle_style=req.subtitle_style,
                    subtitle_position=req.subtitle_position,
                    resolution=req.resolution,
                    climax_hook=req.climax_hook,
                    peak_time=req.peak_time,
                    heatmap_points=cached_heatmap,
                    transcript_cache=cached_transcript,
                    progress_cb=update_progress,
                )
                TASKS[task_id]["status"] = "completed"
                TASKS[task_id]["progress"] = 100
                TASKS[task_id]["message"] = "Short ready!"
                TASKS[task_id]["result"] = result
            finally:
                with RENDER_LOCK:
                    ACTIVE_RENDERS = max(0, ACTIVE_RENDERS - 1)
    except Exception as e:
        with RENDER_LOCK:
            WAITING_QUEUE = max(0, WAITING_QUEUE - 1)
        clean_err = re.sub(r"\x1b\[[0-9;]*m", "", str(e))
        TASKS[task_id]["status"] = "failed"
        TASKS[task_id]["error"] = clean_err
        TASKS[task_id]["message"] = f"Render failed: {clean_err}"


@app.get("/api/rate-limit")
async def api_rate_limit(request: Request):
    """Return link paste rate limit quota for client (Unlimited)."""
    return {
        "success": True,
        "allowed": True,
        "unlimited": True,
        "max_links": 999999,
        "max_clips": 999999,
        "remaining": 999999,
        "window_hours": 0,
        "reset_in_seconds": 0.0,
        "reset_human": "Unlimited",
    }


@app.post("/api/generate")
async def api_generate(req: GenerateRequest, background_tasks: BackgroundTasks):
    """Launch fast asynchronous Short creation (Unlimited exports from analyzed videos!)."""
    _require_access_key(req.access_key)
    _prune_expired_tasks_and_caches()

    task_id = str(uuid.uuid4())
    TASKS[task_id] = {
        "task_id": task_id,
        "status": "processing",
        "progress": 8,
        "message": "Starting...",
        "clip_id": req.clip_id,
        "aspect_ratio": req.aspect_ratio,
        "created_at": time.time(),
        "result": None,
        "error": None,
    }
    background_tasks.add_task(_run_generate_task, task_id, req)
    return {"success": True, "task_id": task_id}


@app.post("/api/live/verify-key")
async def api_live_verify_key(req: LiveKeyVerifyRequest):
    """Validate 6-hour access key obtained from Telegram channel."""
    is_valid, msg, expires_in_sec = verify_live_key(req.key)
    if not is_valid:
        raise HTTPException(
            status_code=403,
            detail={
                "message": msg,
                "telegram_url": TELEGRAM_CHANNEL_URL,
            },
        )
    return {
        "success": True,
        "valid": True,
        "message": msg,
        "expires_in_sec": expires_in_sec,
        "telegram_url": TELEGRAM_CHANNEL_URL,
    }


@app.post("/api/pro/inspect")
async def api_pro_inspect(req: ProInspectRequest):
    """Inspect a YouTube Live Stream or video for Live Studio and return DVR timeline + frame."""
    _require_access_key(req.access_key)
    try:
        data = await asyncio.to_thread(
            inspect_pro_source,
            url=req.url,
            force_refresh=req.force_refresh,
            preview_offset_sec=req.preview_offset_sec,
        )
        return {"success": True, "data": data}
    except Exception as e:
        clean_err = re.sub(r"\x1b\[[0-9;]*m", "", str(e))
        raise HTTPException(status_code=400, detail=clean_err)


@app.post("/api/pro/frames")
async def api_pro_frames(req: ProFramesRequest):
    """Grab exact Start & End frames directly from the exact DVR/VOD timestamps selected by the user."""
    _require_access_key(req.access_key)
    try:
        data = await asyncio.to_thread(
            fetch_pro_boundary_frames,
            url=req.url,
            video_id=req.video_id,
            is_live=req.is_live,
            start_utc_ts=req.start_utc_ts,
            end_utc_ts=req.end_utc_ts,
            start_time=req.start_time,
            end_time=req.end_time,
        )
        return {"success": True, "data": data}
    except Exception as e:
        clean_err = re.sub(r"\x1b\[[0-9;]*m", "", str(e))
        raise HTTPException(status_code=400, detail=clean_err)


@app.get("/api/pro/radar/{video_id}")
async def api_pro_radar(video_id: str, reset: bool = False):
    """Real-time YouTube Live Chat Hype Radar: chat speed graph + auto-detected viral spikes."""
    try:
        data = await asyncio.to_thread(poll_live_chat_radar, video_id, reset)
        return {"success": True, "data": data}
    except Exception as e:
        return {
            "success": False,
            "data": {"available": False, "reason": str(e)[:120], "timeline": [], "spikes": []},
        }


def _run_pro_generate_task(task_id: str, req: ProGenerateRequest):
    """Background task runner for Pro Studio Live/VOD Split-Screen + AI Hinglish Captions."""
    global ACTIVE_RENDERS, WAITING_QUEUE
    def update_progress(message: str, progress: int):
        if task_id in TASKS:
            TASKS[task_id]["message"] = message
            TASKS[task_id]["progress"] = progress

    with RENDER_LOCK:
        WAITING_QUEUE += 1
        pos = WAITING_QUEUE

    try:
        if ACTIVE_RENDERS >= 3 or pos > 1:
            eta = pos * 5
            update_progress(f"In queue (Position #{pos} — wait ~{eta}s)...", 6)
        else:
            update_progress("Starting GPU render slot...", 8)

        with RENDER_SEMAPHORE:
            with RENDER_LOCK:
                WAITING_QUEUE = max(0, WAITING_QUEUE - 1)
                ACTIVE_RENDERS += 1

            try:
                cached_transcript = TRANSCRIPT_CACHE.get(req.video_id)
                result = process_pro_split_pipeline(
                    url=req.url,
                    video_id=req.video_id,
                    is_live=req.is_live,
                    cam_box=req.cam_box.model_dump(),
                    stream_box=req.stream_box.model_dump(),
                    duration_sec=req.duration_sec,
                    rewind_sec=req.rewind_sec,
                    start_utc_ts=req.start_utc_ts,
                    end_utc_ts=req.end_utc_ts,
                    start_time=req.start_time,
                    end_time=req.end_time,
                    cam_ratio=req.cam_ratio,
                    cam_position=req.cam_position,
                    resolution=req.resolution,
                    burn_subtitles=req.burn_subtitles,
                    subtitle_style=req.subtitle_style,
                    subtitle_position=req.subtitle_position,
                    transcript_cache=cached_transcript,
                    progress_cb=update_progress,
                )
                TASKS[task_id]["status"] = "completed"
                TASKS[task_id]["progress"] = 100
                TASKS[task_id]["message"] = "Pro Short ready!"
                TASKS[task_id]["result"] = result
            finally:
                with RENDER_LOCK:
                    ACTIVE_RENDERS = max(0, ACTIVE_RENDERS - 1)
    except Exception as e:
        with RENDER_LOCK:
            WAITING_QUEUE = max(0, WAITING_QUEUE - 1)
        import traceback
        traceback.print_exc()
        clean_err = re.sub(r"\x1b\[[0-9;]*m", "", str(e))
        print(f"[ERROR] Pro render failed for task {task_id}: {clean_err}")
        TASKS[task_id]["status"] = "failed"
        TASKS[task_id]["error"] = clean_err
        TASKS[task_id]["message"] = f"Pro render failed: {clean_err}"


@app.post("/api/pro/generate")
async def api_pro_generate(req: ProGenerateRequest, background_tasks: BackgroundTasks):
    """Launch asynchronous Pro Studio Split-Screen Short render."""
    _require_access_key(req.access_key)
    _prune_expired_tasks_and_caches()
    task_id = str(uuid.uuid4())
    TASKS[task_id] = {
        "task_id": task_id,
        "status": "processing",
        "progress": 8,
        "message": "Starting Pro Studio...",
        "clip_id": "pro_split",
        "aspect_ratio": "9:16",
        "created_at": time.time(),
        "result": None,
        "error": None,
    }
    background_tasks.add_task(_run_pro_generate_task, task_id, req)
    return {"success": True, "task_id": task_id}


def _run_auto_viral_task(task_id: str, req: AutoViralGenerateRequest):
    """Background task runner for Auto-Viral Short creation with 3s hook & auto-boxes."""
    global ACTIVE_RENDERS, WAITING_QUEUE
    def update_progress(message: str, progress: int):
        if task_id in TASKS:
            TASKS[task_id]["message"] = message
            TASKS[task_id]["progress"] = progress

    with RENDER_LOCK:
        WAITING_QUEUE += 1
        pos = WAITING_QUEUE

    try:
        if ACTIVE_RENDERS >= 3 or pos > 1:
            eta = pos * 5
            update_progress(f"In queue (Position #{pos} — wait ~{eta}s)...", 6)
        else:
            update_progress("Starting GPU render slot...", 8)

        with RENDER_SEMAPHORE:
            with RENDER_LOCK:
                WAITING_QUEUE = max(0, WAITING_QUEUE - 1)
                ACTIVE_RENDERS += 1

            try:
                cached_transcript = TRANSCRIPT_CACHE.get(req.video_id)
                cam_b = req.cam_box.model_dump() if req.cam_box else {"x": 0.68, "y": 0.58, "w": 0.30, "h": 0.40}
                str_b = req.stream_box.model_dump() if req.stream_box else {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}

                result = process_pro_split_pipeline(
                    url=req.url,
                    video_id=req.video_id,
                    is_live=req.is_live,
                    cam_box=cam_b,
                    stream_box=str_b,
                    duration_sec=30.0,
                    rewind_sec=req.peak_offset_sec,
                    start_utc_ts=req.clip_start_utc_ts,
                    end_utc_ts=req.clip_end_utc_ts,
                    start_time=req.start_time,
                    end_time=req.end_time,
                    cam_ratio=req.cam_ratio,
                    cam_position=req.cam_position,
                    resolution=req.resolution,
                    burn_subtitles=req.burn_subtitles,
                    subtitle_style=req.subtitle_style,
                    subtitle_position=req.subtitle_position,
                    climax_hook=req.climax_hook,
                    hook_duration=req.hook_duration,
                    transcript_cache=cached_transcript,
                    progress_cb=update_progress,
                )
                TASKS[task_id]["status"] = "completed"
                TASKS[task_id]["progress"] = 100
                TASKS[task_id]["message"] = "Auto Viral Short ready!"
                TASKS[task_id]["result"] = result
            finally:
                with RENDER_LOCK:
                    ACTIVE_RENDERS = max(0, ACTIVE_RENDERS - 1)
    except Exception as e:
        with RENDER_LOCK:
            WAITING_QUEUE = max(0, WAITING_QUEUE - 1)
        clean_err = re.sub(r"\x1b\[[0-9;]*m", "", str(e))
        TASKS[task_id]["status"] = "failed"
        TASKS[task_id]["error"] = clean_err
        TASKS[task_id]["message"] = f"Auto viral render failed: {clean_err}"


@app.post("/api/auto-viral/scan")
async def api_auto_viral_scan(req: AutoViralScanRequest):
    """Scan YouTube Live stream chat spikes & auto-detect viral peaks + auto-framed boxes."""
    _require_access_key(req.access_key)
    try:
        inspect_data = await asyncio.to_thread(inspect_pro_source, url=req.url)
        vid = inspect_data.get("video_id")
        is_live = bool(inspect_data.get("is_live"))
        first_pdt = inspect_data.get("first_pdt_utc")
        dvr_win = float(inspect_data.get("dvr_window_sec") or 300.0)
        frame_img = inspect_data.get("frame_image") or ""

        auto_boxes = compute_auto_boxes(frame_img) if frame_img else get_fallback_boxes()

        peaks = await asyncio.to_thread(
            scan_live_viral_moments,
            video_id=vid,
            first_pdt_utc=first_pdt,
            total_dur=dvr_win,
            max_peaks=req.max_peaks,
            clip_dur=req.clip_duration,
            hook_dur=req.hook_duration,
        )

        for p in peaks:
            p["auto_boxes"] = auto_boxes
            p["thumbnail_b64"] = frame_img

        return {
            "success": True,
            "data": {
                "video_id": vid,
                "title": inspect_data.get("title", "Live Stream"),
                "author": inspect_data.get("author", ""),
                "is_live": is_live,
                "first_pdt_utc": first_pdt,
                "dvr_window_sec": dvr_win,
                "peaks": peaks,
                "auto_boxes": auto_boxes,
            },
        }
    except Exception as e:
        clean_err = re.sub(r"\x1b\[[0-9;]*m", "", str(e))
        raise HTTPException(status_code=400, detail=clean_err)


@app.post("/api/auto-viral/generate")
async def api_auto_viral_generate(req: AutoViralGenerateRequest, background_tasks: BackgroundTasks):
    """Launch asynchronous Auto Viral Short render with 3s hook + auto-framing (Unlimited exports!)."""
    _require_access_key(req.access_key)
    _prune_expired_tasks_and_caches()

    task_id = str(uuid.uuid4())
    TASKS[task_id] = {
        "task_id": task_id,
        "status": "processing",
        "progress": 8,
        "message": "Starting Auto Viral...",
        "clip_id": "auto_viral",
        "aspect_ratio": "9:16",
        "created_at": time.time(),
        "result": None,
        "error": None,
    }
    background_tasks.add_task(_run_auto_viral_task, task_id, req)
    return {"success": True, "task_id": task_id}


@app.get("/api/task/{task_id}")
async def api_get_task(task_id: str):
    """Return current creation status and direct tunnel download URL to bypass Vercel bandwidth limits."""
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    if task.get("status") == "completed" and task.get("result"):
        res = dict(task["result"])
        tunnel = get_current_tunnel_url()
        dl_path = res.get("download_url", "")
        if tunnel and dl_path:
            res["direct_download_url"] = f"{tunnel}{dl_path}"
        task_copy = dict(task)
        task_copy["result"] = res
        return task_copy

    return task


class FastDownloadFileResponse(FileResponse):
    chunk_size = 512 * 1024  # 512 KB chunks for fast I/O throughput


@app.get("/api/stream/{filename}")
@app.get("/api/download/{filename}")
@app.head("/api/stream/{filename}")
@app.head("/api/download/{filename}")
async def api_download(filename: str, inline: bool = False):
    """Serve the newly created Short video for instant playback & download.
    Supports RFC 7233 byte-range streaming for Apple iOS/Safari and Fast I/O downloads."""
    safe_name = Path(filename).name
    filepath = TEMP_DIR / safe_name
    if not filepath.exists() or not filepath.is_file():
        return Response(
            status_code=404,
            content="Video expired or not found",
            media_type="text/plain",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
        )
    disp = f'inline; filename="{safe_name}"' if inline else f'attachment; filename="{safe_name}"'
    return FastDownloadFileResponse(
        path=filepath,
        filename=safe_name,
        media_type="video/mp4",
        headers={
            "Content-Disposition": disp,
            "Accept-Ranges": "bytes",
            "Cache-Control": "public, max-age=86400, stale-while-revalidate=3600",
            "Access-Control-Allow-Origin": "*",
        },
    )


# Cached pre-blurred background JPEG bytes (eliminates browser real-time GPU blur lag!)
_BLUR_BG_CACHE: Dict[str, Any] = {"mtime": 0.0, "bytes": None}


def _get_preblurred_background() -> Optional[bytes]:
    bg_file = BASE_DIR / "background.png"
    if not bg_file.exists():
        return None
    mtime = bg_file.stat().st_mtime
    if _BLUR_BG_CACHE["bytes"] is not None and _BLUR_BG_CACHE["mtime"] == mtime:
        return _BLUR_BG_CACHE["bytes"]

    img = Image.open(bg_file).convert("RGB")
    # Crisp 1600px wide + light dreamy blur (radius=3.5) so artwork stays clearly visible & 60fps
    if img.width > 1600:
        ratio = 1600 / img.width
        img = img.resize((1600, max(1, int(img.height * ratio))), Image.Resampling.LANCZOS)
    img = img.filter(ImageFilter.GaussianBlur(radius=3.5))
    img = ImageEnhance.Brightness(img).enhance(1.02)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=86, optimize=True)
    data = buf.getvalue()
    _BLUR_BG_CACHE["mtime"] = mtime
    _BLUR_BG_CACHE["bytes"] = data
    return data


@app.get("/background-blur.jpg")
async def serve_blurred_background():
    data = await asyncio.to_thread(_get_preblurred_background)
    if data is None:
        raise HTTPException(status_code=404, detail="Background not found")
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/background.png")
@app.get("/background.jpg")
@app.get("/background.jpeg")
async def serve_background():
    for f in [
        FRONTEND_DIR / "background.png",
        BASE_DIR / "background.png",
        FRONTEND_DIR / "background.jpg",
        BASE_DIR / "background.jpg",
        FRONTEND_DIR / "background.jpeg",
        BASE_DIR / "background.jpeg",
    ]:
        if f.exists():
            media = "image/png" if f.suffix.lower() == ".png" else "image/jpeg"
            return FileResponse(f, media_type=media, headers={"Cache-Control": "no-cache, must-revalidate"})
    raise HTTPException(status_code=404, detail="Background not found")


@app.get("/bihari.png")
@app.get("/bihari.jpg")
@app.get("/mobilebackground.png")
async def serve_bihari():
    for f in [FRONTEND_DIR / "mobilebackground.png", BASE_DIR / "mobilebackground.png", FRONTEND_DIR / "bihari.png", BASE_DIR / "bihari.png", FRONTEND_DIR / "bihari.jpg", BASE_DIR / "bihari.jpg"]:
        if f.exists():
            media = "image/png" if f.suffix == ".png" else "image/jpeg"
            return FileResponse(f, media_type=media, headers={"Cache-Control": "no-cache, must-revalidate"})
    raise HTTPException(status_code=404, detail="File not found")


FRONTEND_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/robots.txt")
async def serve_robots():
    f = FRONTEND_DIR / "robots.txt"
    if f.exists():
        return FileResponse(f, media_type="text/plain")
    return Response(content="User-agent: *\nAllow: /\n", media_type="text/plain")


@app.get("/sitemap.xml")
async def serve_sitemap():
    f = FRONTEND_DIR / "sitemap.xml"
    if f.exists():
        return FileResponse(f, media_type="application/xml")
    raise HTTPException(status_code=404, detail="Sitemap not found")


@app.get("/google{ver_id}.html")
async def serve_google_verification(ver_id: str):
    f = FRONTEND_DIR / f"google{ver_id}.html"
    if f.exists():
        return FileResponse(f, media_type="text/html")
    return Response(content=f"google-site-verification: google{ver_id}.html\n", media_type="text/html")


@app.get("/api/debug/ytdlp")
async def api_debug_ytdlp(url: str = "https://www.youtube.com/watch?v=Dz0naoD1QWo"):
    import subprocess, sys, os
    cookie_str = os.environ.get("YOUTUBE_COOKIES", "")
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "-F",
        "--verbose",
        "--no-playlist",
    ]
    if YTDLP_BASE_OPTS.get("cookiefile"):
        cmd.extend(["--cookies", YTDLP_BASE_OPTS["cookiefile"]])
    cmd.append(url)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
    return {
        "cookie_len": len(cookie_str),
        "cookie_file": YTDLP_BASE_OPTS.get("cookiefile"),
        "returncode": proc.returncode,
        "stdout": proc.stdout[:2000],
        "stderr": proc.stderr[-2000:],
    }


@app.get("/")
async def serve_index():
    index_file = FRONTEND_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return {"message": "Frontend index.html not found."}
