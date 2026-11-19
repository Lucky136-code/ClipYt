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
        "bestvideo[height=1080]+bestaudio/"
        "bestvideo[height<=1080]+bestaudio/"
        "best[height=1080][protocol^=m3u8]/"
        "best[height<=1080][protocol^=m3u8]/"
        "bestvideo[height<=1080][protocol^=m3u8]+bestaudio[protocol^=m3u8]/"
        "bv*+ba/b"
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

    # Pick best <=1080p muxed or video stream from formats list, strongly prioritizing 1080p
    fmts = info.get("formats") or []
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
    muxed = [
        f for f in fmts
        if f.get("url")
        and f.get("vcodec") not in (None, "none")
        and f.get("acodec") not in (None, "none")
        and (f.get("height") or 0) <= 1080
    ]

    best_v = None
    if vids:
        vids.sort(key=lambda f: (1 if (f.get("height") or 0) == 1080 else 0, f.get("height") or 0, f.get("tbr") or 0))
        best_v = vids[-1]

    best_m = None
    if muxed:
        muxed.sort(key=lambda f: (1 if (f.get("height") or 0) == 1080 else 0, f.get("height") or 0, f.get("tbr") or 0))
        best_m = muxed[-1]

    # If separate video stream achieves 1080p or >= muxed height, use video+audio to ensure true 1080p
    if best_v and (not best_m or (best_v.get("height") or 0) >= (best_m.get("height") or 0)):
        best_a = sorted(auds, key=lambda f: f.get("abr") or f.get("tbr") or 0)[-1] if auds else None
        return best_v["url"], (best_a["url"] if best_a else None), dict(best_v.get("http_headers") or {})

    if best_m:
        return best_m["url"], None, dict(best_m.get("http_headers") or {})

    return None, None, headers


def _pick_preview_hls_url(info: Dict[str, Any]) -> Tuple[Optional[str], Dict[str, str]]:
    """
    Pick a crisp muxed live HLS playlist (preferring 1080p HD, then 720p)
    for instant, smooth browser playback & scrubbing.
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
        # Strongly prefer 1080p HD first, then fallback to 720p, 480p, 360p
        for target_h in (1080, 720, 480, 360):
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


def _download_dvr_by_sequence(
    m3u8_url: str,
    start_time: Optional[float],
    duration_sec: float,
    rewind_sec: float,
    out_ts_path: Path,
    headers: Optional[Dict[str, str]] = None,
) -> Optional[Tuple[Path, float, float]]:
    """
    Direct Sequence-Based DVR Downloader:
    Uses YouTube's underlying `/sq/<sequence_number>/` chunk addressing to download the exact
    segments requested from the broadcast timeline.
    Works for ANY point in the stream (even 8+ hours into the past), completely bypassing the
    rolling 2-hour manifest window!
    Returns (out_ts_path, sub_offset, available_duration) or None if format is not sequence-based.
    """
    segments, total_dur, _pdt = _parse_hls_segments(m3u8_url, headers)
    if not segments:
        return None

    last_dur, last_url = segments[-1]
    m_sq = re.search(r'/sq/(\d+)/', last_url)
    if not m_sq:
        return None

    last_sq = int(m_sq.group(1))
    chunk_dur = float(last_dur) if (last_dur and last_dur > 0.1) else 2.0

    # YouTube embedded iframe HTML5 player buffers ~3 chunks (15s) + browser decode/network queue (~1s).
    # What the user sees playing on their screen is ~16.0s behind YouTube CDN's latest server edge.
    LIVE_PLAYER_LATENCY_SEC = 16.0

    server_head_sec = (last_sq + 1) * chunk_dur
    screen_head_sec = max(0.0, server_head_sec - LIVE_PLAYER_LATENCY_SEC)
    req_dur = max(3.0, min(120.0, float(duration_sec)))

    # In Live Studio, rewind_sec represents how many seconds behind the live screen edge the user wants.
    if rewind_sec is not None and float(rewind_sec) >= 4.0:
        # User scrubbed back into the past on the live timeline:
        target_start_sec = max(0.0, screen_head_sec - float(rewind_sec))
        target_end_sec = target_start_sec + req_dur
    else:
        # User is at current live edge: record the exact req_dur window that JUST ended on screen!
        target_end_sec = screen_head_sec
        target_start_sec = max(0.0, target_end_sec - req_dur)

    target_sq = max(0, int(math.floor(target_start_sec / chunk_dur)))
    end_sq = min(last_sq, int(math.ceil(target_end_sec / chunk_dur)))
    if end_sq <= target_sq:
        end_sq = min(last_sq, target_sq + max(2, math.ceil(req_dur / chunk_dur)))

    chunk_start_sec = target_sq * chunk_dur
    sub_offset = max(0.0, round(target_start_sec - chunk_start_sec, 3))
    chunks_needed = end_sq - target_sq + 1

    urls = [re.sub(r'/sq/\d+/', f'/sq/{sq}/', last_url) for sq in range(target_sq, target_sq + chunks_needed)]

    req_headers = {}
    if headers and headers.get("User-Agent"):
        req_headers["User-Agent"] = headers["User-Agent"]

    def _fetch_chunk(u: str) -> Optional[bytes]:
        for _ in range(2):
            try:
                r = requests.get(u, headers=req_headers, timeout=12)
                if r.status_code == 200 and len(r.content) > 512:
                    return r.content
            except Exception:
                continue
        return None

    with ThreadPoolExecutor(max_workers=min(12, len(urls))) as ex:
        chunks = list(ex.map(_fetch_chunk, urls))

    valid_chunks = [c for c in chunks if c is not None]
    if len(valid_chunks) < 2:
        return None

    out_ts_path.write_bytes(b"".join(valid_chunks))
    avail_dur = len(valid_chunks) * chunk_dur
    return out_ts_path, sub_offset, max(1.0, avail_dur - sub_offset)


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
        and start_utc_ts >= (first_pdt - 5.0)
        and start_utc_ts <= (first_pdt + total_dur + 10.0)
    ):
        req_dur = max(3.0, min(125.0, float(end_utc_ts - start_utc_ts)))
        win_start = max(0.0, float(start_utc_ts) - first_pdt)
        win_end = min(total_dur, win_start + req_dur)
    else:
        eff_rewind = max(0.0, float(rewind_sec))
        req_dur = max(3.0, min(120.0, float(duration_sec)))
        LIVE_PLAYER_LATENCY_SEC = 16.0
        screen_dur = max(0.0, total_dur - LIVE_PLAYER_LATENCY_SEC)
        if eff_rewind >= 4.0:
            # User rewound into the stream's past:
            win_start = max(0.0, screen_dur - eff_rewind)
            win_end = min(total_dur, win_start + req_dur)
            if (win_end - win_start < req_dur) and win_end >= (total_dur - 0.5):
                win_start = max(0.0, total_dur - req_dur)
                win_end = total_dur
        else:
            # User is at the live edge: record trailing req_dur that just played on screen!
            win_end = max(req_dur, screen_dur)
            win_start = max(0.0, win_end - req_dur)

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
    req_dur = max(3.0, min(120.0, float(duration_sec)))
    if eff_rewind >= 3.0:
        win_start = max(0.0, total_dur - eff_rewind)
        win_end = min(total_dur, win_start + req_dur)
        if (win_end - win_start < req_dur) and win_end >= (total_dur - 0.5):
            win_start = max(0.0, total_dur - req_dur)
            win_end = total_dur
    else:
        win_start = max(0.0, total_dur - req_dur)
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


def _grab_single_ts_frame_b64(
    seg_url: str,
    sub_offset: float = 0.0,
    headers: Optional[Dict[str, str]] = None,
) -> str:
    """Download one exact `.ts` segment (or stream via FFmpeg) and extract a crisp 16:9 JPEG frame."""
    req_headers = {}
    if headers and headers.get("User-Agent"):
        req_headers["User-Agent"] = headers["User-Agent"]
    tmp_ts = TEMP_DIR / f"frm_{uuid.uuid4().hex[:8]}.ts"
    try:
        r = requests.get(seg_url, headers=req_headers, timeout=12)
        if r.status_code != 200 or len(r.content) < 1024:
            return ""
        tmp_ts.write_bytes(r.content)
        # Put -ss AFTER -i (output seek) so non-zero MPEG-TS start_pts never breaks frame extraction!
        attempts = []
        if sub_offset > 0.25:
            attempts.append([
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(tmp_ts),
                "-ss", f"{min(4.2, sub_offset):.2f}",
                "-frames:v", "1",
                "-vf", "scale=960:-2:flags=lanczos",
                "-q:v", "3",
                "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
            ])
        attempts.append([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(tmp_ts),
            "-frames:v", "1",
            "-vf", "scale=960:-2:flags=lanczos",
            "-q:v", "3",
            "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
        ])
        for cmd in attempts:
            res = subprocess.run(cmd, capture_output=True, timeout=10)
            if res.returncode == 0 and res.stdout and len(res.stdout) > 1500:
                return f"data:image/jpeg;base64,{base64.b64encode(res.stdout).decode('ascii')}"
    except Exception:
        pass
    finally:
        try:
            if tmp_ts.exists():
                tmp_ts.unlink()
        except Exception:
            pass
    return ""


def fetch_pro_boundary_frames(
    url: str,
    video_id: str,
    is_live: bool,
    start_utc_ts: Optional[float] = None,
    end_utc_ts: Optional[float] = None,
    start_time: float = 0.0,
    end_time: float = 35.0,
) -> Dict[str, Any]:
    """
    Grab the EXACT Start Frame & End Frame for the user's selected timestamp window
    using the exact same UTC `#EXT-X-PROGRAM-DATE-TIME` segment mapping as Export.
    Guarantees the frames the user verifies ARE the exact start & end of the exported clip!
    """
    cached = PRO_STREAM_CACHE.get(video_id)
    now = time.time()
    if cached and (now - cached["ts"] < PRO_CACHE_TTL_SEC):
        info = cached["info"]
    else:
        info = _extract_pro_info(url, video_id)
        PRO_STREAM_CACHE[video_id] = {"info": info, "ts": now}

    if is_live:
        v_url, headers = _pick_preview_hls_url(info)
        if not v_url:
            v_url, _, headers = _extract_media_urls(info)
        segments, total_dur, first_pdt = _parse_hls_segments(v_url, headers) if v_url else ([], 0.0, None)
        if not segments:
            return {"start_frame": "", "end_frame": "", "first_pdt_utc": first_pdt, "dvr_window_sec": total_dur}

        def _pick_seg_for_rel(rel_sec: float) -> Tuple[str, float]:
            target = max(0.0, min(max(0.0, total_dur - 0.5), rel_sec))
            acc = 0.0
            for seg_dur, seg_url in segments:
                if acc + seg_dur >= target:
                    return seg_url, max(0.0, target - acc)
                acc += seg_dur
            return segments[-1][1], 0.0

        if first_pdt is not None and start_utc_ts is not None and end_utc_ts is not None:
            rel_start = float(start_utc_ts) - first_pdt
            rel_end = float(end_utc_ts) - first_pdt
        else:
            rel_start = float(start_time)
            rel_end = float(end_time)

        s_url, s_off = _pick_seg_for_rel(rel_start)
        e_url, e_off = _pick_seg_for_rel(rel_end)

        with ThreadPoolExecutor(max_workers=2) as pool:
            f_start = pool.submit(_grab_single_ts_frame_b64, s_url, s_off, headers)
            f_end = pool.submit(_grab_single_ts_frame_b64, e_url, e_off, headers)
            start_b64 = f_start.result()
            end_b64 = f_end.result()

        return {
            "start_frame": start_b64,
            "end_frame": end_b64,
            "first_pdt_utc": first_pdt,
            "dvr_window_sec": round(total_dur, 1),
        }

    # Normal Video / VOD: seek directly to start_time and end_time in parallel
    v_url, _a_url, headers = _extract_media_urls(info)
    dur = float(info.get("duration") or 0.0)
    thumb = info.get("thumbnail") or f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_start = pool.submit(_grab_frame_b64, v_url, False, dur, thumb, headers, float(start_time))
        f_end = pool.submit(_grab_frame_b64, v_url, False, dur, thumb, headers, float(end_time))
        s_img, _ = f_start.result()
        e_img, _ = f_end.result()
    return {
        "start_frame": s_img,
        "end_frame": e_img,
        "first_pdt_utc": None,
        "dvr_window_sec": round(dur, 1),
    }


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

    # For active live streams, parse exact DVR window & #EXT-X-PROGRAM-DATE-TIME UTC anchor
    live_elapsed_sec = 0.0
    dvr_window_sec = 0.0
    live_min_sec = 0.0
    first_pdt_utc: Optional[float] = None
    if is_live:
        preview_hls_url, preview_headers = _pick_preview_hls_url(info)
        segs, hls_total, first_pdt_utc = _parse_hls_segments(preview_hls_url or v_url, preview_headers or headers)
        dvr_window_sec = round(hls_total, 1) if hls_total > 0 else _measure_hls_dvr_seconds(v_url, headers)
        # Calculate full broadcast elapsed time from sequence index or release timestamp
        broadcast_elapsed = 0.0
        if segs:
            last_dur, last_url = segs[-1]
            m_sq = re.search(r'/sq/(\d+)/', last_url)
            if m_sq:
                last_sq = int(m_sq.group(1))
                cdur = float(last_dur) if (last_dur and last_dur > 0.1) else 2.0
                broadcast_elapsed = round((last_sq + 1) * cdur, 1)

        rel_ts = info.get("release_timestamp") or info.get("timestamp")
        raw_elapsed = (now - float(rel_ts)) if (rel_ts and now > float(rel_ts)) else 0.0

        live_elapsed_sec = max(broadcast_elapsed, raw_elapsed, dvr_window_sec, 300.0)
        live_min_sec = 0.0
        if first_pdt_utc is None:
            first_pdt_utc = round(now - dvr_window_sec - 15.0, 2)

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
        "first_pdt_utc": first_pdt_utc,
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
    start_time: Optional[float] = None,
) -> Tuple[Path, float]:
    """
    Download an EXACT historical slice from an active YouTube Live Stream (via direct
    parallel `.ts` DVR chunk downloading matched to exact UTC `#EXT-X-PROGRAM-DATE-TIME`)
    so rewound recordings ALWAYS cut the exact recorded frame!
    Returns (raw_downloaded_path, exact_sub_offset).
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
        elif rewind_sec >= 4.0:
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
        # Priority 1: Direct sequence-based DVR downloader (works for ANY time from 00:00:00 to 12+ hours!)
        v_slice = _download_dvr_by_sequence(
            v_url, start_time, duration_sec, rewind_sec, v_ts_local, headers
        )
        if v_slice is None:
            # Priority 2: Standard rolling manifest slice
            v_slice = _download_dvr_ts_slice(
                v_url, rewind_sec, duration_sec, v_ts_local, headers,
                start_utc_ts=start_utc_ts, end_utc_ts=end_utc_ts,
            )

        if v_slice is not None:
            v_file, sub_offset, _avail = v_slice
            a_file = None
            if a_url:
                a_slice = _download_dvr_by_sequence(
                    a_url, start_time, duration_sec, rewind_sec, a_ts_local, headers
                )
                if a_slice is None:
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

            cmd.extend([
                "-c", "copy",
                "-movflags", "+faststart",
                str(output_path),
            ])
            res_ts = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
            if res_ts.returncode == 0 and output_path.exists() and output_path.stat().st_size > 20_000:
                return output_path, sub_offset
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

            cmd.extend([
                "-c", "copy",
                "-movflags", "+faststart",
                str(output_path),
            ])
            res_slice = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
            if res_slice.returncode == 0 and output_path.exists() and output_path.stat().st_size > 20_000:
                return output_path, sub_offset
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
        return output_path, 0.0

    if progress_cb:
        progress_cb(f"DVR disabled on this stream — capturing {int(duration_sec)}s live...", 28)
    res2 = _run_live_ffmpeg(None)
    if res2.returncode == 0 and output_path.exists() and output_path.stat().st_size > 20_000:
        return output_path, 0.0

    # Fallback Strategy: Direct stream recording via yt-dlp
    try:
        if progress_cb:
            progress_cb("Recording directly from live broadcast stream...", 32)
        raw_dl, pad = download_clip_segment(
            url=url,
            video_id=video_id,
            start_time=0.0,
            end_time=duration_sec,
            output_path=output_path,
            progress_cb=progress_cb,
        )
        if raw_dl.exists() and raw_dl.stat().st_size > 20_000:
            return raw_dl, pad
    except Exception:
        pass

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

    if cam_position in ("none", "disabled", "off"):
        # Display Only Mode: Streamer facecam is disabled — record only the display/gameplay box!
        str_cw, str_ch, str_cx, str_cy = _norm_box_to_pixels(stream_box, src_w, src_h)
        bw = max(64, (out_w // 4) // 2 * 2)
        bh = max(64, (out_h // 4) // 2 * 2)
        filters = [
            "[0:v]setpts=PTS-STARTPTS[v_pts]",
            f"[v_pts]crop={str_cw}:{str_ch}:{str_cx}:{str_cy}[vcropped]",
            "[vcropped]split=2[bg_in][fg_in]",
            (
                f"[bg_in]scale={bw}:{bh}:force_original_aspect_ratio=increase,"
                f"crop={bw}:{bh},boxblur=10:3,eq=brightness=-0.15,"
                f"scale={out_w}:{out_h}:flags=bilinear[bg]"
            ),
            (
                f"[fg_in]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,"
                f"setsar=1[fg]"
            ),
            "[bg][fg]overlay=(W-w)/2:(H-h)/2[vstacked]",
        ]
    else:
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
        res = _run_encode(["-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-maxrate", "6000k", "-bufsize", "10000k"])

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
    climax_hook: bool = False,
    hook_duration: float = 3.0,
    transcript_cache: Optional[List[Dict[str, Any]]] = None,
    progress_cb: Optional[Callable[[str, int], None]] = None,
) -> Dict[str, Any]:
    """
    Full Pro Studio pipeline:
      1. Grab Live Stream DVR clip OR Normal Video/VOD segment in HD
      2. If climax_hook: extract high-energy 3s hook & prepend in front of 30s main clip
      3. Transcribe exact cut clip audio with faster-whisper AI (English + Hinglish!)
      4. Render Dual-Box Split-Screen (Streamer + Stream) + Active-Word Hinglish Captions
      5. Clean intermediate temp files & return instant playback/download metadata
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

    intermediate_split: Optional[Path] = None
    hook_clip: Optional[Path] = None
    combo_clip: Optional[Path] = None

    try:
        if is_live:
            if start_utc_ts is not None and end_utc_ts is not None and end_utc_ts > start_utc_ts + 2.0:
                clip_duration = max(5.0, min(120.0, float(end_utc_ts - start_utc_ts)))
            else:
                clip_duration = max(5.0, min(120.0, float(duration_sec)))
            downloaded_raw, exact_pad = download_live_dvr_segment(
                url=url,
                video_id=video_id,
                duration_sec=clip_duration,
                rewind_sec=max(0.0, float(rewind_sec)),
                output_path=raw_clip,
                progress_cb=progress_cb,
                start_utc_ts=start_utc_ts,
                end_utc_ts=end_utc_ts,
                start_time=float(start_time) if start_time is not None else None,
            )
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

        raw_w, raw_h, raw_dur = _probe_video_meta(downloaded_raw)
        if (raw_dur or 0.0) < min(3.5, clip_duration * 0.4) and clip_duration >= 5.0:
            raise RuntimeError(
                "This stream ended very recently and YouTube is still archiving it. "
                "Historical replay segments are not yet released by YouTube. "
                "Please choose an archived VOD or a currently live stream."
            )

        out_w, out_h = (720, 1280) if resolution == "720p" else (1080, 1920)
        is_no_cam = cam_position in ("none", "disabled", "off")
        mode_label = "Display-Only" if is_no_cam else "Split-Screen"
        if is_no_cam:
            seam_y_ratio = 0.72
            subtitle_position = "bottom"
        else:
            seam_y_ratio = float(cam_ratio) if cam_position == "top" else (1.0 - float(cam_ratio))

        # Check if we should generate a 3-second hook prepended to the front
        hook_dur_eff = min(float(hook_duration), max(2.0, clip_duration / 2.0)) if climax_hook else 0.0

        if climax_hook and hook_dur_eff >= 2.0:
            if progress_cb:
                enc_name = detect_best_encoder()["name"]
                gpu_tag = "GPU" if enc_name != "libx264" else "HD"
                progress_cb(f"Rendering {resolution} {mode_label} main cut ({gpu_tag})...", 42)

            intermediate_split = TEMP_DIR / f"pro_split_raw_{video_id}_{job_id}.mp4"
            render_pro_split_short(
                input_path=downloaded_raw,
                output_path=intermediate_split,
                pad_start=exact_pad,
                clip_duration=clip_duration,
                cam_box=cam_box,
                stream_box=stream_box,
                cam_ratio=cam_ratio,
                cam_position=cam_position,
                resolution=resolution,
                ass_file=None,
            )

            # Extract 3-second hook from the middle (peak moment)
            if progress_cb:
                progress_cb(f"Creating {int(hook_dur_eff)}s peak viral hook...", 56)

            mid_t = clip_duration / 2.0
            hook_start = max(0.0, mid_t - (hook_dur_eff / 2.0))
            hook_clip = TEMP_DIR / f"pro_hook_{video_id}_{job_id}.mp4"

            subprocess.run([
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-ss", f"{hook_start:.3f}",
                "-i", str(intermediate_split.resolve()),
                "-t", f"{hook_dur_eff:.3f}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-maxrate", "6000k", "-bufsize", "10000k",
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
                str(hook_clip.resolve())
            ], check=True, timeout=30)

            # Concat hook (in front) + main clip
            if progress_cb:
                progress_cb("Stitching 3s hook + 30s viral clip...", 64)

            combo_clip = TEMP_DIR / f"pro_combo_{video_id}_{job_id}.mp4"
            subprocess.run([
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(hook_clip.resolve()),
                "-i", str(intermediate_split.resolve()),
                "-filter_complex", "[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[v][a]",
                "-map", "[v]", "-map", "[a]",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-maxrate", "6000k", "-bufsize", "10000k",
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
                "-movflags", "+faststart",
                str(combo_clip.resolve())
            ], check=True, timeout=60)

            total_dur = clip_duration + hook_dur_eff
            target_source_for_subtitles = combo_clip
            target_sub_pad = 0.0
        else:
            total_dur = clip_duration
            target_source_for_subtitles = downloaded_raw
            target_sub_pad = exact_pad

        generated_ass: Optional[Path] = None
        caption_engine_used = "none"
        caption_note = ""

        if burn_subtitles:
            if progress_cb:
                progress_cb("AI listening & generating Hinglish + English captions...", 72)
            extracted_wav = _extract_exact_wav(target_source_for_subtitles, target_sub_pad, total_dur, wav_file)
            words = transcribe_clip_hinglish(extracted_wav) if extracted_wav else None

            if words:
                generated_ass = generate_ass_from_words(
                    words=words,
                    clip_duration=total_dur,
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

            if not generated_ass and not is_live and not climax_hook:
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

        # Final render: if climax_hook was used, burn subtitles onto combo_clip
        if climax_hook and hook_dur_eff >= 2.0:
            if progress_cb:
                progress_cb(f"Finishing {resolution} Short with hook & captions...", 88)

            if generated_ass and generated_ass.exists():
                subprocess.run([
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(combo_clip.resolve()),
                    "-vf", f"ass={generated_ass.name}",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-maxrate", "6000k", "-bufsize", "10000k",
                    "-c:a", "copy",
                    "-movflags", "+faststart",
                    str(out_path.resolve())
                ], cwd=str(TEMP_DIR.resolve()), check=True, timeout=120)
            else:
                import shutil
                shutil.copyfile(str(combo_clip.resolve()), str(out_path.resolve()))
        else:
            if progress_cb:
                enc_name = detect_best_encoder()["name"]
                gpu_tag = "GPU" if enc_name != "libx264" else "HD"
                progress_cb(f"Rendering {resolution} {mode_label} Pro Short ({gpu_tag})...", 78)

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

        if not out_path.exists() or size_mb <= 0.05 or (final_dur or 0.0) < 1.0:
            raise RuntimeError(
                "Render produced an empty or corrupted video (0 MB). "
                "The stream segment was unavailable from YouTube. Please try another timestamp or video."
            )

        return {
            "success": True,
            "filename": out_filename,
            "download_url": f"/api/download/{out_filename}",
            "file_size_mb": size_mb,
            "size_mb": size_mb,
            "duration": round(final_dur or total_dur, 1),
            "aspect_ratio": "9:16",
            "resolution": resolution,
            "has_subtitles": bool(generated_ass),
            "caption_engine": caption_engine_used,
            "caption_note": caption_note,
            "climax_hook": climax_hook,
            "hook_duration": hook_dur_eff,
            "is_pro": True,
            "encoder": detect_best_encoder()["name"],
            "render_seconds": elapsed,
        }
    finally:
        for tmp in (raw_clip, downloaded_raw, wav_file, ass_file, intermediate_split, hook_clip, combo_clip):
            if tmp is None:
                continue
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
            if tmp is None:
                continue
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
