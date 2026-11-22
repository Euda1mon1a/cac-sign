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
            # Split multi-column lines on wide gaps or after colons preceding another label.
            for m in re.finditer(r"[^\s].*?(?=\s{3,}|$)", line):
                seg = m.group().strip()
                if not seg or BLANK.fullmatch(seg):
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
                    spans.append((box, seg))
        offset += len(line)
    return spans


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
    """Parse horizontal rules, hollow checkbox squares, and diagonal marks from a PDF page."""
    import pikepdf
    ctm = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    stack = []
    sub_segs, sub_rects = [], []
    cur = None
    start_pt = None
    rules, boxes, diagonals = [], [], []

    def _flush(stroked, filled):
        nonlocal sub_segs, sub_rects, cur, start_pt
        if stroked or filled:
            for (x0, y0, x1, y1) in sub_segs:
                dx, dy = abs(x1 - x0), abs(y1 - y0)
                if stroked and dy <= 1.5 and dx >= 28.0:
                    rules.append((min(x0, x1), (y0 + y1) / 2.0, max(x0, x1)))
                elif stroked and dx >= 3.0 and dy >= 3.0:
                    diagonals.append((min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)))
            for (x0, y0, x1, y1) in sub_rects:
                w, h = x1 - x0, y1 - y0
                if h <= 3.0 and w >= 28.0:
                    rules.append((x0, (y0 + y1) / 2.0, x1))
                elif stroked and not filled and 6.0 <= w <= 22.0 and 6.0 <= h <= 22.0 and 0.7 <= w / h <= 1.4:
                    boxes.append((x0, y0, x1, y1))
            # Also detect 4-segment closed hollow square drawn via m/l/h/S.
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
        return [], []

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

    # Filter out boxes that have diagonal marks (already checked X or ✓).
    clean_boxes = []
    for bx0, by0, bx1, by1 in boxes:
        marked = any(
            dx0 < bx1 - 1 and dx1 > bx0 + 1 and dy0 < by1 - 1 and dy1 > by0 + 1
            for dx0, dy0, dx1, dy1 in diagonals
        )
        if not marked:
            clean_boxes.append((bx0, by0, bx1, by1))
    return rules, clean_boxes


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
        if rw < 0.055 or rw > 0.72 or ry < 0.07 or ry > 0.92:
            continue
        if _text_on_rule(rx0, ry, rx1):
            continue

        label_text = None
        # 1) Label immediately to the left on the same baseline
        left_hits = []
        for tb, txt in text_spans:
            gap = rx0 - tb[2]
            if -0.012 <= gap <= 0.065 and tb[1] - 0.014 <= ry <= tb[3] + 0.014:
                left_hits.append((gap, txt))
        if left_hits:
            left_hits.sort(key=lambda item: item[0])
            cand_txt = _label(left_hits[0][1])
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
        if not label_text:
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


def _raster_rules_and_boxes(gray):
    """Detect horizontal blank lines (tolerant of mild scan skew) and hollow checkbox squares."""
    W, H = gray.size
    raw = gray.tobytes()
    seg_min = max(18, int(W * 0.025))
    min_run = max(36, int(W * 0.055))
    max_run = int(W * 0.72)
    margin_x, margin_y = int(W * 0.075), int(H * 0.075)

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

    rules_disp = []
    for y0, y1, x0, x1, _, _ in merged:
        length = x1 - x0
        if length < min_run or length > max_run or (y1 - y0) > 12:
            continue
        # Verify clear whitespace above the line (excludes thick bars or text baselines)
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
        rules_disp.append((x0 / W, ((y0 + y1) / 2.0) / H, x1 / W))

    # 3) Detect hollow checkbox squares (side 10..30 px at 1.5x) via top-edge runs
    cb_min, cb_max = max(9, int(W * 0.010)), max(24, int(W * 0.034))
    boxes_disp = []
    for y in range(margin_y, H - margin_y - cb_max):
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

    return rules_disp, boxes_disp


def _detect_scanned_page(path, pi, existing_widgets, candidates):
    cg_img, gray = _render_page_cg_and_gray(path, pi, scale=1.5)
    if cg_img is None or gray is None:
        return []
    ocr_spans = _vision_text_spans(cg_img)
    if not ocr_spans:
        return []
    rules_disp, boxes_disp = _raster_rules_and_boxes(gray)
    out = _pair_rules_and_boxes(rules_disp, boxes_disp, ocr_spans, pi, existing_widgets, candidates)

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
                # 1) Selectable-text printed blanks
                candidates.extend(_detect_selectable_text(page, text, pi, geo, to_display, existing_widgets))
                # 2) Vector-drawn fill lines & hollow checkbox squares paired with PDFKit text
                rules_pdf, boxes_pdf = _extract_vector_primitives(pdf.pages[pi])
                if rules_pdf or boxes_pdf:
                    spans = _text_line_spans(page, text, geo, to_display)
                    rules_disp = []
                    for rx0, ry, rx1 in rules_pdf:
                        b = _clamp_box(to_display(geo, [rx0, ry, rx1, ry + 1.0]))
                        if b:
                            rules_disp.append((b[0], b[3], b[2]))
                    boxes_disp = [b for r in boxes_pdf if (b := _clamp_box(to_display(geo, r)))]
                    candidates.extend(_pair_rules_and_boxes(
                        rules_disp, boxes_disp, spans, pi, existing_widgets, candidates
                    ))
            else:
                # 3) Scanned / image-only page: macOS Vision OCR + raster geometry
                candidates.extend(_detect_scanned_page(path, pi, existing_widgets, candidates))

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
