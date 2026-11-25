"""Suggest blank fields from PDF text, vector graphics, and (for scans) macOS Vision OCR.

Detects:
1. Selectable printed blanks (underscores, dashed lines, empty checkbox glyphs, `[ ]`).
2. Vector-drawn horizontal fill lines and hollow checkbox squares paired with PDFKit text.
3. On scanned/image-only pages (near-zero selectable text), macOS Vision OCR labels paired
   with raster blank lines and hollow checkbox squares.

Never alters the source PDF, infers personal values, or selects consent checkboxes.
Suggestions are only instantiated in the reviewed private snapshot.
"""
from __future__ import annotations

import math
import re

BLANK = re.compile(r"_{3,}|-{5,}|[\u2610\u25a1]")
ASCII_CHECKBOX = re.compile(r"(?:^|(?<=\s))\[\s\](?=\s+\S)")
SIGNATURE = re.compile(r"\b(signature|sign\s+here|signed\s+by)\b", re.I)
_VISION_NS: dict | None = None


def _range(text, start, end):
    # PDFKit indexes NSString's UTF-16 units, not Python Unicode code points.
    return (len(text[:start].encode("utf-16-le")) // 2,
            len(text[start:end].encode("utf-16-le")) // 2)


def _label(text):
    return " ".join(text.replace("\ufffc", "").split()).strip(" :\t_[]")[:240]


def _overlaps(box, other, threshold=0.25):
    left, top = max(box[0], other[0]), max(box[1], other[1])
    right, bottom = min(box[2], other[2]), min(box[3], other[3])
    area = max(0.0, right - left) * max(0.0, bottom - top)
    small = min((box[2] - box[0]) * (box[3] - box[1]),
                (other[2] - other[0]) * (other[3] - other[1]))
    return small > 0 and area / small > threshold


def _clamp_box(box):
    b = [round(min(1.0, max(0.0, float(v))), 5) for v in box]
    if any(not math.isfinite(v) for v in b) or b[2] <= b[0] or b[3] <= b[1]:
        return None
    return b


def _already_covered(box, pi, existing_widgets, candidates):
    if any(w["page"] == pi and _overlaps(box, w["box"]) for w in existing_widgets):
        return True
    return any(c["page"] == pi and _overlaps(box, c["box"]) for c in candidates)


# ---------- Layer 1: selectable-text blanks (PDFKit) ----------

def _text_line_spans(page, text, geo, to_display):
    """Return [(display_box, raw_text)] for non-empty selectable-text segments on a page."""
    spans = []
    offset = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped and stripped != "\ufffc":
            tokens = []
            for m in re.finditer(r"\S+", line):
                seg = m.group()
                if BLANK.fullmatch(seg):
                    continue
                sel = page.selectionForRange_(_range(text, offset + m.start(), offset + m.end()))
                if sel is None:
                    continue
                b = sel.boundsForPage_(page)
                if b.size.width <= 0 or b.size.height <= 0:
                    continue
                rect = [float(b.origin.x), float(b.origin.y),
                        float(b.origin.x + b.size.width), float(b.origin.y + b.size.height)]
                box = _clamp_box(to_display(geo, rect))
                if box:
                    tokens.append((box, seg))
            tokens.sort(key=lambda t: (round(t[0][1], 2), t[0][0]))
            cur_box, cur_words = None, []
            for tb, word in tokens:
                prev = cur_words[-1] if cur_words else ""
                colon_split = prev.endswith(":") and word.endswith(":") and len(cur_words) >= 1
                if cur_box is None:
                    cur_box, cur_words = list(tb), [word]
                elif (not colon_split and abs(tb[1] - cur_box[1]) <= 0.012
                      and -0.005 <= tb[0] - cur_box[2] <= 0.016):
                    cur_box = [min(cur_box[0], tb[0]), min(cur_box[1], tb[1]),
                               max(cur_box[2], tb[2]), max(cur_box[3], tb[3])]
                    cur_words.append(word)
                else:
                    spans.append((cur_box, " ".join(cur_words)))
                    cur_box, cur_words = list(tb), [word]
            if cur_box:
                spans.append((cur_box, " ".join(cur_words)))
        offset += len(line)
    return [
        sp for sp in spans
        if not re.fullmatch(r"D\s*R\s*A\s*F\s*T", sp[1].strip()) and (sp[0][3] - sp[0][1]) < 0.042
    ]


def _detect_selectable_text(page, text, pi, geo, to_display, existing_widgets):
    out = []
    offset, previous = 0, ""
    for line in text.splitlines(keepends=True):
        matches = [(m, False) for m in BLANK.finditer(line)]
        matches.extend((m, True) for m in ASCII_CHECKBOX.finditer(line))
        matches.sort(key=lambda pair: pair[0].start())
        for match, is_ascii_cb in matches:
            token = match.group()
            checkbox = is_ascii_cb or token in ("\u2610", "\u25a1")
            prefix = BLANK.split(line[:match.start()])[-1]
            label = _label(prefix)
            if checkbox:
                following = text[offset + match.end():].splitlines()
                first_chunk = following[0] if following else ""
                if is_ascii_cb:
                    first_chunk = re.split(r"\s{2,}|\[\s*[xX]?\s*\]", first_chunk, maxsplit=1)[0]
                    label = _label(first_chunk)
                else:
                    label = _label(re.split(r"(?<=[.!?])\s+", " ".join(following[:2]), maxsplit=1)[0])
                if not label or not any(c.isalpha() for c in label):
                    continue
                kind = "checkbox"
            else:
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
            box = _clamp_box(to_display(geo, rect))
            if not box or _already_covered(box, pi, existing_widgets, out):
                continue
            out.append({"kind": kind, "label": label or "Checkbox", "page": pi, "box": box})
        if line.strip() and line.strip() != "\ufffc":
            previous = line.strip()
        offset += len(line)
    return out


# ---------- Layer 2: PDF vector fill lines & hollow checkbox squares ----------

def _mat_mul(a, b):
    return (
        a[0] * b[0] + a[2] * b[1], a[1] * b[0] + a[3] * b[1],
        a[0] * b[2] + a[2] * b[3], a[1] * b[2] + a[3] * b[3],
        a[0] * b[4] + a[2] * b[5] + a[4], a[1] * b[4] + a[3] * b[5] + a[5],
    )


def _mat_pt(m, x, y):
    return m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]


def _extract_vector_primitives(pdf_page):
    """Parse horizontal/vertical rules, hollow checkbox squares, and diagonal marks from a PDF page."""
    import pikepdf
    ctm = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    stack = []
    sub_segs, sub_rects = [], []
    cur = None
    start_pt = None
    rules, v_rules, short_h, short_v, boxes, diagonals = [], [], [], [], [], []

    def _flush(stroked, filled):
        nonlocal sub_segs, sub_rects, cur, start_pt
        if stroked or filled:
            for (x0, y0, x1, y1) in sub_segs:
                dx, dy = abs(x1 - x0), abs(y1 - y0)
                if stroked and dy <= 1.8 and dx >= 28.0:
                    rules.append((min(x0, x1), (y0 + y1) / 2.0, max(x0, x1)))
                elif stroked and dy <= 1.8 and 6.0 <= dx < 28.0:
                    short_h.append((min(x0, x1), (y0 + y1) / 2.0, max(x0, x1)))
                elif stroked and dx <= 1.8 and dy >= 10.0:
                    v_rules.append(((x0 + x1) / 2.0, min(y0, y1), max(y0, y1)))
                elif stroked and dx <= 1.8 and 6.0 <= dy < 26.0:
                    short_v.append(((x0 + x1) / 2.0, min(y0, y1), max(y0, y1)))
                elif stroked and dx >= 3.0 and dy >= 3.0:
                    diagonals.append((min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)))
            for (x0, y0, x1, y1) in sub_rects:
                w, h = x1 - x0, y1 - y0
                if h <= 3.0 and w >= 28.0:
                    rules.append((x0, (y0 + y1) / 2.0, x1))
                elif h <= 3.0 and 6.0 <= w < 28.0:
                    short_h.append((x0, (y0 + y1) / 2.0, x1))
                elif w <= 3.0 and h >= 10.0:
                    v_rules.append(((x0 + x1) / 2.0, y0, y1))
                    if h <= 26.0:
                        short_v.append(((x0 + x1) / 2.0, y0, y1))
                elif w <= 3.0 and 6.0 <= h < 10.0:
                    short_v.append(((x0 + x1) / 2.0, y0, y1))
                elif stroked and not filled and 6.0 <= w <= 22.0 and 6.0 <= h <= 22.0 and 0.7 <= w / h <= 1.4:
                    boxes.append((x0, y0, x1, y1))
            if stroked and not filled and len(sub_segs) == 4:
                xs = [p for s in sub_segs for p in (s[0], s[2])]
                ys = [p for s in sub_segs for p in (s[1], s[3])]
                w, h = max(xs) - min(xs), max(ys) - min(ys)
                if 6.0 <= w <= 22.0 and 6.0 <= h <= 22.0 and 0.7 <= w / h <= 1.4:
                    if all(abs(s[0] - s[2]) <= 1.5 or abs(s[1] - s[3]) <= 1.5 for s in sub_segs):
                        boxes.append((min(xs), min(ys), max(xs), max(ys)))
        sub_segs, sub_rects, cur, start_pt = [], [], None, None

    try:
        ops = pikepdf.parse_content_stream(pdf_page)
    except Exception:
        return [], [], []

    for operands, operator in ops:
        op = str(operator)
        try:
            if op == "q":
                stack.append(ctm)
            elif op == "Q" and stack:
                ctm = stack.pop()
            elif op == "cm" and len(operands) == 6:
                ctm = _mat_mul(ctm, tuple(float(v) for v in operands))
            elif op == "m" and len(operands) == 2:
                cur = _mat_pt(ctm, float(operands[0]), float(operands[1]))
                start_pt = cur
            elif op == "l" and len(operands) == 2 and cur is not None:
                nxt = _mat_pt(ctm, float(operands[0]), float(operands[1]))
                sub_segs.append((cur[0], cur[1], nxt[0], nxt[1]))
                cur = nxt
            elif op == "h" and cur is not None and start_pt is not None:
                if math.hypot(cur[0] - start_pt[0], cur[1] - start_pt[1]) > 0.5:
                    sub_segs.append((cur[0], cur[1], start_pt[0], start_pt[1]))
                cur = start_pt
            elif op == "re" and len(operands) == 4:
                x, y, w, h = (float(v) for v in operands)
                p1, p2 = _mat_pt(ctm, x, y), _mat_pt(ctm, x + w, y + h)
                sub_rects.append((min(p1[0], p2[0]), min(p1[1], p2[1]),
                                  max(p1[0], p2[0]), max(p1[1], p2[1])))
            elif op in ("S", "s"):
                if op == "s" and cur is not None and start_pt is not None:
                    sub_segs.append((cur[0], cur[1], start_pt[0], start_pt[1]))
                _flush(True, False)
            elif op in ("f", "F", "f*"):
                _flush(False, True)
            elif op in ("B", "B*", "b", "b*"):
                _flush(True, True)
            elif op == "n":
                _flush(False, False)
        except Exception:
            continue

    # Also reconstruct 4-edge hollow checkbox squares drawn as 4 separate thin bars/lines (DoD forms).
    all_v_short = short_v + [v for v in v_rules if v[2] - v[1] <= 26.0]
    for i, (hx0, hy0, hx1) in enumerate(short_h):
        w = hx1 - hx0
        if not (6.0 <= w <= 24.0):
            continue
        for bx0, by0, bx1 in short_h[i + 1:]:
            if abs(bx0 - hx0) > 2.5 or abs(bx1 - hx1) > 2.5:
                continue
            y_lo, y_hi = min(hy0, by0), max(hy0, by0)
            h = y_hi - y_lo
            if not (6.0 <= h <= 24.0 and 0.68 <= w / h <= 1.45):
                continue
            has_l = any(abs(vx - hx0) <= 2.5 and vy0 <= y_lo + 2.5 and vy1 >= y_hi - 2.5 for vx, vy0, vy1 in all_v_short)
            has_r = any(abs(vx - hx1) <= 2.5 and vy0 <= y_lo + 2.5 and vy1 >= y_hi - 2.5 for vx, vy0, vy1 in all_v_short)
            if has_l and has_r:
                cand = (min(hx0, bx0), y_lo, max(hx1, bx1), y_hi)
                if not any(_overlaps(cand, prev, threshold=0.3) for prev in boxes):
                    boxes.append(cand)

    # Filter out boxes that have diagonal marks (already checked X or ✓).
    clean_boxes = []
    for bx0, by0, bx1, by1 in boxes:
        marked = any(
            dx0 < bx1 - 1 and dx1 > bx0 + 1 and dy0 < by1 - 1 and dy1 > by0 + 1
            for dx0, dy0, dx1, dy1 in diagonals
        )
        if not marked:
            clean_boxes.append((bx0, by0, bx1, by1))
    return rules, clean_boxes, v_rules


# ---------- Shared geometry-to-label pairing (display-fraction coordinates) ----------

def _pair_rules_and_boxes(rules_disp, boxes_disp, text_spans, pi, existing_widgets, candidates):
    """Pair display-space horizontal rules [x0, y, x1] and hollow squares [x0, y0, x1, y1]
    with nearby text spans [(box, text)], skipping already-filled lines and non-field dividers."""
    out = []

    def _text_on_rule(rx0, ry, rx1):
        # Skip a rule if non-label text sits directly on it or slices through it.
        for tb, txt in text_spans:
            ov_x = min(rx1, tb[2]) - max(rx0, tb[0])
            if ov_x <= 0.015:
                continue
            s = txt.strip()
            if BLANK.fullmatch(s):
                continue
            # Reject rules slicing through the body of recognized text (e.g. letter baselines on scans).
            if tb[1] + 0.002 <= ry <= tb[3] + 0.004:
                return True
            # A stacked label ending in ':' sits cleanly above the line (handled by above_hits).
            if s.endswith(":") and tb[3] <= ry - 0.006:
                continue
            if ry - 0.014 <= tb[3] <= ry + 0.004:
                return True
        return False

    def _text_inside_box(b):
        for tb, txt in text_spans:
            s = txt.strip()
            if s in ("", "\u2610", "\u25a1", "[]", "[ ]"):
                continue
            # A long OCR line starting right over a hollow square is the checkbox's label, not a check mark.
            if abs(tb[0] - b[0]) <= 0.018 and tb[2] > b[2] + 0.03:
                continue
            if _overlaps(b, tb, threshold=0.35):
                return True
        return False

    # Pair horizontal rules
    for rx0, ry, rx1 in rules_disp:
        rw = rx1 - rx0
        if rw < 0.055 or rw > 0.86 or ry < 0.07 or ry > 0.92:
            continue
        if _text_on_rule(rx0, ry, rx1):
            continue

        label_text = None
        # 1) Label immediately to the left on the same baseline
        left_hits = []
        for tb, txt in text_spans:
            gap = rx0 - tb[2]
            if -0.012 <= gap <= 0.065 and tb[1] - 0.004 <= ry <= tb[3] + 0.014:
                left_hits.append((abs(ry - tb[3]) > 0.012, abs(gap) + abs(ry - tb[3]) * 2.0, txt))
        if left_hits:
            left_hits.sort(key=lambda item: (item[0], item[1]))
            cand_txt = _label(left_hits[0][2])
            if cand_txt and len(cand_txt) <= 90 and any(c.isalpha() for c in cand_txt):
                label_text = cand_txt

        # 2) Label directly above ending with ':' (classic stacked form field)
        if not label_text:
            above_hits = []
            for tb, txt in text_spans:
                vgap = ry - tb[3]
                if 0.006 <= vgap <= 0.038 and txt.rstrip().endswith(":"):
                    if abs(tb[0] - rx0) <= 0.04 or (tb[0] >= rx0 - 0.02 and tb[0] <= rx1 - 0.02):
                        above_hits.append((vgap, txt))
            if above_hits:
                above_hits.sort(key=lambda item: item[0])
                cand_txt = _label(above_hits[0][1])
                if cand_txt and len(cand_txt) <= 90 and any(c.isalpha() for c in cand_txt):
                    label_text = cand_txt

        # 3) Caption directly below the line (e.g. "Applicant Signature", "Date", "Printed Name")
        # Wide rules (> 0.72 of page width) without a left or colon-stacked label are section dividers.
        if not label_text and rw <= 0.72:
            below_hits = []
            for tb, txt in text_spans:
                vgap = tb[1] - ry
                s = txt.strip()
                if s.isupper() and len(s) <= 20:
                    continue  # skip boxed ALL-CAPS stamps like DRAFT / COPY / TEMPLATE
                if 0.0 <= vgap <= 0.024 and tb[0] >= rx0 - 0.03 and tb[2] <= rx1 + 0.08:
                    below_hits.append((vgap, txt))
            if below_hits:
                below_hits.sort(key=lambda item: item[0])
                cand_txt = _label(below_hits[0][1])
                if cand_txt and len(cand_txt) <= 60 and any(c.isalpha() for c in cand_txt):
                    label_text = cand_txt

        if not label_text:
            continue
        kind = "signature" if SIGNATURE.search(label_text) else "text"
        height = 0.028 if kind == "signature" else 0.020
        box = _clamp_box([rx0 + 0.002, ry - height, rx1 - 0.002, ry - 0.001])
        if not box or _already_covered(box, pi, existing_widgets, candidates) or _already_covered(box, pi, [], out):
            continue
        out.append({"kind": kind, "label": label_text, "page": pi, "box": box})

    # Pair hollow checkbox squares
    for b in boxes_disp:
        cb = _clamp_box(b)
        if not cb or _text_inside_box(cb):
            continue
        cy = (cb[1] + cb[3]) / 2.0
        # Prefer label to the right on the same row (or an OCR span whose left edge absorbed the box).
        right_hits = []
        for tb, txt in text_spans:
            if not (tb[1] - 0.012 <= cy <= tb[3] + 0.012):
                continue
            gap = tb[0] - cb[2]
            if -0.004 <= gap <= 0.055:
                right_hits.append((gap, txt))
            elif abs(tb[0] - cb[0]) <= 0.018 and tb[2] > cb[2] + 0.03:
                cleaned = re.sub(r"^(?:[\-\u2022\u2610\u25a1oO0]|\[[^\]]*\])+\s+", "", txt.strip())
                right_hits.append((0.0, cleaned))
        label_text = None
        if right_hits:
            right_hits.sort(key=lambda item: item[0])
            cand_txt = _label(re.split(r"(?<=[.!?])\s+", right_hits[0][1], maxsplit=1)[0])
            if cand_txt and len(cand_txt) <= 120 and any(c.isalpha() for c in cand_txt):
                label_text = cand_txt
        if not label_text:
            continue
        if _already_covered(cb, pi, existing_widgets, candidates) or _already_covered(cb, pi, [], out):
            continue
        out.append({"kind": "checkbox", "label": label_text, "page": pi, "box": cb})

    return out


# ---------- Layer 3: scanned / image-only pages (macOS Vision OCR + raster geometry) ----------

def _load_vision():
    global _VISION_NS
    if _VISION_NS is None:
        import objc
        ns = {}
        objc.loadBundle("Vision", ns, bundle_path="/System/Library/Frameworks/Vision.framework")
        _VISION_NS = ns
    return _VISION_NS


def _render_page_cg_and_gray(path, pi, scale=1.5):
    import io
    import Quartz
    from Foundation import NSMutableData, NSURL
    from PIL import Image
    cg_doc = Quartz.CGPDFDocumentCreateWithURL(NSURL.fileURLWithPath_(path))
    if cg_doc is None:
        return None, None
    page = Quartz.CGPDFDocumentGetPage(cg_doc, pi + 1)
    if page is None:
        return None, None
    crop = Quartz.CGPDFPageGetBoxRect(page, Quartz.kCGPDFCropBox)
    w, h = float(crop.size.width), float(crop.size.height)
    W, H = max(1, int(w * scale)), max(1, int(h * scale))
    ctx = Quartz.CGBitmapContextCreate(
        None, W, H, 8, 0, Quartz.CGColorSpaceCreateDeviceRGB(),
        Quartz.kCGImageAlphaPremultipliedLast,
    )
    Quartz.CGContextSetRGBFillColor(ctx, 1, 1, 1, 1)
    Quartz.CGContextFillRect(ctx, ((0, 0), (W, H)))
    Quartz.CGContextScaleCTM(ctx, scale, scale)
    Quartz.CGContextConcatCTM(
        ctx, Quartz.CGPDFPageGetDrawingTransform(page, Quartz.kCGPDFCropBox, ((0, 0), (w, h)), 0, True)
    )
    Quartz.CGContextDrawPDFPage(ctx, page)
    cg_img = Quartz.CGBitmapContextCreateImage(ctx)
    data = NSMutableData.data()
    dest = Quartz.CGImageDestinationCreateWithData(data, "public.png", 1, None)
    Quartz.CGImageDestinationAddImage(dest, cg_img, None)
    Quartz.CGImageDestinationFinalize(dest)
    gray = Image.open(io.BytesIO(bytes(data))).convert("L")
    return cg_img, gray


def _vision_text_spans(cg_img):
    ns = _load_vision()
    req = ns["VNRecognizeTextRequest"].alloc().init()
    req.setRecognitionLevel_(0)  # accurate
    req.setUsesLanguageCorrection_(False)
    handler = ns["VNImageRequestHandler"].alloc().initWithCGImage_options_(cg_img, None)
    ok = handler.performRequests_error_([req], None)
    if isinstance(ok, tuple):
        ok = ok[0]
    if not ok:
        return []
    spans = []
    for obs in (req.results() or []):
        cands = obs.topCandidates_(1)
        if not cands:
            continue
        text = str(cands[0].string() or "").strip()
        if not text:
            continue
        bb = obs.boundingBox()
        l = float(bb.origin.x)
        r = l + float(bb.size.width)
        t = 1.0 - (float(bb.origin.y) + float(bb.size.height))
        b = 1.0 - float(bb.origin.y)
        box = _clamp_box([l, t, r, b])
        if box:
            spans.append((box, text))
    return spans


_SECTION_PATTERNS = (
    ("Provider", re.compile(
        r"\b(provider\s+information|part\s+c\s*-\s*provider|\d+\.\s*(?:a\.\s*)?provider\s+printed\s+name|"
        r"efmp\s+medical\s+practitioner|physician'?s\s+authentication)\b", re.I)),
    ("Admin", re.compile(
        r"\b(for\s+administrative\s+use\s+only|administrative\s+certification|case\s+manager\s+contact|"
        r"8\.\s*authentication\b|military\s+personnel\s+division)\b", re.I)),
    ("Parent/Guardian", re.compile(
        r"\b(parent\/guardian\s+or\s+person\s+of\s+majority|6\.\s*certification\b|"
        r"authorization\s+for\s+disclosure)\b", re.I)),
    ("Sponsor", re.compile(
        r"^(?:\d+\.?\s*(?:a\.\s*)?(?:sponsor|service\s*member)\b|4\.a\.\s*are\s+both\s+spouses|"
        r"5\.a\.\s*is\s+family\s+member\s+enrolled|part\s+a\s*-\s*soldier|1\.\s*name\s+of\s+soldier)", re.I)),
    ("Patient", re.compile(
        r"(?:^\d+\.?\s*(?:a\.\s*)?(?:exceptional\s+)?family\s+member|\b7\.?\s*family\s+members\b|"
        r"^part\s+b\s*-\s*family\s+member\s+screening)", re.I)),
    ("", re.compile(
        r"^(?:medical\s+summary\b|part\s+[ab]\s*-\s*(?:patient\s+status|required\s+care)|addendum\s+\d)", re.I)),
)
_EXPLICIT_SUBJECT_RE = re.compile(
    r"\b(sponsor|soldier|spouse|family\s+member|patient|provider|physician|practitioner|"
    r"guardian|administrative|case\s+manager|military\s+personnel)\b", re.I,
)


def _merge_segments(h_raw, v_raw):
    hs, vs = [], []
    for x0, y, x1 in sorted(h_raw, key=lambda r: (round(r[1], 3), r[0])):
        for m in hs:
            if abs(y - m[1]) <= 0.0035 and x0 <= m[2] + 0.005 and x1 >= m[0] - 0.005:
                m[0], m[2] = min(m[0], x0), max(m[2], x1)
                break
        else:
            hs.append([x0, y, x1])
    for x, y0, y1 in sorted(v_raw, key=lambda r: (round(r[0], 3), r[1])):
        for m in vs:
            if abs(x - m[0]) <= 0.0035 and y0 <= m[2] + 0.005 and y1 >= m[1] - 0.005:
                m[1], m[2] = min(m[1], y0), max(m[2], y1)
                break
        else:
            vs.append([x, y0, y1])
    return hs, vs


def _pair_grid_cells(h_raw, v_raw, boxes_disp, text_spans, pi, existing_widgets, candidates):
    """Detect blank bordered table/form cells (e.g. DoD DD/DA forms) and return (cell_fields, vs)."""
    hs, vs = _merge_segments(h_raw, v_raw)
    if len(hs) < 2 or len(vs) < 2:
        return [], vs
    clean_spans = [
        (tb, txt) for tb, txt in text_spans
        if not re.fullmatch(r"D\s*R\s*A\s*F\s*T", txt.strip()) and (tb[3] - tb[1]) < 0.042
    ]
    if any("INSTRUCTIONS FOR COMPLETING" in txt.upper() for tb, txt in clean_spans if tb[1] < 0.14):
        return [], vs

    xs = sorted({round(v[0], 4) for v in vs})
    ys = sorted({round(h[1], 4) for h in hs})
    if len(xs) > 40 or len(ys) > 80:
        return [], vs

    def v_covers(x, y0, y1):
        return any(abs(v[0] - x) <= 0.0045 and v[1] <= y0 + 0.006 and v[2] >= y1 - 0.006 for v in vs)

    def h_covers(y, x0, x1):
        return any(abs(h[1] - y) <= 0.0045 and h[0] <= x0 + 0.006 and h[2] >= x1 - 0.006 for h in hs)

    raw_cells = []
    for i, yt in enumerate(ys):
        for yb in ys[i + 1:]:
            ch = yb - yt
            if ch < 0.012 or ch > 0.26:
                continue
            v_act = [x for x in xs if v_covers(x, yt, yb)]
            for j in range(len(v_act) - 1):
                xl, xr = v_act[j], v_act[j + 1]
                if xr - xl < 0.045:
                    continue
                if not (h_covers(yt, xl, xr) and h_covers(yb, xl, xr)):
                    continue
                if any(yt + 0.006 < ym < yb - 0.006 and h_covers(ym, xl, xr) for ym in ys):
                    continue
                raw_cells.append((xl, yt, xr, yb))

    cells = []
    for c in raw_cells:
        area_c = (c[2] - c[0]) * (c[3] - c[1])
        if any(
            o != c and (o[2] - o[0]) * (o[3] - o[1]) < area_c * 0.92 and _overlaps(o, c, threshold=0.5)
            for o in raw_cells
        ):
            continue
        cells.append(c)
    cells.sort(key=lambda c: (round(c[1], 3), round(c[0], 3)))

    sec_marks = []
    for tb, txt in sorted(clean_spans, key=lambda s: (s[0][1], s[0][0])):
        s = txt.strip()
        for tag, pat in _SECTION_PATTERNS:
            if pat.search(s):
                sec_marks.append((tb[1], tag))
                break

    def section_at(y):
        cur_tag = ""
        for sy, tag in sec_marks:
            if sy <= y + 0.008:
                cur_tag = tag
            else:
                break
        return cur_tag

    def spans_in(xl, yt, xr, yb):
        hits = [
            (tb, txt) for tb, txt in clean_spans
            if xl - 0.002 <= (tb[0] + tb[2]) / 2.0 <= xr + 0.002
            and yt - 0.002 <= (tb[1] + tb[3]) / 2.0 <= yb + 0.002
        ]
        hits.sort(key=lambda s: (round(s[0][1], 3), s[0][0]))
        return hits

    out, header_cells = [], []
    for xl, yt, xr, yb in cells:
        cw, ch = xr - xl, yb - yt
        if any(xl <= (b[0] + b[2]) / 2.0 <= xr and yt <= (b[1] + b[3]) / 2.0 <= yb for b in boxes_disp):
            continue
        cspans = spans_in(xl, yt, xr, yb)
        sec = section_at(yt)
        if cspans:
            min_t = min(tb[1] for tb, _ in cspans)
            max_b = max(tb[3] for tb, _ in cspans)
            raw_lbl = _label(" ".join(txt for _, txt in cspans))
            if not raw_lbl or not any(c.isalpha() for c in raw_lbl):
                continue
            if min_t <= yt + 0.020 and yb - max_b >= 0.010 and (max_b - yt) <= max(0.028, ch * 0.62):
                if re.match(r"^(?:[a-z0-9.()]+\s+)*official\s+stamp$", raw_lbl, re.I):
                    continue
                if re.search(r"\b(privacy\s+act\s+statement|authority:|principal\s+purpose)\b", raw_lbl, re.I):
                    continue
                lbl = f"{sec} — {raw_lbl}" if (sec and not _EXPLICIT_SUBJECT_RE.search(raw_lbl)) else raw_lbl
                kind = "signature" if SIGNATURE.search(raw_lbl) else "text"
                box = _clamp_box([xl + 0.003, max_b + 0.001, xr - 0.003, yb - 0.002])
                if box and not _already_covered(box, pi, existing_widgets, candidates) and not _already_covered(box, pi, [], out):
                    out.append({"kind": kind, "label": lbl[:240], "page": pi, "box": box,
                                "multiline": kind == "text" and (yb - max_b) >= 0.048})
            elif ch <= 0.070:
                header_cells.append((xl, yt, xr, yb, raw_lbl))
        elif 0.011 <= ch <= 0.058:
            hdr = next(
                (hlbl for hxl, hyt, hxr, hyb, hlbl in header_cells
                 if abs(hyb - yt) <= 0.005 and abs(hxl - xl) <= 0.008 and abs(hxr - xr) <= 0.008),
                None,
            )
            if hdr and (cw >= 0.095 or re.search(r"\b(dob|date|fmp|rank|mos|ssn|sex|age)\b", hdr, re.I)):
                lbl = f"{sec} — {hdr}" if (sec and not _EXPLICIT_SUBJECT_RE.search(hdr)) else hdr
                box = _clamp_box([xl + 0.003, yt + 0.002, xr - 0.003, yb - 0.002])
                if box and not _already_covered(box, pi, existing_widgets, candidates) and not _already_covered(box, pi, [], out):
                    out.append({"kind": "text", "label": lbl[:240], "page": pi, "box": box, "multiline": False})
    return out, vs


def _is_grid_border(rx0, ry, rx1, vs):
    has_l = any(abs(v[0] - rx0) <= 0.007 and v[1] - 0.007 <= ry <= v[2] + 0.007 for v in vs)
    has_r = any(abs(v[0] - rx1) <= 0.007 and v[1] - 0.007 <= ry <= v[2] + 0.007 for v in vs)
    return has_l and has_r


def _raster_rules_and_boxes(gray):
    """Detect horizontal blank lines (tolerant of mild scan skew) and hollow checkbox squares."""
    W, H = gray.size
    raw = gray.tobytes()
    seg_min = max(18, int(W * 0.025))
    min_run = max(36, int(W * 0.050))
    max_run = int(W * 0.86)
    grid_max = int(W * 0.95)
    margin_x, margin_y = int(W * 0.025), int(H * 0.025)

    # 1) Collect short dark horizontal runs (so mildly skewed lines stitch cleanly)
    row_runs = []
    for y in range(margin_y, H - margin_y):
        off = y * W
        x = margin_x
        end_x = W - margin_x
        while x < end_x:
            if raw[off + x] < 185:
                s = x
                gaps = 0
                while x < end_x:
                    if raw[off + x] < 185:
                        gaps = 0
                    else:
                        gaps += 1
                        if gaps > 1:
                            break
                    x += 1
                e = x - gaps
                if e - s >= seg_min:
                    row_runs.append((y, s, e))
            else:
                x += 1

    # 2) Stitch overlapping or horizontally adjacent segments within <=3 px vertically (handles ±3° skew)
    merged = []  # [y_min, y_max, x_min, x_max, y_left, y_right]
    for y, s, e in row_runs:
        matched = False
        for m in merged:
            near_y = abs(y - m[1]) <= 3 or abs(y - m[0]) <= 3
            ov = min(e, m[3]) - max(s, m[2])
            touch = s <= m[3] + 8 and e >= m[2] - 8
            if near_y and (ov >= 0 or touch):
                if y - m[0] <= 12:
                    m[0] = min(m[0], y)
                    m[1] = max(m[1], y)
                    if s < m[2]:
                        m[2], m[4] = s, y
                    if e > m[3]:
                        m[3], m[5] = e, y
                    matched = True
                    break
        if not matched:
            merged.append([y, y, s, e, y, y])

    rules_disp, grid_h_disp = [], []
    for y0, y1, x0, x1, _, _ in merged:
        length = x1 - x0
        if length < min_run or length > grid_max or (y1 - y0) > 12:
            continue
        ym = ((y0 + y1) / 2.0) / H
        grid_h_disp.append((x0 / W, ym, x1 / W))
        if length > max_run:
            continue
        # Verify clear whitespace above free-standing lines (excludes thick bars or text baselines)
        check_y0 = max(margin_y, y0 - 12)
        check_y1 = max(check_y0 + 1, y0 - 2)
        dark = 0
        total = (check_y1 - check_y0) * max(1, length)
        for cy in range(check_y0, check_y1):
            row_off = cy * W
            for cx in range(x0, x1):
                if raw[row_off + cx] < 165:
                    dark += 1
        if dark / total > 0.08:
            continue
        rules_disp.append((x0 / W, ym, x1 / W))

    # 2b) Collect vertical dark runs for bordered grid cell reconstruction on scans (skip glyph stems <28pt)
    v_seg_min = max(36, int(H * 0.032))
    col_runs = []
    for x in range(margin_x, W - margin_x):
        y, end_y = margin_y, H - margin_y
        while y < end_y:
            if raw[y * W + x] < 185:
                s, gaps = y, 0
                while y < end_y:
                    if raw[y * W + x] < 185:
                        gaps = 0
                    else:
                        gaps += 1
                        if gaps > 1:
                            break
                    y += 1
                e = y - gaps
                if e - s >= v_seg_min:
                    col_runs.append((x, s, e))
            else:
                y += 1
    v_merged = []
    for x, s, e in col_runs:
        for m in v_merged:
            if (abs(x - m[1]) <= 3 or abs(x - m[0]) <= 3) and s <= m[3] + 8 and e >= m[2] - 8 and x - m[0] <= 10:
                m[0], m[1], m[2], m[3] = min(m[0], x), max(m[1], x), min(m[2], s), max(m[3], e)
                break
        else:
            v_merged.append([x, x, s, e])
    grid_v_disp = [(((x0 + x1) / 2.0) / W, y0 / H, y1 / H) for x0, x1, y0, y1 in v_merged if (x1 - x0) <= 10]

    # 3) Detect hollow checkbox squares (side 14..30 px at 1.5x; excludes 10-12pt letter counters like 'D'/'O')
    cb_min, cb_max = max(14, int(W * 0.015)), max(24, int(W * 0.034))
    boxes_disp = []
    for y in range(margin_y, int(H * 0.925) - cb_max):
        off = y * W
        x = margin_x
        end_x = W - margin_x - cb_min
        while x < end_x:
            if raw[off + x] < 200:
                s = x
                while x < W - margin_x and raw[off + x] < 200:
                    x += 1
                w = x - s
                if cb_min <= w <= cb_max:
                    for h in range(max(cb_min, w - 3), min(cb_max, w + 3) + 1):
                        by = y + h - 1
                        if by >= H - margin_y:
                            continue
                        bot_dark = sum(
                            1 for cx in range(s, x)
                            if raw[by * W + cx] < 200 or raw[(by - 1) * W + cx] < 200
                        )
                        if bot_dark < w * 0.70:
                            continue
                        lx0, lx1 = max(0, s - 1), min(W - 1, s + 2)
                        rx0, rx1 = max(0, x - 3), min(W - 1, x)
                        left_dark = sum(
                            1 for cy in range(y, by + 1)
                            if any(raw[cy * W + cx] < 200 for cx in range(lx0, lx1 + 1))
                        )
                        right_dark = sum(
                            1 for cy in range(y, by + 1)
                            if any(raw[cy * W + cx] < 200 for cx in range(rx0, rx1 + 1))
                        )
                        if left_dark < h * 0.70 or right_dark < h * 0.70:
                            continue
                        in_dark, in_tot = 0, 0
                        for cy in range(y + 3, by - 2):
                            roff = cy * W
                            for cx in range(s + 3, x - 3):
                                in_tot += 1
                                if raw[roff + cx] < 175:
                                    in_dark += 1
                        if in_tot > 0 and in_dark / in_tot <= 0.14:
                            cand = [s / W, y / H, x / W, by / H]
                            if not any(_overlaps(cand, prev, threshold=0.2) for prev in boxes_disp):
                                boxes_disp.append(cand)
                            break
            else:
                x += 1

    return rules_disp, boxes_disp, grid_h_disp, grid_v_disp


def _detect_scanned_page(path, pi, existing_widgets, candidates):
    cg_img, gray = _render_page_cg_and_gray(path, pi, scale=1.5)
    if cg_img is None or gray is None:
        return []
    ocr_spans = _vision_text_spans(cg_img)
    if not ocr_spans:
        return []
    if any("INSTRUCTIONS FOR COMPLETING" in txt.upper() for tb, txt in ocr_spans if tb[1] < 0.14):
        return []
    rules_disp, boxes_disp, grid_h_disp, grid_v_disp = _raster_rules_and_boxes(gray)
    grid_out, vs = _pair_grid_cells(grid_h_disp, grid_v_disp, boxes_disp, ocr_spans, pi, existing_widgets, candidates)
    free_rules = [r for r in rules_disp if not _is_grid_border(r[0], r[1], r[2], vs)]
    out = list(grid_out)
    out.extend(_pair_rules_and_boxes(free_rules, boxes_disp, ocr_spans, pi, existing_widgets, candidates + out))

    # Also catch ASCII `[ ]` empty checkboxes recognized verbatim by Vision OCR
    for tb, txt in ocr_spans:
        for m in ASCII_CHECKBOX.finditer(txt):
            after = re.split(r"\s{2,}|\[\s*[xX]?\s*\]", txt[m.end():], maxsplit=1)[0]
            lbl = _label(after)
            if not lbl or not any(c.isalpha() for c in lbl):
                continue
            frac = m.start() / max(1, len(txt))
            end_frac = m.end() / max(1, len(txt))
            bw = tb[2] - tb[0]
            cb = _clamp_box([tb[0] + frac * bw, tb[1], tb[0] + end_frac * bw, tb[3]])
            if cb and not _already_covered(cb, pi, existing_widgets, candidates) and not _already_covered(cb, pi, [], out):
                out.append({"kind": "checkbox", "label": lbl, "page": pi, "box": cb})
    return out


# ---------- Public entry point ----------

def suggest_fields(path, model, geometry, to_display):
    """Return editable-field suggestions and signature-line candidates; never alter a PDF."""
    if model.get("blocked") or any(f.get("signature") for f in model["fields"]):
        return [], []
    import pikepdf
    import Quartz
    from Foundation import NSURL

    doc = Quartz.PDFDocument.alloc().initWithURL_(NSURL.fileURLWithPath_(path))
    if doc is None or doc.pageCount() != len(geometry):
        raise ValueError("PDFKit could not read the document pages")

    existing_widgets = [w for f in model["fields"] for w in f["widgets"]]
    candidates = []

    with pikepdf.open(path) as pdf:
        for pi, geo in enumerate(geometry):
            if geo["rot"]:
                continue  # placement on rotated pages is not supported by Add text yet
            page = doc.pageAtIndex_(pi)
            text = str(page.string() or "")
            alnum_count = len(re.findall(r"[A-Za-z0-9]", text))

            if alnum_count >= 20:
                if "INSTRUCTIONS FOR COMPLETING" in text[:300].upper():
                    continue
                # 1) Selectable-text printed blanks
                candidates.extend(_detect_selectable_text(page, text, pi, geo, to_display, existing_widgets))
                # 2) Vector-drawn grid cells, fill lines & hollow checkbox squares paired with PDFKit text
                rules_pdf, boxes_pdf, v_rules_pdf = _extract_vector_primitives(pdf.pages[pi])
                if rules_pdf or boxes_pdf or v_rules_pdf:
                    spans = _text_line_spans(page, text, geo, to_display)
                    rules_disp = []
                    for rx0, ry, rx1 in rules_pdf:
                        b = _clamp_box(to_display(geo, [rx0, ry, rx1, ry + 1.0]))
                        if b:
                            rules_disp.append((b[0], b[3], b[2]))
                    v_disp = []
                    for vx, vy0, vy1 in v_rules_pdf:
                        b = _clamp_box(to_display(geo, [vx, vy0, vx + 1.0, vy1]))
                        if b:
                            v_disp.append((b[0], b[1], b[3]))
                    boxes_disp = [b for r in boxes_pdf if (b := _clamp_box(to_display(geo, r)))]
                    grid_out, vs = _pair_grid_cells(
                        rules_disp, v_disp, boxes_disp, spans, pi, existing_widgets, candidates
                    )
                    candidates.extend(grid_out)
                    free_rules = [r for r in rules_disp if not _is_grid_border(r[0], r[1], r[2], vs)]
                    candidates.extend(_pair_rules_and_boxes(
                        free_rules, boxes_disp, spans, pi, existing_widgets, candidates
                    ))
            else:
                # 3) Scanned / image-only page: macOS Vision OCR + raster geometry
                candidates.extend(_detect_scanned_page(path, pi, existing_widgets, candidates))

    candidates.sort(key=lambda c: (c["page"], c["box"][1], c["box"][0]))
    fields, signatures = [], []
    names = {f["name"] for f in model["fields"]}
    for item in candidates[:400]:
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
                       "readonly": False, "locked": False, "required": False,
                       "multiline": bool(item.get("multiline")),
                       "editable": True, "added": True, "detected": True})
    return fields, signatures
