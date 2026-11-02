"""Extract "HH:MM" times (with optional labels) from typed text or images.

Images are read with Tesseract OCR via pytesseract (must be installed on the
host - see README / Dockerfile). Set OCR_LANG to change languages, e.g.
"eng+rus+uzb".
"""

from __future__ import annotations

import os
import re

import pytesseract
from PIL import Image

OCR_LANG = os.getenv("OCR_LANG", "eng")

# A time token inside OCR'd word text, e.g. "14:30" "9.05" "07-15" (punctuation tolerated)
_TIME_TOKEN = re.compile(r"^(\d{1,2})\s*[:.\-–—]\s*(\d{2})\s*[.,]?$")
# Times anywhere inside typed text
_TIME_IN_TEXT = re.compile(r"\b(\d{1,2})\s*[:.\-]\s*(\d{2})\b")
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
            if _AM_PM.search(line[m.end(): m.end() + 5]) and re.search(r"pm", line[m.end(): m.end() + 5], re.I) \
                    and h < 12:
                h += 12
            key = _hhmm(h, minutes)
            if key in seen:
                continue
            seen.add(key)
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(line)
            label = _clean_label([line[m.end():end]])
            items.append({"time": key, "label": label})
    return items


def parse_times_from_image(path: str) -> list[dict]:
    """OCR an image and return times with the nearest text on the same line as label."""
    img = Image.open(path)
    img = img.convert("L")  # grayscale usually OCRs better

    # Upscale small images (phone screenshots) for better OCR accuracy
    width, height = img.size
    if width < 1200:
        scale = 2 if width < 800 else 1.5
        img = img.resize((int(width * scale), int(height * scale)))

    data = pytesseract.image_to_data(
        img,
        lang=OCR_LANG,
        output_type=pytesseract.Output.DICT,
        config="--psm 6",
    )

    # Group OCR words into visual lines
    lines: dict[tuple, list] = {}
    count = len(data["text"])
    for i in range(count):
        word = (data["text"][i] or "").replace("—", "-").replace("–", "-").strip()
        if not word:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append((data["left"][i], word))

    items: list[dict] = []
    seen: set[str] = set()

    for key in sorted(lines):
        words = sorted(lines[key], key=lambda w: w[0])  # left to right
        texts = [w for _, w in words]
        is_pm = bool(re.search(r"p\.?m\.?", " ".join(texts), re.IGNORECASE))

        for pos, (x, word) in enumerate(words):
            m = _TIME_TOKEN.match(word)
            if not m:
                continue
            h, minutes = int(m.group(1)), int(m.group(2))
            if not _valid(h, minutes):
                continue
            if is_pm and h < 12:
                h += 12
            time_key = _hhmm(h, minutes)
            if time_key in seen:
                continue
            seen.add(time_key)

            # Label: other text on the same line, preferring words right of the time
            right = [w for j, (x2, w) in enumerate(words) if j > pos and not _TIME_TOKEN.match(w)]
            left = [w for j, (x2, w) in enumerate(words) if j < pos and not _TIME_TOKEN.match(w)]
            label = _clean_label(right) or _clean_label(left)

            items.append({"time": time_key, "label": label})

    items.sort(key=lambda it: it["time"])
    return items
