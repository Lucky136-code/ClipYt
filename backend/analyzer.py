import re
import io
import base64
from typing import List, Dict, Any, Optional
import requests
from PIL import Image
import yt_dlp
import numpy as np
from backend.config import YTDLP_BASE_OPTS, ensure_fresh_cookiefile
from backend.smart_hype import build_smart_hype_heatmap
from backend.subtitles import fetch_youtube_transcript


def extract_video_id(url: str) -> Optional[str]:
    """Extract YouTube video ID from live, watch, shorts, embed, and youtu.be URL formats."""
    patterns = [
        r"(?:live\/)([0-9A-Za-z_-]{11})",
        r"(?:shorts\/)([0-9A-Za-z_-]{11})",
        r"(?:embed\/)([0-9A-Za-z_-]{11})",
        r"(?:youtu\.be\/)([0-9A-Za-z_-]{11})",
        r"(?:[?&]v=)([0-9A-Za-z_-]{11})",
        r"(?:v=|\/)([0-9A-Za-z_-]{11})(?:[?&#/]|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def format_timestamp(seconds: float, force_hours: bool = False) -> str:
    """Format seconds into HH:MM:SS (if >= 1 hr or force_hours) else MM:SS."""
    seconds = max(0, int(round(seconds)))
    hrs = seconds // 3600
    mins = (seconds % 3600) // 60
    secs = seconds % 60
    if hrs > 0 or force_hours:
        return f"{hrs:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def _get_best_storyboard_meta(info: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Extract highest-resolution YouTube storyboard sprite metadata for 60fps live frame scrubbing."""
    formats = info.get("formats") or []
    storyboards = [
        f for f in formats
        if f.get("protocol") == "mhtml" and f.get("fragments") and f.get("rows") and f.get("columns")
    ]
    if not storyboards:
        return None

    storyboards.sort(key=lambda f: (f.get("width") or 0) * (f.get("height") or 0), reverse=True)
    sb = storyboards[0]

    cell_w = int(sb.get("width") or 160)
    cell_h = int(sb.get("height") or 90)
    rows = int(sb.get("rows") or 5)
    cols = int(sb.get("columns") or 5)
    fps = float(sb.get("fps") or 0.0)
    fragments = sb.get("fragments") or []

    frag_list = []
    t_acc = 0.0
    for frag in fragments:
        dur = float(frag.get("duration") or 0.0)
        url = frag.get("url")
        if url and dur > 0:
            frag_list.append({"start": round(t_acc, 3), "end": round(t_acc + dur, 3), "url": url})
            t_acc += dur

    if not frag_list or t_acc <= 0:
        return None

    return {
        "width": cell_w,
        "height": cell_h,
        "rows": rows,
        "cols": cols,
        "fps": fps,
        "total_duration": round(t_acc, 3),
        "fragments": frag_list,
    }


def _extract_storyboard_frames(
    sb_meta: Optional[Dict[str, Any]], timestamps: List[float]
) -> Dict[int, str]:
    """
    Crops actual video frame thumbnails for the given timestamps from YouTube's
    highest-resolution storyboard sprite sheets (zero video download required).
    Returns mapping of timestamp index -> base64 JPEG data URL.
    """
    frames_out: Dict[int, str] = {}
    if not sb_meta:
        return frames_out

    cell_w = sb_meta["width"]
    cell_h = sb_meta["height"]
    rows = sb_meta["rows"]
    cols = sb_meta["cols"]
    cells_per_sheet = rows * cols
    fps = sb_meta["fps"]
    t_acc = sb_meta["total_duration"]
    frag_windows = [(f["start"], f["end"], f["url"]) for f in sb_meta["fragments"]]

    sheet_cache: Dict[str, Optional[Image.Image]] = {}

    for idx, ts in enumerate(timestamps):
        ts_clamped = max(0.0, min(ts, t_acc - 0.01))
        chosen_frag = None
        frag_start = 0.0
        frag_dur = 1.0

        for f_start, f_end, f_url in frag_windows:
            if f_start <= ts_clamped <= f_end:
                chosen_frag = f_url
                frag_start = f_start
                frag_dur = max(0.01, f_end - f_start)
                break

        if not chosen_frag:
            chosen_frag = frag_windows[-1][2]
            frag_start = frag_windows[-1][0]
            frag_dur = max(0.01, frag_windows[-1][1] - frag_start)

        if not chosen_frag:
            continue

        # Fetch sprite sheet once and cache
        if chosen_frag not in sheet_cache:
            try:
                resp = requests.get(chosen_frag, timeout=6)
                if resp.status_code == 200:
                    sheet_cache[chosen_frag] = Image.open(io.BytesIO(resp.content)).convert("RGB")
                else:
                    sheet_cache[chosen_frag] = None
            except Exception:
                sheet_cache[chosen_frag] = None

        sheet_img = sheet_cache.get(chosen_frag)
        if sheet_img is None:
            continue

        # Determine cell index inside this sprite sheet
        actual_cols = max(1, sheet_img.width // cell_w)
        actual_rows = max(1, sheet_img.height // cell_h)
        actual_cells = actual_cols * actual_rows

        rel_t = max(0.0, ts_clamped - frag_start)
        if fps > 0:
            cell_idx = int(rel_t * fps)
        else:
            cell_idx = int((rel_t / frag_dur) * cells_per_sheet)

        cell_idx = max(0, min(cell_idx, actual_cells - 1))
        r = cell_idx // actual_cols
        c = cell_idx % actual_cols

        x1 = c * cell_w
        y1 = r * cell_h
        x2 = min(sheet_img.width, x1 + cell_w)
        y2 = min(sheet_img.height, y1 + cell_h)

        if x2 > x1 and y2 > y1:
            crop = sheet_img.crop((x1, y1, x2, y2))
            buf = io.BytesIO()
            crop.save(buf, format="JPEG", quality=88)
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            frames_out[idx] = f"data:image/jpeg;base64,{b64}"

    return frames_out


def analyze_youtube_video(url: str, clip_duration: float = 35.0, max_clips: int = 5) -> Dict[str, Any]:
    """
    Extracts video metadata, caption/description, storyboard frame pictures,
    and analyzes the YouTube interaction heatmap to detect high-intensity moments.
    """
    video_id = extract_video_id(url)
    clean_url = f"https://www.youtube.com/watch?v={video_id}" if video_id else url

    ensure_fresh_cookiefile()
    ydl_opts = dict(YTDLP_BASE_OPTS)
    ydl_opts.update({
        "extract_flat": False,
        "ignore_no_formats_error": True,
        "format": None,
        "writesubtitles": True,
        "writeautomaticsub": True,
    })

    info = None
    last_err = ""
    client_strategies = [
        ("default", {}),
        ("web_safari", {"extractor_args": {"youtube": {"player_client": ["web_safari"]}}}),
        ("tv", {"extractor_args": {"youtube": {"player_client": ["tv"]}}}),
        ("android_vr", {"extractor_args": {"youtube": {"player_client": ["android_vr"]}}}),
    ]

    for c_name, extra in client_strategies:
        opts = dict(ydl_opts)
        opts.update(extra)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(clean_url, download=False)
            if info and (info.get("duration") is not None or info.get("title")):
                break
        except Exception as e:
            last_err = str(e)
            continue

    if not info:
        raise RuntimeError(f"Could not load YouTube video ({last_err or 'No formats found'}).")

    duration = float(info.get("duration") or 0.0)
    use_hours = duration >= 3600
    title = info.get("title", "YouTube Video")
    author = info.get("uploader", info.get("channel", "Unknown"))
    v_id = info.get("id", video_id or "video")
    description = (info.get("description") or "").strip()
    view_count = info.get("view_count", 0)
    thumbnail = info.get("thumbnail", "")
    thumbs = info.get("thumbnails") or []
    if thumbs:
        valid_thumbs = [t for t in thumbs if t.get("url")]
        if valid_thumbs:
            valid_thumbs.sort(key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))
            thumbnail = valid_thumbs[-1]["url"]
    if not thumbnail:
        thumbnail = f"https://i.ytimg.com/vi/{v_id}/maxresdefault.jpg"

    caption_info = {
        "language": info.get("language"),
        "subtitles": info.get("subtitles") or {},
        "automatic_captions": info.get("automatic_captions") or {},
    }

    raw_heatmap = info.get("heatmap") or []
    has_heatmap = bool(raw_heatmap and len(raw_heatmap) > 0)
    hype_source = "youtube_heatmap" if has_heatmap else "fallback"
    hype_signals: List[str] = ["YouTube Most Replayed"] if has_heatmap else []
    prefetched_transcript = None

    if not has_heatmap and duration > 10.0:
        # Fresh upload / no YouTube replay graph yet -> build Smart Hype Graph!
        try:
            prefetched_transcript = fetch_youtube_transcript(v_id, caption_info)
        except Exception:
            prefetched_transcript = None
        smart_points, smart_labels = build_smart_hype_heatmap(
            video_id=v_id,
            info=info,
            duration=duration,
            transcript_words=prefetched_transcript,
        )
        if smart_points:
            raw_heatmap = smart_points
            has_heatmap = True
            hype_source = "smart_hype"
            hype_signals = smart_labels

    if has_heatmap:
        clips = _detect_peaks_from_heatmap(
            raw_heatmap=raw_heatmap,
            video_duration=duration,
            clip_duration=clip_duration,
            max_clips=max_clips,
            use_hours=use_hours,
        )
    else:
        clips = _generate_fallback_clips(
            video_duration=duration,
            clip_duration=clip_duration,
            max_clips=max_clips,
            use_hours=use_hours,
        )

    # Extract storyboard metadata + actual video frame pictures for each clip's peak moment
    sb_meta = _get_best_storyboard_meta(info)
    peak_timestamps = [c["peak_time"] for c in clips]
    frame_map = _extract_storyboard_frames(sb_meta, peak_timestamps)
    for idx, clip in enumerate(clips):
        clip["frame_image"] = frame_map.get(idx) or thumbnail

    # Format heatmap for UI chart display
    timeline_chart_data = []
    for pt in raw_heatmap:
        start = pt.get("start_time", 0.0)
        end = pt.get("end_time", 0.0)
        mid = round((start + end) / 2.0, 1)
        val = round(float(pt.get("value", 0.0)) * 100, 1)
        timeline_chart_data.append({
            "time": mid,
            "time_str": format_timestamp(mid, force_hours=use_hours),
            "intensity": val,
        })

    return {
        "video_id": v_id,
        "title": title,
        "description": description,
        "author": author,
        "duration": duration,
        "use_hours": use_hours,
        "duration_str": format_timestamp(duration, force_hours=use_hours),
        "thumbnail": thumbnail,
        "view_count": view_count,
        "has_heatmap": has_heatmap,
        "hype_source": hype_source,
        "hype_signals": hype_signals,
        "heatmap": timeline_chart_data,
        "storyboard": sb_meta,
        "clips": clips,
        "_caption_info": caption_info,
        "_prefetched_transcript": prefetched_transcript,
    }


def _detect_peaks_from_heatmap(
    raw_heatmap: List[Dict[str, Any]],
    video_duration: float,
    clip_duration: float = 35.0,
    max_clips: int = 5,
    use_hours: bool = False,
) -> List[Dict[str, Any]]:
    """
    Identifies high-intensity intervals from YouTube heatmap points
    using peak ranking and non-maximum suppression.
    """
    if not raw_heatmap:
        return []

    times = []
    values = []
    for pt in raw_heatmap:
        s = pt.get("start_time", 0.0)
        e = pt.get("end_time", 0.0)
        v = float(pt.get("value", 0.0))
        times.append((s + e) / 2.0)
        values.append(v)

    times = np.array(times)
    values = np.array(values)

    # Smooth the curve with a small rolling window
    smoothed = np.convolve(values, np.ones(3) / 3, mode="same")

    # Dampen artificial t=0s drop-off spike so true mid-video replay peaks rank first
    if len(smoothed) > 4 and video_duration > clip_duration * 1.5:
        if values[0] > 0.85 and values[1] < 0.6:
            smoothed[0] = values[1]

    max_val = float(np.max(smoothed)) if len(smoothed) > 0 else 1.0
    if max_val <= 0:
        max_val = 1.0

    candidate_windows = []
    sorted_indices = np.argsort(smoothed)[::-1]

    for idx in sorted_indices:
        peak_t = times[idx]
        val = smoothed[idx]

        half_dur = clip_duration / 2.0
        start = max(0.0, peak_t - half_dur)
        end = min(video_duration, start + clip_duration)
        if (end - start) < clip_duration and start > 0:
            start = max(0.0, end - clip_duration)

        mask = (times >= start) & (times <= end)
        window_intensity = float(np.mean(values[mask])) if np.any(mask) else float(val)

        rel_peak = val / max_val
        rel_window = min(1.0, window_intensity / max_val)
        score = int(round((rel_peak * 0.65 + rel_window * 0.35) * 100))
        score = min(100, max(1, score))

        candidate_windows.append({
            "peak_time": round(float(peak_t), 2),
            "start_time": round(float(start), 2),
            "end_time": round(float(end), 2),
            "duration": round(float(end - start), 2),
            "intensity_score": score,
        })

    selected_clips = []
    for cand in candidate_windows:
        overlap = False
        for sel in selected_clips:
            overlap_start = max(cand["start_time"], sel["start_time"])
            overlap_end = min(cand["end_time"], sel["end_time"])
            if overlap_end > overlap_start:
                overlap_dur = overlap_end - overlap_start
                if overlap_dur > (clip_duration * 0.15):
                    overlap = True
                    break
        if not overlap:
            selected_clips.append(cand)
            if len(selected_clips) >= max_clips:
                break

    formatted_clips = []
    for rank, clip in enumerate(selected_clips, 1):
        formatted_clips.append({
            "clip_id": f"clip_{rank}",
            "rank": rank,
            "label": f"Moment #{rank}",
            "start_time": clip["start_time"],
            "end_time": clip["end_time"],
            "start_str": format_timestamp(clip["start_time"], force_hours=use_hours),
            "end_str": format_timestamp(clip["end_time"], force_hours=use_hours),
            "duration": clip["duration"],
            "duration_str": format_timestamp(clip["duration"], force_hours=False),
            "peak_time": clip["peak_time"],
            "peak_str": format_timestamp(clip["peak_time"], force_hours=use_hours),
            "intensity_score": clip["intensity_score"],
        })

    return formatted_clips


def _generate_fallback_clips(
    video_duration: float,
    clip_duration: float = 35.0,
    max_clips: int = 5,
    use_hours: bool = False,
) -> List[Dict[str, Any]]:
    """Generates evenly spaced candidate clips when no YouTube heatmap is present."""
    if video_duration <= clip_duration:
        return [{
            "clip_id": "clip_1",
            "rank": 1,
            "label": "Full Video Clip",
            "start_time": 0.0,
            "end_time": round(video_duration, 2),
            "start_str": format_timestamp(0.0, force_hours=use_hours),
            "end_str": format_timestamp(video_duration, force_hours=use_hours),
            "duration": round(video_duration, 2),
            "duration_str": format_timestamp(video_duration, force_hours=False),
            "peak_time": round(video_duration / 2.0, 2),
            "peak_str": format_timestamp(video_duration / 2.0, force_hours=use_hours),
            "intensity_score": 75,
        }]

    clips = []
    usable_duration = video_duration - clip_duration
    step = usable_duration / max(1, (max_clips + 1))

    for i in range(1, max_clips + 1):
        start = round(min(usable_duration, step * i), 2)
        end = round(min(video_duration, start + clip_duration), 2)
        clips.append({
            "clip_id": f"clip_{i}",
            "rank": i,
            "label": f"Moment #{i}",
            "start_time": start,
            "end_time": end,
            "start_str": format_timestamp(start, force_hours=use_hours),
            "end_str": format_timestamp(end, force_hours=use_hours),
            "duration": round(end - start, 2),
            "duration_str": format_timestamp(end - start, force_hours=False),
            "peak_time": round(start + (end - start) / 2.0, 2),
            "peak_str": format_timestamp(start + (end - start) / 2.0, force_hours=use_hours),
            "intensity_score": 70 - (i * 3),
        })

    return clips
