import io
import re
import uuid
import asyncio
from pathlib import Path
from typing import Dict, Any, Optional
import requests
from PIL import Image, ImageFilter, ImageEnhance
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend.config import BASE_DIR, TEMP_DIR, FRONTEND_DIR
from backend.analyzer import analyze_youtube_video, extract_video_id
from backend.subtitles import fetch_youtube_transcript
from backend.video_processor import (
    process_clip_pipeline,
    prewarm_video_streams,
    detect_best_encoder,
)
from backend.pro_studio import (
    inspect_pro_source,
    process_pro_split_pipeline,
)

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


@app.on_event("startup")
async def on_startup():
    """Probe GPU encoder once on startup so first Short creation is instant."""
    await asyncio.to_thread(detect_best_encoder)


class AnalyzeRequest(BaseModel):
    url: str
    clip_duration: float = Field(default=35.0, ge=10.0, le=120.0)
    max_clips: int = Field(default=5, ge=1, le=15)


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


class BoxModel(BaseModel):
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    w: float = Field(ge=0.05, le=1.0)
    h: float = Field(ge=0.05, le=1.0)


class ProInspectRequest(BaseModel):
    url: str
    force_refresh: bool = False


class ProGenerateRequest(BaseModel):
    url: str
    video_id: str
    is_live: bool = False
    duration_sec: float = Field(default=35.0, ge=10.0, le=90.0)
    rewind_sec: float = Field(default=0.0, ge=0.0, le=600.0)
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

    clients = await asyncio.to_thread(_probe)
    return {
        "yt_dlp_version": yt_dlp.version.__version__,
        "deno_path": shutil.which("deno"),
        "node_path": shutil.which("node"),
        "ffmpeg_path": shutil.which("ffmpeg"),
        "cookie_entries": cookie_lines,
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
    thumbnail = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
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
async def api_analyze(req: AnalyzeRequest, background_tasks: BackgroundTasks):
    """Analyze YouTube link, extract engagement map & frame pictures, and pre-warm streams."""
    try:
        data = await asyncio.to_thread(
            analyze_youtube_video,
            url=req.url,
            clip_duration=req.clip_duration,
            max_clips=req.max_clips,
        )
        vid = data["video_id"]
        caption_info = data.pop("_caption_info", None)
        transcript = await asyncio.to_thread(fetch_youtube_transcript, vid, caption_info)
        data["has_transcript"] = bool(transcript and len(transcript) > 0)
        TRANSCRIPT_CACHE[vid] = transcript
        HEATMAP_CACHE[vid] = data.get("heatmap") or []

        # Pre-warm HD stream URLs in background while user picks a moment!
        background_tasks.add_task(prewarm_video_streams, req.url, vid)

        return {"success": True, "data": data}
    except Exception as e:
        clean_err = re.sub(r"\x1b\[[0-9;]*m", "", str(e))
        raise HTTPException(status_code=400, detail=clean_err)


def _run_generate_task(task_id: str, req: GenerateRequest):
    """Background task runner for fast GPU Short creation."""
    def update_progress(message: str, progress: int):
        if task_id in TASKS:
            TASKS[task_id]["message"] = message
            TASKS[task_id]["progress"] = progress

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
    except Exception as e:
        clean_err = re.sub(r"\x1b\[[0-9;]*m", "", str(e))
        TASKS[task_id]["status"] = "failed"
        TASKS[task_id]["error"] = clean_err
        TASKS[task_id]["message"] = f"Render failed: {clean_err}"


@app.post("/api/generate")
async def api_generate(req: GenerateRequest, background_tasks: BackgroundTasks):
    """Launch fast asynchronous Short creation."""
    task_id = str(uuid.uuid4())
    TASKS[task_id] = {
        "task_id": task_id,
        "status": "processing",
        "progress": 8,
        "message": "Starting...",
        "clip_id": req.clip_id,
        "aspect_ratio": req.aspect_ratio,
        "result": None,
        "error": None,
    }
    background_tasks.add_task(_run_generate_task, task_id, req)
    return {"success": True, "task_id": task_id}


@app.post("/api/pro/inspect")
async def api_pro_inspect(req: ProInspectRequest):
    """Inspect a YouTube Live Stream or video for Pro Studio and return a fresh 16:9 frame."""
    try:
        data = await asyncio.to_thread(
            inspect_pro_source,
            url=req.url,
            force_refresh=req.force_refresh,
        )
        return {"success": True, "data": data}
    except Exception as e:
        clean_err = re.sub(r"\x1b\[[0-9;]*m", "", str(e))
        raise HTTPException(status_code=400, detail=clean_err)


def _run_pro_generate_task(task_id: str, req: ProGenerateRequest):
    """Background task runner for Pro Studio Live/VOD Split-Screen + AI Hinglish Captions."""
    def update_progress(message: str, progress: int):
        if task_id in TASKS:
            TASKS[task_id]["message"] = message
            TASKS[task_id]["progress"] = progress

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
    except Exception as e:
        clean_err = re.sub(r"\x1b\[[0-9;]*m", "", str(e))
        TASKS[task_id]["status"] = "failed"
        TASKS[task_id]["error"] = clean_err
        TASKS[task_id]["message"] = f"Pro render failed: {clean_err}"


@app.post("/api/pro/generate")
async def api_pro_generate(req: ProGenerateRequest, background_tasks: BackgroundTasks):
    """Launch asynchronous Pro Studio Split-Screen Short render."""
    task_id = str(uuid.uuid4())
    TASKS[task_id] = {
        "task_id": task_id,
        "status": "processing",
        "progress": 8,
        "message": "Starting Pro Studio...",
        "clip_id": "pro_split",
        "aspect_ratio": "9:16",
        "result": None,
        "error": None,
    }
    background_tasks.add_task(_run_pro_generate_task, task_id, req)
    return {"success": True, "task_id": task_id}


@app.get("/api/task/{task_id}")
async def api_get_task(task_id: str):
    """Return current creation status and download URL once complete."""
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.get("/api/download/{filename}")
async def api_download(filename: str):
    """Serve the newly created Short video for instant playback & download."""
    safe_name = Path(filename).name
    filepath = TEMP_DIR / safe_name
    if not filepath.exists() or not filepath.is_file():
        raise HTTPException(status_code=404, detail="Video expired or not found")
    return FileResponse(
        path=filepath,
        filename=safe_name,
        media_type="video/mp4",
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
async def serve_background():
    bg_file = BASE_DIR / "background.png"
    if bg_file.exists():
        return FileResponse(bg_file, media_type="image/png")
    raise HTTPException(status_code=404, detail="Background not found")


FRONTEND_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/")
async def serve_index():
    index_file = FRONTEND_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return {"message": "Frontend index.html not found."}
