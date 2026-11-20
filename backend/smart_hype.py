import re
import subprocess
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import requests

_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

_TIMESTAMP_RE = re.compile(r"(?<!\d)(?:(\d{1,2}):)?([0-5]?\d):([0-5]\d)(?!\d)")

_HYPE_TRANSCRIPT_RE = re.compile(
    r"\b("
    r"bhai|arey|arre|yaar|kya|clutch|insane|crazy|bro|omg|wtf|wow|whoa|"
    r"no|way|wait|watch|look|dekho|dekh|mast|op|gajab|baap|pagal|khatam|"
    r"boom|lets|let's|go|killed|dead|laugh|haha|lmao|lol|awesome|epic|"
    r"unbelievable|impossible|god|oh|my|mar|gaya|abe|bhaag|ruko|shocked|"
    r"secret|finally|win|won|victory|clutch|headshot|ace|1v4|1v3|1v5"
    r")\b",
    re.IGNORECASE,
)


def _parse_ts_match(m: re.Match) -> float:
    h_str, m_str, s_str = m.groups()
    h = int(h_str) if h_str else 0
    mins = int(m_str)
    secs = int(s_str)
    return float(h * 3600 + mins * 60 + secs)


def _fetch_top_comment_texts(video_id: str, max_comments: int = 40) -> List[str]:
    """
    Lightweight, fast (~1.5s) fetch of top YouTube comments via InnerTube `next` API
    so we can mine viewer-posted viral timestamps (e.g. '4:32 bhai kya scene tha 😂').
    """
    headers = {"User-Agent": _CHROME_UA, "Accept-Language": "en-US,en;q=0.9"}
    try:
        watch_resp = requests.get(
            f"https://www.youtube.com/watch?v={video_id}",
            headers=headers,
            timeout=6,
        )
        if watch_resp.status_code != 200:
            return []
        html = watch_resp.text
        key_m = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', html)
        # Find the comments section continuation token
        cont_m = re.search(
            r'"comment-item-section"[^}]*?"continuationCommand":\{"token":"([^"]+)"',
            html,
        )
        if not cont_m:
            # Alternative JSON layout where token precedes sectionIdentifier
            cont_m = re.search(
                r'"token":"([^"]+)"[^}]{0,220}"sectionIdentifier":"comment-item-section"',
                html,
            )
        if not cont_m:
            return []

        api_url = "https://www.youtube.com/youtubei/v1/next?prettyPrint=false"
        if key_m:
            api_url += f"&key={key_m.group(1)}"

        payload = {
            "context": {
                "client": {
                    "clientName": "WEB",
                    "clientVersion": "2.20250101.00.00",
                }
            },
            "continuation": cont_m.group(1),
        }
        c_resp = requests.post(api_url, json=payload, headers=headers, timeout=6)
        if c_resp.status_code != 200:
            return []

        raw_json = c_resp.text
        # Extract comment content strings from frameworkUpdates (modern YT) or legacy commentRenderer
        texts = re.findall(r'"content":\{"content":"((?:[^"\\]|\\.)+)"', raw_json)
        if not texts:
            texts = re.findall(r'"contentText":\{"runs":\[\{"text":"((?:[^"\\]|\\.)+)"', raw_json)

        cleaned = []
        for t in texts[:max_comments]:
            try:
                decoded = bytes(t, "utf-8").decode("unicode_escape", errors="ignore")
            except Exception:
                decoded = t
            if decoded.strip():
                cleaned.append(decoded.strip())
        return cleaned
    except Exception:
        return []


def _extract_timestamp_signal(
    video_id: str,
    duration: float,
    description: str,
    chapters: Optional[List[Dict[str, Any]]],
    num_bins: int,
) -> Optional[np.ndarray]:
    """Build a spike array from viewer comment timestamps + description timestamps."""
    if duration <= 10.0:
        return None

    hits: List[Tuple[float, float]] = []  # (timestamp_sec, weight)

    # 1. Top viewer comments (highest weight — real humans marking funny/viral moments!)
    comments = _fetch_top_comment_texts(video_id)
    for c_txt in comments:
        seen_in_comment = set()
        for m in _TIMESTAMP_RE.finditer(c_txt):
            ts = _parse_ts_match(m)
            if 2.0 <= ts <= (duration - 2.0):
                bucket_key = int(ts // 5)
                if bucket_key not in seen_in_comment:
                    seen_in_comment.add(bucket_key)
                    hits.append((ts, 1.5))

    # 2. Description timestamps (moderate weight — creator highlights/timestamps)
    if description:
        desc_matches = list(_TIMESTAMP_RE.finditer(description))
        # Ignore full table-of-contents if it's just 15+ evenly spaced chapters
        if 1 <= len(desc_matches) <= 12:
            for m in desc_matches:
                ts = _parse_ts_match(m)
                if 5.0 <= ts <= (duration - 5.0):
                    hits.append((ts, 0.7))

    # 3. Video chapters (light hint only if no comment timestamps found)
    if not hits and chapters and 2 <= len(chapters) <= 12:
        for ch in chapters[1:]:
            ts = float(ch.get("start_time") or 0.0)
            if 5.0 <= ts <= (duration - 5.0):
                hits.append((ts, 0.5))

    if not hits:
        return None

    arr = np.zeros(num_bins, dtype=np.float64)
    bin_centers = (np.arange(num_bins) + 0.5) * (duration / num_bins)
    sigma = max(4.0, min(14.0, duration * 0.018))

    for ts, weight in hits:
        gauss = np.exp(-0.5 * ((bin_centers - ts) / sigma) ** 2)
        arr += gauss * weight

    mx = float(np.max(arr))
    if mx <= 1e-6:
        return None
    return arr / mx


def _extract_transcript_hype_signal(
    transcript_words: Optional[List[Dict[str, Any]]],
    duration: float,
    num_bins: int,
) -> Optional[np.ndarray]:
    """
    Score speech energy (words/sec bursts + English/Hinglish hype reaction keywords)
    across `num_bins` bins with zero extra network calls.
    """
    if not transcript_words or len(transcript_words) < 8 or duration <= 10.0:
        return None

    bin_dur = duration / num_bins
    pace = np.zeros(num_bins, dtype=np.float64)
    hype = np.zeros(num_bins, dtype=np.float64)

    for item in transcript_words:
        w = str(item.get("word") or item.get("text") or "").strip()
        if not w:
            continue
        t = float(item.get("start") or 0.0)
        idx = int(t / bin_dur)
        if 0 <= idx < num_bins:
            pace[idx] += 1.0
            if _HYPE_TRANSCRIPT_RE.search(w) or "!" in w or "?" in w:
                hype[idx] += 1.0

    if float(np.max(pace)) <= 0:
        return None

    # Normalize using 95th percentile so one crazy bin doesn't crush the rest
    p_norm = np.clip(pace / max(1.0, float(np.percentile(pace, 95))), 0.0, 1.0)
    h_scale = float(np.percentile(hype, 95))
    if h_scale > 0:
        h_norm = np.clip(hype / max(1.0, h_scale), 0.0, 1.0)
        combined = p_norm * 0.42 + h_norm * 0.58
    else:
        combined = p_norm

    # Smooth gently
    kernel = np.array([0.2, 0.6, 0.2])
    combined = np.convolve(combined, kernel, mode="same")
    mx = float(np.max(combined))
    return (combined / mx) if mx > 1e-6 else None


def _pick_lightweight_audio_url(info: Dict[str, Any]) -> Tuple[Optional[str], Dict[str, str]]:
    """Pick the smallest/lowest-bitrate audio-only stream URL for ultra-fast loudness scan."""
    fmts = info.get("formats") or []
    audio_only = [
        f for f in fmts
        if f.get("url")
        and f.get("acodec") not in (None, "none")
        and f.get("vcodec") in (None, "none")
        and f.get("protocol") in ("https", "http", "m3u8_native", "m3u8")
    ]
    if audio_only:
        # Sort by lowest bitrate first so download is tiny (e.g., 48kbps opus/m4a = ~1.5 MB for 5 min!)
        audio_only.sort(key=lambda f: float(f.get("abr") or f.get("tbr") or 9999))
        best = audio_only[0]
        return best["url"], dict(best.get("http_headers") or info.get("http_headers") or {})
    return None, {}


def _extract_audio_energy_signal(
    info: Dict[str, Any],
    duration: float,
    num_bins: int,
) -> Optional[np.ndarray]:
    """
    Pipe the lowest-bitrate audio track at 2000Hz mono PCM into NumPy to measure
    per-bin loudness & sudden scream/laugh/reaction energy spikes in ~2-4 seconds.
    """
    # Keep analysis snappy: skip full audio download on ultra-long >40 min VODs
    if duration <= 10.0 or duration > 2400.0:
        return None

    audio_url, headers = _pick_lightweight_audio_url(info)
    if not audio_url:
        return None

    sr = 2000
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    if headers.get("User-Agent"):
        cmd.extend(["-user_agent", headers["User-Agent"]])
    cmd.extend([
        "-t", "180",
        "-i", audio_url,
        "-vn", "-ac", "1", "-ar", str(sr),
        "-f", "s16le",
        "pipe:1",
    ])

    try:
        res = subprocess.run(cmd, capture_output=True, timeout=4.5)
        if res.returncode != 0 or not res.stdout or len(res.stdout) < sr * 4:
            return None

        samples = np.frombuffer(res.stdout, dtype=np.int16).astype(np.float32) / 32768.0
        n = len(samples)
        if n < num_bins * 10:
            return None

        # Trim/split cleanly into `num_bins` equal chunks
        chunk_len = n // num_bins
        usable = samples[: chunk_len * num_bins].reshape(num_bins, chunk_len)

        # RMS loudness + peak transient punch (catches shouts, laughs, sudden reactions!)
        rms = np.sqrt(np.mean(usable ** 2, axis=1) + 1e-10)
        peak = np.percentile(np.abs(usable), 98, axis=1)
        energy = rms * 0.65 + peak * 0.35

        # Emphasize contrast above median so quiet background stays low and shouts spike high
        med = float(np.median(energy))
        p98 = float(np.percentile(energy, 98))
        if p98 <= 1e-6:
            return None

        # Map [0.5*med .. p98] -> [0.05 .. 1.0]
        floor = med * 0.45
        norm = np.clip((energy - floor) / max(1e-6, p98 - floor), 0.0, 1.0)
        # Light smoothing
        kernel = np.array([0.2, 0.6, 0.2])
        norm = np.convolve(norm, kernel, mode="same")
        mx = float(np.max(norm))
        return (norm / mx) if mx > 1e-6 else None
    except Exception:
        return None


def build_smart_hype_heatmap(
    video_id: str,
    info: Dict[str, Any],
    duration: float,
    transcript_words: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Build a real 0..1 Smart Hype Graph for freshly uploaded videos without a YouTube
    heatmap by fusing:
      1. Audio Loudness & Reaction Spikes (screams, laughs, intense moments)
      2. Speech Burst & English/Hinglish Hype Keywords
      3. Viewer Comment Timestamps & Description Highlights
    Returns (raw_heatmap_points, list_of_signals_used).
    """
    if duration <= 10.0:
        return [], []

    num_bins = min(100, max(25, int(duration // 3)))
    signals: List[Tuple[str, float, np.ndarray]] = []

    ts_sig = _extract_timestamp_signal(
        video_id=video_id,
        duration=duration,
        description=(info.get("description") or ""),
        chapters=info.get("chapters"),
        num_bins=num_bins,
    )
    if ts_sig is not None:
        signals.append(("Comment Timestamps", 0.48, ts_sig))

    aud_sig = _extract_audio_energy_signal(info=info, duration=duration, num_bins=num_bins)
    if aud_sig is not None:
        signals.append(("Audio Energy Spikes", 0.42, aud_sig))

    tr_sig = _extract_transcript_hype_signal(
        transcript_words=transcript_words,
        duration=duration,
        num_bins=num_bins,
    )
    if tr_sig is not None:
        signals.append(("Hype Speech Bursts", 0.35, tr_sig))

    if not signals:
        return [], []

    total_w = sum(w for _, w, _ in signals)
    fused = np.zeros(num_bins, dtype=np.float64)
    labels = []
    for name, w, arr in signals:
        fused += (w / total_w) * arr
        labels.append(name)

    # Normalize cleanly to [0.05, 1.0] so the Engagement Map displays crisp, legible spikes
    lo = float(np.min(fused))
    hi = float(np.max(fused))
    if hi - lo > 1e-6:
        fused = 0.06 + 0.94 * ((fused - lo) / (hi - lo))
    else:
        fused = np.clip(fused, 0.1, 1.0)

    bin_dur = duration / num_bins
    raw_points: List[Dict[str, Any]] = []
    for i in range(num_bins):
        raw_points.append({
            "start_time": round(i * bin_dur, 2),
            "end_time": round((i + 1) * bin_dur, 2),
            "value": round(float(fused[i]), 4),
        })

    return raw_points, labels
