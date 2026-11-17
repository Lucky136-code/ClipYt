import re
from pathlib import Path
from typing import List, Dict, Any, Optional
from youtube_transcript_api import YouTubeTranscriptApi


def _format_ass_time(seconds: float) -> str:
    """Convert seconds to ASS timestamp format: H:MM:SS.cs"""
    seconds = max(0.0, seconds)
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    centis = int(round((seconds - int(seconds)) * 100))
    if centis >= 100:
        centis = 99
    return f"{hrs}:{mins:02d}:{secs:02d}.{centis:02d}"


def _clean_text(text: str) -> str:
    """Clean YouTube transcript text of newlines, extra spaces, and escape ASS braces."""
    text = text.replace("\n", " ").replace("\r", " ")
    text = re.sub(r"\s+", " ", text).strip()
    # Escape curly braces so ASS parser doesn't interpret them as override tags
    text = text.replace("{", "(").replace("}", ")")
    return text


def fetch_youtube_transcript(video_id: str) -> Optional[List[Dict[str, Any]]]:
    """
    Fetch YouTube captions quickly via youtube-transcript-api (no ML downloads).
    Prefers English, falls back to any available language.
    """
    try:
        api = YouTubeTranscriptApi()
        transcript_list = api.list(video_id)

        transcript = None
        # Prefer manual English, then auto English, then any available language
        try:
            transcript = transcript_list.find_transcript(["en", "en-US", "en-GB"])
        except Exception:
            for t in transcript_list:
                transcript = t
                break

        if not transcript:
            return None

        fetched = transcript.fetch()
        items = []
        for snippet in fetched:
            text = _clean_text(getattr(snippet, "text", ""))
            start = float(getattr(snippet, "start", 0.0))
            duration = float(getattr(snippet, "duration", 0.0))
            if text:
                items.append({
                    "text": text,
                    "start": start,
                    "end": start + max(0.2, duration),
                })
        return items
    except Exception:
        return None


def _split_into_punchy_chunks(
    text: str, start: float, end: float, max_words: int = 4
) -> List[Dict[str, Any]]:
    """Split a long caption snippet into punchy short-form chunks with proportional timing."""
    words = text.split()
    if len(words) <= max_words:
        return [{"text": text, "start": start, "end": end}]

    chunks = []
    total_dur = max(0.2, end - start)
    total_chars = max(1, sum(len(w) for w in words))

    curr_time = start
    for i in range(0, len(words), max_words):
        chunk_words = words[i : i + max_words]
        chunk_text = " ".join(chunk_words)
        chunk_chars = sum(len(w) for w in chunk_words)
        chunk_dur = total_dur * (chunk_chars / total_chars)
        chunk_end = min(end, curr_time + chunk_dur)
        chunks.append({
            "text": chunk_text,
            "start": curr_time,
            "end": chunk_end,
        })
        curr_time = chunk_end

    return chunks


def _collect_window_events(
    transcript: List[Dict[str, Any]],
    win_start: float,
    win_end: float,
    time_offset: float,
    max_words: int,
) -> List[Dict[str, Any]]:
    """Extract and chunk subtitle events within [win_start, win_end], shifted to time_offset."""
    win_dur = max(0.2, win_end - win_start)
    events: List[Dict[str, Any]] = []
    for item in transcript:
        s = item["start"]
        e = item["end"]
        if e <= win_start or s >= win_end:
            continue
        rel_start = max(0.0, s - win_start)
        rel_end = min(win_dur, e - win_start)
        if rel_end - rel_start < 0.15:
            continue
        chunks = _split_into_punchy_chunks(
            item["text"], rel_start + time_offset, rel_end + time_offset, max_words=max_words
        )
        events.extend(chunks)
    return events


def generate_ass_subtitles(
    video_id: str,
    start_time: float,
    end_time: float,
    output_path: Path,
    video_width: int = 1080,
    video_height: int = 1920,
    style_preset: str = "viral_pop",
    position: str = "bottom",
    transcript_cache: Optional[List[Dict[str, Any]]] = None,
    hook_teaser_offset: Optional[float] = None,
    hook_teaser_duration: float = 0.0,
) -> Optional[Path]:
    """
    Generate a styled .ass subtitle file for the specified clip interval.
    If hook_teaser_offset & hook_teaser_duration > 0 are given, prepends
    captions for the climax teaser slice at 0:00 and shifts main captions accordingly.
    Returns path to generated .ass file, or None if no captions are available.
    """
    transcript = transcript_cache if transcript_cache is not None else fetch_youtube_transcript(video_id)
    if not transcript:
        return None

    max_words = 4 if video_width <= video_height else 7
    clip_events: List[Dict[str, Any]] = []

    if hook_teaser_offset is not None and hook_teaser_duration > 0.2:
        t_abs_start = start_time + hook_teaser_offset
        t_abs_end = t_abs_start + hook_teaser_duration
        clip_events.extend(
            _collect_window_events(transcript, t_abs_start, t_abs_end, 0.0, max_words)
        )
        clip_events.extend(
            _collect_window_events(transcript, start_time, end_time, hook_teaser_duration, max_words)
        )
    else:
        clip_events.extend(
            _collect_window_events(transcript, start_time, end_time, 0.0, max_words)
        )

    if not clip_events:
        return None

    # Prevent overlapping timestamps between consecutive lines
    for i in range(len(clip_events) - 1):
        if clip_events[i]["end"] > clip_events[i + 1]["start"]:
            clip_events[i]["end"] = max(clip_events[i]["start"] + 0.1, clip_events[i + 1]["start"] - 0.02)

    # Scale font size proportionally to resolution
    base_scale = min(video_width, video_height)
    font_size = int(round(base_scale * 0.068))  # ~73px at 1080 width
    margin_lr = int(round(video_width * 0.08))

    # Alignment & Vertical Margin (ASS numpad alignment: 2=bottom-center, 5=mid-center, 8=top-center)
    if position == "top":
        alignment = 8
        margin_v = int(round(video_height * 0.12))
    elif position == "center":
        alignment = 5
        margin_v = 0
    else:  # bottom-safe (stays above Shorts / TikTok UI overlays)
        alignment = 2
        margin_v = int(round(video_height * (0.20 if video_height > video_width else 0.10)))

    # Style definitions (ASS colors use &HAABBGGRR)
    uppercase = False
    if style_preset == "neon_yellow":
        primary_color = "&H0000E5FF"   # Bright Golden Yellow
        outline_color = "&H00000000"   # Pure Black
        back_color = "&H90000000"
        border_style = 1
        outline = max(4, int(font_size * 0.10))
        shadow = 4
        font_name = "Arial Black"
        uppercase = True
    elif style_preset == "clean_modern":
        primary_color = "&H00FFFFFF"   # Pure White
        outline_color = "&H95000000"   # Semi-transparent black box
        back_color = "&H95000000"
        border_style = 3               # Opaque box
        outline = max(10, int(font_size * 0.18))
        shadow = 0
        font_name = "Segoe UI"
        uppercase = False
    else:  # viral_pop (default)
        primary_color = "&H00FFFFFF"   # Crisp White
        outline_color = "&H00000000"   # Thick Black Stroke
        back_color = "&HA0000000"      # Drop shadow
        border_style = 1
        outline = max(5, int(font_size * 0.11))
        shadow = 4
        font_name = "Arial Black"
        uppercase = True

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {video_width}
PlayResY: {video_height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_name},{font_size},{primary_color},&H000000FF,{outline_color},{back_color},-1,0,0,0,100,100,0,0,{border_style},{outline},{shadow},{alignment},{margin_lr},{margin_lr},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    lines = [header]
    for ev in clip_events:
        txt = ev["text"].upper() if uppercase else ev["text"]
        s_str = _format_ass_time(ev["start"])
        e_str = _format_ass_time(ev["end"])
        # Add subtle pop-in animation tag for viral_pop & neon_yellow
        if style_preset in ("viral_pop", "neon_yellow"):
            effect_tag = r"{\fscx92\fscy92\t(0,80,\fscx100\fscy100)}"
            lines.append(f"Dialogue: 0,{s_str},{e_str},Default,,0,0,0,,{effect_tag}{txt}\n")
        else:
            lines.append(f"Dialogue: 0,{s_str},{e_str},Default,,0,0,0,,{txt}\n")

    output_path.write_text("".join(lines), encoding="utf-8")
    return output_path
