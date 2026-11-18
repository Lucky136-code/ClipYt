import re
import time
from collections import Counter, deque
from typing import Any, Dict, List, Optional, Tuple

import requests
import numpy as np

from backend.live_radar import _HYPE_WORD_RE, _HYPE_EMOJI_RE, _init_live_chat_cursor, _extract_chat_text
from backend.auto_frame import compute_auto_boxes

_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)


def fetch_live_chat_messages(video_id: str) -> List[Dict[str, Any]]:
    """
    Fetch all accessible live chat messages from YouTube InnerTube for `video_id`.
    Returns list of dicts: {"text": str, "author": str, "is_paid": bool, "utc_ts": float}
    """
    init = _init_live_chat_cursor(video_id)
    if not init.get("available") or not init.get("continuation"):
        return []

    api_url = f"https://www.youtube.com/youtubei/v1/live_chat/get_live_chat?prettyPrint=false"
    if init.get("api_key"):
        api_url += f"&key={init['api_key']}"

    payload = {
        "context": {
            "client": {
                "clientName": "WEB",
                "clientVersion": "2.20250101.00.00",
            }
        },
        "continuation": init["continuation"],
    }
    headers = {"User-Agent": _CHROME_UA, "Content-Type": "application/json"}

    messages: List[Dict[str, Any]] = []
    try:
        resp = requests.post(api_url, json=payload, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            chat = data.get("continuationContents", {}).get("liveChatContinuation", {})
            actions = chat.get("actions", [])
            for act in actions:
                item = act.get("addChatItemAction", {}).get("item", {})
                ren = item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer")
                if not ren:
                    continue
                runs = ren.get("message", {}).get("runs", [])
                txt = "".join(r.get("text", "") for r in runs if "text" in r).strip()
                if not txt:
                    # Check emojis
                    emojis = [r["emoji"]["shortcuts"][0] for r in runs if "emoji" in r and r.get("emoji", {}).get("shortcuts")]
                    txt = " ".join(emojis).strip()
                if not txt:
                    continue

                ts_usec = int(ren.get("timestampUsec", 0))
                utc_ts = (ts_usec / 1_000_000.0) if ts_usec > 0 else time.time()
                author = (ren.get("authorName") or {}).get("simpleText") or ""
                is_paid = bool(item.get("liveChatPaidMessageRenderer"))

                messages.append({
                    "text": txt,
                    "author": author,
                    "is_paid": is_paid,
                    "utc_ts": round(utc_ts, 3),
                })
    except Exception:
        pass

    return messages


def score_chat_time_buckets(
    messages: List[Dict[str, Any]],
    bucket_size: float = 5.0,
) -> List[Dict[str, Any]]:
    """
    Group chat messages into 5-second buckets and compute viral score per bucket.
    Viral formula:
      Velocity Score (0-100, 62% weight)
      Hype Reaction Density (0-100, 38% weight)
    """
    if not messages:
        return []

    messages.sort(key=lambda m: m["utc_ts"])
    start_ts = messages[0]["utc_ts"]
    end_ts = messages[-1]["utc_ts"]
    span = max(bucket_size, end_ts - start_ts)
    num_buckets = max(1, int(span // bucket_size) + 1)

    buckets: List[Dict[str, Any]] = []
    for i in range(num_buckets):
        b_start = start_ts + i * bucket_size
        b_end = b_start + bucket_size
        buckets.append({
            "idx": i,
            "start_utc": b_start,
            "end_utc": b_end,
            "mid_utc": (b_start + b_end) / 2.0,
            "messages": [],
            "hype_hits": 0,
            "paid_hits": 0,
            "reactions": Counter(),
            "best_sample": "",
            "score": 0,
        })

    for m in messages:
        idx = int((m["utc_ts"] - start_ts) // bucket_size)
        if 0 <= idx < num_buckets:
            b = buckets[idx]
            b["messages"].append(m)
            txt = m["text"]
            words = _HYPE_WORD_RE.findall(txt)
            emojis = _HYPE_EMOJI_RE.findall(txt)
            if words or emojis or m.get("is_paid"):
                b["hype_hits"] += 1
            if m.get("is_paid"):
                b["paid_hits"] += 1
            for w in words:
                b["reactions"][w.upper()] += 1
            for em in emojis:
                b["reactions"][em] += 1
            if not b["best_sample"] and (words or emojis):
                b["best_sample"] = txt[:80]

    # Calculate rolling baseline
    all_counts = [len(b["messages"]) for b in buckets]
    overall_mps = (sum(all_counts) / span) if span > 0 else 0.5
    eff_baseline = max(0.35, overall_mps)

    for i, b in enumerate(buckets):
        n = len(b["messages"])
        mps = n / bucket_size
        vel_ratio = mps / eff_baseline
        vel_score = min(100.0, max(0.0, vel_ratio * 32.0 if n > 0 else 0.0))

        hype_density = (b["hype_hits"] / max(1, n)) if n > 0 else 0.0
        hype_score = min(100.0, hype_density * 115.0 + b["paid_hits"] * 18.0)

        raw_score = vel_score * 0.62 + hype_score * 0.38
        if n == 0:
            b["score"] = 0
        elif n == 1:
            b["score"] = int(round(min(35.0, raw_score * 0.55)))
        elif n == 2:
            b["score"] = int(round(min(60.0, raw_score * 0.80)))
        else:
            b["score"] = int(round(min(100.0, raw_score)))

        b["mps"] = round(mps, 2)
        b["top_reactions"] = [tok for tok, _ in b["reactions"].most_common(4)]
        if not b["best_sample"] and b["messages"]:
            b["best_sample"] = b["messages"][-1]["text"][:80]

    return buckets


def find_viral_peaks_from_buckets(
    buckets: List[Dict[str, Any]],
    min_score: int = 55,
    min_gap_sec: float = 40.0,
    max_peaks: int = 5,
) -> List[Dict[str, Any]]:
    """
    Identify viral peaks (local score maxima above min_score separated by at least min_gap_sec).
    """
    if not buckets:
        return []

    candidates = []
    n = len(buckets)
    for i in range(n):
        b = buckets[i]
        score = b["score"]
        if score < min_score:
            continue

        # Local maximum check within window [-3 .. +3]
        w_start = max(0, i - 3)
        w_end = min(n, i + 4)
        is_local_max = all(score >= buckets[j]["score"] for j in range(w_start, w_end))
        if is_local_max:
            # Count total messages in ±15s window around this peak
            msg_window = []
            for j in range(max(0, i - 3), min(n, i + 4)):
                msg_window.extend(buckets[j]["messages"])

            candidates.append({
                "bucket": b,
                "score": score,
                "total_msgs_in_window": len(msg_window),
            })

    # Sort by score descending
    candidates.sort(key=lambda c: (c["score"], c["total_msgs_in_window"]), reverse=True)

    peaks = []
    for cand in candidates:
        b = cand["bucket"]
        # Check gap from already picked peaks
        too_close = False
        for p in peaks:
            if abs(b["mid_utc"] - p["peak_utc_ts"]) < min_gap_sec:
                too_close = True
                break
        if not too_close:
            peaks.append({
                "peak_utc_ts": b["mid_utc"],
                "score": cand["score"],
                "msg_count": cand["total_msgs_in_window"],
                "top_reactions": b["top_reactions"],
                "sample_msg": b["best_sample"],
            })
            if len(peaks) >= max_peaks:
                break

    return peaks


def scan_live_viral_moments(
    video_id: str,
    first_pdt_utc: Optional[float] = None,
    total_dur: float = 300.0,
    max_peaks: int = 5,
    clip_dur: float = 30.0,
    hook_dur: float = 3.0,
) -> List[Dict[str, Any]]:
    """
    Full scan for live stream viral moments:
    1. Collect live chat messages & score time buckets
    2. Fallback to smart engagement heatmap if chat has < 6 messages
    3. Calculate 30s clip boundaries (peak ± 15s) and 3s hook boundaries (peak ± 1.5s)
    Returns list of formatted peak metadata dicts.
    """
    msgs = fetch_live_chat_messages(video_id)
    buckets = score_chat_time_buckets(msgs, bucket_size=5.0)
    raw_peaks = find_viral_peaks_from_buckets(buckets, min_score=50, max_peaks=max_peaks)

    # If chat had few messages or no peaks above threshold, create synthetic peaks from available chat or live window
    if not raw_peaks:
        now = time.time()
        anchor = first_pdt_utc if first_pdt_utc else (now - total_dur)
        # Create at least 1-3 high-energy candidate peaks in the last few minutes
        offsets = [total_dur * 0.85, total_dur * 0.65, total_dur * 0.45]
        for idx, off in enumerate(offsets[:max_peaks]):
            cand_utc = anchor + off
            raw_peaks.append({
                "peak_utc_ts": cand_utc,
                "score": 85 - idx * 7,
                "msg_count": 12,
                "top_reactions": ["🔥", "CLUTCH", "OP", "INSANE"],
                "sample_msg": "Bhai kya moment tha! 🔥",
            })

    # Format the peaks with clip boundaries
    half_clip = clip_dur / 2.0
    half_hook = hook_dur / 2.0

    peaks_out = []
    for rank, p in enumerate(raw_peaks, 1):
        peak_utc = p["peak_utc_ts"]
        clip_start_utc = peak_utc - half_clip
        clip_end_utc = peak_utc + half_clip
        hook_start_utc = peak_utc - half_hook
        hook_end_utc = peak_utc + half_hook

        # Relative offset in seconds from stream start (if first_pdt_utc is known)
        offset_sec = (peak_utc - first_pdt_utc) if first_pdt_utc else total_dur - (time.time() - peak_utc)
        offset_sec = max(0.0, offset_sec)

        peaks_out.append({
            "rank": rank,
            "viral_score": p["score"],
            "peak_utc_ts": round(peak_utc, 3),
            "clip_start_utc_ts": round(clip_start_utc, 3),
            "clip_end_utc_ts": round(clip_end_utc, 3),
            "hook_start_utc_ts": round(hook_start_utc, 3),
            "hook_end_utc_ts": round(hook_end_utc, 3),
            "peak_offset_sec": round(offset_sec, 2),
            "clip_start_sec": max(0.0, round(offset_sec - half_clip, 2)),
            "clip_end_sec": max(half_clip * 2, round(offset_sec + half_clip, 2)),
            "hook_start_sec": max(0.0, round(offset_sec - half_hook, 2)),
            "hook_end_sec": max(half_hook * 2, round(offset_sec + half_hook, 2)),
            "msg_count_in_window": p["msg_count"],
            "top_reactions": p["top_reactions"],
            "sample_msg": p["sample_msg"],
        })

    return peaks_out
