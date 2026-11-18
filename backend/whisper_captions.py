import os
import re
import threading
from pathlib import Path
from typing import List, Dict, Any, Optional

from backend.config import TEMP_DIR

_MODEL_LOCK = threading.Lock()
_TRANSCRIBE_LOCK = threading.Lock()
_WHISPER_MODEL = None
_WHISPER_MODEL_NAME = None
_LAST_WHISPER_ERROR = ""


def get_whisper_status() -> Dict[str, Any]:
    """Return safe diagnostic info about the AI Whisper caption engine."""
    installed = False
    version = None
    try:
        import faster_whisper
        installed = True
        version = getattr(faster_whisper, "__version__", "installed")
    except Exception as e:
        return {
            "installed": False,
            "version": None,
            "model_loaded": None,
            "last_error": str(e)[:200],
        }
    return {
        "installed": installed,
        "version": version,
        "model_loaded": _WHISPER_MODEL_NAME,
        "last_error": _LAST_WHISPER_ERROR[:240] if _LAST_WHISPER_ERROR else "",
    }

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

# Safety-net Urdu/Arabic -> Roman Hinglish mapping (in case any stray Arabic/Urdu chars ever appear)
_URDU_ARABIC_MAP = {
    "ا": "a", "آ": "aa", "أ": "a", "إ": "i", "ب": "b", "پ": "p", "ت": "t", "ٹ": "t",
    "ث": "s", "ج": "j", "چ": "ch", "ح": "h", "خ": "kh", "د": "d", "ڈ": "d", "ذ": "z",
    "ر": "r", "ڑ": "r", "ز": "z", "ژ": "zh", "س": "s", "ش": "sh", "ص": "s", "ض": "z",
    "ط": "t", "ظ": "z", "ع": "a", "غ": "gh", "ف": "f", "ق": "q", "ک": "k", "ك": "k",
    "گ": "g", "ل": "l", "م": "m", "ن": "n", "ں": "n", "و": "o", "ہ": "h", "ھ": "h",
    "ه": "h", "ء": "", "ی": "i", "ي": "i", "ے": "e", "ئ": "i", "ؤ": "o", "ة": "a",
    "۔": ".", "،": ",", "؟": "?",
}


def _arabic_urdu_to_hinglish(text: str) -> str:
    if not text or not re.search(r"[\u0600-\u06FF]", text):
        return text
    out = []
    for ch in text:
        if ch in _URDU_ARABIC_MAP:
            out.append(_URDU_ARABIC_MAP[ch])
        elif "\u0600" <= ch <= "\u06FF":
            # Drop diacritics/harakat
            continue
        else:
            out.append(ch)
    return "".join(out)


def to_english_or_hinglish(text: str) -> str:
    """Guarantee output contains ONLY clean English / Roman Hinglish (never Devanagari or Arabic/Urdu)."""
    if not text:
        return ""
    text = devanagari_to_hinglish(text)
    text = _arabic_urdu_to_hinglish(text)
    # Remove any other stray non-Latin script characters (keep ASCII letters, digits, punctuation, spaces)
    text = re.sub(r"[^\x20-\x7E]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


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
    Lazily load and cache faster-whisper for English + Hindi/Hinglish speech recognition.
    - Locally on NVIDIA GPU: `small` float16 (blazing fast + ultra accurate)
    - On Hugging Face CPU Spaces: `base` int8 (~145MB, multilingual English+Hindi, 3.5x faster on CPU)
    Falls back cleanly across model sizes and cache directories.
    """
    global _WHISPER_MODEL, _WHISPER_MODEL_NAME, _LAST_WHISPER_ERROR
    if _WHISPER_MODEL is not None:
        return _WHISPER_MODEL

    with _MODEL_LOCK:
        if _WHISPER_MODEL is not None:
            return _WHISPER_MODEL

        try:
            from faster_whisper import WhisperModel
        except Exception as e:
            _LAST_WHISPER_ERROR = f"faster-whisper not installed ({e})"
            raise RuntimeError(_LAST_WHISPER_ERROR) from e

        is_hf_space = bool(os.environ.get("SPACE_ID"))
        default_size = "base" if is_hf_space else "small"
        requested_size = os.environ.get("CLIPYT_WHISPER_MODEL", default_size)
        candidate_sizes = [requested_size] + ([s for s in ("base", "tiny") if s != requested_size])

        whisper_cache = TEMP_DIR / "whisper_cache"
        whisper_cache.mkdir(parents=True, exist_ok=True)
        # Ensure huggingface_hub writes lock/download files into writable /tmp on cloud containers
        os.environ.setdefault("HF_HUB_CACHE", str(whisper_cache))

        use_cuda = False
        # Never touch CUDA outside @spaces.GPU on Hugging Face ZeroGPU
        if not is_hf_space:
            try:
                import ctranslate2
                use_cuda = ctranslate2.get_cuda_device_count() > 0
            except Exception:
                use_cuda = False

        last_err = ""
        for model_size in candidate_sizes:
            if use_cuda:
                try:
                    _WHISPER_MODEL = WhisperModel(
                        model_size,
                        device="cuda",
                        compute_type="float16",
                        download_root=str(whisper_cache),
                    )
                    _WHISPER_MODEL_NAME = f"{model_size}-cuda-fp16"
                    _LAST_WHISPER_ERROR = ""
                    return _WHISPER_MODEL
                except Exception as e:
                    last_err = f"cuda:{e}"

            try:
                _WHISPER_MODEL = WhisperModel(
                    model_size,
                    device="cpu",
                    compute_type="int8",
                    cpu_threads=min(4, os.cpu_count() or 2),
                    download_root=str(whisper_cache),
                )
                _WHISPER_MODEL_NAME = f"{model_size}-cpu-int8"
                _LAST_WHISPER_ERROR = ""
                return _WHISPER_MODEL
            except Exception as e:
                last_err = f"cpu:{e}"
                continue

        _LAST_WHISPER_ERROR = f"Whisper model load failed: {last_err[:200]}"
        raise RuntimeError(_LAST_WHISPER_ERROR)


def _extract_words_from_segments(segments) -> List[Dict[str, Any]]:
    words_out: List[Dict[str, Any]] = []
    for seg in segments:
        seg_words = getattr(seg, "words", None) or []
        if seg_words:
            for w in seg_words:
                raw_txt = (getattr(w, "word", "") or "").strip()
                if not raw_txt:
                    continue
                hinglish = to_english_or_hinglish(raw_txt)
                hinglish = hinglish.replace("{", "(").replace("}", ")")
                if not hinglish:
                    continue

                ws = max(0.0, float(getattr(w, "start", 0.0)))
                we = max(ws + 0.10, float(getattr(w, "end", ws + 0.18)))

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
        else:
            # Segment-level fallback if word timestamps weren't produced for a segment
            seg_txt = to_english_or_hinglish((getattr(seg, "text", "") or "").strip())
            seg_txt = seg_txt.replace("{", "(").replace("}", ")")
            parts = seg_txt.split()
            if parts:
                s0 = max(0.0, float(getattr(seg, "start", 0.0)))
                s1 = max(s0 + 0.25, float(getattr(seg, "end", s0 + 0.5)))
                span = s1 - s0
                tot_c = max(1, sum(len(p) for p in parts))
                t_c = s0
                for p in parts:
                    d = span * (len(p) / tot_c)
                    words_out.append({"word": p, "start": round(t_c, 3), "end": round(t_c + d, 3)})
                    t_c += d
    return words_out


def _choose_en_or_hi_language(model, media_path: Path) -> str:
    """
    Strictly choose ONLY 'en' (English) or 'hi' (Hindi -> Hinglish).
    Prevents Whisper from ever mistaking Indian Hindi/Hinglish speech for Urdu ('ur') or Arabic ('ar')!
    """
    try:
        from faster_whisper.audio import decode_audio
        audio = decode_audio(str(media_path), sampling_rate=16000)
        lang, prob, all_probs = model.detect_language(audio)
        probs_map = dict(all_probs or [])
        en_p = float(probs_map.get("en", 0.0))
        hi_ur_p = float(probs_map.get("hi", 0.0)) + float(probs_map.get("ur", 0.0))

        # Only pick English if English clearly dominates; otherwise force 'hi' (converted to Hinglish!)
        if lang == "en" and prob >= 0.55 and en_p >= hi_ur_p:
            return "en"
        if en_p > 0.65 and en_p > (hi_ur_p * 1.5):
            return "en"
        return "hi"
    except Exception:
        return "hi"


def transcribe_clip_hinglish(media_path: Path) -> Optional[List[Dict[str, Any]]]:
    """
    Transcribe a cut clip's audio directly using faster-whisper strictly in English or Hinglish:
      - Forces language='en' or language='hi' (NEVER Urdu/Arabic!)
      - Acoustic word-level timestamps (0.000s sync accuracy!)
      - 2-pass robustness: Pass 1 gentle Silero VAD -> Pass 2 no-VAD retry for noisy gaming streams!
      - Converts any Devanagari Hindi to clean Roman Hinglish!
    Returns list of [{"word": str, "start": float, "end": float}, ...].
    """
    global _LAST_WHISPER_ERROR
    try:
        model = _get_whisper_model()
        target_lang = _choose_en_or_hi_language(model, media_path)

        # Thread-safe GPU/CPU execution guard for 20-30 concurrent users
        with _TRANSCRIBE_LOCK:
            # Pass 1: Gentle Silero VAD (captions vanish cleanly during real silence)
            words_out: List[Dict[str, Any]] = []
            try:
                segments, _info = model.transcribe(
                    str(media_path),
                    language=target_lang,
                    task="transcribe",
                    beam_size=2,
                    best_of=1,
                    word_timestamps=True,
                    vad_filter=True,
                    vad_parameters={
                        "threshold": 0.30,
                        "min_silence_duration_ms": 450,
                        "speech_pad_ms": 200,
                    },
                    initial_prompt=HINGLISH_PROMPT,
                    condition_on_previous_text=False,
                )
                words_out = _extract_words_from_segments(segments)
            except Exception as vad_err:
                _LAST_WHISPER_ERROR = f"VAD pass fallback: {str(vad_err)[:140]}"
                words_out = []

            # Pass 2: No-VAD retry if stream has loud game SFX/music or ONNX VAD errored
            if not words_out:
                segments2, _info2 = model.transcribe(
                    str(media_path),
                    language=target_lang,
                    task="transcribe",
                    beam_size=2,
                    best_of=1,
                    word_timestamps=True,
                    vad_filter=False,
                    no_speech_threshold=0.78,
                    initial_prompt=HINGLISH_PROMPT,
                    condition_on_previous_text=False,
                )
                words_out = _extract_words_from_segments(segments2)

        if words_out:
            _LAST_WHISPER_ERROR = ""
            return words_out

        _LAST_WHISPER_ERROR = "No speech detected in this clip audio."
        return None
    except Exception as e:
        _LAST_WHISPER_ERROR = str(e)[:220]
        return None
