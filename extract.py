"""Extract "HH:MM" times (with labels) from typed text or images.

Two image modes:

1. Prayer-schedule screenshots (Bomdod / Quyosh / Peshin / Asr / Shom / Xufton rows,
   label left, time right — e.g. "Eslatish" screens). Rows are located by their
   KNOWN labels, so OCR only has to read digits reliably.
2. Any other schedule — generic scan for time-shaped text and nearest text on the
   same line is used as the label.

Requires the Tesseract binary (see README / Dockerfile). OCR_LANG selects OCR languages.
"""

from __future__ import annotations

import os
import re

import pytesseract
from PIL import Image

OCR_LANG = os.getenv("OCR_LANG", "eng")

# Quyosh is sunrise (not an alarm) inside prayer apps — excluded unless told otherwise
SKIP_QUYOSH = os.getenv("SKIP_QUYOSH", "1") == "1"
# Reminders fire this many minutes AFTER the prayer time shown on the screenshot
PRAYER_FIRE_OFFSET_MINUTES = int(os.getenv("PRAYER_FIRE_OFFSET_MINUTES", "30"))

# Known row labels of the prayer-schedule format (full-word fuzzy matching)
_PRAYER_NAMES = ["Bomdod", "Quyosh", "Peshin", "Asr", "Shom", "Xufton"]

# A time token inside OCR'd word text, e.g. "14:30" "9.05" "07-15" (punctuation tolerated)
_TIME_TOKEN = re.compile(r"^(\d{1,2})\s*[:.\-–—]\s*(\d{2})\s*[.,]?$")
# Times anywhere inside typed text
_TIME_IN_TEXT = re.compile(r"\b(\d{1,2})\s*[:.\-]\s*(\d{2})\b")
# Digits glued right onto a label token, e.g. "Shom17:22" — no \b, letter-digit
# transitions are not word boundaries
_TIME_EMBEDDED = re.compile(r"(\d{1,2})\s*[:.\-–—]\s*(\d{2})")
_AM_PM = re.compile(r"\b(a\.?m\.?|p\.?m\.?)\b", re.IGNORECASE)

MAX_LABEL_LEN = 64


def _valid(h: int, m: int) -> bool:
    return 0 <= h < 24 and 0 <= m < 60


def _hhmm(h: int, m: int) -> str:
    return f"{h:02d}:{m:02d}"


def _clean_label(words: list[str]) -> str:
    label = " ".join(w for w in words if w)
    label = label.strip(" ,.;:·|…-–—")
    return label[:MAX_LABEL_LEN]


# ------------------------------------------------------------------ OCR

def _prep_image(path: str) -> Image.Image:
    img = Image.open(path).convert("L")  # grayscale usually OCRs better
    width, height = img.size
    if width < 1200:  # upscale small screenshots for better OCR
        scale = 2 if width < 800 else 1.5
        img = img.resize((int(width * scale), int(height * scale)))
    return img


def _ocr_spans(img: Image.Image, tesseract_psm: str) -> list[dict]:
    """Word-level OCR data as spans with positions."""
    data = pytesseract.image_to_data(
        img, lang=OCR_LANG, output_type=pytesseract.Output.DICT, config=tesseract_psm
    )
    spans: list[dict] = []
    for i in range(len(data["text"])):
        text = (data["text"][i] or "").replace("—", "-").replace("–", "-").strip()
        if not text:
            continue
        spans.append(
            {
                "text": text,
                "left": data["left"][i],
                "top": data["top"][i],
                "w": data["width"][i],
                "h": data["height"][i],
                "line": (data["block_num"][i], data["par_num"][i], data["line_num"][i]),
            }
        )
    return spans


def _yc(s: dict) -> float:
    return s["top"] + s["h"] / 2


def _direct_time_spans(spans: list[dict]) -> list[tuple[str, dict]]:
    out = []
    for s in spans:
        m = _TIME_TOKEN.match(s["text"])
        if m:
            h, mi = int(m.group(1)), int(m.group(2))
            if _valid(h, mi):
                out.append((_hhmm(h, mi), s))
            continue
        # time glued into the label token, e.g. "Shom17:22" or "Asr-15:36"
        for mt in _TIME_EMBEDDED.finditer(s["text"]):
            h, mi = int(mt.group(1)), int(mt.group(2))
            if _valid(h, mi):
                out.append((_hhmm(h, mi), s))
    return out


def _pair_time_spans(spans: list[dict]) -> list[tuple[str, dict]]:
    """Catch times OCR split into two plain numbers ('05' + '34')."""
    out = []
    for a, b in zip(spans, spans[1:]):
        ta, tb = a["text"], b["text"]
        if not (ta.isdigit() and len(ta) == 2 and tb.isdigit() and len(tb) == 2):
            continue
        ha, mb = int(ta), int(tb)
        same_row = abs(_yc(a) - _yc(b)) < max(a["h"], b["h"])
        gap = b["left"] - (a["left"] + a["w"])
        if _valid(ha, mb) and same_row and 0 < gap < 2.0 * a["w"]:
            synth = dict(a)
            synth["text"] = f"{ta}:{tb}"
            out.append((_hhmm(ha, mb), synth))
    return out


# ------------------------------------------------- known prayer-row parser

# Lookalike digits that OCR swaps into letters, e.g. "B0mdod" or "Sh0m"
_LOOKALIKE = str.maketrans({
    "0": "o", "1": "l", "2": "z", "3": "e", "4": "a",
    "5": "s", "6": "b", "7": "t", "8": "b", "9": "g",
})


def _lev_within(a: str, b: str, limit: int) -> bool:
    """Is the Levenshtein distance between a and b at most `limit`? (with early exit)"""
    if abs(len(a) - len(b)) > limit:
        return False
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > limit:
            return False
        prev = cur
    return prev[-1] <= limit


def _known_label(text: str) -> str | None:
    """Match OCR'd row title to a known prayer name, tolerating misreads.

    Word-based matching keeps greetings/titles ("Assalomu", "Eslatish") from
    colliding with prayer names.
    """
    letters = "".join(ch for ch in text.lower() if ch.isalnum())
    if len(letters) < 3:
        return None
    letters = letters.translate(_LOOKALIKE)
    for name in _PRAYER_NAMES:
        word = name.lower()
        # tolerate up to 2 misreads for the longer names, 1 for Asr
        if _lev_within(letters[: len(word) + 2], word, 1 if len(word) <= 3 else 2):
            return name
    return None


def _rows_from_labels_and_times(
    label_spans: list[tuple[str, dict]], time_spans: list[tuple[str, dict]]
) -> list[dict]:
    """Nearest-vertical-centre assignment of each time to a known label."""
    span_height = max((s["h"] for _, s in label_spans + time_spans), default=20)

    candidates = []
    for ti, (time_value, ts) in enumerate(time_spans):
        for li, (label, ls) in enumerate(label_spans):
            dist = abs(_yc(ts) - _yc(ls))
            if dist <= 2.0 * span_height:
                candidates.append((dist, li, ti))
    candidates.sort()

    used_labels: set[int] = set()
    used_times: set[int] = set()
    matches: list[tuple[str, str]] = []
    for _, li, ti in candidates:
        if li in used_labels or ti in used_times:
            continue
        used_labels.add(li)
        used_times.add(ti)
        matches.append((label_spans[li][0], time_spans[ti][0]))

    if SKIP_QUYOSH:
        matches = [(t, v) for t, v in matches if t != "Quyosh"]

    items = [{"time": v, "label": t} for t, v in matches]
    items.sort(key=lambda it: it["time"])
    return items


def _shift_time(hhmm: str, minutes: int) -> str:
    """Shift HH:MM by a number of minutes (wraps past midnight)."""
    h, m = (int(x) for x in hhmm.split(":"))
    total = (h * 60 + m + minutes) % (24 * 60)
    return _hhmm(total // 60, total % 60)


def _joined_pairs(spans: list[dict]) -> list[dict]:
    """Offers synthetic spans joining consecutive same-row OCR tokens.

    Catches labels OCR split apart ('Sh' + 'om' -> 'Shom') and label+time bumps.
    Geometry-only concatenation — callers decide what a joined token means.
    """
    out = []
    for a, b in zip(spans, spans[1:]):
        same_row = abs(_yc(a) - _yc(b)) < max(a["h"], b["h"])
        close = b["left"] <= (a["left"] + a["w"] + max(3, a["h"] * 0.6))
        if same_row and close:
            out.append({**a, "text": a["text"] + b["text"]})
    return out


def parse_prayer_schedule(spans: list[dict]) -> list[dict]:
    candidates = spans + _joined_pairs(spans)
    label_spans = [(lbl, s) for s in candidates if (lbl := _known_label(s["text"]))]
    time_spans = _direct_time_spans(candidates) + _pair_time_spans(candidates)
    if not label_spans:
        return []

    rows = _rows_from_labels_and_times(label_spans, time_spans)
    offset = PRAYER_FIRE_OFFSET_MINUTES
    if offset:
        return [
            {
                "time": _shift_time(it["time"], offset),
                "label": f"{it['label']} ({it['time']})",
            }
            for it in rows
        ]
    return rows



# ------------------------------------------------------- generic fallback

def parse_generic_schedule(spans: list[dict]) -> list[dict]:
    """Scan for times; on each visual line, text right of the time (else left) is the label."""
    lines: dict[tuple, list] = {}
    for s in spans:
        lines.setdefault(s["line"], []).append(s)

    items: list[dict] = []
    seen: set[str] = set()
    for key in sorted(lines):
        words = sorted(lines[key], key=lambda s: s["left"])
        joined = " ".join(s["text"] for s in words)
        is_pm = bool(re.search(r"p\.?m\.?", joined, re.IGNORECASE))

        for pos, (time_value, ts) in enumerate(_direct_time_spans(words) + _pair_time_spans(words)):
            h, mi = (int(x) for x in time_value.split(":"))
            if is_pm and h < 12:
                h += 12
                time_value = _hhmm(h, mi)
            if time_value in seen:
                continue
            seen.add(time_value)
            # find ts position inside words list
            idx = words.index(ts) if ts in words else None
            right = [s["text"] for s in words[idx + 1:]] if idx is not None else []
            left = [s["text"] for s in words[:idx]] if idx is not None else []
            label = _clean_label(right) or _clean_label(left)
            items.append({"time": time_value, "label": label})

    items.sort(key=lambda it: it["time"])
    return items


# ---------------------------------------------------------------- public

def parse_times_from_image(path: str) -> list[dict]:
    img = _prep_image(path)
    spans = _ocr_spans(img, "--psm 6")
    _debug_spans(spans)

    items = parse_prayer_schedule(spans)      # fastest, format-aware path
    if items:
        return items
    items = parse_generic_schedule(spans)
    if items:
        return items

    # retry: sparse layouts sometimes OCR better in psm 11
    spans = _ocr_spans(img, "--psm 11")
    _debug_spans(spans)
    return parse_prayer_schedule(spans) or parse_generic_schedule(spans)


def _debug_spans(spans: list[dict]) -> None:
    """OCR_DEBUG=1 dumps raw OCR spans to stderr — for diagnosing runner OCR."""
    if os.getenv("OCR_DEBUG") != "1":
        return
    import sys
    print(f"[ocr-debug] {len(spans)} spans:", file=sys.stderr)
    for s in spans:
        print(
            f"[ocr-debug] text={s['text']!r} x={s['left']} y={s['top']} "
            f"w={s['w']} h={s['h']} line={s['line']}",
            file=sys.stderr,
        )


# ------------------------------------------------------------ typed text

def parse_times_from_text(text: str) -> list[dict]:
    """Parse times from typed text, line by line.

    Example: "09:30 gym 14:00 client meeting" ->
        [{"time": "09:30", "label": "gym"}, {"time": "14:00", "label": "client meeting"}]
    """
    items: list[dict] = []
    seen: set[str] = set()
    for line in text.splitlines():
        matches = list(_TIME_IN_TEXT.finditer(line))
        for idx, m in enumerate(matches):
            h, minutes = int(m.group(1)), int(m.group(2))
            if not _valid(h, minutes):
                continue
            if re.search(r"pm", line[m.end(): m.end() + 5], re.I) and h < 12:
                h += 12
            key = _hhmm(h, minutes)
            if key in seen:
                continue
            seen.add(key)
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(line)
            label = _clean_label([line[m.end():end]])
            items.append({"time": key, "label": label})
    return items
