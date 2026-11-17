import copy
import time
import subprocess
import uuid
from pathlib import Path
from typing import Dict, Any, Optional, Callable, Tuple
import yt_dlp
from yt_dlp.utils import download_range_func

from backend.config import (
    TEMP_DIR,
    YTDLP_BASE_OPTS,
    ASPECT_RATIOS,
    ASPECT_RATIOS_720,
)
from backend.subtitles import generate_ass_subtitles

# In-memory cache of pre-warmed yt-dlp stream metadata per video_id (expires after 20 min)
STREAM_INFO_CACHE: Dict[str, Dict[str, Any]] = {}
STREAM_CACHE_TTL_SEC = 1200

# Detected GPU encoder cached at startup
_BEST_ENCODER: Optional[Dict[str, Any]] = None


def detect_best_encoder() -> Dict[str, Any]:
    """Probe hardware GPU H.264 encoders once and cache the fastest high-quality option."""
    global _BEST_ENCODER
    if _BEST_ENCODER is not None:
        return _BEST_ENCODER

    candidates = [
        (
            "h264_nvenc",
            ["-hwaccel", "cuda"],
            ["-c:v", "h264_nvenc", "-preset", "p3", "-rc", "vbr", "-cq", "19", "-b:v", "0"],
        ),
        (
            "h264_qsv",
            [],
            ["-c:v", "h264_qsv", "-preset", "veryfast", "-global_quality", "20"],
        ),
        (
            "h264_amf",
            [],
            ["-c:v", "h264_amf", "-quality", "speed", "-rc", "cqp", "-qp_i", "19", "-qp_p", "19"],
        ),
    ]

    for name, hw_in, enc_args in candidates:
        test_cmd = [
            "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=640x360:d=0.2",
            *enc_args, "-f", "null", "-",
        ]
        try:
            res = subprocess.run(test_cmd, capture_output=True, text=True, timeout=6)
            if res.returncode == 0:
                _BEST_ENCODER = {"name": name, "hw_in": hw_in, "enc_args": enc_args}
                return _BEST_ENCODER
        except Exception:
            continue

    _BEST_ENCODER = {
        "name": "libx264",
        "hw_in": [],
        "enc_args": ["-c:v", "libx264", "-preset", "superfast", "-crf", "19"],
    }
    return _BEST_ENCODER


def _build_stream_strategies() -> list:
    """Preferred fast H.264 1080p stream strategies (zero AV1 decode slowdown)."""
    hd_fmt = (
        "bestvideo[height<=1080][fps<=30][vcodec^=avc1]+bestaudio[ext=m4a]/"
        "bestvideo[height<=1080][vcodec^=avc1]+bestaudio[ext=m4a]/"
        "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best"
    )
    strategies = []
    if YTDLP_BASE_OPTS.get("cookiefile"):
        strategies.extend([
            (
                "Authenticated TV HD Stream",
                {
                    "extractor_args": {"youtube": {"player_client": ["tv"]}},
                    "format": hd_fmt,
                },
            ),
            (
                "Authenticated Web HD Stream",
                {
                    "format": hd_fmt,
                },
            ),
        ])

    strategies.extend([
        (
            "HD Embedded Stream",
            {
                "extractor_args": {"youtube": {"player_client": ["web_embedded"]}},
                "format": hd_fmt,
            },
        ),
        (
            "HD HLS Stream",
            {
                "format": (
                    "bestvideo[height<=1080][protocol^=m3u8]+bestaudio[protocol^=m3u8]/"
                    "best[height<=1080][protocol^=m3u8]"
                ),
            },
        ),
        (
            "Android VR HD Stream",
            {
                "extractor_args": {"youtube": {"player_client": ["android_vr"]}},
                "format": hd_fmt,
            },
        ),
        (
            "Universal Fallback Stream",
            {
                "extractor_args": {"youtube": {"player_client": ["tv_simply"]}},
                "format": "best[height<=1080]/best",
            },
        ),
    ])
    return strategies


def prewarm_video_streams(url: str, video_id: str) -> None:
    """
    Background helper run immediately after analysis so clicking 'Create this Short'
    skips YouTube webpage scraping and JS challenge solving completely (0s overhead).
    """
    existing = STREAM_INFO_CACHE.get(video_id)
    if existing and (time.time() - existing["ts"] < STREAM_CACHE_TTL_SEC):
        return

    clean_url = f"https://www.youtube.com/watch?v={video_id}" if video_id else url
    for label, strat_opts in _build_stream_strategies():
        ydl_opts = dict(YTDLP_BASE_OPTS)
        ydl_opts.update(strat_opts)
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(clean_url, download=False)
            if info and (info.get("requested_formats") or info.get("url")):
                STREAM_INFO_CACHE[video_id] = {
                    "info": info,
                    "strat_opts": strat_opts,
                    "label": label,
                    "ts": time.time(),
                }
                return
        except Exception:
            continue


def cleanup_old_temp_files(max_age_sec: int = 900) -> None:
    """Remove temporary clips older than 15 minutes so disk never fills up."""
    now = time.time()
    for f in TEMP_DIR.glob("*"):
        if f.is_file():
            try:
                if (now - f.stat().st_mtime) > max_age_sec:
                    f.unlink()
            except Exception:
                pass


def download_clip_segment(
    url: str,
    video_id: str,
    start_time: float,
    end_time: float,
    output_path: Path,
    progress_cb: Optional[Callable[[str, int], None]] = None,
) -> Tuple[Path, float]:
    """
    Downloads ONLY the requested segment using stream-copy (-c copy, NO CPU re-encode!)
    and reuses pre-warmed stream metadata when available for near-instant speed.
    """
    if progress_cb:
        progress_cb("Fetching video clip...", 20)

    # Pad 1.5s before start so stream-copy hits an earlier keyframe cleanly;
    # our single GPU FFmpeg pass trims the exact leading_pad accurately.
    pad_start = max(0.0, start_time - 1.5)
    pad_end = end_time + 1.0
    leading_pad = start_time - pad_start

    # Attempt 0: Instant pre-warmed cached metadata (skips webpage + JS solve!)
    cached = STREAM_INFO_CACHE.get(video_id)
    if cached and (time.time() - cached["ts"] < STREAM_CACHE_TTL_SEC):
        ydl_opts = dict(YTDLP_BASE_OPTS)
        ydl_opts.update(cached["strat_opts"])
        ydl_opts.update({
            "download_ranges": download_range_func(None, [(pad_start, pad_end)]),
            "force_keyframes_at_cuts": False,  # Stream copy (-c copy) -> instant!
            "outtmpl": str(output_path.with_suffix(".%(ext)s")),
            "merge_output_format": "mp4",
        })
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.process_ie_result(copy.deepcopy(cached["info"]), download=True)
            candidates = list(output_path.parent.glob(f"{output_path.stem}.*"))
            for cand in candidates:
                if cand.suffix.lower() in [".mp4", ".mkv", ".webm"] and cand.stat().st_size > 1024:
                    return cand, leading_pad
        except Exception:
            # Cache expired or failed; drop cache and fall through to live waterfall
            STREAM_INFO_CACHE.pop(video_id, None)

    # Live multi-tier waterfall fallback
    last_error = None
    for idx, (label, strat_opts) in enumerate(_build_stream_strategies(), 1):
        if idx > 1 and progress_cb:
            progress_cb(f"Switching to {label}...", 25 + idx * 5)

        for leftover in output_path.parent.glob(f"{output_path.stem}.*"):
            try:
                leftover.unlink()
            except Exception:
                pass

        ydl_opts = dict(YTDLP_BASE_OPTS)
        ydl_opts.update(strat_opts)
        ydl_opts.update({
            "download_ranges": download_range_func(None, [(pad_start, pad_end)]),
            "force_keyframes_at_cuts": False,  # Stream copy (-c copy) -> instant!
            "outtmpl": str(output_path.with_suffix(".%(ext)s")),
            "merge_output_format": "mp4",
        })

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                # Cache for subsequent clips from the same video
                if info:
                    STREAM_INFO_CACHE[video_id] = {
                        "info": info,
                        "strat_opts": strat_opts,
                        "label": label,
                        "ts": time.time(),
                    }

            candidates = list(output_path.parent.glob(f"{output_path.stem}.*"))
            for cand in candidates:
                if cand.suffix.lower() in [".mp4", ".mkv", ".webm"] and cand.stat().st_size > 1024:
                    return cand, leading_pad
        except Exception as e:
            last_error = e
            continue

    raise RuntimeError(f"Could not download video segment: {last_error}")


def _has_audio_stream(filepath: Path) -> bool:
    """Fast ffprobe check for audio stream presence."""
    try:
        res = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=codec_type", "-of", "csv=p=0",
                str(filepath),
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return "audio" in (res.stdout or "")
    except Exception:
        return True


def _choose_climax_teaser_offset(
    clip_start: float,
    clip_end: float,
    peak_time: Optional[float] = None,
    heatmap_points: Optional[list] = None,
    teaser_dur: float = 2.5,
) -> Optional[float]:
    """
    Choose the best relative start offset (inside [0, duration]) for the 2.5s
    Climax Teaser Hook. Returns None if clip is too short (< 7s).
    """
    duration = clip_end - clip_start
    if duration < 7.0:
        return None

    min_off = 2.5
    max_off = max(min_off, duration - teaser_dur - 0.3)

    chosen_abs = None
    # 1. Prefer explicit clip peak_time if it lies inside the clip after the opening 2.5s
    if peak_time is not None and (clip_start + min_off) <= peak_time <= (clip_end - 1.0):
        chosen_abs = peak_time
    # 2. Otherwise find the strongest YouTube heatmap spike inside [clip_start + 2.5, clip_end - 1.0]
    elif heatmap_points:
        best_val = -1.0
        for pt in heatmap_points:
            t = float(pt.get("time", -1.0))
            v = float(pt.get("intensity", 0.0))
            if (clip_start + min_off) <= t <= (clip_end - 1.0) and v > best_val:
                best_val = v
                chosen_abs = t

    if chosen_abs is not None:
        rel = (chosen_abs - clip_start) - (teaser_dur * 0.45)
    else:
        # Fallback: classic climax point ~68% into the clip
        rel = duration * 0.68

    return round(max(min_off, min(max_off, rel)), 3)


def render_short(
    input_path: Path,
    output_path: Path,
    aspect_ratio: str = "9:16",
    framing_mode: str = "blur",
    burn_subtitles: bool = True,
    video_id: Optional[str] = None,
    clip_start: float = 0.0,
    clip_end: float = 0.0,
    leading_pad: float = 0.0,
    subtitle_style: str = "viral_pop",
    subtitle_position: str = "bottom",
    resolution: str = "1080p",
    climax_hook: bool = True,
    peak_time: Optional[float] = None,
    heatmap_points: Optional[list] = None,
    transcript_cache: Optional[Any] = None,
    progress_cb: Optional[Callable[[str, int], None]] = None,
) -> Path:
    """
    Ultra-fast single-pass GPU-accelerated render:
    - Optional 2.5s Climax Teaser Hook prepended at 0:00 (with synced captions!)
    - Keeps foreground at crisp full 1080p/720p quality
    - Uses 1/4-scale blur pass (15x faster, visually identical creamy blur)
    - Uses NVIDIA NVENC / Intel QSV / AMD AMF when available (with CPU fallback)
    """
    res_table = ASPECT_RATIOS if resolution == "1080p" else ASPECT_RATIOS_720
    target_spec = res_table.get(aspect_ratio, res_table["9:16"])
    target_w = target_spec["width"]
    target_h = target_spec["height"]

    duration = max(0.5, clip_end - clip_start)
    teaser_dur = 2.5
    teaser_offset: Optional[float] = None
    if climax_hook:
        teaser_offset = _choose_climax_teaser_offset(
            clip_start=clip_start,
            clip_end=clip_end,
            peak_time=peak_time,
            heatmap_points=heatmap_points,
            teaser_dur=teaser_dur,
        )

    # 1. Generate fast YouTube ASS subtitles (synced to Climax Hook if active!)
    ass_file: Optional[Path] = None
    if burn_subtitles and video_id:
        if progress_cb:
            progress_cb("Adding captions...", 55)
        temp_ass = TEMP_DIR / f"sub_{uuid.uuid4().hex[:8]}.ass"
        ass_file = generate_ass_subtitles(
            video_id=video_id,
            start_time=clip_start,
            end_time=clip_end,
            output_path=temp_ass,
            video_width=target_w,
            video_height=target_h,
            style_preset=subtitle_style,
            position=subtitle_position,
            transcript_cache=transcript_cache,
            hook_teaser_offset=teaser_offset,
            hook_teaser_duration=teaser_dur if teaser_offset is not None else 0.0,
        )

    encoder = detect_best_encoder()
    if progress_cb:
        gpu_label = "GPU" if encoder["name"] != "libx264" else "CPU"
        hook_msg = " + Climax Hook" if teaser_offset is not None else ""
        progress_cb(f"Creating Short ({gpu_label}{hook_msg})...", 72)

    # 2. Build ultra-fast, high-quality filter graph
    has_audio = _has_audio_stream(input_path)
    prefix_filter = ""
    v_in_label = "[0:v]"
    audio_map = "0:a?"

    if teaser_offset is not None:
        if has_audio:
            fade_out_st = max(0.0, teaser_dur - 0.05)
            prefix_filter = (
                f"[0:v]split=2[vh_in][vm_in];"
                f"[vh_in]trim=start={teaser_offset:.3f}:duration={teaser_dur:.3f},setpts=PTS-STARTPTS[v_hook];"
                f"[vm_in]trim=start=0:duration={duration:.3f},setpts=PTS-STARTPTS[v_main];"
                f"[0:a]asplit=2[ah_in][am_in];"
                f"[ah_in]atrim=start={teaser_offset:.3f}:duration={teaser_dur:.3f},asetpts=PTS-STARTPTS,"
                f"afade=t=out:st={fade_out_st:.3f}:d=0.05[a_hook];"
                f"[am_in]atrim=start=0:duration={duration:.3f},asetpts=PTS-STARTPTS,"
                f"afade=t=in:st=0:d=0.05[a_main];"
                f"[v_hook][a_hook][v_main][a_main]concat=n=2:v=1:a=1[v_src][outa];"
            )
            audio_map = "[outa]"
        else:
            prefix_filter = (
                f"[0:v]split=2[vh_in][vm_in];"
                f"[vh_in]trim=start={teaser_offset:.3f}:duration={teaser_dur:.3f},setpts=PTS-STARTPTS[v_hook];"
                f"[vm_in]trim=start=0:duration={duration:.3f},setpts=PTS-STARTPTS[v_main];"
                f"[v_hook][v_main]concat=n=2:v=1:a=0[v_src];"
            )
        v_in_label = "[v_src]"

    ass_clause = f",ass={ass_file.name}" if ass_file and ass_file.exists() else ""

    if aspect_ratio == "16:9" and framing_mode != "blur":
        body_filter = (
            f"{v_in_label}scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
            f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2{ass_clause}[outv]"
        )
    elif framing_mode == "crop":
        body_filter = (
            f"{v_in_label}scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
            f"crop={target_w}:{target_h}{ass_clause}[outv]"
        )
    elif framing_mode == "fit":
        body_filter = (
            f"{v_in_label}scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
            f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:black{ass_clause}[outv]"
        )
    else:
        bw = max(64, (target_w // 4) // 2 * 2)
        bh = max(64, (target_h // 4) // 2 * 2)
        body_filter = (
            f"{v_in_label}split=2[bg_in][fg_in];"
            f"[bg_in]scale={bw}:{bh}:force_original_aspect_ratio=increase,"
            f"crop={bw}:{bh},boxblur=8:2,eq=brightness=-0.15,"
            f"scale={target_w}:{target_h}:flags=bilinear[bg];"
            f"[fg_in]scale={target_w}:{target_h}:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2{ass_clause}[outv]"
        )

    filter_complex = prefix_filter + body_filter
    working_dir = str(TEMP_DIR.resolve())

    def _run_ffmpeg(use_hw_in: bool, enc_args: list) -> subprocess.CompletedProcess:
        cmd = ["ffmpeg", "-y"]
        if use_hw_in and encoder["hw_in"]:
            cmd.extend(encoder["hw_in"])
        if leading_pad > 0.01:
            cmd.extend(["-ss", f"{leading_pad:.3f}"])
        cmd.extend(["-i", str(input_path)])
        # Only clamp outer -t when climax hook isn't concatenating teaser + main
        if teaser_offset is None:
            cmd.extend(["-t", f"{duration:.3f}"])
        cmd.extend([
            "-filter_complex", filter_complex,
            "-map", "[outv]",
            "-map", audio_map,
            *enc_args,
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "160k",
            "-movflags", "+faststart",
            str(output_path),
        ])
        return subprocess.run(cmd, cwd=working_dir, capture_output=True, text=True)

    # Try GPU decode+encode -> retry GPU encode with CPU decode (e.g. AV1/VP9 input) -> CPU fallback
    result = _run_ffmpeg(use_hw_in=True, enc_args=encoder["enc_args"])
    if result.returncode != 0 and encoder["hw_in"]:
        result = _run_ffmpeg(use_hw_in=False, enc_args=encoder["enc_args"])
    if result.returncode != 0 and encoder["name"] != "libx264":
        result = _run_ffmpeg(
            use_hw_in=False,
            enc_args=["-c:v", "libx264", "-preset", "superfast", "-crf", "19"],
        )

    if ass_file and ass_file.exists():
        try:
            ass_file.unlink()
        except Exception:
            pass

    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg render error: {result.stderr[-500:]}")

    if progress_cb:
        progress_cb("Done!", 100)

    return output_path


def process_clip_pipeline(
    url: str,
    video_id: str,
    clip_id: str,
    start_time: float,
    end_time: float,
    aspect_ratio: str = "9:16",
    framing_mode: str = "blur",
    burn_subtitles: bool = True,
    subtitle_style: str = "viral_pop",
    subtitle_position: str = "bottom",
    resolution: str = "1080p",
    climax_hook: bool = True,
    peak_time: Optional[float] = None,
    heatmap_points: Optional[list] = None,
    transcript_cache: Optional[Any] = None,
    progress_cb: Optional[Callable[[str, int], None]] = None,
) -> Dict[str, Any]:
    """
    End-to-end ultra-fast Short creation pipeline.
    Outputs to temporary download directory (no persistent Saved Shorts clutter).
    """
    cleanup_old_temp_files()

    uid = uuid.uuid4().hex[:8]
    raw_temp_path = TEMP_DIR / f"raw_{video_id}_{uid}"
    final_filename = f"Short_{video_id}_{clip_id}_{aspect_ratio.replace(':', 'x')}_{uid}.mp4"
    final_output_path = TEMP_DIR / final_filename

    raw_downloaded = None
    try:
        raw_downloaded, leading_pad = download_clip_segment(
            url=url,
            video_id=video_id,
            start_time=start_time,
            end_time=end_time,
            output_path=raw_temp_path,
            progress_cb=progress_cb,
        )

        render_short(
            input_path=raw_downloaded,
            output_path=final_output_path,
            aspect_ratio=aspect_ratio,
            framing_mode=framing_mode,
            burn_subtitles=burn_subtitles,
            video_id=video_id,
            clip_start=start_time,
            clip_end=end_time,
            leading_pad=leading_pad,
            subtitle_style=subtitle_style,
            subtitle_position=subtitle_position,
            resolution=resolution,
            climax_hook=climax_hook,
            peak_time=peak_time,
            heatmap_points=heatmap_points,
            transcript_cache=transcript_cache,
            progress_cb=progress_cb,
        )

        file_size_mb = round(final_output_path.stat().st_size / (1024 * 1024), 2)
        base_dur = round(end_time - start_time, 2)
        hook_applied = bool(climax_hook and base_dur >= 7.0)
        final_dur = round(base_dur + (2.5 if hook_applied else 0.0), 2)

        return {
            "success": True,
            "filename": final_filename,
            "file_size_mb": file_size_mb,
            "download_url": f"/api/download/{final_filename}",
            "aspect_ratio": aspect_ratio,
            "duration": final_dur,
            "climax_hook": hook_applied,
        }
    finally:
        if raw_downloaded and raw_downloaded.exists():
            try:
                raw_downloaded.unlink()
            except Exception:
                pass
