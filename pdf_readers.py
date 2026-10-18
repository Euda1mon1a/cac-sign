"""
What the PDF readers see: page rendering, and whether the readers agree on a file.

Used by cac_sign_editor.py, for the review page and its pixel check, and by
cac_sign.py, as a pre-flight refusal. Three independent readers are compared: qpdf
(pikepdf), pyHanko's strict parser and Apple's CoreGraphics. Acrobat is a fourth
reader this cannot run. A file they read differently can look one way here and
another way to the recipient, so it is not signed. Nor is a file that hides on
screen something it prints: the review shows the screen view, and a printout is
what gets filed.
"""
from __future__ import annotations
import functools, hashlib, io, os

import cac_sign


@functools.lru_cache(maxsize=8)
def _flattened(path, *stat_key):
    """The PDF with every on-screen annotation drawn into the page, as other viewers show it."""
    import pikepdf
    with pikepdf.open(path) as pdf:
        pdf.flatten_annotations("screen")  # in memory only; the file is never rewritten
        buf = io.BytesIO()
        pdf.save(buf)
    return buf.getvalue()


def _cg_doc(data):
    import Quartz
    from Foundation import NSData
    provider = Quartz.CGDataProviderCreateWithCFData(NSData.dataWithBytes_length_(data, len(data)))
    return Quartz.CGPDFDocumentCreateWithProvider(provider)


def _render(doc, index, scale):
    """PNG of one page of a CoreGraphics document (page content only; CG draws no annotations)."""
    import Quartz
    from Foundation import NSMutableData
    page = Quartz.CGPDFDocumentGetPage(doc, index + 1)
    if page is None:
        raise IndexError("no such page")
    box = Quartz.CGPDFPageGetBoxRect(page, Quartz.kCGPDFCropBox)
    w, h = box.size.width, box.size.height
    if Quartz.CGPDFPageGetRotationAngle(page) % 180:
        w, h = h, w
    W, H = int(w * scale), int(h * scale)
    ctx = Quartz.CGBitmapContextCreate(None, W, H, 8, 0, Quartz.CGColorSpaceCreateDeviceRGB(),
                                       Quartz.kCGImageAlphaPremultipliedLast)
    Quartz.CGContextSetRGBFillColor(ctx, 1, 1, 1, 1)
    Quartz.CGContextFillRect(ctx, ((0, 0), (W, H)))
    Quartz.CGContextScaleCTM(ctx, scale, scale)
    Quartz.CGContextConcatCTM(ctx, Quartz.CGPDFPageGetDrawingTransform(page, Quartz.kCGPDFCropBox,
                                                                       ((0, 0), (w, h)), 0, True))
    Quartz.CGContextDrawPDFPage(ctx, page)
    out = NSMutableData.data()
    dest = Quartz.CGImageDestinationCreateWithData(out, "public.png", 1, None)
    Quartz.CGImageDestinationAddImage(dest, Quartz.CGBitmapContextCreateImage(ctx), None)
    Quartz.CGImageDestinationFinalize(dest)
    return bytes(out)


def render_page(path, index, scale=2.0):
    """PNG of one page as viewers will show it: page content plus annotation appearances."""
    return _render(_cg_doc(_flattened(path, *_stat_key(path))), index, scale)


def _stat_key(path):
    """Cache key for a file's contents. ctime can't be set back by the file's writer, unlike mtime."""
    st = os.stat(path)
    return st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def _differs(png_a, png_b, mask=None):
    """True if two renders differ visibly (anti-aliasing noise ignored), outside `mask` (fractions)."""
    from PIL import Image, ImageChops, ImageDraw
    a = Image.open(io.BytesIO(png_a)).convert("RGB")
    b = Image.open(io.BytesIO(png_b)).convert("RGB")
    if a.size != b.size:
        return True
    diff = ImageChops.difference(a, b).convert("L").point(lambda v: 255 if v > 24 else 0)
    if mask:
        l, t, r, btm = mask
        W, H = diff.size
        ImageDraw.Draw(diff).rectangle([l * W - 4, t * H - 4, r * W + 4, btm * H + 4], fill=0)
    return diff.getbbox() is not None


# ---------- do the PDF readers agree? ----------

def _plain(v):
    """A field value both parsers can be compared on: text or a name, else None (not compared)."""
    import pikepdf
    from pyhanko.pdf_utils import generic
    if isinstance(v, (pikepdf.String, generic.TextStringObject)):
        return ("s", str(v))
    if isinstance(v, (pikepdf.Name, generic.NameObject)):
        return ("n", str(v))
    return None


def _qpdf_fields(acro):
    out = {}

    def visit(node, parent, ft, v, seen):
        if node.is_indirect:
            if node.objgen in seen:
                return
            seen.add(node.objgen)
        t = node.get("/T")
        name = f"{parent}.{t}" if (parent and t is not None) else (str(t) if t is not None else parent)
        ft, v = node.get("/FT", ft), node.get("/V", v)
        kids = list(node.get("/Kids", []) or [])
        if any("/T" in k for k in kids):
            for k in kids:
                visit(k, name, ft, v, seen)
        else:
            out[name] = (str(ft), None if str(ft) == "/Sig" else _plain(v))
    for f in (acro.get("/Fields", []) if acro is not None else []) or []:
        visit(f, "", None, None, set())
    return out


def _sha(b):
    return hashlib.sha256(b).hexdigest() if b is not None else None


def _q_streams(obj):
    import pikepdf
    if isinstance(obj, pikepdf.Stream):
        return [obj]
    return [x for x in (obj or []) if isinstance(x, pikepdf.Stream)] if isinstance(obj, pikepdf.Array) else []


def _q_view(pdf):
    """qpdf's view: per page (content digest, [(subtype, rect, flags, state, appearance digest)])."""
    import pikepdf
    out = []
    for pg in pdf.pages:
        content = _sha(b"\n".join(st.read_bytes() for st in _q_streams(pg.obj.get("/Contents"))))
        annots = []
        for a in pg.obj.get("/Annots", []) or []:
            n = (a.get("/AP") or {}).get("/N")
            state = str(a.get("/AS")) if "/AS" in a else ""
            if isinstance(n, pikepdf.Dictionary) and not isinstance(n, pikepdf.Stream):
                n = n.get(state) if state else None
            ap = _sha(n.read_bytes()) if isinstance(n, pikepdf.Stream) else None
            rect = tuple(round(float(v), 2) for v in list(a.get("/Rect") or [])[:4])
            annots.append((str(a.get("/Subtype")), rect, int(a.get("/F", 0)), state, ap))
        out.append((content, annots))
    return out


def _h_view(pages):
    """pyHanko's view, same shape as _q_view."""
    from pyhanko.pdf_utils import generic
    out = []
    for pg in pages:
        c = pg["/Contents"] if "/Contents" in pg else None
        streams = [c] if isinstance(c, generic.StreamObject) else \
            [x.get_object() for x in c] if isinstance(c, generic.ArrayObject) else []
        content = _sha(b"\n".join(st.data for st in streams if isinstance(st, generic.StreamObject)))
        annots = []
        arr = pg["/Annots"] if "/Annots" in pg else []
        for i in range(len(arr)):
            a = arr[i]
            ap_dict = a["/AP"] if "/AP" in a else None
            n = ap_dict["/N"] if ap_dict is not None and "/N" in ap_dict else None
            state = str(a["/AS"]) if "/AS" in a else ""
            if isinstance(n, generic.DictionaryObject) and not isinstance(n, generic.StreamObject):
                n = n[state] if state and state in n else None
            ap = _sha(n.data) if isinstance(n, generic.StreamObject) else None
            r = a["/Rect"] if "/Rect" in a else []
            rect = tuple(round(float(r[k]), 2) for k in range(min(4, len(r))))
            annots.append((str(a["/Subtype"]) if "/Subtype" in a else "None", rect,
                           int(a["/F"]) if "/F" in a else 0, state, ap))
        out.append((content, annots))
    return out


def _cg_view(doc):
    """Apple CoreGraphics' view, same shape as _q_view."""
    import ctypes
    import Quartz

    def name(d, key):
        import warnings
        with warnings.catch_warnings():  # pyobjc warns on every raw char* it hands back
            warnings.simplefilter("ignore")
            ok, ptr = Quartz.CGPDFDictionaryGetName(d, key, None)
        return "/" + ctypes.string_at(ptr.pointerAsInteger).decode("latin-1") if ok and ptr else None

    def data(stream):
        dat, _fmt = Quartz.CGPDFStreamCopyData(stream, None)
        return bytes(dat) if dat is not None else b""

    out = []
    for i in range(Quartz.CGPDFDocumentGetNumberOfPages(doc)):
        d = Quartz.CGPDFPageGetDictionary(Quartz.CGPDFDocumentGetPage(doc, i + 1))
        ok, st = Quartz.CGPDFDictionaryGetStream(d, b"Contents", None)
        if ok:
            content = _sha(data(st))
        else:
            ok, arr = Quartz.CGPDFDictionaryGetArray(d, b"Contents", None)
            parts = []
            for j in range(Quartz.CGPDFArrayGetCount(arr) if ok else 0):
                oks, st = Quartz.CGPDFArrayGetStream(arr, j, None)
                if oks:
                    parts.append(data(st))
            content = _sha(b"\n".join(parts))
        annots = []
        ok, arr = Quartz.CGPDFDictionaryGetArray(d, b"Annots", None)
        for j in range(Quartz.CGPDFArrayGetCount(arr) if ok else 0):
            oka, a = Quartz.CGPDFArrayGetDictionary(arr, j, None)
            if not oka:
                annots.append(None)
                continue
            okr, r = Quartz.CGPDFDictionaryGetArray(a, b"Rect", None)
            rect = tuple(round(float(Quartz.CGPDFArrayGetNumber(r, k, None)[1]), 2)
                         for k in range(min(4, Quartz.CGPDFArrayGetCount(r) if okr else 0)))
            okf, flags = Quartz.CGPDFDictionaryGetInteger(a, b"F", None)
            state = name(a, b"AS") or ""
            ap = None
            okap, apd = Quartz.CGPDFDictionaryGetDictionary(a, b"AP", None)
            if okap:
                okn, st = Quartz.CGPDFDictionaryGetStream(apd, b"N", None)
                if okn:
                    ap = _sha(data(st))
                elif state:
                    okd, nd = Quartz.CGPDFDictionaryGetDictionary(apd, b"N", None)
                    if okd:
                        oks, st = Quartz.CGPDFDictionaryGetStream(nd, state[1:].encode("latin-1"), None)
                        ap = _sha(data(st)) if oks else None
            annots.append((name(a, b"Subtype") or "None", rect, int(flags) if okf else 0, state, ap))
        out.append((content, annots))
    return out


def _pyhanko_pages(reader):
    out = []

    def visit(node):
        obj = node.get_object() if hasattr(node, "get_object") else node
        if "/Kids" in obj:
            kids = obj["/Kids"]
            for i in range(len(kids)):
                visit(kids.raw_get(i))
        else:
            out.append(obj)
    visit(reader.root.raw_get("/Pages"))
    return out


def _orphan_widgets(pdf):
    """Form-field boxes on no page's /Annots: (count pointing at a page via /P, count not). pikepdf document.

    The review draws a page's /Annots; a viewer that builds fields from /AcroForm may still
    place a widget that names its page in /P. Leftovers of flattened forms carry no /P.
    """
    import pikepdf
    on_pages = {a.objgen for pg in pdf.pages for a in (pg.obj.get("/Annots", []) or []) if a.is_indirect}
    acro = pdf.Root.get("/AcroForm")
    seen, counts = set(), [0, 0]

    def visit(node, depth):
        # Deeper than any real form, or not a dictionary at all: count it as one that may show, so it is refused.
        if depth > 32 or not isinstance(node, pikepdf.Dictionary):
            counts[0] += 1
            return
        if node.is_indirect:
            if node.objgen in seen:
                return
            seen.add(node.objgen)
        if "/Rect" in node and not (node.is_indirect and node.objgen in on_pages):
            counts[0 if "/P" in node else 1] += 1
        for kid in node.get("/Kids", []) or []:
            visit(kid, depth + 1)
    for f in (acro.get("/Fields", []) if acro is not None else []) or []:
        visit(f, 0)
    return tuple(counts)


def _misplaced_annotations(pdf):
    """Annotations on a page other than the one their /P names, on several pages, or not dictionaries at
    all. Viewers that place by /Annots (as the review does) and viewers that place by /P (or by a form's
    fields) would draw them in different places. Every kind counts, direct or indirect; none of 177 real
    PDFs has one."""
    import pikepdf
    page_of = {pg.obj.objgen: i for i, pg in enumerate(pdf.pages)}
    where, named, bad = {}, {}, 0
    for i, pg in enumerate(pdf.pages):
        for j, a in enumerate(pg.obj.get("/Annots", []) or []):
            if not isinstance(a, pikepdf.Dictionary):
                bad += 1
                continue
            key = a.objgen if a.is_indirect else ("direct", i, j)
            where.setdefault(key, set()).add(i)
            if "/P" in a:
                named[key] = page_of.get(a.P.objgen) if a.P.is_indirect else None
    for key, pages in where.items():
        bad += len(pages) > 1 or (key in named and named[key] not in pages)
    return bad


def _screen_print_problems(pdf):
    """What the screen view (and so the review) leaves out but a printout shows. pikepdf document."""
    out, hidden_print = [], 0
    placed, _ = _orphan_widgets(pdf)
    if placed:
        out.append(f"{placed} form field box(es) name a page but are missing from it, so viewers disagree on "
                   "whether they show")
    misplaced = _misplaced_annotations(pdf)
    if misplaced:
        out.append(f"{misplaced} annotation(s) or form box(es) sit on a different page from the one they name, "
                   "or on several pages, so viewers disagree on where they show")
    for pg in pdf.pages:
        for a in pg.obj.get("/Annots", []) or []:
            flags = int(a.get("/F", 0))
            if flags & (2 | 32) and flags & 4:  # Hidden or NoView, yet Print
                hidden_print += 1
    if hidden_print:
        out.append(f"{hidden_print} annotation(s) are hidden on screen but print, so this review cannot show them")
    oc = pdf.Root.get("/OCProperties")
    if oc is not None:
        d = oc.get("/D") or {}
        if len(d.get("/OFF", []) or []) or str(d.get("/BaseState", "/ON")) != "/ON" or len(d.get("/AS", []) or []):
            out.append("it has layers (optional content) that are switched off, or that show differently on "
                       "screen and in print")
    return out


def viewer_warnings(path):
    """Things other viewers may show differently, in a file that is otherwise consistent.

    These do not block signing; the signer has to have seen them first. The review page
    makes them tick to acknowledge, and the CLI and the quick Quick Action print them in
    the plan the signer confirms.
    """
    import pikepdf
    out, scripted = [], 0
    with pikepdf.open(path) as pdf:
        root = pdf.Root
        acro = root.get("/AcroForm")
        if acro is not None and bool(acro.get("/NeedAppearances", False)):
            out.append("This form asks viewers to redraw its fields, so other viewers may show values "
                       "differently from what you see here. Compare the values listed with the page.")
        doc_js = "/OpenAction" in root or "/AA" in root or ("/Names" in root and "/JavaScript" in root.Names)
        seen = set()

        def visit(node):
            nonlocal scripted
            if node.is_indirect:
                if node.objgen in seen:
                    return
                seen.add(node.objgen)
            if "/AA" in node or ("/A" in node and str(node.A.get("/S")) == "/JavaScript"):
                scripted += 1
            for kid in node.get("/Kids", []) or []:
                visit(kid)
        for f in (acro.get("/Fields", []) if acro is not None else []) or []:
            visit(f)
        if doc_js or scripted:
            out.append("This PDF contains Acrobat scripts" + (f" on {scripted} field(s)" if scripted else "") +
                       "; Acrobat may reformat or recalculate values when it opens the file.")
        _, unplaced = _orphan_widgets(pdf)
        if unplaced:
            out.append(f"{unplaced} form field(s) are on no page: this review can't show them, and common "
                       "viewers don't either. Check the file in Acrobat if they could matter.")
        cropped = [i + 1 for i, pg in enumerate(pdf.pages)
                   if [round(float(v), 1) for v in pg.cropbox] != [round(float(v), 1) for v in pg.mediabox]]
        if cropped:
            out.append(f"Part of page {', '.join(map(str, cropped[:5]))} lies outside the area viewers show "
                       "(its crop box), so anything there is not shown here but is in what you sign.")
    return out


@functools.lru_cache(maxsize=8)
def _reader_problems(path, *stat_key):
    import pikepdf
    from pyhanko.pdf_utils.reader import PdfFileReader
    try:
        import Quartz
    except ImportError:  # not a Mac: no CoreGraphics to compare against
        Quartz = None
    with open(path, "rb") as f:
        data = f.read()
    problems = []
    hybrid = b"/XRefStm" in data  # belt; the parsed checks below are the braces
    with pikepdf.open(io.BytesIO(data)) as pdf:
        syntax = pdf.check_pdf_syntax()
        hybrid = hybrid or "/XRefStm" in pdf.trailer
        q_pages = len(pdf.pages)
        q_annots = [[tuple(a.objgen) for a in (pg.obj.get("/Annots", []) or []) if a.is_indirect] for pg in pdf.pages]
        q_view = _q_view(pdf)
        acro = pdf.Root.get("/AcroForm")
        if acro is not None and "/XFA" in acro:
            problems.append("it is an XFA (LiveCycle) form: Acrobat shows its own XFA layer, which this page "
                            "cannot show or fill")
        q_fields = _qpdf_fields(acro)
        problems += _screen_print_problems(pdf)
        buf = io.BytesIO()
        pdf.save(buf)
        resaved = buf.getvalue()
        warnings = list(pdf.get_warnings())
    if warnings or syntax:
        problems.append("qpdf had to repair it or found errors (" + "; ".join(warnings + syntax)[:240] + ")")
    try:
        r = PdfFileReader(io.BytesIO(data), strict=True)
        pages = _pyhanko_pages(r)
        h_annots = []
        for pg in pages:
            arr = pg["/Annots"] if "/Annots" in pg else []
            h_annots.append([(arr.raw_get(i).idnum, arr.raw_get(i).generation) for i in range(len(arr))
                             if hasattr(arr.raw_get(i), "idnum")])
        h_fields = {}
        if "/AcroForm" in r.root:
            for n, fld in cac_sign.walk_fields(r.root["/AcroForm"]).items():
                h_fields[n] = (fld.ft, None if fld.ft == "/Sig" else _plain(cac_sign._get(fld.obj, "/V", fld.inh.get("/V"))))
        _ = r.embedded_signatures
        hybrid = hybrid or bool(r.xrefs.hybrid_xrefs_present)
    except Exception as e:
        problems.append(f"pyHanko's strict parser rejects it ({type(e).__name__}: {str(e)[:160]})")
        return problems
    h_view = _h_view(pages)  # outside the try: a bug here must surface, not pose as a rejection
    if hybrid:
        problems.append("it uses hybrid cross-reference tables, which readers resolve differently")
    if len(pages) != q_pages:
        problems.append(f"qpdf sees {q_pages} page(s) but pyHanko sees {len(pages)}")
    elif h_annots != q_annots or h_view != q_view:
        problems.append("qpdf and pyHanko disagree on the page content or the annotations (fields, stamps)")
    if h_fields != q_fields:
        odd = sorted(set(h_fields) ^ set(q_fields)) or [n for n in h_fields if h_fields[n] != q_fields.get(n)]
        problems.append(f"qpdf and pyHanko disagree on the form fields (e.g. {str(odd[:3])[:120]})")
    if Quartz is None:
        return problems
    cg = _cg_doc(data)
    if cg is None or Quartz.CGPDFDocumentGetNumberOfPages(cg) != q_pages:
        problems.append("Apple's PDF reader and qpdf disagree on the pages")
    elif _cg_view(cg) != q_view:
        problems.append("Apple's PDF reader and qpdf disagree on the page content or the annotations")
    else:
        resaved_cg = _cg_doc(resaved)
        for i in range(q_pages):
            if _differs(_render(cg, i, 1.0), _render(resaved_cg, i, 1.0)):
                problems.append(f"Apple's PDF reader and qpdf draw page {i + 1} differently")
                break
    return problems


def reader_problems(path):
    """Ways the PDF readers involved disagree about this file, had to repair it, or would print what the
    screen hides. Empty means signing may go ahead."""
    try:
        return _reader_problems(path, *_stat_key(path))
    except Exception as e:  # a structure these checks can't walk is not signable: refuse, and say why
        return [f"its structure could not be checked ({type(e).__name__}: {str(e)[:120]})"]
