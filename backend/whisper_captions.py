import os
import re
import threading
from pathlib import Path
from typing import List, Dict, Any, Optional

from backend.config import TEMP_DIR

_MODEL_LOCK = threading.Lock()
_WHISPER_MODEL = None

# Hinglish-primed prompt steers Whisper toward natural Roman Hindi + English gaming/creator speech
HINGLISH_PROMPT = (
    "Bhai kya scene hai, let's go! Yaar ye kya clutch tha, "
    "arey bhai dekho abhi kya hone wala hai, awesome gameplay bro, no way!"
)

# High-frequency Hindi words spelled the way Indian creators actually write Hinglish
_COMMON_HINGLISH = {
    "मैं": "main", "मेरा": "mera", "मेरी": "meri", "मेरे": "mere", "मुझे": "mujhe",
    "तुम": "tum", "तुम्हारा": "tumhara", "तुम्हें": "tumhe", "तू": "tu", "तेरा": "tera",
    "आप": "aap", "हम": "hum", "हमारा": "hamara", "वो": "wo", "वह": "woh", "ये": "ye", "यह": "yeh",
    "है": "hai", "हैं": "hain", "था": "tha", "थी": "thi", "थे": "the", "हो": "ho", "हूँ": "hoon", "हूं": "hoon",
    "नहीं": "nahi", "नही": "nahi", "ना": "na", "मत": "mat",
    "क्या": "kya", "क्यों": "kyun", "क्योंकि": "kyunki", "कैसे": "kaise", "कैसा": "kaisa", "कहाँ": "kahan", "कहां": "kahan", "कब": "kab", "कौन": "kaun",
    "की": "ki", "के": "ke", "का": "ka", "को": "ko", "से": "se", "में": "mein", "पर": "par", "पे": "pe",
    "और": "aur", "या": "ya", "तो": "toh", "भी": "bhi", "ही": "hi", "बस": "bas", "अब": "ab", "अभी": "abhi",
    "यहाँ": "yahan", "यहां": "yahan", "वहाँ": "wahan", "वहां": "wahan",
    "भाई": "bhai", "यार": "yaar", "अरे": "arey", "देखो": "dekho", "देख": "dekh",
    "बहुत": "bahut", "ज़्यादा": "zyada", "ज्यादा": "zyada", "थोड़ा": "thoda", "पूरा": "poora",
    "अच्छा": "accha", "बढ़िया": "badhiya", "मस्त": "mast", "सही": "sahi", "गलत": "galat",
    "चलो": "chalo", "रुको": "ruko", "जाओ": "jao", "आओ": "aao", "करो": "karo", "कर": "kar", "रहा": "raha", "रही": "rahi", "रहे": "rahe",
    "गया": "gaya", "गई": "gayi", "गए": "gaye", "दिया": "diya", "लिया": "liya", "किया": "kiya",
    "बोल": "bol", "बोला": "bola", "पता": "pata", "लग": "lag", "लगा": "laga",
    "लोग": "log", "सब": "sab", "कुछ": "kuch", "कोई": "koi", "किसी": "kisi",
    "एक": "ek", "दो": "do", "तीन": "teen", "चार": "chaar", "पाँच": "paanch",
    "जी": "ji", "सी": "si", "सा": "sa", "वाला": "wala", "वाली": "wali", "वाले": "wale",
}

# Accurate Devanagari -> Hinglish (Roman Hindi) phonetic mapping
_INDEPENDENT_VOWELS = {
    "अ": "a", "आ": "aa", "इ": "i", "ई": "ee", "उ": "u", "ऊ": "oo",
    "ऋ": "ri", "ए": "e", "ऐ": "ai", "ओ": "o", "औ": "au", "ऑ": "o", "ऍ": "e",
}

_MATRAS = {
    "ा": "a", "ि": "i", "ी": "ee", "ु": "u", "ू": "oo", "ृ": "ri",
    "े": "e", "ै": "ai", "ो": "o", "ौ": "au", "ॉ": "o", "ॅ": "e",
}

_CONSONANTS = {
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "ng",
    "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "ny",
    "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n",
    "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n",
    "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m",
    "य": "y", "र": "r", "ल": "l", "व": "v", "श": "sh",
    "ष": "sh", "स": "s", "ह": "h", "ळ": "l",
    "क़": "q", "ख़": "kh", "ग़": "gh", "ज़": "z", "ड़": "r", "ढ़": "rh", "फ़": "f", "य़": "y",
}

_NUKTA_MAP = {
    "क": "q", "ख": "kh", "ग": "gh", "ज": "z", "ड": "r", "ढ": "rh", "फ": "f", "य": "y",
}

_DIGITS = {
    "०": "0", "१": "1", "२": "2", "३": "3", "४": "4",
    "५": "5", "६": "6", "७": "7", "८": "8", "९": "9",
}

_HALANT = "्"
_NUKTA = "़"


def _transliterate_devanagari_word(word: str) -> str:
    """Convert a single Devanagari word into natural Hinglish phonetic Roman spelling."""
    # Check common Hinglish dictionary first (preserving leading/trailing punctuation)
    m = re.match(r"^([^\u0900-\u097F]*)([\u0900-\u097F]+)([^\u0900-\u097F]*)$", word)
    if m:
        pre, core, suf = m.groups()
        if core in _COMMON_HINGLISH:
            return pre + _COMMON_HINGLISH[core] + suf.replace("।", ".")

    out: List[str] = []
    n = len(word)
    i = 0

    while i < n:
        ch = word[i]

        if ch in _DIGITS:
            out.append(_DIGITS[ch])
            i += 1
            continue

        if ch in _INDEPENDENT_VOWELS:
            out.append(_INDEPENDENT_VOWELS[ch])
            i += 1
            continue

        if ch in _CONSONANTS:
            base = _CONSONANTS[ch]
            # Handle nukta (e.g. ज़ -> z, फ़ -> f)
            if i + 1 < n and word[i + 1] == _NUKTA:
                base = _NUKTA_MAP.get(ch, base)
                i += 1

            # Look ahead for halant (virama) or vowel matra
            if i + 1 < n and word[i + 1] == _HALANT:
                out.append(base)
                i += 2
                continue

            if i + 1 < n and word[i + 1] in _MATRAS:
                out.append(base + _MATRAS[word[i + 1]])
                i += 2
                continue

            # Inherent 'a' schwa: drop at the end of multi-character words (e.g. राम -> ram, काम -> kam)
            is_word_end = (i == n - 1) or (word[i + 1] in ".,!?;:।")
            if is_word_end and len(out) > 0:
                out.append(base)
            else:
                out.append(base + "a")
            i += 1
            continue

        if ch in ("ं", "ँ"):
            # Nasalization (anusvara / chandrabindu)
            out.append("n")
            i += 1
            continue

        if ch == "ः":
            out.append("h")
            i += 1
            continue

        if ch == "।":
            out.append(".")
            i += 1
            continue

        out.append(ch)
        i += 1

    result = "".join(out)
    # Natural Hinglish polish for very common spellings
    result = re.sub(r"aa$", "a", result) if len(result) > 3 else result
    return result


def devanagari_to_hinglish(text: str) -> str:
    """
    Convert any Devanagari Hindi words inside `text` to clean Hinglish Roman script
    while keeping English words, punctuation, and numbers completely untouched.
    """
    if not text or not re.search(r"[\u0900-\u097F]", text):
        return text

    tokens = re.split(r"(\s+)", text)
    converted = []
    for tok in tokens:
        if re.search(r"[\u0900-\u097F]", tok):
            converted.append(_transliterate_devanagari_word(tok))
        else:
            converted.append(tok)
    return "".join(converted)


def _get_whisper_model():
    """
    Lazily load and cache faster-whisper (`small`) for high-accuracy English + Hindi/Hinglish.
    Uses CUDA float16 on local NVIDIA GPUs, and fast int8 CPU on cloud (HF Spaces).
    """
    global _WHISPER_MODEL
    if _WHISPER_MODEL is not None:
        return _WHISPER_MODEL

    with _MODEL_LOCK:
        if _WHISPER_MODEL is not None:
            return _WHISPER_MODEL

        from faster_whisper import WhisperModel

        model_size = os.environ.get("CLIPYT_WHISPER_MODEL", "small")
        whisper_cache = TEMP_DIR / "whisper_cache"
        whisper_cache.mkdir(parents=True, exist_ok=True)
        use_cuda = False
        # Never touch CUDA outside @spaces.GPU on Hugging Face ZeroGPU
        if not os.environ.get("SPACE_ID"):
            try:
                import ctranslate2
                use_cuda = ctranslate2.get_cuda_device_count() > 0
            except Exception:
                use_cuda = False

        if use_cuda:
            try:
                _WHISPER_MODEL = WhisperModel(
                    model_size,
                    device="cuda",
                    compute_type="float16",
                    download_root=str(whisper_cache),
                )
                return _WHISPER_MODEL
            except Exception:
                pass

        _WHISPER_MODEL = WhisperModel(
            model_size,
            device="cpu",
            compute_type="int8",
            cpu_threads=min(4, os.cpu_count() or 2),
            download_root=str(whisper_cache),
        )
        return _WHISPER_MODEL


def transcribe_clip_hinglish(media_path: Path) -> Optional[List[Dict[str, Any]]]:
    """
    Transcribe a cut clip's audio directly using faster-whisper with:
      - Acoustic word-level timestamps (0.000s sync accuracy!)
      - Silero VAD silence filtering (captions vanish immediately when speaker stops!)
      - Automatic English + Hindi detection converted to clean Hinglish Roman script!
    Returns list of [{"word": str, "start": float, "end": float}, ...].
    """
    try:
        model = _get_whisper_model()
        segments, _info = model.transcribe(
            str(media_path),
            beam_size=2,
            best_of=1,
            word_timestamps=True,
            vad_filter=True,
            vad_parameters={
                "min_silence_duration_ms": 300,
                "speech_pad_ms": 100,
            },
            initial_prompt=HINGLISH_PROMPT,
            condition_on_previous_text=False,
        )

        words_out: List[Dict[str, Any]] = []
        for seg in segments:
            seg_words = getattr(seg, "words", None) or []
            for w in seg_words:
                raw_txt = (getattr(w, "word", "") or "").strip()
                if not raw_txt:
                    continue
                hinglish = devanagari_to_hinglish(raw_txt).strip()
                hinglish = hinglish.replace("{", "(").replace("}", ")")
                if not hinglish:
                    continue

                ws = max(0.0, float(getattr(w, "start", 0.0)))
                we = max(ws + 0.10, float(getattr(w, "end", ws + 0.18)))

                # Split if Whisper grouped multiple space-separated words into one token
                parts = hinglish.split()
                if len(parts) == 1:
                    words_out.append({
                        "word": parts[0],
                        "start": round(ws, 3),
                        "end": round(we, 3),
                    })
                else:
                    span = max(0.15, we - ws)
                    tot_c = max(1, sum(len(p) for p in parts))
                    t_c = ws
                    for p in parts:
                        d = span * (len(p) / tot_c)
                        words_out.append({
                            "word": p,
                            "start": round(t_c, 3),
                            "end": round(t_c + d, 3),
                        })
                        t_c += d

        return words_out if words_out else None
    except Exception:
        return None
