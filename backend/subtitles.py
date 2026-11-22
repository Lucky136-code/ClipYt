import os
import re
from pathlib import Path
from typing import List, Dict, Any, Optional
import requests
import yt_dlp
from youtube_transcript_api import YouTubeTranscriptApi
from backend.config import YTDLP_BASE_OPTS
from backend.whisper_captions import devanagari_to_hinglish


def _format_ass_time(seconds: float) -> str:
    """Convert seconds to ASS timestamp format: H:MM:SS.cs"""
    seconds = max(0.0, seconds)
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    centis = int(round((seconds - int(seconds)) * 100))
    if centis >= 100:
        secs += 1
        centis = 0
        if secs >= 60:
            mins += 1
            secs = 0
            if mins >= 60:
                hrs += 1
                mins = 0
    return f"{hrs}:{mins:02d}:{secs:02d}.{centis:02d}"


def _clean_word(text: str) -> str:
    """Clean a single caption token, convert Devanagari Hindi to Hinglish, and escape ASS braces."""
    text = text.replace("\n", " ").replace("\r", " ")
    text = re.sub(r"\s+", " ", text).strip()
    text = devanagari_to_hinglish(text)
    text = text.replace("{", "(").replace("}", ")")
    return text


def _is_non_speech(text: str) -> bool:
    """Filter out sound-effect tags like [Music], [Applause], ♪, etc."""
    t = text.strip()
    if not t:
        return True
    if re.fullmatch(r"\[[^\]]*\]", t) or re.fullmatch(r"\([^\)]*\)", t):
        return True
    if re.fullmatch(r"[♪♫🎵🎶\s\-_.]+", t):
        return True
    return False


def _estimate_word_duration(word: str) -> float:
    """Realistic spoken duration for a word so captions vanish right when speech stops."""
    clean_len = len(re.sub(r"[^A-Za-z0-9]", "", word))
    return min(0.62, max(0.18, 0.13 + 0.055 * clean_len))


def _is_hindi_video(info: Dict[str, Any]) -> bool:
    """
    Determine if the video was originally spoken in Hindi/Hinglish.
    Ensures Hindi speech is never translated to English, while English videos remain in English.
    """
    auto = info.get("automatic_captions") or {}
    manual = info.get("subtitles") or {}
    vlang = (info.get("language") or "").strip().lower()

    if vlang.startswith("hi"):
        return True
    if vlang.startswith("en") and "en-orig" in auto:
        return False
    if "hi-orig" in auto or "hi-Latn" in auto or "hi-Latn" in manual:
        return True
    if "hi" in auto and "en-orig" not in auto:
        return True
    return False


def _pick_best_caption_url(info: Dict[str, Any]) -> Optional[str]:
    """
    Pick the best YouTube json3 caption track URL from yt-dlp info.
    Prefers auto-captions (which include exact word-level tOffsetMs!).
    CRITICAL:
    - If the video is originally Hindi/Hinglish, prioritizes original Hindi speech (hi-orig, hi-Latn, hi)
      so that captions preserve the exact spoken words in Hinglish, and NEVER chooses YouTube's
      auto-translated English track!
    - If the video is originally English, prioritizes original English speech (en-orig, en).
    """
    auto = info.get("automatic_captions") or {}
    manual = info.get("subtitles") or {}
    video_lang = (info.get("language") or "").strip().lower()

    def _find_json3(tracks: List[Dict[str, Any]]) -> Optional[str]:
        for t in tracks or []:
            if t.get("ext") == "json3" and t.get("url"):
                return t["url"]
        return None

    is_hi = _is_hindi_video(info)

    priority_langs: List[str] = []
    if is_hi:
        # Spoken language is Hindi/Hinglish: prioritize Hindi tracks first!
        # Devanagari Hindi is automatically converted to clean Roman Hinglish.
        # NEVER pick 'en' here because 'en' on a Hindi video is YouTube's auto-generated translation!
        priority_langs.extend(["hi-Latn", "hi-orig", "hi", "ur-orig", "ur", "mr-orig", "pa-orig"])
        if video_lang and not video_lang.startswith("en"):
            priority_langs.extend([f"{video_lang}-orig", video_lang])
        # Only fallback to English if Indic tracks fail
        priority_langs.extend(["en-orig", "en-IN", "en", "en-US", "en-GB"])
    else:
        # Spoken language is English or another language: prioritize original English
        if video_lang:
            priority_langs.extend([f"{video_lang}-orig", video_lang, video_lang.split("-")[0]])
        priority_langs.extend(["en-orig", "en", "en-US", "en-GB", "en-IN"])
        priority_langs.extend(["hi-Latn", "hi-orig", "hi"])

    # Prefer auto-captions first because YouTube auto-captions have exact per-word tOffsetMs!
    for lang in priority_langs:
        if lang in auto:
            url = _find_json3(auto[lang])
            if url:
                return url

    # Check any '-orig' auto-caption key (always the video's actual spoken language)
    for lang, tracks in auto.items():
        if lang.endswith("-orig"):
            url = _find_json3(tracks)
            if url:
                return url

    # Fall back to manual subtitles (often clean punctuation, line-level timing)
    for lang in priority_langs:
        if lang in manual:
            url = _find_json3(manual[lang])
            if url:
                return url

    # Last resort: first simple language key in manual or auto
    for source in (manual, auto):
        for lang, tracks in source.items():
            if "-" not in lang:
                url = _find_json3(tracks)
                if url:
                    return url

    return None


def _parse_json3_to_words(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Parse YouTube json3 captions into exact word-level entries:
    [{"word": str, "start": float, "end": float}, ...]
    Uses exact tStartMs + tOffsetMs per word and clamps trailing silence so
    captions appear right when spoken and disappear immediately when speech pauses.
    """
    raw_words: List[Dict[str, Any]] = []
    events = data.get("events") or []

    for ev in events:
        segs = ev.get("segs")
        if not segs or "tStartMs" not in ev:
            continue

        ev_start = float(ev["tStartMs"]) / 1000.0
        ev_dur = float(ev.get("dDurationMs") or 0.0) / 1000.0

        # Check if this event actually has per-word tOffsetMs
        has_offsets = any("tOffsetMs" in s for s in segs[1:]) if len(segs) > 1 else False

        if has_offsets or len(segs) == 1:
            for seg in segs:
                txt = _clean_word(seg.get("utf8", ""))
                if not txt or _is_non_speech(txt):
                    continue
                offset = float(seg.get("tOffsetMs") or 0.0) / 1000.0
                w_start = ev_start + offset

                # If a single segment contains multiple words, split proportionally
                sub_words = txt.split()
                if len(sub_words) == 1:
                    raw_words.append({"word": sub_words[0], "start": w_start, "hint_end": None})
                else:
                    sub_span = min(max(0.3, ev_dur - offset), sum(_estimate_word_duration(w) for w in sub_words))
                    total_c = max(1, sum(len(w) for w in sub_words))
                    t_cur = w_start
                    for sw in sub_words:
                        d = sub_span * (len(sw) / total_c)
                        raw_words.append({"word": sw, "start": t_cur, "hint_end": t_cur + d})
                        t_cur += d
        else:
            # Manual subtitle line without per-word offsets: distribute smoothly across natural speech rate
            line_text = " ".join(_clean_word(s.get("utf8", "")) for s in segs)
            line_text = re.sub(r"\s+", " ", line_text).strip()
            if not line_text or _is_non_speech(line_text):
                continue
            words = [w for w in line_text.split() if not _is_non_speech(w)]
            if not words:
                continue
            natural_dur = sum(_estimate_word_duration(w) for w in words)
            eff_dur = min(ev_dur if ev_dur > 0.2 else natural_dur, max(natural_dur * 1.15, 0.5))
            total_c = max(1, sum(len(w) for w in words))
            t_cur = ev_start
            for w in words:
                d = eff_dur * (len(w) / total_c)
                raw_words.append({"word": w, "start": t_cur, "hint_end": t_cur + d})
                t_cur += d

    if not raw_words:
        return []

    # Sort chronologically and remove near-duplicate identical words from rolling windows
    raw_words.sort(key=lambda x: x["start"])
    deduped: List[Dict[str, Any]] = []
    for item in raw_words:
        if deduped:
            prev = deduped[-1]
            if (
                prev["word"].lower() == item["word"].lower()
                and abs(item["start"] - prev["start"]) < 0.12
            ):
                continue
        deduped.append(item)

    # Compute tight, silence-aware end times so captions vanish immediately after speaking ends
    words_out: List[Dict[str, Any]] = []
    for i, item in enumerate(deduped):
        start = item["start"]
        est_dur = _estimate_word_duration(item["word"])
        hint_end = item.get("hint_end")
        natural_end = min(hint_end, start + est_dur * 1.3) if hint_end else (start + est_dur)

        if i + 1 < len(deduped):
            next_start = deduped[i + 1]["start"]
            gap = next_start - start
            if gap <= 0.02:
                # Avoid zero/negative duration when two words share identical timestamp
                end = start + 0.14
            elif gap <= 0.65:
                # Continuous speech: hold right up to next word
                end = next_start
            else:
                # Speaker paused! Cut off cleanly right after this word finishes (no lingering!)
                end = min(next_start - 0.05, natural_end + 0.08)
        else:
            # Final word of video
            end = natural_end + 0.10

        end = max(start + 0.12, end)
        words_out.append({
            "word": item["word"],
            "start": round(start, 3),
            "end": round(end, 3),
        })

    return words_out


def extract_word_transcript_from_info(info: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    """
    Fast extraction of exact word-level YouTube json3 captions directly from an
    already-fetched yt-dlp info dict (zero extra YouTube scrape, works on cloud!).
    """
    try:
        json3_url = _pick_best_caption_url(info)
        if not json3_url:
            return None
        resp = requests.get(json3_url, timeout=10)
        if resp.status_code != 200:
            return None
        words = _parse_json3_to_words(resp.json())
        return words if words else None
    except Exception:
        return None


def fetch_youtube_transcript(
    video_id: str,
    info_dict: Optional[Dict[str, Any]] = None,
) -> Optional[List[Dict[str, Any]]]:
    """
    Fetch word-level YouTube captions:
    1. Primary: YouTube json3 via yt-dlp info (word-level tOffsetMs, works with cookies on cloud!)
    2. Secondary: Fresh yt-dlp metadata json3 fetch (if info_dict wasn't passed)
    3. Fallback: youtube-transcript-api (de-overlapped + silence-trimmed)
    Returns list of word items: [{"word": str, "start": float, "end": float}, ...]
    """
    # 1. Reuse already-fetched yt-dlp info (instant, zero extra request)
    if info_dict:
        words = extract_word_transcript_from_info(info_dict)
        if words:
            return words
        # If info_dict was already provided, yt-dlp has already run! Skip redundant second scrape.
    else:
        # 2. Fetch json3 directly via yt-dlp only when info_dict was not supplied
        try:
            opts = dict(YTDLP_BASE_OPTS)
            opts.update({
                "extract_flat": False,
                "ignore_no_formats_error": True,
                "format": None,
                "writesubtitles": True,
                "writeautomaticsub": True,
                "subtitleslangs": ["hi.*", "hi-orig", "hi-Latn", "en.*", "en-orig"],
            })
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            words = extract_word_transcript_from_info(info)
            if words:
                return words
        except Exception:
            pass

    # 3. Fallback: youtube-transcript-api (de-overlapped into word timings)
    try:
        api = YouTubeTranscriptApi()
        transcript_list = api.list(video_id)
        transcript = None

        # If video has generated Hindi speech track, use it so words remain in Hinglish!
        try:
            transcript = transcript_list.find_generated_transcript(["hi", "hi-Latn"])
        except Exception:
            pass

        # If not, try manual Hinglish/Hindi or English
        if not transcript:
            try:
                transcript = transcript_list.find_transcript(["hi-Latn", "en", "en-US", "en-GB", "en-IN"])
            except Exception:
                for t in transcript_list:
                    transcript = t
                    break
        if not transcript:
            return None

        fetched = list(transcript.fetch())
        words_out: List[Dict[str, Any]] = []
        for idx, snippet in enumerate(fetched):
            txt = _clean_word(getattr(snippet, "text", ""))
            if not txt or _is_non_speech(txt):
                continue
            s = float(getattr(snippet, "start", 0.0))
            d = float(getattr(snippet, "duration", 0.0))
            raw_end = s + max(0.25, d)
            # Clamp overlapping YouTube rolling-caption end to next snippet's start
            if idx + 1 < len(fetched):
                next_s = float(getattr(fetched[idx + 1], "start", raw_end))
                if next_s > s + 0.15:
                    raw_end = min(raw_end, next_s)

            seg_words = [w for w in txt.split() if not _is_non_speech(w)]
            if not seg_words:
                continue
            natural_span = sum(_estimate_word_duration(w) for w in seg_words)
            eff_span = min(raw_end - s, max(natural_span * 1.15, 0.35))
            total_c = max(1, sum(len(w) for w in seg_words))
            t_cur = s
            for w in seg_words:
                wd = max(0.14, eff_span * (len(w) / total_c))
                words_out.append({
                    "word": w,
                    "start": round(t_cur, 3),
                    "end": round(t_cur + wd, 3),
                })
                t_cur += wd
        return words_out if words_out else None
    except Exception:
        return None


def _group_words_into_phrases(
    words: List[Dict[str, Any]],
    win_start: float,
    win_end: float,
    time_offset: float,
    max_words: int = 3,
    max_chars: int = 20,
    pause_threshold: float = 0.40,
) -> List[List[Dict[str, Any]]]:
    """
    Slice words inside [win_start, win_end], shift to time_offset, and group into
    punchy short-form phrases that:
      - Start at the exact millisecond the first word is spoken
      - Never bridge across a speech pause (>= pause_threshold seconds)
      - Break cleanly on sentence punctuation (. ? !)
      - Disappear cleanly right after the last word finishes
    """
    win_dur = max(0.2, win_end - win_start)
    clipped: List[Dict[str, Any]] = []

    for w in words:
        ws = float(w["start"])
        we = float(w["end"])
        # Only include words that actually start inside (or mostly inside) the clip window
        if we <= win_start + 0.05 or ws >= win_end - 0.05:
            continue
        rel_s = max(0.0, ws - win_start) + time_offset
        rel_e = min(win_dur, we - win_start) + time_offset
        if rel_e - rel_s < 0.08:
            rel_e = min(win_dur + time_offset, rel_s + 0.14)
        clipped.append({
            "word": w["word"],
            "start": rel_s,
            "end": rel_e,
        })

    if not clipped:
        return []

    phrases: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    current_chars = 0

    for w in clipped:
        if current:
            prev = current[-1]
            gap = w["start"] - prev["end"]
            prev_ends_sentence = prev["word"].endswith((".", "?", "!"))
            would_exceed = (
                len(current) >= max_words
                or (current_chars + len(w["word"]) + 1) > max_chars
            )
            # Split phrase on any noticeable silence pause, punctuation, or word/char cap
            if gap >= pause_threshold or prev_ends_sentence or would_exceed:
                phrases.append(current)
                current = []
                current_chars = 0

        current.append(w)
        current_chars += len(w["word"]) + (1 if current_chars > 0 else 0)

    if current:
        phrases.append(current)

    return phrases


def _build_word_highlight_dialogues(
    phrases: List[List[Dict[str, Any]]],
    uppercase: bool,
    active_color_ass: str,
    pop_active: bool,
) -> List[str]:
    """
    Convert phrases of word-timed tokens into ASS Dialogue lines with exact
    word-by-word active highlighting (Opus Clip / CapCut style).
    Each phrase disappears cleanly the moment its last word finishes!
    """
    dialogues: List[str] = []

    for phrase in phrases:
        if not phrase:
            continue

        # Ensure words inside a phrase are strictly non-overlapping & contiguous while speaking
        for i in range(len(phrase)):
            w = phrase[i]
            w_start = w["start"]
            if i + 1 < len(phrase):
                next_start = phrase[i + 1]["start"]
                # Hold active highlight seamlessly until next word in same phrase starts
                w_end = max(w_start + 0.08, next_start)
            else:
                # Final word of phrase: disappear cleanly right when word finishes!
                w_end = max(w_start + 0.14, w["end"])

            parts = []
            for j, token in enumerate(phrase):
                txt = token["word"].upper() if uppercase else token["word"]
                if j == i:
                    if pop_active:
                        parts.append(
                            rf"{{\c{active_color_ass}&\fscx108\fscy108\t(0,70,\fscx100\fscy100)}}{txt}{{\r}}"
                        )
                    else:
                        parts.append(rf"{{\c{active_color_ass}&}}{txt}{{\r}}")
                else:
                    parts.append(txt)

            line_text = " ".join(parts)
            # Subtle entrance pop on the very first word of each new phrase
            if i == 0 and pop_active:
                line_text = r"{\fscx94\fscy94\t(0,65,\fscx100\fscy100)}" + line_text

            s_str = _format_ass_time(w_start)
            e_str = _format_ass_time(w_end)
            dialogues.append(f"Dialogue: 0,{s_str},{e_str},Default,,0,0,0,,{line_text}\n")

    return dialogues


def _write_phrases_to_ass(
    phrases: List[List[Dict[str, Any]]],
    output_path: Path,
    video_width: int = 1080,
    video_height: int = 1920,
    style_preset: str = "viral_pop",
    position: str = "bottom",
    seam_y_ratio: Optional[float] = None,
) -> Optional[Path]:
    """Render grouped phrase lists into a stylized active-word ASS subtitle file."""
    if not phrases:
        return None

    # Scale font size proportionally to resolution
    base_scale = min(video_width, video_height)
    font_size = int(round(base_scale * 0.072))  # Bold & super legible on mobile
    margin_lr = int(round(video_width * 0.08))

    if position == "seam" and seam_y_ratio is not None:
        # Place cleanly right at the split-screen seam (clamped inside safe vertical zone)
        safe_ratio = max(0.24, min(0.76, float(seam_y_ratio)))
        alignment = 2  # bottom-center anchor measured from bottom edge
        margin_v = int(round(video_height * (1.0 - safe_ratio) - (font_size * 0.55)))
        margin_v = max(int(video_height * 0.14), min(int(video_height * 0.78), margin_v))
    elif position == "top":
        alignment = 8
        margin_v = int(round(video_height * 0.13))
    elif position == "center":
        alignment = 5
        margin_v = 0
    else:  # bottom-safe (stays above Shorts / Reels / TikTok bottom UI)
        alignment = 2
        margin_v = int(round(video_height * (0.21 if video_height > video_width else 0.10)))

    # Style definitions (ASS colors use &HAABBGGRR)
    is_windows = os.name == "nt"
    bold_font = "Arial Black" if is_windows else "DejaVu Sans"
    clean_font = "Segoe UI" if is_windows else "Liberation Sans"

    uppercase = False
    pop_active = True
    if style_preset == "neon_yellow":
        primary_color = "&H0000E5FF"   # Bright Golden Yellow
        active_color = "&H0055FF33"    # Punchy Neon Green on active spoken word
        outline_color = "&H00000000"
        back_color = "&H90000000"
        border_style = 1
        outline = max(5, int(font_size * 0.11))
        shadow = 4
        font_name = bold_font
        uppercase = True
    elif style_preset == "clean_modern":
        primary_color = "&H00FFFFFF"   # Crisp White
        active_color = "&H0000E5FF"    # Warm Gold highlight on active spoken word
        outline_color = "&H9A000000"   # Semi-transparent dark pill box
        back_color = "&H9A000000"
        border_style = 3
        outline = max(12, int(font_size * 0.19))
        shadow = 0
        font_name = clean_font
        uppercase = False
        pop_active = False
    else:  # viral_pop (default: White bold + active spoken word pops in vibrant yellow!)
        primary_color = "&H00FFFFFF"   # Crisp White
        active_color = "&H0000EAFF"    # Vibrant Viral Yellow on active spoken word
        outline_color = "&H00000000"   # Thick Black Stroke
        back_color = "&HA0000000"      # Clean Drop Shadow
        border_style = 1
        outline = max(5, int(font_size * 0.11))
        shadow = 4
        font_name = bold_font
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

    dialogues = _build_word_highlight_dialogues(
        phrases=phrases,
        uppercase=uppercase,
        active_color_ass=active_color,
        pop_active=pop_active,
    )
    if not dialogues:
        return None

    output_path.write_text(header + "".join(dialogues), encoding="utf-8")
    return output_path


def generate_ass_from_words(
    words: List[Dict[str, Any]],
    clip_duration: float,
    output_path: Path,
    video_width: int = 1080,
    video_height: int = 1920,
    style_preset: str = "viral_pop",
    position: str = "seam",
    seam_y_ratio: Optional[float] = None,
) -> Optional[Path]:
    """
    Build active-word highlighted .ass subtitles directly from clip-relative acoustic words
    (e.g. from faster-whisper Hinglish+English transcription with 0.000s drift).
    """
    if not words:
        return None
    is_vertical = video_width <= video_height
    max_words = 3 if is_vertical else 5
    max_chars = 19 if is_vertical else 32
    phrases = _group_words_into_phrases(
        words,
        0.0,
        max(0.5, float(clip_duration)),
        0.0,
        max_words=max_words,
        max_chars=max_chars,
    )
    return _write_phrases_to_ass(
        phrases=phrases,
        output_path=output_path,
        video_width=video_width,
        video_height=video_height,
        style_preset=style_preset,
        position=position,
        seam_y_ratio=seam_y_ratio,
    )


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
    seam_y_ratio: Optional[float] = None,
) -> Optional[Path]:
    """
    Generate an advanced word-synced .ass subtitle file with real-time active-word
    highlighting and silence-aware cutoff.
    """
    words = transcript_cache if transcript_cache is not None else fetch_youtube_transcript(video_id)
    if not words:
        return None

    # Backward-compatibility check if cached items are legacy snippet dicts
    if words and "word" not in words[0] and "text" in words[0]:
        converted: List[Dict[str, Any]] = []
        for item in words:
            seg_w = [w for w in _clean_word(item["text"]).split() if not _is_non_speech(w)]
            if not seg_w:
                continue
            s = float(item["start"])
            e = float(item["end"])
            span = max(0.2, min(e - s, sum(_estimate_word_duration(w) for w in seg_w) * 1.15))
            tot_c = max(1, sum(len(w) for w in seg_w))
            t_c = s
            for w in seg_w:
                wd = max(0.14, span * (len(w) / tot_c))
                converted.append({"word": w, "start": t_c, "end": t_c + wd})
                t_c += wd
        words = converted

    is_vertical = video_width <= video_height
    max_words = 3 if is_vertical else 5
    max_chars = 19 if is_vertical else 32

    phrases: List[List[Dict[str, Any]]] = []
    if hook_teaser_offset is not None and hook_teaser_duration > 0.2:
        t_abs_start = start_time + hook_teaser_offset
        t_abs_end = t_abs_start + hook_teaser_duration
        phrases.extend(
            _group_words_into_phrases(
                words, t_abs_start, t_abs_end, 0.0, max_words=max_words, max_chars=max_chars
            )
        )
        phrases.extend(
            _group_words_into_phrases(
                words, start_time, end_time, hook_teaser_duration, max_words=max_words, max_chars=max_chars
            )
        )
    else:
        phrases.extend(
            _group_words_into_phrases(
                words, start_time, end_time, 0.0, max_words=max_words, max_chars=max_chars
            )
        )

    return _write_phrases_to_ass(
        phrases=phrases,
        output_path=output_path,
        video_width=video_width,
        video_height=video_height,
        style_preset=style_preset,
        position=position,
        seam_y_ratio=seam_y_ratio,
    )
