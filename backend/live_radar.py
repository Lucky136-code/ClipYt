import re
import time
import threading
from collections import Counter, deque
from typing import Any, Dict, List, Optional

import requests

_RADAR_LOCK = threading.Lock()
_RADAR_SESSIONS: Dict[str, Dict[str, Any]] = {}
_SESSION_TTL_SEC = 1200  # 20 minutes idle cleanup

_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

# High-signal English + Hinglish gaming/streamer hype patterns
_HYPE_WORD_RE = re.compile(
    r"\b("
    r"w+|op|lol+|lmao+|lmfao+|omg+|wtf+|bro+|bhai+|arey|arre|kya|clutch|"
    r"insane|crazy|clip|clip\s*it|no\s*way|rip|gg+|sheesh|goat|pagal|"
    r"mast|gajab|baap|khatam|mar\s*gaya|abe|saale|haha+|hehe+|xd+|"
    r"o+h+|a+h+|lets\s*go|let's\s*go|boom|dead|cooked|rage|cooked|peak|fire"
    r")\b",
    re.IGNORECASE,
)

_HYPE_EMOJI_RE = re.compile(r"[😂🤣💀😭🔥💯😱🤯⚡👑🐐🎉💥🙌❤️]")


def _cleanup_stale_sessions() -> None:
    now = time.time()
    stale = [vid for vid, s in _RADAR_SESSIONS.items() if (now - s.get("last_poll", 0)) > _SESSION_TTL_SEC]
    for vid in stale:
        _RADAR_SESSIONS.pop(vid, None)


def _init_live_chat_cursor(video_id: str) -> Dict[str, Any]:
    """Fetch initial YouTube Live Chat continuation token and API key."""
    headers = {"User-Agent": _CHROME_UA, "Accept-Language": "en-US,en;q=0.9"}
    try:
        resp = requests.get(
            f"https://www.youtube.com/live_chat?is_popout=1&v={video_id}",
            headers=headers,
            timeout=10,
        )
        if resp.status_code != 200:
            return {"available": False, "reason": "Live chat unavailable"}

        html = resp.text
        if "Chat is disabled" in html or "liveChatRenderer" not in html:
            # Some popout shells still have continuation even without literal string
            pass

        conts = re.findall(
            r'"(?:invalidation|timed)ContinuationData":\{[^}]*?"continuation":"([^"]+)"',
            html,
        )
        if not conts:
            conts = re.findall(r'"continuation":"([^"]+)"', html)
        if not conts:
            return {"available": False, "reason": "Live chat is turned off for this stream"}

        key_m = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', html)
        api_key = key_m.group(1) if key_m else ""
        return {
            "available": True,
            "continuation": conts[-1],
            "api_key": api_key,
            "reason": "",
        }
    except Exception as e:
        return {"available": False, "reason": f"Chat connect error: {str(e)[:80]}"}


def _extract_chat_text(action: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Extract clean text + superchat flag from a YouTube live chat action."""
    item = action.get("addChatItemAction", {}).get("item", {})
    if not item:
        return None
    is_paid = False
    ren = item.get("liveChatTextMessageRenderer")
    if not ren:
        ren = item.get("liveChatPaidMessageRenderer")
        is_paid = bool(ren)
    if not ren:
        return None

    runs = ren.get("message", {}).get("runs") or []
    parts: List[str] = []
    for run in runs:
        if "text" in run:
            parts.append(run["text"])
        elif "emoji" in run:
            em = run["emoji"]
            if em.get("emojiId") and len(em["emojiId"]) <= 4:
                parts.append(em["emojiId"])
            else:
                shortcuts = em.get("shortcuts") or []
                if shortcuts:
                    parts.append(shortcuts[0])
    txt = "".join(parts).strip()
    if not txt:
        return None
    author = (ren.get("authorName") or {}).get("simpleText") or ""
    return {"text": txt, "author": author, "is_paid": is_paid}


def _score_chat_messages(messages: List[Dict[str, Any]], dt_sec: float, baseline_mps: float) -> Dict[str, Any]:
    """
    Compute 0..100 Hype Score from message velocity + hype reaction keywords/emojis + superchats.
    Auto-calibrates to each stream's baseline chat speed (works for 100-viewer & 100k-viewer streams).
    """
    n_msgs = len(messages)
    mps = n_msgs / max(1.0, dt_sec)

    hype_hits = 0
    paid_hits = 0
    reaction_counter: Counter = Counter()
    best_sample = ""
    best_sample_weight = -1

    for m in messages:
        txt = m["text"]
        words = _HYPE_WORD_RE.findall(txt)
        emojis = _HYPE_EMOJI_RE.findall(txt)
        caps_energy = 1 if (len(txt) >= 4 and sum(1 for c in txt if c.isupper()) / max(1, len(txt)) > 0.65) else 0
        exclaim = 1 if ("!!" in txt or "??" in txt) else 0
        paid = 2 if m.get("is_paid") else 0

        w_weight = len(words) * 2 + len(emojis) + caps_energy + exclaim + paid
        if w_weight > 0:
            hype_hits += 1
        if paid:
            paid_hits += 1

        for w in words:
            reaction_counter[w.upper()] += 1
        for em in emojis:
            reaction_counter[em] += 1

        if w_weight > best_sample_weight and 2 <= len(txt) <= 80:
            best_sample_weight = w_weight
            best_sample = txt

    if not best_sample and messages:
        best_sample = messages[-1]["text"][:80]

    # Velocity score relative to rolling baseline
    eff_base = max(0.35, baseline_mps)
    vel_ratio = mps / eff_base
    # Map ratio: 1.0x -> ~32, 2.0x -> ~65, 3.0x+ -> ~95
    vel_score = min(100.0, max(0.0, (vel_ratio * 32.0) if n_msgs > 0 else 0.0))

    # Hype density score (percentage of messages expressing shock/laugh/clutch)
    hype_density = (hype_hits / max(1, n_msgs)) if n_msgs > 0 else 0.0
    hype_score = min(100.0, hype_density * 115.0 + paid_hits * 18.0)

    # Combined score (0..100), suppressed slightly when total messages < 3 to prevent 1-msg false spikes
    raw_score = vel_score * 0.62 + hype_score * 0.38
    if n_msgs == 0:
        combined = 0
    elif n_msgs == 1:
        combined = int(round(min(38.0, raw_score * 0.55)))
    elif n_msgs == 2:
        combined = int(round(min(62.0, raw_score * 0.80)))
    else:
        combined = int(round(min(100.0, raw_score)))

    top_reactions = [tok for tok, _cnt in reaction_counter.most_common(4)]
    return {
        "score": combined,
        "msg_count": n_msgs,
        "mps": round(mps, 2),
        "hype_hits": hype_hits,
        "top_reactions": top_reactions,
        "sample_msg": best_sample,
        "vel_ratio": round(vel_ratio, 2),
    }


def poll_live_chat_radar(video_id: str, reset: bool = False) -> Dict[str, Any]:
    """
    Poll YouTube Live Chat for `video_id`, update rolling timeline buckets,
    auto-detect viral Hype Spikes, and return chart + alert data for Pro Studio.
    """
    now = time.time()

    with _RADAR_LOCK:
        _cleanup_stale_sessions()
        if reset:
            _RADAR_SESSIONS.pop(video_id, None)
        session = _RADAR_SESSIONS.get(video_id)

    if not session:
        init = _init_live_chat_cursor(video_id)
        session = {
            "video_id": video_id,
            "available": init["available"],
            "reason": init["reason"],
            "continuation": init.get("continuation", ""),
            "api_key": init.get("api_key", ""),
            "created_at": now,
            "last_poll": now - 4.0,
            "first_poll_done": False,
            "mps_history": deque(maxlen=24),
            "timeline": deque(maxlen=40),  # ~2-3 minutes of live points
            "spikes": deque(maxlen=8),     # latest detected hype alerts
            "last_spike_ts": 0.0,
            "total_messages": 0,
        }
        with _RADAR_LOCK:
            _RADAR_SESSIONS[video_id] = session

    if not session["available"] or not session["continuation"]:
        return {
            "available": False,
            "reason": session.get("reason") or "Live chat unavailable",
            "timeline": [],
            "spikes": [],
            "current_score": 0,
            "current_mps": 0.0,
            "total_messages": 0,
        }

    # Prevent over-polling YouTube faster than every 2.0s per video
    dt = max(2.0, now - session["last_poll"])
    if (now - session["last_poll"]) >= 2.0 or not session["first_poll_done"]:
        api_url = "https://www.youtube.com/youtubei/v1/live_chat/get_live_chat?prettyPrint=false"
        if session.get("api_key"):
            api_url += f"&key={session['api_key']}"

        payload = {
            "context": {
                "client": {
                    "clientName": "WEB",
                    "clientVersion": "2.20250101.00.00",
                }
            },
            "continuation": session["continuation"],
        }
        headers = {"User-Agent": _CHROME_UA, "Content-Type": "application/json"}

        new_msgs: List[Dict[str, Any]] = []
        try:
            r = requests.post(api_url, json=payload, headers=headers, timeout=8)
            if r.status_code == 200:
                data = r.json()
                chat = data.get("continuationContents", {}).get("liveChatContinuation", {})
                actions = chat.get("actions") or []
                for act in actions:
                    parsed = _extract_chat_text(act)
                    if parsed:
                        new_msgs.append(parsed)

                next_cont = None
                for c in (chat.get("continuations") or []):
                    for k in ("invalidationContinuationData", "timedContinuationData"):
                        if k in c and c[k].get("continuation"):
                            next_cont = c[k]["continuation"]
                            break
                    if next_cont:
                        break
                if next_cont:
                    session["continuation"] = next_cont
        except Exception:
            pass

        # On very first poll, YouTube dumps the last ~15s backlog of chat — treat dt as ~15s
        eff_dt = 15.0 if not session["first_poll_done"] else min(20.0, dt)
        baseline_mps = (
            sum(session["mps_history"]) / len(session["mps_history"])
            if len(session["mps_history"]) > 0
            else max(0.5, len(new_msgs) / max(1.0, eff_dt))
        )

        metrics = _score_chat_messages(new_msgs, eff_dt, baseline_mps)
        session["mps_history"].append(metrics["mps"])
        session["total_messages"] += metrics["msg_count"]
        session["last_poll"] = now
        session["first_poll_done"] = True

        label_t = time.strftime("%H:%M:%S", time.localtime(now))
        point = {
            "ts": round(now, 1),
            "label": label_t,
            "score": metrics["score"],
            "mps": metrics["mps"],
            "msg_count": metrics["msg_count"],
        }
        session["timeline"].append(point)

        # Trigger a Hype Spike alert when chat genuinely surges (with 18s cooldown between alerts)
        is_spike = (
            metrics["score"] >= 70
            and metrics["msg_count"] >= 3
            and (metrics["vel_ratio"] >= 1.55 or metrics["hype_hits"] >= 3)
            and (now - session["last_spike_ts"]) >= 18.0
        )
        if is_spike:
            session["last_spike_ts"] = now
            session["spikes"].appendleft({
                "id": f"spk_{int(now)}",
                "ts": round(now, 1),
                "label": label_t,
                "score": metrics["score"],
                "mps": metrics["mps"],
                "top_reactions": metrics["top_reactions"],
                "sample_msg": metrics["sample_msg"],
            })

    # Format spikes with fresh `seconds_ago` relative to right now
    spikes_out = []
    for spk in list(session["spikes"]):
        ago = max(0, int(round(now - spk["ts"])))
        if ago <= 360:  # Keep alerts from the last 6 minutes (inside Live DVR window)
            spikes_out.append({
                **spk,
                "seconds_ago": ago,
            })

    timeline_list = list(session["timeline"])
    latest = timeline_list[-1] if timeline_list else {"score": 0, "mps": 0.0}

    return {
        "available": True,
        "reason": "",
        "timeline": timeline_list,
        "spikes": spikes_out,
        "current_score": latest["score"],
        "current_mps": latest["mps"],
        "total_messages": session["total_messages"],
    }
