import base64
import json
import math
import re
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests
import yt_dlp

from backend.analyzer import extract_video_id, format_timestamp
from backend.config import TEMP_DIR, YTDLP_BASE_OPTS, ensure_fresh_cookiefile
from backend.subtitles import generate_ass_from_words, generate_ass_subtitles
from backend.video_processor import (
    _make_ydl_opts,
    cleanup_old_temp_files,
    detect_best_encoder,
    download_clip_segment,
)
from backend.whisper_captions import transcribe_clip_hinglish, get_whisper_status

# Cache inspected Pro sources for 15 minutes
PRO_STREAM_CACHE: Dict[str, Dict[str, Any]] = {}
PRO_CACHE_TTL_SEC = 900


def _build_pro_live_strategies() -> List[Tuple[str, Dict[str, Any], bool]]:
    """yt-dlp strategies tailored for both YouTube Live HLS streams (96=1080p m3u8) and VODs."""
    live_fmt = (
        "best[height<=1080][protocol^=m3u8]/"
        "bestvideo[height<=1080][protocol^=m3u8]+bestaudio[protocol^=m3u8]/"
        "best[height<=1080]/bv*+ba/b"
    )
    strategies: List[Tuple[str, Dict[str, Any], bool]] = []
    if YTDLP_BASE_OPTS.get("cookiefile"):
        strategies.extend([
            (
                "Auth Default Live HLS",
                {"format": live_fmt},
                True,
            ),
            (
                "Auth Safari HLS",
                {
                    "extractor_args": {"youtube": {"player_client": ["web_safari"]}},
                    "format": live_fmt,
                },
                True,
            ),
            (
                "Auth TV Live",
                {
                    "extractor_args": {"youtube": {"player_client": ["tv"]}},
                    "format": "best[height<=1080]/bv*+ba/b",
                },
                True,
            ),
        ])

    strategies.extend([
        (
            "Default Live HLS",
            {"format": live_fmt},
            False,
        ),
        (
            "Android VR Live HLS",
            {
                "extractor_args": {"youtube": {"player_client": ["android_vr"]}},
                "format": live_fmt,
            },
            False,
        ),
        (
            "TV Simply Live HLS",
            {
                "extractor_args": {"youtube": {"player_client": ["tv_simply"]}},
                "format": live_fmt,
            },
            False,
        ),
        (
            "Safari Live HLS",
            {
                "extractor_args": {"youtube": {"player_client": ["web_safari"]}},
                "format": live_fmt,
            },
            False,
        ),
    ])
    return strategies


def _extract_pro_info(url: str, video_id: Optional[str]) -> Dict[str, Any]:
    """Extract live/VOD stream info using our cookie-aware multi-client waterfall."""
    clean_url = f"https://www.youtube.com/watch?v={video_id}" if video_id else url
    last_err = ""
    for label, strat_opts, allow_cookies in _build_pro_live_strategies():
        ydl_opts = _make_ydl_opts(strat_opts, allow_cookies)
        ydl_opts["ignore_no_formats_error"] = True
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(clean_url, download=False)
            if info and (info.get("url") or info.get("requested_formats") or (info.get("formats") and len(info["formats"]) > 0)):
                return info
        except Exception as e:
            last_err = f"{label}: {str(e)[:180]}"
            continue
    raise RuntimeError(f"Could not load YouTube stream ({last_err or 'no playable formats found'}).")


def _extract_media_urls(info: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Dict[str, str]]:
    """
    Return (video_url, audio_url_or_None, http_headers) from yt-dlp info dict.
    For muxed HLS/MP4 streams, audio_url is None (video_url already contains audio).
    """
    headers: Dict[str, str] = {}
    req_fmts = info.get("requested_formats") or []
    if len(req_fmts) >= 2:
        v_url = req_fmts[0].get("url") or req_fmts[0].get("manifest_url")
        a_url = req_fmts[1].get("url") or req_fmts[1].get("manifest_url")
        headers = dict(req_fmts[0].get("http_headers") or info.get("http_headers") or {})
        return v_url, a_url, headers

    if info.get("url"):
        headers = dict(info.get("http_headers") or {})
        return info["url"], None, headers

    # Pick best <=1080p muxed or video stream from formats list
    fmts = info.get("formats") or []
    muxed = [
        f for f in fmts
        if f.get("url")
        and f.get("vcodec") not in (None, "none")
        and f.get("acodec") not in (None, "none")
        and (f.get("height") or 0) <= 1080
    ]
    if muxed:
        muxed.sort(key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
        best = muxed[-1]
        return best["url"], None, dict(best.get("http_headers") or {})

    vids = [
        f for f in fmts
        if f.get("url")
        and f.get("vcodec") not in (None, "none")
        and (f.get("height") or 0) <= 1080
    ]
    auds = [
        f for f in fmts
        if f.get("url")
        and f.get("acodec") not in (None, "none")
        and f.get("vcodec") in (None, "none")
    ]
    if vids:
        vids.sort(key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
        best_v = vids[-1]
        best_a = sorted(auds, key=lambda f: f.get("abr") or f.get("tbr") or 0)[-1] if auds else None
        return best_v["url"], (best_a["url"] if best_a else None), dict(best_v.get("http_headers") or {})

    return None, None, headers


def _pick_preview_hls_url(info: Dict[str, Any]) -> Tuple[Optional[str], Dict[str, str]]:
    """
    Pick a crisp, lightweight muxed live HLS playlist (preferring 720p itag 95 or 480p itag 94)
    for instant, smooth browser playback & scrubbing via hls.js.
    """
    fmts = info.get("formats") or []
    muxed_hls = [
        f for f in fmts
        if f.get("url")
        and (f.get("protocol") or "").startswith("m3u8")
        and f.get("vcodec") not in (None, "none")
        and f.get("acodec") not in (None, "none")
    ]
    if muxed_hls:
        # Prefer 720p (smooth + crisp), fallback to 480p, then highest <=1080p
        for target_h in (720, 480, 360, 1080):
            for f in muxed_hls:
                if int(f.get("height") or 0) == target_h:
                    return f["url"], dict(f.get("http_headers") or info.get("http_headers") or {})
        muxed_hls.sort(key=lambda f: int(f.get("height") or 0))
        best = muxed_hls[-1]
        return best["url"], dict(best.get("http_headers") or info.get("http_headers") or {})

    v_url, _a_url, headers = _extract_media_urls(info)
    return v_url, headers


def _parse_pdt_timestamp(m3u8_text: str) -> Optional[float]:
    """Parse #EXT-X-PROGRAM-DATE-TIME from a YouTube HLS playlist into UTC Unix epoch seconds."""
    m = re.search(r"#EXT-X-PROGRAM-DATE-TIME:([^\r\n]+)", m3u8_text)
    if not m:
        return None
    try:
        dt = datetime.fromisoformat(m.group(1).strip().replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return None


def _parse_hls_segments(
    m3u8_url: str,
    headers: Optional[Dict[str, str]] = None,
) -> Tuple[List[Tuple[float, str]], float, Optional[float]]:
    """
    Download a live HLS media playlist (.m3u8) and parse every (duration_sec, segment_url)
    in chronological order plus its #EXT-X-PROGRAM-DATE-TIME UTC epoch start.
    Returns (segments_list, total_duration_sec, first_pdt_utc_ts_or_None).
    """
    req_headers = {}
    if headers and headers.get("User-Agent"):
        req_headers["User-Agent"] = headers["User-Agent"]

    try:
        resp = requests.get(m3u8_url, headers=req_headers, timeout=8)
        if resp.status_code != 200:
            return [], 0.0, None
        text = resp.text
        base_url = resp.url or m3u8_url

        if "#EXT-X-STREAM-INF" in text:
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            best_sub = None
            best_bw = -1
            for i, ln in enumerate(lines):
                if ln.startswith("#EXT-X-STREAM-INF") and i + 1 < len(lines) and not lines[i + 1].startswith("#"):
                    res_m = re.search(r"RESOLUTION=\d+x(\d+)", ln)
                    h = int(res_m.group(1)) if res_m else 720
                    bw_m = re.search(r"BANDWIDTH=(\d+)", ln)
                    bw = int(bw_m.group(1)) if bw_m else h * 1000
                    if h <= 1080 and bw > best_bw:
                        best_bw = bw
                        best_sub = urljoin(base_url, lines[i + 1])
            if best_sub:
                resp = requests.get(best_sub, headers=req_headers, timeout=8)
                if resp.status_code != 200:
                    return [], 0.0, None
                text = resp.text
                base_url = resp.url or best_sub

        first_pdt = _parse_pdt_timestamp(text)
        segments: List[Tuple[float, str]] = []
        pending_dur: Optional[float] = None
        for raw_ln in text.splitlines():
            ln = raw_ln.strip()
            if not ln:
                continue
            if ln.startswith("#EXTINF:"):
                m = re.match(r"#EXTINF:([0-9.]+)", ln)
                if m:
                    pending_dur = float(m.group(1))
            elif not ln.startswith("#"):
                if pending_dur is not None:
                    segments.append((pending_dur, urljoin(base_url, ln)))
                    pending_dur = None

        total_dur = sum(d for d, _ in segments)
        return segments, total_dur, first_pdt
    except Exception:
        return [], 0.0, None


def _download_dvr_ts_slice(
    m3u8_url: str,
    rewind_sec: float,
    duration_sec: float,
    out_ts_path: Path,
    headers: Optional[Dict[str, str]] = None,
    start_utc_ts: Optional[float] = None,
    end_utc_ts: Optional[float] = None,
) -> Optional[Tuple[Path, float, float]]:
    """
    Directly download ONLY the exact historical `.ts` segments from YouTube's live HLS
    playlist using parallel HTTP fetches and concatenate them into a single local `.ts` file.
    - When `start_utc_ts` & `end_utc_ts` are supplied (from our live hls.js player!), matches
      directly against `#EXT-X-PROGRAM-DATE-TIME` for 0.000s-drift frame-accurate cuts!
    - Otherwise uses `[total_dur - rewind_sec - duration_sec .. total_dur - rewind_sec]`.
    """
    segments, total_dur, first_pdt = _parse_hls_segments(m3u8_url, headers)
    if not segments or total_dur < 2.0:
        return None

    # Exact UTC timestamp matching (zero drift, zero broadcast-lag offset!)
    if (
        start_utc_ts is not None
        and end_utc_ts is not None
        and end_utc_ts > start_utc_ts + 1.0
        and first_pdt is not None
    ):
        req_dur = max(3.0, min(125.0, float(end_utc_ts - start_utc_ts)))
        win_start = max(0.0, float(start_utc_ts) - first_pdt)
        win_end = min(total_dur, win_start + req_dur)
        if win_start >= total_dur - 1.0:
            win_end = total_dur
            win_start = max(0.0, total_dur - req_dur)
    else:
        eff_rewind = max(0.0, float(rewind_sec))
        win_end = max(min(total_dur, duration_sec), total_dur - eff_rewind)
        win_start = max(0.0, win_end - float(duration_sec))
        if win_end - win_start < 1.0:
            win_start = max(0.0, total_dur - float(duration_sec))
            win_end = total_dur

    selected: List[Tuple[float, str]] = []
    slice_start_t: Optional[float] = None
    cur_t = 0.0
    for seg_dur, seg_url in segments:
        seg_start = cur_t
        seg_end = cur_t + seg_dur
        if seg_end > win_start and seg_start < (win_end + 1.0):
            if slice_start_t is None:
                slice_start_t = seg_start
            selected.append((seg_dur, seg_url))
        cur_t = seg_end

    if not selected:
        selected = segments[-max(2, math.ceil(duration_sec / 5.0) + 1):]
        slice_start_t = max(0.0, total_dur - sum(d for d, _ in selected))

    req_headers = {}
    if headers and headers.get("User-Agent"):
        req_headers["User-Agent"] = headers["User-Agent"]

    def _fetch_one(url_str: str) -> Optional[bytes]:
        for _attempt in range(2):
            try:
                r = requests.get(url_str, headers=req_headers, timeout=15)
                if r.status_code == 200 and len(r.content) > 512:
                    return r.content
            except Exception:
                continue
        return None

    urls = [u for _d, u in selected]
    with ThreadPoolExecutor(max_workers=min(4, len(urls))) as pool:
        chunks = list(pool.map(_fetch_one, urls))

    valid_chunks = [c for c in chunks if c]
    if not valid_chunks:
        return None

    with open(out_ts_path, "wb") as f:
        for chunk in valid_chunks:
            f.write(chunk)

    sub_offset = max(0.0, round(win_start - (slice_start_t or 0.0), 3))
    slice_total = sum(d for d, _ in selected)
    return out_ts_path, sub_offset, max(1.0, slice_total - sub_offset)


def _build_local_dvr_vod_m3u8(
    m3u8_url: str,
    rewind_sec: float,
    duration_sec: float,
    out_m3u8_path: Path,
    headers: Optional[Dict[str, str]] = None,
) -> Optional[Tuple[Path, float, float]]:
    """
    Slice ONLY the exact historical DVR segments from YouTube's live HLS playlist
    for `[live_edge - rewind_sec - duration_sec .. live_edge - rewind_sec]` and write
    a finite local VOD `.m3u8` file (`#EXT-X-PLAYLIST-TYPE:VOD` + `#EXT-X-ENDLIST`).
    Makes it physically impossible for FFmpeg to jump to the live edge!
    Returns (out_m3u8_path, sub_offset_sec, actual_available_dur) or None.
    """
    segments, total_dur, _first_pdt = _parse_hls_segments(m3u8_url, headers)
    if not segments or total_dur < 2.0:
        return None

    # Compute exact requested window [win_start, win_end] inside [0, total_dur]
    eff_rewind = max(0.0, float(rewind_sec))
    win_end = max(min(total_dur, duration_sec), total_dur - eff_rewind)
    win_start = max(0.0, win_end - float(duration_sec))
    if win_end - win_start < 1.0:
        win_start = max(0.0, total_dur - float(duration_sec))
        win_end = total_dur

    # Select all segments overlapping [win_start, win_end] (plus 1 tiny trailing buffer seg for safety)
    selected: List[Tuple[float, str]] = []
    slice_start_t: Optional[float] = None
    cur_t = 0.0
    for idx, (seg_dur, seg_url) in enumerate(segments):
        seg_start = cur_t
        seg_end = cur_t + seg_dur
        if seg_end > win_start and seg_start < (win_end + 1.0):
            if slice_start_t is None:
                slice_start_t = seg_start
            selected.append((seg_dur, seg_url))
        cur_t = seg_end

    if not selected:
        selected = segments[-max(2, math.ceil(duration_sec / 5.0) + 1):]
        slice_start_t = max(0.0, total_dur - sum(d for d, _ in selected))

    max_seg_dur = max(1, int(math.ceil(max(d for d, _ in selected))))
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{max_seg_dur}",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD",
    ]
    for seg_dur, seg_url in selected:
        lines.append(f"#EXTINF:{seg_dur:.3f},")
        lines.append(seg_url)
    lines.append("#EXT-X-ENDLIST")

    out_m3u8_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    sub_offset = max(0.0, round(win_start - (slice_start_t or 0.0), 3))
    slice_total = sum(d for d, _ in selected)
    return out_m3u8_path, sub_offset, max(1.0, slice_total - sub_offset)


def _grab_frame_b64(
    video_url: Optional[str],
    is_live: bool,
    duration: float,
    thumbnail_url: str,
    headers: Optional[Dict[str, str]] = None,
    preview_offset_sec: Optional[float] = None,
) -> Tuple[str, str]:
    """
    Grab a crisp 16:9 JPEG frame from the live stream or VOD so the creator can
    position the Streamer Facecam box & Stream box with pixel accuracy.
    If `preview_offset_sec` is set:
      - For LIVE streams: grabs the exact historical `.ts` segment from `preview_offset_sec` ago!
      - For VODs: grabs the frame at exact timestamp `preview_offset_sec` seconds!
    Returns (data_uri_base64, source_kind).
    """
    if video_url:
        target_input = video_url
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
        if headers and headers.get("User-Agent"):
            cmd.extend(["-user_agent", headers["User-Agent"]])
        if not is_live:
            if preview_offset_sec is not None and preview_offset_sec >= 0:
                seek_sec = min(max(0.0, float(preview_offset_sec)), max(0.0, duration - 1.0)) if duration > 2 else float(preview_offset_sec)
            elif duration > 15:
                seek_sec = min(max(8.0, duration * 0.22), 90.0)
            else:
                seek_sec = 0.0
            if seek_sec > 0.1:
                cmd.extend(["-ss", f"{seek_sec:.2f}"])
        else:
            # Live stream: pick the EXACT single `.ts` segment URL from the HLS playlist!
            segments, total_dur, _first_pdt = _parse_hls_segments(video_url, headers)
            if segments:
                secs_back = float(preview_offset_sec) if (preview_offset_sec is not None and preview_offset_sec > 2.0) else 2.0
                target_t = max(0.0, total_dur - secs_back)
                acc = 0.0
                picked_url = segments[-1][1]
                for seg_dur, seg_url in segments:
                    if acc + seg_dur >= target_t:
                        picked_url = seg_url
                        break
                    acc += seg_dur
                target_input = picked_url
            else:
                cmd.extend(["-live_start_index", "-2"])

        cmd.extend([
            "-i", target_input,
            "-frames:v", "1",
            "-vf", "scale=960:-2:flags=lanczos",
            "-q:v", "3",
            "-f", "image2pipe",
            "-vcodec", "mjpeg",
            "pipe:1",
        ])
        try:
            res = subprocess.run(cmd, capture_output=True, timeout=14)
            if res.returncode == 0 and res.stdout and len(res.stdout) > 2000:
                b64 = base64.b64encode(res.stdout).decode("ascii")
                return f"data:image/jpeg;base64,{b64}", ("live_frame" if is_live else "video_frame")
        except Exception:
            pass

    # Fallback to crisp YouTube thumbnail (still 16:9)
    for thumb in [
        thumbnail_url,
        thumbnail_url.replace("hqdefault.jpg", "maxresdefault.jpg") if thumbnail_url else "",
    ]:
        if not thumb:
            continue
        try:
            resp = requests.get(thumb, timeout=8)
            if resp.status_code == 200 and len(resp.content) > 2000:
                b64 = base64.b64encode(resp.content).decode("ascii")
                return f"data:image/jpeg;base64,{b64}", "thumbnail"
        except Exception:
            continue

    return "", "none"


def _measure_hls_dvr_seconds(
    m3u8_url: Optional[str],
    headers: Optional[Dict[str, str]] = None,
) -> float:
    """Measure total available DVR seconds currently present in a live HLS playlist."""
    if not m3u8_url:
        return 0.0
    try:
        req_headers = {}
        if headers and headers.get("User-Agent"):
            req_headers["User-Agent"] = headers["User-Agent"]
        resp = requests.get(m3u8_url, headers=req_headers, timeout=6)
        if resp.status_code != 200:
            return 0.0
        extinfs = [float(x) for x in re.findall(r"#EXTINF:([0-9.]+)", resp.text)]
        return round(sum(extinfs), 1) if extinfs else 0.0
    except Exception:
        return 0.0


def inspect_pro_source(
    url: str,
    force_refresh: bool = False,
    preview_offset_sec: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Inspect a YouTube Live Stream or normal video for Pro Studio:
    - Supports watch?v=, /live/ID, youtu.be/ID, AND @Channel/live links!
    - Detects live vs VOD status + exact live elapsed clock (from stream start to ongoing!)
    - Grabs a fresh 16:9 frame (at live edge, DVR rewind offset, or exact VOD timestamp!)
    - Caches stream URLs so clicking 'Create Pro Short' is super fast
    """
    video_id = extract_video_id(url)
    is_channel_live = ("youtube.com/" in url.lower()) and ("/live" in url.lower())
    if not video_id and not is_channel_live:
        raise ValueError("Please paste a valid YouTube Live or video link.")

    cached = PRO_STREAM_CACHE.get(video_id) if video_id else None
    now = time.time()
    if cached and not force_refresh and (now - cached["ts"] < PRO_CACHE_TTL_SEC):
        info = cached["info"]
    else:
        info = _extract_pro_info(url, video_id)
        video_id = info.get("id") or video_id
        if not video_id:
            raise ValueError("Could not resolve a YouTube video ID from that link.")
        PRO_STREAM_CACHE[video_id] = {"info": info, "ts": now}

    is_live = bool(info.get("is_live")) or (info.get("live_status") == "is_live")
    duration = float(info.get("duration") or 0.0)
    title = info.get("title") or "YouTube Stream"
    author = info.get("uploader") or info.get("channel") or "Creator"
    thumbnail = info.get("thumbnail") or f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"

    v_url, _a_url, headers = _extract_media_urls(info)

    # For active live streams, compute stream elapsed duration (from 00:00 to ongoing now)
    live_elapsed_sec = 0.0
    dvr_window_sec = 0.0
    live_min_sec = 0.0
    if is_live:
        dvr_window_sec = _measure_hls_dvr_seconds(v_url, headers)
        rel_ts = info.get("release_timestamp") or info.get("timestamp")
        raw_elapsed = (now - float(rel_ts)) if (rel_ts and now > float(rel_ts)) else 0.0

        # If stream has been running for days (e.g. 24/7 radio) beyond YouTube's DVR window,
        # anchor the selectable timeline cleanly to [0 .. dvr_window_sec] so 00:00..LIVE matches YouTube's scrubber!
        if dvr_window_sec >= 60.0:
            if raw_elapsed > 0 and raw_elapsed <= (dvr_window_sec + 300.0):
                live_elapsed_sec = round(raw_elapsed, 1)
                live_min_sec = 0.0
            else:
                live_elapsed_sec = round(dvr_window_sec, 1)
                live_min_sec = 0.0
        elif raw_elapsed > 30.0 and raw_elapsed <= 43200.0:
            live_elapsed_sec = round(raw_elapsed, 1)
            dvr_window_sec = live_elapsed_sec
            live_min_sec = 0.0
        else:
            # Fallback if DVR metadata isn't exposed yet
            live_elapsed_sec = 300.0
            dvr_window_sec = 300.0
            live_min_sec = 0.0

    effective_total = live_elapsed_sec if is_live else duration
    use_hours = effective_total >= 3600

    frame_image, frame_source = _grab_frame_b64(
        video_url=v_url,
        is_live=is_live,
        duration=duration,
        thumbnail_url=thumbnail,
        headers=headers,
        preview_offset_sec=preview_offset_sec,
    )

    return {
        "video_id": video_id,
        "title": title,
        "author": author,
        "is_live": is_live,
        "live_status": info.get("live_status") or ("is_live" if is_live else "not_live"),
        "duration": round(duration, 2),
        "live_elapsed_sec": round(live_elapsed_sec, 1),
        "dvr_window_sec": round(dvr_window_sec, 1),
        "live_min_sec": round(live_min_sec, 1),
        "duration_str": (
            f"LIVE • {format_timestamp(live_elapsed_sec, force_hours=use_hours)}"
            if is_live
            else format_timestamp(duration, force_hours=use_hours)
        ),
        "use_hours": use_hours,
        "thumbnail": thumbnail,
        "frame_image": frame_image,
        "frame_source": frame_source,
    }


def _estimate_hls_live_start_index(
    m3u8_url: str,
    seconds_back: float,
    headers: Optional[Dict[str, str]] = None,
) -> Optional[int]:
    """
    Fetch an HLS media playlist and compute the exact negative `-live_start_index`
    needed to jump `seconds_back` seconds before the live edge (DVR instant clip!).
    """
    try:
        req_headers = {}
        if headers and headers.get("User-Agent"):
            req_headers["User-Agent"] = headers["User-Agent"]
        resp = requests.get(m3u8_url, headers=req_headers, timeout=8)
        if resp.status_code != 200:
            return None
        text = resp.text
        # Find all segment durations (#EXTINF:5.000,)
        extinfs = [float(x) for x in re.findall(r"#EXTINF:([0-9.]+)", text)]
        if not extinfs:
            return None
        total_segs = len(extinfs)
        acc = 0.0
        segs_needed = 0
        for dur in reversed(extinfs):
            acc += dur
            segs_needed += 1
            if acc >= seconds_back:
                break
        # Clamp inside available DVR playlist window
        segs_needed = max(2, min(total_segs, segs_needed + 1))
        return -segs_needed
    except Exception:
        return None


def download_live_dvr_segment(
    url: str,
    video_id: str,
    duration_sec: float,
    rewind_sec: float,
    output_path: Path,
    progress_cb: Optional[Callable[[str, int], None]] = None,
    start_utc_ts: Optional[float] = None,
    end_utc_ts: Optional[float] = None,
) -> Path:
    """
    Download an EXACT historical slice from an active YouTube Live Stream (via direct
    parallel `.ts` DVR chunk downloading matched to exact UTC `#EXT-X-PROGRAM-DATE-TIME`)
    so rewound recordings ALWAYS cut the exact recorded frame!
    """
    if progress_cb:
        progress_cb("Connecting to live stream DVR buffer...", 14)

    # Always extract fresh live manifest URL so segment tokens are valid
    info = _extract_pro_info(url, video_id)
    PRO_STREAM_CACHE[video_id] = {"info": info, "ts": time.time()}
    v_url, a_url, headers = _extract_media_urls(info)
    if not v_url:
        raise RuntimeError("Could not find a live HLS stream URL for this broadcast.")

    if progress_cb:
        if start_utc_ts is not None and end_utc_ts is not None:
            progress_cb(f"Grabbing exact {int(duration_sec)}s recorded slice in 1080p HD...", 24)
        elif rewind_sec >= 5:
            mins = int(rewind_sec // 60)
            secs = int(rewind_sec % 60)
            ago_txt = f"{mins}m {secs:02d}s ago" if mins > 0 else f"{secs}s ago"
            progress_cb(f"Grabbing exact {int(duration_sec)}s clip from {ago_txt} in Live DVR...", 24)
        else:
            progress_cb(f"Grabbing latest {int(duration_sec)}s from live stream...", 24)

    ua_args = ["-user_agent", headers["User-Agent"]] if (headers and headers.get("User-Agent")) else []
    timeout_s = max(45, int(duration_sec * 2.5) + 25)

    # Strategy 1 (EXACT DIRECT .TS DVR DOWNLOAD): Download ONLY the exact historical .ts chunks in Python!
    # Works identically on Debian Linux (HF Spaces) & Windows — 100% immune to FFmpeg HLS live-edge jumps!
    v_ts_local = TEMP_DIR / f"dvr_v_{video_id}_{uuid.uuid4().hex[:6]}.ts"
    a_ts_local = TEMP_DIR / f"dvr_a_{video_id}_{uuid.uuid4().hex[:6]}.ts"
    try:
        v_slice = _download_dvr_ts_slice(
            v_url, rewind_sec, duration_sec, v_ts_local, headers,
            start_utc_ts=start_utc_ts, end_utc_ts=end_utc_ts,
        )
        if v_slice is not None:
            v_file, sub_offset, _avail = v_slice
            a_file = None
            if a_url:
                a_slice = _download_dvr_ts_slice(
                    a_url, rewind_sec, duration_sec, a_ts_local, headers,
                    start_utc_ts=start_utc_ts, end_utc_ts=end_utc_ts,
                )
                if a_slice is not None:
                    a_file = a_slice[0]

            cmd = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(v_file.resolve()),
            ]
            if a_file:
                cmd.extend([
                    "-i", str(a_file.resolve()),
                    "-map", "0:v:0", "-map", "1:a:0",
                ])
            else:
                cmd.extend(["-map", "0:v:0", "-map", "0:a:0?"])

            if sub_offset > 0.05:
                cmd.extend(["-ss", f"{sub_offset:.3f}"])
            cmd.extend([
                "-t", f"{duration_sec:.2f}",
                "-c", "copy",
                "-movflags", "+faststart",
                str(output_path),
            ])
            res_ts = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
            if res_ts.returncode == 0 and output_path.exists() and output_path.stat().st_size > 20_000:
                return output_path
    finally:
        for tmp_ts in (v_ts_local, a_ts_local):
            try:
                if tmp_ts.exists():
                    tmp_ts.unlink()
            except Exception:
                pass

    # Strategy 2 (Fallback): Local VOD .m3u8 playlist via FFmpeg
    v_m3u8_local = TEMP_DIR / f"dvr_v_{video_id}_{uuid.uuid4().hex[:6]}.m3u8"
    a_m3u8_local = TEMP_DIR / f"dvr_a_{video_id}_{uuid.uuid4().hex[:6]}.m3u8"
    try:
        v_slice = _build_local_dvr_vod_m3u8(v_url, rewind_sec, duration_sec, v_m3u8_local, headers)
        if v_slice is not None:
            v_file, sub_offset, _avail = v_slice
            a_file = None
            if a_url:
                a_slice = _build_local_dvr_vod_m3u8(a_url, rewind_sec, duration_sec, a_m3u8_local, headers)
                if a_slice is not None:
                    a_file = a_slice[0]

            cmd = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
                *ua_args,
                "-i", str(v_file.resolve()),
            ]
            if a_file:
                cmd.extend([
                    "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
                    *ua_args,
                    "-i", str(a_file.resolve()),
                    "-map", "0:v:0", "-map", "1:a:0",
                ])
            else:
                cmd.extend(["-map", "0:v:0", "-map", "0:a:0?"])

            if sub_offset > 0.05:
                cmd.extend(["-ss", f"{sub_offset:.3f}"])
            cmd.extend([
                "-t", f"{duration_sec:.2f}",
                "-c", "copy",
                "-movflags", "+faststart",
                str(output_path),
            ])
            res_slice = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
            if res_slice.returncode == 0 and output_path.exists() and output_path.stat().st_size > 20_000:
                return output_path
    finally:
        for tmp_m3u8 in (v_m3u8_local, a_m3u8_local):
            try:
                if tmp_m3u8.exists():
                    tmp_m3u8.unlink()
            except Exception:
                pass

    # Fallback Strategy 2: FFmpeg native -live_start_index / direct live edge
    seconds_back = max(duration_sec + 2.0, rewind_sec + duration_sec + 2.0)
    start_idx = _estimate_hls_live_start_index(v_url, seconds_back, headers)

    def _run_live_ffmpeg(use_start_index: Optional[int]) -> subprocess.CompletedProcess:
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
        cmd.extend(ua_args)
        if use_start_index is not None:
            cmd.extend(["-live_start_index", str(use_start_index)])
        cmd.extend(["-i", v_url])

        if a_url:
            cmd.extend(ua_args)
            if use_start_index is not None:
                a_idx = _estimate_hls_live_start_index(a_url, seconds_back, headers) or use_start_index
                cmd.extend(["-live_start_index", str(a_idx)])
            cmd.extend(["-i", a_url])
            cmd.extend(["-map", "0:v:0", "-map", "1:a:0"])
        else:
            cmd.extend(["-map", "0:v:0", "-map", "0:a:0?"])

        cmd.extend([
            "-t", f"{duration_sec:.2f}",
            "-c", "copy",
            "-movflags", "+faststart",
            str(output_path),
        ])
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)

    res = _run_live_ffmpeg(start_idx if start_idx is not None else -max(4, math.ceil(seconds_back / 5.0)))
    if res.returncode == 0 and output_path.exists() and output_path.stat().st_size > 20_000:
        return output_path

    if progress_cb:
        progress_cb(f"DVR disabled on this stream — capturing {int(duration_sec)}s live...", 28)
    res2 = _run_live_ffmpeg(None)
    if res2.returncode == 0 and output_path.exists() and output_path.stat().st_size > 20_000:
        return output_path

    err_msg = (res2.stderr or res.stderr or "FFmpeg live download failed").strip()[:260]
    raise RuntimeError(f"Could not capture live stream clip: {err_msg}")


def _probe_video_meta(filepath: Path) -> Tuple[int, int, float]:
    """Return (width, height, duration) of a video file via ffprobe."""
    res = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height:format=duration",
            "-of", "json",
            str(filepath),
        ],
        capture_output=True,
        text=True,
        timeout=8,
    )
    if res.returncode != 0:
        return 1920, 1080, 0.0
    data = json.loads(res.stdout or "{}")
    streams = data.get("streams") or [{}]
    w = int(streams[0].get("width") or 1920)
    h = int(streams[0].get("height") or 1080)
    dur = float((data.get("format") or {}).get("duration") or 0.0)
    return w, h, dur


def _even(val: int, minimum: int = 2) -> int:
    v = max(minimum, int(round(val)))
    return v - (v % 2)


def _norm_box_to_pixels(box: Dict[str, float], src_w: int, src_h: int) -> Tuple[int, int, int, int]:
    """Convert normalized {x, y, w, h} (0..1) into safe FFmpeg pixel crop (cw, ch, cx, cy)."""
    nx = max(0.0, min(0.95, float(box.get("x", 0.0))))
    ny = max(0.0, min(0.95, float(box.get("y", 0.0))))
    nw = max(0.08, min(1.0 - nx, float(box.get("w", 0.3))))
    nh = max(0.08, min(1.0 - ny, float(box.get("h", 0.3))))

    cx = _even(int(nx * src_w), 0)
    cy = _even(int(ny * src_h), 0)
    cw = _even(int(nw * src_w), 32)
    ch = _even(int(nh * src_h), 32)

    if cx + cw > src_w:
        cw = _even(src_w - cx, 32)
    if cy + ch > src_h:
        ch = _even(src_h - cy, 32)
    return cw, ch, cx, cy


def _extract_exact_wav(
    input_path: Path,
    pad_start: float,
    duration: float,
    wav_path: Path,
) -> Optional[Path]:
    """Extract a speech-enhanced 16kHz mono WAV of the EXACT clip window for 0.000s-drift Whisper AI captions."""
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    if pad_start > 0.01:
        cmd.extend(["-ss", f"{pad_start:.3f}"])
    cmd.extend([
        "-i", str(input_path),
        "-t", f"{duration:.3f}",
        "-vn", "-ac", "1", "-ar", "16000",
        "-af", "aresample=async=1:first_pts=0,highpass=f=80,dynaudnorm=f=150:g=15",
        "-c:a", "pcm_s16le",
        str(wav_path),
    ])
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=25)
        if res.returncode == 0 and wav_path.exists() and wav_path.stat().st_size > 1000:
            return wav_path
    except Exception:
        pass
    # Fallback without dynamic normalization if filter failed on an unusual codec
    cmd_plain = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    if pad_start > 0.01:
        cmd_plain.extend(["-ss", f"{pad_start:.3f}"])
    cmd_plain.extend([
        "-i", str(input_path),
        "-t", f"{duration:.3f}",
        "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le",
        str(wav_path),
    ])
    try:
        res2 = subprocess.run(cmd_plain, capture_output=True, timeout=25)
        if res2.returncode == 0 and wav_path.exists() and wav_path.stat().st_size > 1000:
            return wav_path
    except Exception:
        pass
    return None


def render_pro_split_short(
    input_path: Path,
    output_path: Path,
    pad_start: float,
    clip_duration: float,
    cam_box: Dict[str, float],
    stream_box: Dict[str, float],
    cam_ratio: float = 0.40,
    cam_position: str = "top",
    resolution: str = "1080p",
    ass_file: Optional[Path] = None,
) -> Path:
    """
    Single-pass GPU/CPU FFmpeg dual-box Split-Screen renderer:
      - Crops Box 1 (Streamer Facecam) & Box 2 (Stream/Gameplay) cleanly with zero distortion
      - Stacks vertically into crisp 1080x1920 (or 720x1280) with a sleek 4px glass seam divider
      - Burns active-word AI Hinglish+English ASS captions in the exact same pass!
    """
    src_w, src_h, probed_dur = _probe_video_meta(input_path)
    actual_dur = clip_duration
    if probed_dur > 1.0 and (pad_start + actual_dur) > (probed_dur + 0.5):
        actual_dur = max(1.0, probed_dur - pad_start)

    out_w, out_h = (720, 1280) if resolution == "720p" else (1080, 1920)
    ratio = max(0.25, min(0.65, float(cam_ratio)))
    divider_h = 4
    cam_h = _even(int(round(out_h * ratio)), 160)
    stream_h = _even(out_h - cam_h - divider_h, 160)
    # Re-balance cam_h if rounding left 2px slack
    cam_h = out_h - stream_h - divider_h

    cam_cw, cam_ch, cam_cx, cam_cy = _norm_box_to_pixels(cam_box, src_w, src_h)
    str_cw, str_ch, str_cx, str_cy = _norm_box_to_pixels(stream_box, src_w, src_h)

    filters = [
        # Reset PTS to exact 0.000s so burned ASS captions ALWAYS match 0:00:00..duration even on HLS/copyts!
        "[0:v]setpts=PTS-STARTPTS,split=2[vcam_in][vstr_in]",
        (
            f"[vcam_in]crop={cam_cw}:{cam_ch}:{cam_cx}:{cam_cy},"
            f"scale={out_w}:{cam_h}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={out_w}:{cam_h},setsar=1[vcam]"
        ),
        (
            f"[vstr_in]crop={str_cw}:{str_ch}:{str_cx}:{str_cy},"
            f"scale={out_w}:{stream_h}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={out_w}:{stream_h},setsar=1[vstr]"
        ),
        f"color=c=#120d24:s={out_w}x{divider_h}:r=30:d={actual_dur + 1.0:.2f}[div]",
    ]

    if cam_position == "bottom":
        filters.append("[vstr][div][vcam]vstack=inputs=3[vstacked]")
    else:
        filters.append("[vcam][div][vstr]vstack=inputs=3[vstacked]")

    if ass_file and ass_file.exists():
        # Run with cwd=TEMP_DIR so filename has no Windows drive-letter colon issues
        filters.append(f"[vstacked]ass={ass_file.name}[vout]")
    else:
        filters.append("[vstacked]null[vout]")

    filter_complex = ";".join(filters)
    enc = detect_best_encoder()

    def _run_encode(enc_args: List[str]) -> subprocess.CompletedProcess:
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{max(0.0, pad_start):.3f}",
            "-i", str(input_path.resolve()),
            "-t", f"{actual_dur:.3f}",
            "-filter_complex", filter_complex,
            "-map", "[vout]",
            "-map", "0:a:0?",
            *enc_args,
            "-pix_fmt", "yuv420p",
            "-r", "30",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-shortest",
            "-movflags", "+faststart",
            str(output_path.resolve()),
        ]
        return subprocess.run(
            cmd,
            cwd=str(TEMP_DIR.resolve()),
            capture_output=True,
            text=True,
            timeout=240,
        )

    res = _run_encode(enc["enc_args"])
    if res.returncode != 0 and enc["name"] != "libx264":
        # Safe CPU fallback if hardware encoder rejected a specific stream profile
        res = _run_encode(["-c:v", "libx264", "-preset", "superfast", "-crf", "19"])

    if res.returncode != 0 or not output_path.exists():
        raise RuntimeError(f"Split-screen render failed: {(res.stderr or 'unknown error').strip()[:260]}")

    return output_path


def process_pro_split_pipeline(
    url: str,
    video_id: str,
    is_live: bool,
    cam_box: Dict[str, float],
    stream_box: Dict[str, float],
    duration_sec: float = 35.0,
    rewind_sec: float = 0.0,
    start_utc_ts: Optional[float] = None,
    end_utc_ts: Optional[float] = None,
    start_time: float = 0.0,
    end_time: float = 35.0,
    cam_ratio: float = 0.40,
    cam_position: str = "top",
    resolution: str = "1080p",
    burn_subtitles: bool = True,
    subtitle_style: str = "viral_pop",
    subtitle_position: str = "seam",
    transcript_cache: Optional[List[Dict[str, Any]]] = None,
    progress_cb: Optional[Callable[[str, int], None]] = None,
) -> Dict[str, Any]:
    """
    Full Pro Studio pipeline:
      1. Grab Live Stream DVR clip OR Normal Video/VOD segment in HD
      2. Transcribe exact cut clip audio with faster-whisper AI (English + Hinglish!)
      3. Render Dual-Box Split-Screen (Streamer + Stream) + Active-Word Hinglish Captions
      4. Clean intermediate temp files & return instant playback/download metadata
    """
    cleanup_old_temp_files()
    t0 = time.time()
    job_id = uuid.uuid4().hex[:8]
    raw_clip = TEMP_DIR / f"pro_raw_{video_id}_{job_id}.mp4"
    downloaded_raw: Optional[Path] = None
    wav_file = TEMP_DIR / f"pro_aud_{video_id}_{job_id}.wav"
    ass_file = TEMP_DIR / f"pro_sub_{video_id}_{job_id}.ass"
    out_filename = f"ClipYT_Pro_{video_id}_{job_id}.mp4"
    out_path = TEMP_DIR / out_filename

    try:
        if is_live:
            if start_utc_ts is not None and end_utc_ts is not None and end_utc_ts > start_utc_ts + 2.0:
                clip_duration = max(5.0, min(120.0, float(end_utc_ts - start_utc_ts)))
            else:
                clip_duration = max(5.0, min(120.0, float(duration_sec)))
            downloaded_raw = download_live_dvr_segment(
                url=url,
                video_id=video_id,
                duration_sec=clip_duration,
                rewind_sec=max(0.0, float(rewind_sec)),
                output_path=raw_clip,
                progress_cb=progress_cb,
                start_utc_ts=start_utc_ts,
                end_utc_ts=end_utc_ts,
            )
            exact_pad = 0.0
        else:
            clip_start = max(0.0, float(start_time))
            clip_end = max(clip_start + 5.0, float(end_time))
            clip_duration = min(120.0, clip_end - clip_start)
            clip_end = clip_start + clip_duration

            if progress_cb:
                progress_cb("Fetching crisp HD video segment...", 16)
            downloaded_raw, exact_pad = download_clip_segment(
                url=url,
                video_id=video_id,
                start_time=clip_start,
                end_time=clip_end,
                output_path=raw_clip,
                progress_cb=progress_cb,
            )

        out_w, out_h = (720, 1280) if resolution == "720p" else (1080, 1920)
        seam_y_ratio = float(cam_ratio) if cam_position == "top" else (1.0 - float(cam_ratio))

        generated_ass: Optional[Path] = None
        caption_engine_used = "none"
        caption_note = ""

        if burn_subtitles:
            if progress_cb:
                progress_cb("AI listening & generating Hinglish + English captions...", 48)
            extracted_wav = _extract_exact_wav(downloaded_raw, exact_pad, clip_duration, wav_file)
            words = transcribe_clip_hinglish(extracted_wav) if extracted_wav else None

            if words:
                generated_ass = generate_ass_from_words(
                    words=words,
                    clip_duration=clip_duration,
                    output_path=ass_file,
                    video_width=out_w,
                    video_height=out_h,
                    style_preset=subtitle_style,
                    position=subtitle_position,
                    seam_y_ratio=seam_y_ratio,
                )
                if generated_ass:
                    caption_engine_used = "ai_whisper_hinglish"
                    caption_note = f"AI Hinglish ({len(words)} words)"
            else:
                ws = get_whisper_status()
                if not extracted_wav:
                    caption_note = "No audio track found in clip"
                elif not ws.get("installed"):
                    caption_note = "AI captions need requirements.txt uploaded to server"
                else:
                    caption_note = ws.get("last_error") or "No speech detected in clip"

            # Graceful fallback to YouTube transcript if VOD & Whisper returned nothing
            if not generated_ass and not is_live:
                generated_ass = generate_ass_subtitles(
                    video_id=video_id,
                    start_time=start_time,
                    end_time=start_time + clip_duration,
                    output_path=ass_file,
                    video_width=out_w,
                    video_height=out_h,
                    style_preset=subtitle_style,
                    position=subtitle_position,
                    transcript_cache=transcript_cache,
                    seam_y_ratio=seam_y_ratio,
                )
                if generated_ass:
                    caption_engine_used = "youtube_hinglish"
                    caption_note = "YouTube Hinglish Captions"

        if progress_cb:
            enc_name = detect_best_encoder()["name"]
            gpu_tag = "GPU" if enc_name != "libx264" else "HD"
            progress_cb(f"Rendering {resolution} Split-Screen Pro Short ({gpu_tag})...", 72)

        render_pro_split_short(
            input_path=downloaded_raw,
            output_path=out_path,
            pad_start=exact_pad,
            clip_duration=clip_duration,
            cam_box=cam_box,
            stream_box=stream_box,
            cam_ratio=cam_ratio,
            cam_position=cam_position,
            resolution=resolution,
            ass_file=generated_ass,
        )

        elapsed = round(time.time() - t0, 1)
        size_mb = round(out_path.stat().st_size / (1024 * 1024), 2)
        _, _, final_dur = _probe_video_meta(out_path)

        return {
            "success": True,
            "filename": out_filename,
            "download_url": f"/api/download/{out_filename}",
            "file_size_mb": size_mb,
            "size_mb": size_mb,
            "duration": round(final_dur or clip_duration, 1),
            "aspect_ratio": "9:16",
            "resolution": resolution,
            "has_subtitles": bool(generated_ass),
            "caption_engine": caption_engine_used,
            "caption_note": caption_note,
            "climax_hook": False,
            "is_pro": True,
            "encoder": detect_best_encoder()["name"],
            "render_seconds": elapsed,
        }
    finally:
        for tmp in (raw_clip, downloaded_raw, wav_file, ass_file):
            if tmp is None:
                continue
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
