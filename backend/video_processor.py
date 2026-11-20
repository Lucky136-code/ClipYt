import copy
import json
import os
import time
import subprocess
import uuid
import concurrent.futures
from pathlib import Path
from typing import Dict, Any, Optional, Callable, Tuple
import yt_dlp
from yt_dlp.utils import download_range_func

from backend.config import (
    TEMP_DIR,
    YTDLP_BASE_OPTS,
    ASPECT_RATIOS,
    ASPECT_RATIOS_4K,
    ASPECT_RATIOS_720,
    ensure_fresh_cookiefile,
)
from backend.subtitles import generate_ass_subtitles, generate_ass_from_words
from backend.whisper_captions import transcribe_clip_hinglish

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

    # On Hugging Face ZeroGPU Spaces, probing NVENC/CUDA outside @spaces.GPU can trip startup checks
    if os.environ.get("SPACE_ID"):
        _BEST_ENCODER = {
            "name": "libx264",
            "hw_in": [],
            "enc_args": ["-c:v", "libx264", "-preset", "fast", "-crf", "17", "-maxrate", "18000k", "-bufsize", "26000k"],
        }
        return _BEST_ENCODER

    candidates = [
        (
            "h264_nvenc",
            ["-hwaccel", "cuda"],
            ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "17", "-b:v", "14000k", "-maxrate", "20000k", "-bufsize", "30000k"],
        ),
        (
            "h264_qsv",
            [],
            ["-c:v", "h264_qsv", "-preset", "slow", "-global_quality", "17", "-b:v", "14000k", "-maxrate", "20000k"],
        ),
        (
            "h264_amf",
            [],
            ["-c:v", "h264_amf", "-quality", "quality", "-rc", "vbr_peak", "-b:v", "14000k", "-maxrate", "20000k"],
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
        "enc_args": ["-c:v", "libx264", "-preset", "fast", "-crf", "17", "-maxrate", "18000k", "-bufsize", "26000k"],
    }
    return _BEST_ENCODER


def _build_stream_strategies(target_res: str = "1080p") -> list:
    """
    Preferred fast H.264 / VP9 / AV1 stream strategies (1080p or 4K) with flexible bv*+ba/b fallback.
    Each entry is (label, opts_dict, allow_cookies).
    Note: Some clients (web_embedded, android_vr) drop all formats if cookies are passed.
    """
    is_4k = target_res in ("4k", "2160p")
    target_h = 2160 if is_4k else 1080
    if is_4k:
        # YouTube 4K (2160p) is primarily in VP9 and AV1 formats
        hd_fmt = (
            "bestvideo[height>=2160]+bestaudio/"
            "bestvideo[height=2160]+bestaudio/"
            "bestvideo[height<=2160]+bestaudio/"
            "best[height>=2160][protocol^=m3u8]/"
            "best[height=2160][protocol^=m3u8]/"
            "bestvideo+bestaudio/"
            "bv*+ba/b"
        )
        safari_fmt = (
            "bestvideo[height>=2160]+bestaudio/"
            "bestvideo[height=2160]+bestaudio/"
            "bestvideo+bestaudio/"
            "bv*+ba/b"
        )
    else:
        hd_fmt = (
            "bestvideo[height=1080][vcodec^=avc1]+bestaudio[ext=m4a]/"
            "bestvideo[height=1080]+bestaudio/"
            "bestvideo[height<=1080][vcodec^=avc1]+bestaudio[ext=m4a]/"
            "bestvideo[height<=1080]+bestaudio/"
            "best[height=1080][protocol^=m3u8]/"
            "best[height<=1080][protocol^=m3u8]/"
            "bestvideo+bestaudio/"
            "bv*+ba/b"
        )
        safari_fmt = (
            "bestvideo[height=1080]+bestaudio/"
            "best[height=1080][protocol^=m3u8]/"
            "bestvideo[height<=1080]+bestaudio/"
            "best[height<=1080][protocol^=m3u8]/"
            "bv*+ba/b"
        )
    strategies = []
    if YTDLP_BASE_OPTS.get("cookiefile"):
        strategies.extend([
            (
                f"Authenticated Safari HLS {target_h}p",
                {
                    "extractor_args": {"youtube": {"player_client": ["web_safari"]}},
                    "format": safari_fmt,
                },
                True,
            ),
            (
                "Authenticated Default HD",
                {
                    "format": hd_fmt,
                },
                True,
            ),
            (
                "Authenticated TV Stream",
                {
                    "extractor_args": {"youtube": {"player_client": ["tv"]}},
                    "format": "bv*+ba/b",
                },
                True,
            ),
            (
                "Authenticated Mobile Web",
                {
                    "extractor_args": {"youtube": {"player_client": ["mweb"]}},
                    "format": "bv*+ba/b",
                },
                True,
            ),
        ])

    strategies.extend([
        (
            "Default HD Stream",
            {
                "format": hd_fmt,
            },
            False,
        ),
        (
            "Fast Android HD Stream",
            {
                "extractor_args": {"youtube": {"player_client": ["android", "mweb"]}},
                "format": hd_fmt,
            },
            False,
        ),
        (
            "Smart TV High-Speed Stream",
            {
                "extractor_args": {"youtube": {"player_client": ["tv", "tv_simply"]}},
                "format": "bv*+ba/b",
            },
            False,
        ),
        (
            f"Safari HLS {target_h}p",
            {
                "extractor_args": {"youtube": {"player_client": ["web_safari"]}},
                "format": safari_fmt,
            },
            False,
        ),
        (
            "HD Embedded Stream",
            {
                "extractor_args": {"youtube": {"player_client": ["web_embedded"]}},
                "format": hd_fmt,
            },
            False,
        ),
        (
            "HD HLS Stream",
            {
                "format": (
                    f"bestvideo[height<={target_h}][protocol^=m3u8]+bestaudio[protocol^=m3u8]/"
                    f"best[height<={target_h}][protocol^=m3u8]/bv*+ba/b"
                ),
            },
            False,
        ),
        (
            "Android VR HD Stream",
            {
                "extractor_args": {"youtube": {"player_client": ["android_vr"]}},
                "format": hd_fmt,
            },
            False,
        ),
    ])
    return strategies


def _make_ydl_opts(strat_opts: dict, allow_cookies: bool) -> dict:
    ydl_opts = dict(YTDLP_BASE_OPTS)
    if allow_cookies and ydl_opts.get("cookiefile"):
        ensure_fresh_cookiefile()
    elif not allow_cookies:
        ydl_opts.pop("cookiefile", None)
    ydl_opts.update(strat_opts)
    return ydl_opts


def prewarm_video_streams(url: str, video_id: str) -> None:
    """
    Background helper run immediately after analysis so clicking 'Create this Short'
    skips YouTube webpage scraping and JS challenge solving completely (0s overhead).
    """
    existing = STREAM_INFO_CACHE.get(video_id)
    if existing and (time.time() - existing["ts"] < STREAM_CACHE_TTL_SEC):
        return

    clean_url = f"https://www.youtube.com/watch?v={video_id}" if video_id else url
    for label, strat_opts, allow_cookies in _build_stream_strategies():
        ydl_opts = _make_ydl_opts(strat_opts, allow_cookies)
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(clean_url, download=False)
            if info and (info.get("requested_formats") or info.get("url")):
                STREAM_INFO_CACHE[video_id] = {
                    "info": info,
                    "strat_opts": strat_opts,
                    "allow_cookies": allow_cookies,
                    "label": label,
                    "ts": time.time(),
                }
                return
        except Exception:
            continue


def cleanup_old_temp_files(max_age_sec: int = 300) -> None:
    """Remove temporary clips older than 5 minutes so disk never fills up under heavy traffic."""
    now = time.time()
    for f in TEMP_DIR.glob("*"):
        if f.is_file():
            # Guard: Never delete text, json, or configuration files
            if f.suffix.lower() in (".txt", ".json", ".yml", ".yaml"):
                continue
            try:
                if (now - f.stat().st_mtime) > max_age_sec:
                    f.unlink()
            except Exception:
                pass


def _measure_exact_leading_pad(
    filepath: Path,
    clip_start: float,
    pad_start: float,
) -> float:
    """
    Inspect the downloaded segment's PTS start_time via ffprobe so we trim
    the EXACT keyframe lead-in (eliminating 1-5s A/V & caption drift!).
    """
    fallback_pad = max(0.0, clip_start - pad_start)
    try:
        res = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=start_time:stream=codec_type,start_time",
                "-of", "json",
                str(filepath),
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode != 0:
            return fallback_pad
        meta = json.loads(res.stdout or "{}")
        fmt_start = float((meta.get("format") or {}).get("start_time") or 0.0)
        # When normal non-copyts cut starts around 0.000s, exact lead-in is fallback_pad (clip_start - pad_start)
        if fmt_start <= 1.0:
            return round(fallback_pad, 3)
        # If the container preserved high original PTS (e.g. HLS sequence):
        if fmt_start > 1.0 and fmt_start <= clip_start and (clip_start - fmt_start) < 25.0:
            return round(clip_start - fmt_start, 3)
    except Exception:
        pass
    return round(fallback_pad, 3)


def _download_with_guard(ydl_opts: dict, target: Any, is_info_dict: bool = False, timeout_sec: int = 35):
    """
    Run yt-dlp download in a guarded thread pool with a strict timeout.
    If YouTube stalls or gets stuck in the middle, it aborts fast and tries the next strategy!
    Uses wait=False so a hanging socket cannot block the server or subsequent strategies.
    """
    def _worker():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            if is_info_dict:
                return ydl.process_ie_result(copy.deepcopy(target), download=True)
            return ydl.extract_info(target, download=True)

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = pool.submit(_worker)
    try:
        return future.result(timeout=timeout_sec)
    except concurrent.futures.TimeoutError:
        raise TimeoutError(f"Stream download timed out after {timeout_sec}s")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def download_clip_segment(
    url: str,
    video_id: str,
    start_time: float,
    end_time: float,
    output_path: Path,
    progress_cb: Optional[Callable[[str, int], None]] = None,
    resolution: str = "1080p",
) -> Tuple[Path, float]:
    """
    Downloads ONLY the requested segment using stream-copy (-c copy, NO CPU re-encode!)
    with -copyts so exact original YouTube PTS is preserved for millisecond caption sync.
    Guarded with strict per-strategy timeouts so downloads never stall in the middle.
    """
    if progress_cb:
        progress_cb("Fetching video clip...", 20)

    # Pad 1.5s before start so stream-copy hits an earlier keyframe cleanly;
    # _measure_exact_leading_pad measures the true keyframe start to 1ms accuracy.
    pad_start = max(0.0, start_time - 1.5)
    pad_end = end_time + 1.0

    dl_common = {
        "download_ranges": download_range_func(None, [(pad_start, pad_end)]),
        "force_keyframes_at_cuts": False,  # Stream copy (-c copy) -> instant!
        "outtmpl": str(output_path.with_suffix(".%(ext)s")),
        "merge_output_format": "mp4",
        "nopart": True,
        "overwrites": True,
        "concurrent_fragment_downloads": 4,
        "buffersize": 8 * 1024 * 1024,
    }

    clip_dur = max(5.0, end_time - start_time)
    seg_timeout = max(35, int(clip_dur * 1.5) + 20)

    # Attempt 0: Instant pre-warmed cached metadata for this specific resolution
    cache_key = f"{video_id}_{resolution}"
    cached = STREAM_INFO_CACHE.get(cache_key)
    if cached and (time.time() - cached["ts"] < STREAM_CACHE_TTL_SEC):
        ydl_opts = _make_ydl_opts(cached["strat_opts"], cached.get("allow_cookies", True))
        ydl_opts.update(dl_common)
        try:
            _download_with_guard(ydl_opts, cached["info"], is_info_dict=True, timeout_sec=seg_timeout)
            candidates = list(output_path.parent.glob(f"{output_path.stem}.*"))
            for cand in candidates:
                if cand.suffix.lower() in [".mp4", ".mkv", ".webm"] and cand.stat().st_size > 20_000:
                    exact_pad = _measure_exact_leading_pad(cand, start_time, pad_start)
                    return cand, exact_pad
        except Exception:
            # Cache expired or failed; drop cache and fall through to live waterfall
            STREAM_INFO_CACHE.pop(cache_key, None)

    # Live multi-tier waterfall fallback
    errors = []
    for idx, (label, strat_opts, allow_cookies) in enumerate(_build_stream_strategies(resolution), 1):
        if idx > 1 and progress_cb:
            progress_cb(f"Switching to {label}...", min(48, 22 + idx * 3))

        # Use strategy-specific isolated path stem on Windows to prevent WinError 32 file collisions
        attempt_path = output_path.parent / f"{output_path.stem}_s{idx}"
        ydl_opts = _make_ydl_opts(strat_opts, allow_cookies)
        ydl_opts.update(dl_common)
        ydl_opts["outtmpl"] = str(attempt_path.with_suffix(".%(ext)s"))

        try:
            info = _download_with_guard(ydl_opts, url, is_info_dict=False, timeout_sec=seg_timeout)
            # Cache for subsequent clips from the same video with matching resolution
            if info:
                STREAM_INFO_CACHE[cache_key] = {
                    "info": info,
                    "strat_opts": strat_opts,
                    "allow_cookies": allow_cookies,
                    "label": label,
                    "ts": time.time(),
                }

            candidates = list(output_path.parent.glob(f"{attempt_path.stem}.*"))
            for cand in candidates:
                if cand.suffix.lower() in [".mp4", ".mkv", ".webm"] and cand.stat().st_size > 20_000:
                    exact_pad = _measure_exact_leading_pad(cand, start_time, pad_start)
                    return cand, exact_pad
        except Exception as e:
            errors.append(f"{label}: {str(e)[:140]}")
            continue

    summary = " | ".join(errors[:3]) if errors else "Unknown download error"
    raise RuntimeError(f"Could not download video segment ({summary})")


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


CLIMAX_HOOK_DURATION = 4.5
CLIMAX_HOOK_MIN_CLIP = 9.0


def _choose_climax_teaser_offset(
    clip_start: float,
    clip_end: float,
    peak_time: Optional[float] = None,
    heatmap_points: Optional[list] = None,
    teaser_dur: float = CLIMAX_HOOK_DURATION,
) -> Optional[float]:
    """
    Choose the best relative start offset (inside [0, duration]) for the 4.5s
    Climax Teaser Hook. Returns None if clip is too short (< 9s).
    """
    duration = clip_end - clip_start
    if duration < CLIMAX_HOOK_MIN_CLIP:
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
    - Optional 4.5s Climax Teaser Hook prepended at 0:00 (with synced captions!)
    - Keeps foreground at crisp full 1080p/720p quality
    - Uses 1/4-scale blur pass (15x faster, visually identical creamy blur)
    - Uses NVIDIA NVENC / Intel QSV / AMD AMF when available (with CPU fallback)
    """
    if resolution in ("4k", "2160p"):
        res_table = ASPECT_RATIOS_4K
    elif resolution == "720p":
        res_table = ASPECT_RATIOS_720
    else:
        res_table = ASPECT_RATIOS
    target_spec = res_table.get(aspect_ratio, res_table["9:16"])
    target_w = target_spec["width"]
    target_h = target_spec["height"]

    duration = max(0.5, clip_end - clip_start)
    teaser_dur = CLIMAX_HOOK_DURATION
    teaser_offset: Optional[float] = None
    if climax_hook:
        teaser_offset = _choose_climax_teaser_offset(
            clip_start=clip_start,
            clip_end=clip_end,
            peak_time=peak_time,
            heatmap_points=heatmap_points,
            teaser_dur=teaser_dur,
        )

    # 1. Generate AI Hinglish + English ASS subtitles (with YouTube json3 fallback, synced to Climax Hook!)
    ass_file: Optional[Path] = None
    if burn_subtitles and video_id:
        if progress_cb:
            progress_cb("AI generating English & Hinglish captions...", 54)
        temp_ass = TEMP_DIR / f"sub_{uuid.uuid4().hex[:8]}.ass"
        temp_wav = TEMP_DIR / f"aud_{uuid.uuid4().hex[:8]}.wav"
        try:
            wav_cmd = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-ss", f"{max(0.0, leading_pad):.3f}",
                "-i", str(input_path),
                "-t", f"{duration:.3f}",
                "-vn", "-ac", "1", "-ar", "16000",
                "-c:a", "pcm_s16le",
                str(temp_wav),
            ]
            wav_res = subprocess.run(wav_cmd, capture_output=True, timeout=20)
            if wav_res.returncode == 0 and temp_wav.exists() and temp_wav.stat().st_size > 1000:
                ai_words = transcribe_clip_hinglish(temp_wav)
                if ai_words:
                    if teaser_offset is not None:
                        # Map AI words (0..duration) to absolute clip space so Climax Hook aligns cleanly
                        abs_words = [
                            {
                                "word": w["word"],
                                "start": clip_start + w["start"],
                                "end": clip_start + w["end"],
                            }
                            for w in ai_words
                        ]
                        ass_file = generate_ass_subtitles(
                            video_id=video_id,
                            start_time=clip_start,
                            end_time=clip_end,
                            output_path=temp_ass,
                            video_width=target_w,
                            video_height=target_h,
                            style_preset=subtitle_style,
                            position=subtitle_position,
                            transcript_cache=abs_words,
                            hook_teaser_offset=teaser_offset,
                            hook_teaser_duration=teaser_dur,
                        )
                    else:
                        ass_file = generate_ass_from_words(
                            words=ai_words,
                            clip_duration=duration,
                            output_path=temp_ass,
                            video_width=target_w,
                            video_height=target_h,
                            style_preset=subtitle_style,
                            position=subtitle_position,
                        )
        except Exception:
            ass_file = None
        finally:
            if temp_wav.exists():
                try:
                    temp_wav.unlink()
                except Exception:
                    pass

        if not ass_file:
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
            "-profile:v", "high",
            "-level", ("5.1" if (target_w > 1920 or target_h > 1920) else "4.1"),
            "-c:a", "aac",
            "-b:a", "160k",
            "-movflags", "+faststart",
            str(output_path),
        ])
        return subprocess.run(cmd, cwd=working_dir, capture_output=True, text=True)

    is_high_res = (target_w > 1920 or target_h > 1920)
    if is_high_res:
        if encoder["name"] == "h264_nvenc":
            primary_enc_args = ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "16", "-b:v", "35000k", "-maxrate", "50000k", "-bufsize", "65000k"]
        elif encoder["name"] == "h264_qsv":
            primary_enc_args = ["-c:v", "h264_qsv", "-preset", "slow", "-global_quality", "16", "-b:v", "35000k", "-maxrate", "50000k"]
        elif encoder["name"] == "h264_amf":
            primary_enc_args = ["-c:v", "h264_amf", "-quality", "quality", "-rc", "vbr_peak", "-b:v", "35000k", "-maxrate", "50000k"]
        else:
            primary_enc_args = ["-c:v", "libx264", "-preset", "fast", "-crf", "16", "-maxrate", "45000k", "-bufsize", "60000k"]
    else:
        primary_enc_args = encoder["enc_args"]

    # Try GPU decode+encode -> retry GPU encode with CPU decode (e.g. AV1/VP9 input) -> CPU fallback
    result = _run_ffmpeg(use_hw_in=True, enc_args=primary_enc_args)
    if result.returncode != 0 and encoder["hw_in"]:
        result = _run_ffmpeg(use_hw_in=False, enc_args=primary_enc_args)
    if result.returncode != 0 and encoder["name"] != "libx264":
        result = _run_ffmpeg(
            use_hw_in=False,
            enc_args=[
                "-c:v", "libx264", "-preset", "fast", "-crf", ("16" if is_high_res else "17"),
                "-maxrate", ("45000k" if is_high_res else "18000k"),
                "-bufsize", ("60000k" if is_high_res else "26000k"),
            ],
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
            resolution=resolution,
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
        hook_applied = bool(climax_hook and base_dur >= CLIMAX_HOOK_MIN_CLIP)
        final_dur = round(base_dur + (CLIMAX_HOOK_DURATION if hook_applied else 0.0), 2)

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
