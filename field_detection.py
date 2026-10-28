"""Suggest blank fields from selectable PDF text, locally through macOS PDFKit.

This uses printed underscores, dashed blanks and empty checkbox glyphs. It does
not infer values or consent, OCR scans, or claim to understand arbitrary forms.
Suggestions are only instantiated in the reviewed private snapshot.
"""
from __future__ import annotations

import re

BLANK = re.compile(r"_{3,}|-{5,}|[\u2610\u25a1]")
SIGNATURE = re.compile(r"\b(signature|sign\s+here|signed\s+by)\b", re.I)


def _range(text, start, end):
    # PDFKit indexes NSString's UTF-16 units, not Python Unicode code points.
    return (len(text[:start].encode("utf-16-le")) // 2,
            len(text[start:end].encode("utf-16-le")) // 2)


def _label(text):
    return " ".join(text.replace("\ufffc", "").split()).strip(" :\t_")[:240]


def _overlaps(box, other):
    left, top = max(box[0], other[0]), max(box[1], other[1])
    right, bottom = min(box[2], other[2]), min(box[3], other[3])
    area = max(0, right - left) * max(0, bottom - top)
    small = min((box[2] - box[0]) * (box[3] - box[1]),
                (other[2] - other[0]) * (other[3] - other[1]))
    return small > 0 and area / small > 0.25


def suggest_fields(path, model, geometry, to_display):
    """Return editable-field suggestions and signature-line candidates; never alter a PDF."""
    if model.get("blocked") or any(f.get("signature") for f in model["fields"]):
        return [], []
    import Quartz
    from Foundation import NSURL
    doc = Quartz.PDFDocument.alloc().initWithURL_(NSURL.fileURLWithPath_(path))
    if doc is None or doc.pageCount() != len(geometry):
        raise ValueError("PDFKit could not read the document pages")
    candidates = []
    for pi, geo in enumerate(geometry):
        if geo["rot"]:
            continue  # placement on rotated pages is not supported by Add text yet
        page = doc.pageAtIndex_(pi)
        text = str(page.string() or "")
        offset, previous = 0, ""
        for line in text.splitlines(keepends=True):
            for match in BLANK.finditer(line):
                token = match.group()
                checkbox = token in ("\u2610", "\u25a1")
                prefix = BLANK.split(line[:match.start()])[-1]
                label = _label(prefix)
                if checkbox:
                    following = text[offset + match.end():].splitlines()
                    label = _label(re.split(r"(?<=[.!?])\s+", " ".join(following[:2]), maxsplit=1)[0])
                    kind = "checkbox"
                else:
                    # A rule below a heading is not enough: a blank on the next
                    # line needs a nearby label ending in a colon.
                    if not label and previous.rstrip().endswith(":"):
                        label = _label(previous)
                    if not label or len(label) > 90 or not any(c.isalpha() for c in label):
                        continue
                    kind = "signature" if SIGNATURE.search(label) else "text"
                start, end = offset + match.start(), offset + match.end()
                selection = page.selectionForRange_(_range(text, start, end))
                if selection is None:
                    continue
                bounds = selection.boundsForPage_(page)
                x, y, width, height = (float(bounds.origin.x), float(bounds.origin.y),
                                       float(bounds.size.width), float(bounds.size.height))
                if width <= 0 or height <= 0:
                    continue
                if checkbox:
                    side = min(width, height)
                    rect = [x, y + (height - side) / 2, x + side, y + (height + side) / 2]
                else:
                    rect = [x + 1, y + 1, x + width - 1, y + height + (8 if kind == "signature" else 5)]
                box = [min(1.0, max(0.0, v)) for v in to_display(geo, rect)]
                if box[2] <= box[0] or box[3] <= box[1]:
                    continue
                if any(w["page"] == pi and _overlaps(box, w["box"])
                       for f in model["fields"] for w in f["widgets"]):
                    continue
                candidates.append({"kind": kind, "label": label or "Checkbox", "page": pi, "box": box})
            if line.strip() and line.strip() != "\ufffc":
                previous = line.strip()
            offset += len(line)
    candidates.sort(key=lambda c: (c["page"], c["box"][1], c["box"][0]))
    fields, signatures = [], []
    names = {f["name"] for f in model["fields"]}
    for item in candidates[:100]:
        if item["kind"] == "signature":
            signatures.append(item)
            continue
        number = 1
        while f"AddedText{number}" in names:
            number += 1
        name = f"AddedText{number}"
        names.add(name)
        fields.append({"name": name, "label": item["label"], "kind": item["kind"],
                       "value": False if item["kind"] == "checkbox" else "",
                       "widgets": [{"page": item["page"], "box": item["box"]}],
                       "readonly": False, "locked": False, "required": False, "multiline": False,
                       "editable": True, "added": True, "detected": True})
    return fields, signatures
