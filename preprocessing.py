"""
preprocessing.py - Noise stripping, normalization, language detection, PII redaction.

Design principle: we produce TWO views of every message.

  * `cleaned`     -> light cleanup (forward markers, emoji floods, spacing). Original
                     words/script are untouched. This is what the LLM sees, so slang,
                     Hinglish and Devanagari keep their full meaning.
  * `normalized`  -> aggressive canonical form used ONLY for hashing / fuzzy matching.
                     Spelling variants of common Hinglish words are unified, but no
                     words are deleted, translated or stemmed.

Pure standard library: no heavyweight NLP dependencies, so it is fast and testable.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Iterable, List, Set, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit

# ---------------------------------------------------------------------------
# 1. Noise patterns
# ---------------------------------------------------------------------------

# Characters that are invisible but break hashing. Bidi marks, soft hyphen, BOM, etc.
_INVISIBLE_ALWAYS = "\u200b\u2060\ufeff\u00ad\u200e\u200f\u202a\u202b\u202c\u202d\u202e"
_INVISIBLE_ALWAYS_TABLE = {ord(c): None for c in _INVISIBLE_ALWAYS}
# ZWJ / ZWNJ are meaningful in some Indic conjuncts, so only drop them for hashing.
_INVISIBLE_HASH_TABLE = {**_INVISIBLE_ALWAYS_TABLE, 0x200C: None, 0x200D: None}

_FWD_PATTERNS = [
    # --- English WhatsApp chrome ---
    r"\*?\s*forwarded\s+many\s+times\s*\*?",
    r"\*?\s*forwarded\s+as\s+received\s*\*?",
    r"(?im)^\s*\*?\s*forwarded\s*\*?\s*$",
    r"(?i)\b(?:fwd|fw)\s*:",
    r"(?i)(?:please\s+)?(?:share|forward)\s+(?:(?:this|it)(?:\s+message)?\s+)?(?:with|to)\s+(?:all|everyone|everybody|"
    r"your\s+(?:friends|family|contacts|groups?))(?:\s+and\s+(?:family|friends))?",
    r"(?i)share\s+(?:this\s+)?(?:message\s+)?as\s+much\s+as\s+(?:you\s+)?can",
    r"(?i)<\s*media\s+omitted\s*>",
    r"(?i)this\s+message\s+was\s+deleted",
    # --- Exported-chat headers:  [12/03/24, 10:15 AM] Name:   |  12/03/2024, 10:15 - Name:
    r"(?i)\[\s*\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4},?\s*\d{1,2}:\d{2}(?::\d{2})?\s*(?:am|pm)?\s*\]\s*[^:\n]{1,40}:",
    r"(?i)\d{1,2}/\d{1,2}/\d{2,4},?\s*\d{1,2}:\d{2}\s*(?:am|pm)?\s*-\s*[^:\n]{1,40}:",
    # --- Hinglish "forward kar do" style chain-letter pleas ---
    r"(?i)(?:isse|ise|is\s+message\s+ko|is\s+msg\s+ko)\s+(?:sabko|sab\s+ko|sabhi\s+ko|"
    r"jyada\s+se\s+jyada\s+logo(?:n)?\s+tak)\s+(?:forward|share|bhej\w*)\s*(?:kar\w*)?",
    r"(?i)\b(?:forward|share)\s+(?:kar(?:o|na|iye|en)|karein)\b",
    r"(?i)\b(?:aage|agey|aagey)\s+(?:forward|bhej\w*|badha\w*)\b",
    # --- Devanagari equivalents ---
    r"(?:आगे|अधिक\s+से\s+अधिक\s+लोगों\s+तक)\s*(?:फॉरवर्ड|फारवर्ड|भेजें|भेजिए|शेयर)\s*(?:करें|कीजिए|करो)?",
    r"(?:फॉरवर्ड|फारवर्ड|शेयर)\s*(?:करें|कीजिए|करो|कर\s*दो)",
    r"जनहित\s+में\s+जारी",
]
_FWD_RE = [re.compile(p, re.IGNORECASE) for p in _FWD_PATTERNS]

# Emoji / pictograph ranges (Devanagari, Tamil etc. are NOT in these ranges).
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"  # all emoji planes, flags, symbols
    "\U000E0020-\U000E007F"  # tag characters (subdivision flags)
    "\u2300-\u23FF\u25A0-\u25FF\u2600-\u27BF\u2900-\u297F\u2B00-\u2BFF"
    "\uFE00-\uFE0F\u20E3\u00A9\u00AE\u203C\u2049\u2122\u2139\u3030\u303D"
    "]+"
)
_EMOJI_RUN_RE = re.compile(
    "((?:[\U0001F000-\U0001FAFF\u2300-\u23FF\u25A0-\u25FF\u2600-\u27BF\u2900-\u297F\u2B00-\u2BFF"
    "\uFE00-\uFE0F\u200D\u20E3\u00A9\u00AE]" + r"\s*){2,})"
)

_URL_RE = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>\"']+")
_TRACKING_PARAMS = {
    "fbclid", "gclid", "igshid", "si", "ref", "ref_src", "mc_cid", "mc_eid", "feature",
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "s", "t",
}

# ---------------------------------------------------------------------------
# 2. Hinglish / slang canonicalisation (spelling variants only - never deletes words)
# ---------------------------------------------------------------------------
# Extend this dictionary freely; it is deliberately small and unambiguous.
_VARIANTS = {
    "nahin": "nahi", "nhi": "nahi", "nahee": "nahi",
    "hain": "hai", "hei": "hai",
    "kyaa": "kya", "kia": "kya", "kyu": "kyun", "kyon": "kyun", "kyoon": "kyun",
    "bhee": "bhi",
    "yeh": "ye", "yah": "ye", "woh": "wo", "vo": "wo",
    "jhoot": "jhooth", "jhuth": "jhooth", "jhut": "jhooth", "jhutha": "jhootha",
    "afvah": "afwah", "afwaah": "afwah", "rumour": "rumor", "rumours": "rumor", "rumors": "rumor",
    "sarkaar": "sarkar", "paise": "paisa", "paisey": "paisa", "rupaye": "rupee",
    "rupees": "rupee", "rupiya": "rupee", "bhejo": "bhej", "bhejna": "bhej", "bhejiye": "bhej",
    "bhejein": "bhej", "plz": "please", "pls": "please", "plss": "please", "pliz": "please",
    "msg": "message", "mssg": "message", "msgs": "message", "govt": "government",
    "watsapp": "whatsapp", "whatsap": "whatsapp", "wtsp": "whatsapp",
    "ur": "your", "u": "you", "abhi": "abhi", "abi": "abhi", "jaldi": "jaldi", "jldi": "jaldi",
    "mat": "mat", "sabhi": "sabhi", "sbhi": "sabhi", "bohot": "bahut", "bahot": "bahut",
    "bohut": "bahut", "bhut": "bahut",
}

_HINGLISH_MARKERS = {
    "hai", "hain", "hoga", "hogi", "honge", "nahi", "nahin", "nhi", "kya", "kyun", "kyu", "mein",
    "aur", "yeh", "ye", "woh", "wo", "bhi", "toh", "kar", "karo", "kare", "karna", "karein",
    "mat", "sab", "sabko", "jaldi", "abhi", "aaj", "kal", "ko", "ka", "ki", "ke", "se", "pe",
    "liye", "lie", "milega", "milegi", "mil", "raha", "rahi", "rahe", "rha", "gaya", "gayi",
    "gaye", "wala", "wali", "bhej", "bhejo", "sarkar", "paisa", "log", "dost", "bhai", "dhyan",
    "savdhan", "khabar", "sach", "jhooth", "jhoot", "afwah", "bina", "sirf", "bahut", "bohot",
    "zyada", "jyada", "agar", "lekin", "magar", "isse", "iska", "uska", "unka", "hum", "tum",
    "aap", "mera", "meri", "tera", "apna", "apni", "kisi", "koi", "kuch", "sabhi", "hoga",
    "bata", "batao", "dekho", "suno", "chahiye", "padega", "padegi", "band", "chalu", "naya",
}

_NEGATIONS = {
    "not", "no", "never", "cannot", "cant", "wont", "dont", "doesnt", "isnt", "wasnt", "arent",
    "didnt", "nahi", "mat", "without", "false", "fake", "hoax", "jhooth", "jhootha", "galat",
    "nahin", "नहीं", "नही", "मत", "गलत", "झूठ", "झूठा", "बिना", "नकली", "अफवाह", "afwah",
}

_STOPWORDS = {
    "the", "and", "for", "are", "was", "this", "that", "with", "have", "has", "you", "your", "from",
    "will", "can", "all", "but", "not", "his", "her", "its", "who", "how", "why", "when", "what",
    "hai", "hain", "ka", "ki", "ke", "ko", "se", "me", "mein", "pe", "par", "ye", "wo", "aur",
    "bhi", "toh", "kya", "ho", "hi", "na", "to", "of", "in", "on", "is", "it", "at", "be", "by",
    "का", "की", "के", "को", "से", "में", "पर", "है", "हैं", "और", "भी", "तो", "यह", "ये", "वो", "एक",
}

# ---------------------------------------------------------------------------
# 3. Language / script detection (cheap heuristics, good enough to steer the LLM)
# ---------------------------------------------------------------------------
_SCRIPT_RANGES = [
    ("devanagari", 0x0900, 0x097F),
    ("bengali", 0x0980, 0x09FF),
    ("gurmukhi", 0x0A00, 0x0A7F),
    ("gujarati", 0x0A80, 0x0AFF),
    ("odia", 0x0B00, 0x0B7F),
    ("tamil", 0x0B80, 0x0BFF),
    ("telugu", 0x0C00, 0x0C7F),
    ("kannada", 0x0C80, 0x0CFF),
    ("malayalam", 0x0D00, 0x0D7F),
    ("urdu", 0x0600, 0x06FF),
    ("urdu", 0x0750, 0x077F),
]

LANGUAGE_LABELS = {
    "english": "English",
    "hinglish": "Hinglish (Hindi written in Roman/English letters, mixed with English words)",
    "devanagari": "the same language as the user's message, written in Devanagari script (Hindi/Marathi/etc.)",
    "bengali": "Bengali (Bangla script)",
    "gurmukhi": "Punjabi (Gurmukhi script)",
    "gujarati": "Gujarati (Gujarati script)",
    "odia": "Odia (Odia script)",
    "tamil": "Tamil (Tamil script)",
    "telugu": "Telugu (Telugu script)",
    "kannada": "Kannada (Kannada script)",
    "malayalam": "Malayalam (Malayalam script)",
    "urdu": "Urdu (Perso-Arabic script)",
    "mixed": "the dominant language and script of the user's message",
}


def detect_language(text: str) -> Tuple[str, str]:
    """Return (language_tag, human_readable_label). Heuristic, not a classifier."""
    counts: dict = {}
    latin = 0
    for ch in text:
        if not ch.isalpha():
            continue
        o = ord(ch)
        if o < 0x250:
            latin += 1
            continue
        for name, lo, hi in _SCRIPT_RANGES:
            if lo <= o <= hi:
                counts[name] = counts.get(name, 0) + 1
                break
    indic_total = sum(counts.values())
    total = indic_total + latin
    if total == 0:
        return "english", LANGUAGE_LABELS["english"]
    if indic_total:
        top, n = max(counts.items(), key=lambda kv: kv[1])
        if n / total >= 0.5:
            return top, LANGUAGE_LABELS[top]
        if latin / total < 0.5:
            return "mixed", LANGUAGE_LABELS["mixed"]
    # Latin script dominant: English vs romanised Hindi
    words = re.findall(r"[a-z']+", text.lower())
    if words:
        hits = sum(1 for w in words if w in _HINGLISH_MARKERS)
        if hits / len(words) >= 0.15 and hits >= 2:
            return "hinglish", LANGUAGE_LABELS["hinglish"]
    return "english", LANGUAGE_LABELS["english"]


# ---------------------------------------------------------------------------
# 4. Helpers
# ---------------------------------------------------------------------------
def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", "ignore")).hexdigest()


def truncate(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def strip_forwarding_noise(text: str) -> str:
    """Remove WhatsApp forwarding markers / chain-letter pleas / chat-export headers."""
    for rx in _FWD_RE:
        text = rx.sub(" ", text)
    return text


def _ascii_digits(text: str) -> str:
    """Map any Unicode decimal digit (Devanagari, Bengali, Arabic-Indic...) to ASCII."""
    out = []
    for ch in text:
        if ch.isdigit() and not ch.isascii():
            try:
                out.append(str(unicodedata.decimal(ch)))
                continue
            except ValueError:
                pass
        out.append(ch)
    return "".join(out)


def _canonical_url(match: "re.Match[str]") -> str:
    raw = match.group(0).rstrip(".,;:!?)]}>\"'")
    trailing = match.group(0)[len(raw):]
    try:
        parsed = urlsplit(raw if raw.lower().startswith("http") else "http://" + raw)
        host = parsed.netloc.lower().removeprefix("www.")
        query = urlencode(
            sorted((k, v) for k, v in parse_qsl(parsed.query) if k.lower() not in _TRACKING_PARAMS)
        )
        path = parsed.path.rstrip("/")
        return f" {host}{path}{'?' + query if query else ''} {trailing}"
    except ValueError:
        return f" {raw} {trailing}"


def index_tokens(tokens: Iterable[str]) -> List[str]:
    """Tokens worth indexing for candidate retrieval (drops stop-words & very short tokens)."""
    seen, out = set(), []
    for t in tokens:
        if len(t) > 2 and t not in _STOPWORDS and t not in seen:
            seen.add(t)
            out.append(t)
    return out


# ---------------------------------------------------------------------------
# 5. Normalization
# ---------------------------------------------------------------------------
def normalize_text(text: str) -> str:
    """
    Aggressive canonical form for hashing / fuzzy matching.

    Keeps: letters, combining marks (Indic matras!), digits, decimal points inside numbers.
    Drops: emojis, punctuation, symbols, forwarding noise, tracking params.
    """
    t = strip_forwarding_noise(text)
    t = unicodedata.normalize("NFKC", t)
    t = t.translate(_INVISIBLE_HASH_TABLE)
    t = _URL_RE.sub(_canonical_url, t)
    t = _ascii_digits(t)
    t = t.casefold()
    t = _EMOJI_RE.sub(" ", t)
    t = re.sub(r"(?<=\d),(?=\d{2,3}\b)", "", t)               # 1,00,000 -> 100000
    t = re.sub(r"[₹]|\b(?:rs|inr)\b\.?", " rs ", t)             # unify rupee notations
    t = t.replace("%", " percent ").replace("°", " degree ")

    out: List[str] = []
    n = len(t)
    for i, ch in enumerate(t):
        cat = unicodedata.category(ch)
        if cat[0] in "LMN":
            out.append(ch)
        elif ch == "." and 0 < i < n - 1 and t[i - 1].isdigit() and t[i + 1].isdigit():
            out.append(".")
        elif ch in "'’`" and 0 < i < n - 1 and t[i - 1].isalpha() and t[i + 1].isalpha():
            continue                                             # don't -> dont
        else:
            out.append(" ")
    t = "".join(out)
    t = re.sub(r"([a-z])\1{2,}", r"\1\1", t)                    # sooooo -> soo
    words = [_VARIANTS.get(w, w) for w in t.split()]
    return " ".join(words)


def clean_for_llm(text: str, max_chars: int = 4000) -> Tuple[str, bool]:
    """
    Light cleanup that PRESERVES wording, script and slang. Returns (text, was_truncated).
    Keeps head+tail when the text is too long so a claim at the end of a long forward survives.
    """
    t = strip_forwarding_noise(text)
    t = unicodedata.normalize("NFC", t).translate(_INVISIBLE_ALWAYS_TABLE)
    t = _EMOJI_RUN_RE.sub(lambda m: m.group(1).strip()[:1] + " ", t)   # emoji flood -> 1 emoji
    t = re.sub(r"([!?.,*_~=\-#])\1{2,}", r"\1\1", t)                   # !!!!!! -> !!
    t = re.sub(r"(\w)\1{4,}", r"\1\1\1", t)                            # sooooooo -> sooo
    t = re.sub(r"[ \t\u00a0\u3000]+", " ", t)
    t = re.sub(r"\s*\n\s*", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    if len(t) > max_chars:
        head, tail = int(max_chars * 0.7), int(max_chars * 0.3)
        return t[:head].rstrip() + "\n[…]\n" + t[-tail:].lstrip(), True
    return t, False


@dataclass(frozen=True)
class PreprocessedText:
    original: str
    cleaned: str                 # for the LLM
    normalized: str              # for hashing / matching
    tokens: Tuple[str, ...]
    text_hash: str
    language_tag: str
    language_label: str
    truncated: bool = False
    numbers: frozenset = field(default_factory=frozenset)
    negations: frozenset = field(default_factory=frozenset)

    @property
    def is_empty(self) -> bool:
        return len(self.tokens) == 0


def preprocess(text: str, max_chars: int = 4000) -> PreprocessedText:
    text = text or ""
    cleaned, truncated = clean_for_llm(text, max_chars)
    normalized = normalize_text(cleaned if truncated else text)
    tokens = tuple(normalized.split())
    tag, label = detect_language(cleaned)
    return PreprocessedText(
        original=text,
        cleaned=cleaned,
        normalized=normalized,
        tokens=tokens,
        text_hash=sha256_hex(normalized),
        language_tag=tag,
        language_label=label,
        truncated=truncated,
        numbers=frozenset(t for t in tokens if any(c.isdigit() for c in t)),
        negations=frozenset(t for t in tokens if t in _NEGATIONS),
    )


# ---------------------------------------------------------------------------
# 6. Fuzzy similarity with safety guards
# ---------------------------------------------------------------------------
def fuzzy_guards_ok(a_tokens: Iterable[str], b_tokens: Iterable[str]) -> bool:
    """
    Two messages that look 95% identical can still mean opposite things
    ("is true" vs "is NOT true") or carry different facts ("Rs 500" vs "Rs 5000",
    a different date or phone number). Never fuzzy-match across those differences.
    """
    a, b = set(a_tokens), set(b_tokens)
    num_a = {t for t in a if any(c.isdigit() for c in t)}
    num_b = {t for t in b if any(c.isdigit() for c in t)}
    if num_a != num_b:
        return False
    return {t for t in a if t in _NEGATIONS} == {t for t in b if t in _NEGATIONS}


def fuzzy_similarity(a_norm: str, b_norm: str) -> float:
    """Blend of token-set Jaccard and character-sequence ratio, in [0, 1]."""
    ta, tb = set(a_norm.split()), set(b_norm.split())
    if not ta or not tb:
        return 0.0
    jac = len(ta & tb) / len(ta | tb)
    if jac < 0.5:                       # cheap early exit, can never reach the threshold
        return jac
    sm = SequenceMatcher(None, a_norm[:800], b_norm[:800], autojunk=False)
    return 0.5 * jac + 0.5 * sm.ratio()


# ---------------------------------------------------------------------------
# 7. PII redaction (for audit logs / admin samples - never for matching)
# ---------------------------------------------------------------------------
_HONORIFIC = r"(?:mr|mrs|ms|miss|dr|shri|shree|smt|sri|sh|prof|er|adv|capt|col|maj|gen|sir|madam)"
_PII_RULES: List[Tuple["re.Pattern[str]", str]] = [
    (re.compile(r"(?i)\botp\b\D{0,12}\d{4,8}"), "[OTP]"),
    (re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+"), "[EMAIL]"),
    (re.compile(r"\b[\w.\-]{2,64}@[A-Za-z]{2,32}\b"), "[UPI_ID]"),
    (re.compile(r"\b(?:\d[ \-]?){13,19}\b"), "[CARD_OR_ID]"),
    (re.compile(r"\b\d{4}[ \-]?\d{4}[ \-]?\d{4}\b"), "[AADHAAR]"),
    (re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"), "[PAN]"),
    (re.compile(r"(?<!\d)(?:\+?91[\s\-]?|0)?[6-9]\d{4}[\s\-]?\d{5}(?!\d)"), "[PHONE]"),
    (re.compile(r"\+\d{1,3}[\s\-]?\(?\d{2,4}\)?[\s\-]?\d{3,4}[\s\-]?\d{3,4}"), "[PHONE]"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "[IP]"),
    # Heuristic name masking (no NER model): honorific + Capitalised words, self-introductions.
    (re.compile(rf"\b(?i:{_HONORIFIC})\.?\s+[A-Z][\w'’\-]+(?:\s+[A-Z][\w'’\-]+){{0,3}}"), "[NAME]"),
    (re.compile(r"(?:श्री|श्रीमती|श्रीमान|डॉ\.?|सुश्री)\s+[\u0900-\u097F]+(?:\s+[\u0900-\u097F]+){0,2}"), "[NAME]"),
    (re.compile(r"\b(?i:my\s+name\s+is|mera\s+naam|mera\s+nam|naam\s+hai)\s+[A-Za-z][\w'’\-]+(?:\s+[A-Za-z][\w'’\-]+){0,2}"),
     "[NAME]"),
    (re.compile(r"(?:मेरा\s+नाम|मेरा\s+नाम\s+है)\s+[\u0900-\u097F]+(?:\s+[\u0900-\u097F]+){0,2}"), "[NAME]"),
]


def redact_pii(text: str) -> str:
    """
    Best-effort masking of phone numbers, e-mails, UPI IDs, Aadhaar/PAN/card numbers, OTPs,
    IPs and heuristically detected personal names.

    Limitation: regexes cannot find *every* name. For regulated deployments plug a
    NER model (e.g. Microsoft Presidio / spaCy) into this function - the call sites
    will not need to change.
    """
    if not text:
        return ""
    for rx, repl in _PII_RULES:
        text = rx.sub(repl, text)
    return text
