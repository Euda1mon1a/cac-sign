#!/usr/bin/env python3
"""
cac-sign review page: open PDFs in the browser, see every field on the page, edit any
of them, optionally fill the blanks, then sign. The "CAC Sign..." Quick Action runs it.

Usage:
  cac_sign_editor.py FORM.pdf [MORE.pdf ...]    # opens the review page in the default browser
  cac_sign_editor.py --background FORM.pdf      # same, but returns at once (Quick Action)

Signing goes through cac_sign.main(), so every check is the same as the CLI: refusals,
the post-sign verification, and macOS asking for the PIN in its own window.

What you see is what you sign:
- Pages are drawn with every annotation flattened in, the way other viewers show
  them, not from the field values.
- The page signs only files that three independent PDF readers agree on: qpdf,
  pyHanko's strict parser and Apple's CoreGraphics. They must agree on:
  - the pages and each page's decoded content;
  - every annotation's type, rectangle, flags, appearance state and decoded
    appearance bytes;
  - the form fields;
  - the drawn page content.

  Repaired, XFA and hybrid-reference files are refused; hybrid tables are detected
  from the parsed cross-reference data, not by searching bytes. Acrobat is a fourth
  reader this cannot run.
- Before the PIN, Sign snapshots the PDF's bytes into a private folder. The review
  shows the snapshot with your edits, made by the same fill code the signing step
  uses. Signing signs that snapshot, after re-checking its SHA-256, so a file
  swapped on disk after the review is never what gets signed.
- After signing, the signed file is rendered and compared pixel by pixel with what
  was reviewed. Any visible difference outside the signature box fails it, and
  the file is kept as *.FAILED.pdf.
- Over HTTP, signing is possible only through that review step (prepare, then
  commit).

Safety:
- The server listens on 127.0.0.1 only, on a random port.
- Every API call needs a one-time token. The token is only in the URL fragment the
  browser was opened with, so it never appears in requests or logs.
- The Host header is checked, which blocks DNS rebinding.
- There are no CORS headers, so other web pages cannot call it.
- It serves only the PDFs it was started with, and writes only new output files
  next to them.
- It exits after 10 idle minutes.
- Text from the PDF reaches the page as JSON and is shown with textContent, never
  as HTML.
- The browser is opened in-process (NSWorkspace), so the token never appears in a
  process list. No secrets are ever written to the log.
"""
from __future__ import annotations
import argparse, hashlib, hmac, io, json, os, re, secrets, shutil, subprocess, sys, tempfile, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cac_sign  # noqa: E402
from cac_sign_finder import run_cli  # noqa: E402
from cac_signing import verify_signatures  # noqa: E402
from pdf_readers import _differs, reader_problems, render_page, viewer_warnings  # noqa: E402

PAGE_HTML = os.path.join(HERE, "editor.html")
IDLE_SECONDS = 600           # the open page pings every 60 s, so this only ends abandoned sessions
MAX_BODY = 1 << 20
FF_REQUIRED, FF_COMBO, FF_EDIT = 1 << 1, 1 << 17, 1 << 18


# ---------- page geometry: PDF user space <-> the displayed page, as fractions ----------

def _rotation(page):
    node = page.obj
    while node is not None:
        if "/Rotate" in node:
            return int(node.Rotate) % 360
        node = node.get("/Parent")
    return 0


def page_geometry(path):
    """[{crop, rot, w, h}] per page (w, h as displayed), and {(objnum, gen): page index} for annotations."""
    import pikepdf
    pages, annot_page = [], {}
    with pikepdf.open(path) as pdf:
        for i, pg in enumerate(pdf.pages):
            x0, y0, x1, y1 = (float(v) for v in pg.cropbox)
            x0, x1, y0, y1 = min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1)
            rot = _rotation(pg)
            w, h = (y1 - y0, x1 - x0) if rot in (90, 270) else (x1 - x0, y1 - y0)
            pages.append({"crop": (x0, y0, x1, y1), "rot": rot, "w": w, "h": h})
            for a in pg.obj.get("/Annots", []) or []:
                if a.is_indirect:
                    annot_page.setdefault(a.objgen, i)
    return pages, annot_page


def _to_display(g, x, y):
    x0, y0, x1, y1 = g["crop"]
    W, H = x1 - x0, y1 - y0
    px, py = x - x0, y1 - y
    X, Y = {0: (px, py), 90: (H - py, px), 180: (W - px, H - py), 270: (py, W - px)}[g["rot"]]
    return X / g["w"], Y / g["h"]


def to_display(g, rect):
    """PDF rect [x1 y1 x2 y2] -> [left, top, right, bottom] as fractions of the displayed page."""
    (u0, v0), (u1, v1) = _to_display(g, rect[0], rect[1]), _to_display(g, rect[2], rect[3])
    return [round(min(u0, u1), 5), round(min(v0, v1), 5), round(max(u0, u1), 5), round(max(v0, v1), 5)]


def from_display(g, box):
    """Inverse of to_display, for a signature box drawn on the page."""
    x0, y0, x1, y1 = g["crop"]
    W, H = x1 - x0, y1 - y0
    pts = []
    for u, v in ((box[0], box[1]), (box[2], box[3])):
        X, Y = u * g["w"], v * g["h"]
        px, py = {0: (X, Y), 90: (Y, H - X), 180: (W - X, H - Y), 270: (W - Y, X)}[g["rot"]]
        pts.append((x0 + px, y1 - py))
    (ax, ay), (bx, by) = pts
    return [round(min(ax, bx), 2), round(min(ay, by), 2), round(max(ax, bx), 2), round(max(ay, by), 2)]


# ---------- the form, as the page sees it ----------

def _ui_value(kind, raw):
    if kind == "signature":
        return ""  # its /V is the signature dictionary, not something to show
    if kind == "checkbox":
        return raw not in (None, "", "/Off", "Off")
    if kind == "radio":
        return "" if raw in (None, "", "/Off", "Off") else str(raw).lstrip("/")
    return "" if raw is None else str(raw)


def _refusal(disagree):
    return ("Not signing this file here: PDF readers do not agree on it (" + "; ".join(disagree) +
            "), so what this page shows might not be what Acrobat shows or what gets signed.")


def form_model(path, profiles=None, identity=None):
    """The form as the page sees it. A file the PDF readers disagree on is shown but never signable."""
    disagree = reader_problems(path)
    try:
        return _form_model(path, profiles, identity, disagree)
    except Exception:
        if not disagree:
            raise
        try:
            pages = [{"w": g["w"], "h": g["h"]} for g in page_geometry(path)[0]]
        except Exception:
            pages = []
        return {"file": os.path.basename(path), "pages": pages, "profile": None, "reason": None, "next_step": None,
                "fields": [], "sign_target": None, "blocked": _refusal(disagree), "notes": []}


def _form_model(path, profiles, identity, disagree):
    from pyhanko.pdf_utils.reader import PdfFileReader
    profiles = cac_sign.load_profiles() if profiles is None else profiles
    identity = cac_sign.load_identity() if identity is None else identity
    geo, annot_page = page_geometry(path)
    warnings = viewer_warnings(path)
    notes = list(warnings)
    with open(path, "rb") as f:
        r = PdfFileReader(f, strict=False)
        acro = cac_sign._get(r.root, "/AcroForm")
        fields = cac_sign.walk_fields(acro) if acro is not None else {}
        sigs = {s["field"]: s for s in verify_signatures(path)}
        locked, lock_all, no_changes = cac_sign.locked_fields(r.root, fields)
        prof = cac_sign.detect_profile(profiles, fields)

        proposals, profile_fields = {}, []
        for fname, spec in (prof or {}).get("fill", {}).items():
            spec = spec if isinstance(spec, dict) else {"value": spec}
            try:
                fld = cac_sign.find_field(fields, fname)
            except LookupError:
                continue
            profile_fields.append(fld.name)
            if spec.get("if_empty") and fld.value not in (None, "", "/Off"):
                continue
            try:
                val = cac_sign.expand(spec["value"], identity)
            except SystemExit as e:
                notes.append(str(e))
                continue
            proposals[fld.name] = (val.strip().lower() in cac_sign.TRUTHY) if fld.kind == "checkbox" else val

        out = []
        for name, fld in fields.items():
            kind = fld.kind
            if kind in ("button", "?") or kind.startswith("/"):
                continue
            ff = fld.flags
            widgets = []
            for wref in fld.widgets:
                page = annot_page.get((getattr(wref, "idnum", None), getattr(wref, "generation", 0)))
                if page is None:
                    continue  # not on any page
                wd = wref.get_object()
                rect = [float(v) for v in cac_sign._get(wd, "/Rect", [0, 0, 0, 0])]
                w = {"page": page, "box": to_display(geo[page], rect)}
                if kind == "radio":
                    w["option"] = next((s.lstrip("/") for s in cac_sign._on_states(wd)), "")
                widgets.append(w)
            item = {"name": name, "kind": kind, "value": _ui_value(kind, fld.value), "widgets": widgets,
                    "readonly": bool(ff & cac_sign.FF_READONLY),
                    "locked": bool(no_changes or lock_all or name in locked),
                    "required": bool(ff & FF_REQUIRED), "multiline": bool(ff & cac_sign.FF_MULTILINE),
                    "maxlen": int(fld.inh["/MaxLen"]) if "/MaxLen" in fld.inh else None,
                    "editable": "=" not in name}
            label = cac_sign._get(fld.obj, "/TU")
            if label:
                item["label"] = str(label)
            if kind == "radio":
                item["options"] = sorted({w["option"] for w in widgets if w.get("option")})
            if kind == "choice":
                opts = []
                for o in cac_sign._get(fld.obj, "/Opt", []) or []:
                    o = o.get_object() if hasattr(o, "get_object") else o
                    if isinstance(o, list) and len(o) == 2:  # [export value, display text]
                        opts.append({"value": str(o[0]), "label": str(o[1])})
                    else:
                        opts.append({"value": str(o), "label": str(o)})
                item["options"] = opts
                item["custom"] = bool(ff & FF_EDIT) or not (ff & FF_COMBO)
            if kind == "signature":
                s = sigs.get(name)
                item["signature"] = ({"signer": s["signer"], "when": s["when"], "ok": s["ok"], "detail": s["detail"]}
                                     if s else None)
            if name in proposals:
                item["proposed"] = proposals[name]
            out.append(item)

    empty_sigs = [f["name"] for f in out if f["kind"] == "signature" and not f["signature"] and not f["locked"]]
    want = (prof or {}).get("sign_field")
    target = next((n for n in empty_sigs if want and cac_sign.norm(n) == cac_sign.norm(want)), None)
    if target is None and len(empty_sigs) == 1:
        target = empty_sigs[0]
    blocked = None
    broken = [s for s in sigs.values() if not s["ok"]]
    if disagree:
        blocked = _refusal(disagree)
    elif broken:
        blocked = ("An existing signature does not check out (" +
                   "; ".join(f'{s["field"]}: {s["detail"]}' for s in broken) +
                   "). Check it in Acrobat; cac-sign --force in Terminal signs anyway.")
    elif no_changes:
        blocked = f"Signing is not possible: {no_changes}."
    else:
        signed = {s["field"] for s in sigs.values()}
        need = [n for n in (prof or {}).get("require_signed", []) if n not in signed]
        if need:
            blocked = f'"{need[0]}" is not signed yet; this form expects it first.'
    return {"file": os.path.basename(path), "pages": [{"w": g["w"], "h": g["h"]} for g in geo],
            "profile": {"id": prof["id"], "description": prof.get("description", prof["id"])} if prof else None,
            "reason": (prof or {}).get("reason"), "next_step": (prof or {}).get("next_step"),
            "fields": out, "sign_target": target, "blocked": blocked, "notes": notes, "warnings": warnings,
            "profile_fields": profile_fields if prof else None,
            "signed_before": [{"when": cac_sign._when(e.get("when")), "name": os.path.basename(e.get("output", "?"))}
                              for e in cac_sign.signed_before(cac_sign.file_sha256(path))[:3]]}


# ---------- doing it ----------

def free_output(path, suffix):
    stem = os.path.splitext(path)[0]
    out, n = f"{stem}{suffix}.pdf", 2
    while os.path.exists(out) or os.path.exists(out[:-4] + ".FAILED.pdf"):
        out, n = f"{stem}{suffix} {n}.pdf", n + 1
    return out


def _cli_value(kind, v):
    if kind == "checkbox":
        return "yes" if v else "no"
    if kind == "radio":
        return v or "Off"
    return str(v)


def _sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _sets(model, values):
    """--set arguments for the page's edits, or an error message."""
    kinds = {f["name"]: f for f in model["fields"]}
    argv = []
    for name, value in (values or {}).items():
        f = kinds.get(name)
        if f is None or not f["editable"] or f["kind"] == "signature" or f["readonly"] or f["locked"]:
            return None, f'"{name}" is not a field this page can fill.'
        argv += ["--set", f"{name}={_cli_value(f['kind'], value)}"]
    return argv, None


def _shown(text, *paths):
    """The CLI's output for the page: the result if there is one, and no folder paths."""
    head, sep, tail = text.partition("\n\n")
    text = tail if sep and any(w in tail for w in ("DONE", "FAIL", "STOP")) else text
    for d in sorted({os.path.dirname(os.path.abspath(p)) for p in paths if p}, key=len, reverse=True):
        text = text.replace(d + os.sep, "")
    return text


def fill_copy(path, request, extra_args=(), _out=None):
    """Save a filled, unsigned copy next to the original."""
    if request.get("additions"):
        with tempfile.TemporaryDirectory(prefix="cac-sign-") as folder:
            snap, _ = _snapshot(path, folder)
            try:
                _add_text_fields(snap, request["additions"])
            except ValueError as e:
                return {"ok": False, "text": str(e)}
            changed = dict(request, additions=[])
            return fill_copy(snap, changed, extra_args, _out=free_output(path, "_filled"))
    model = form_model(path)
    sets, err = _sets(model, request.get("values"))
    if err:
        return {"ok": False, "text": err}
    out = _out or free_output(path, "_filled")
    rc, text = run_cli([path, "--no-profile-fill", *sets, "--no-sign", "-o", out, *extra_args])
    res = {"ok": rc == 0 and os.path.exists(out), "text": _shown(text, path, out)}
    if res["ok"]:
        res["output"], res["name"] = out, os.path.basename(out)
    return res


def _visual_diff(reference, path, where):
    """First page index where `path` renders differently from the reviewed images, or None.

    The signature box (plus a small margin) is ignored: the signature appears there.
    """
    for i, ref in enumerate(reference):
        if _differs(ref, render_page(path, i, scale=1.0), where["box"] if where and where["page"] == i else None):
            return i
    return None


def _snapshot(path, stage_dir):
    """Copy the PDF's bytes once, into the private folder: from here on, this is the file."""
    with open(path, "rb") as f:
        data = f.read()
    snap = os.path.join(stage_dir, f"snap-{secrets.token_hex(6)}.pdf")
    fd = os.open(snap, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return snap, hashlib.sha256(data).hexdigest()


def _remove(*paths):
    for p in paths:
        if p and os.path.exists(p):
            os.remove(p)


def _add_text_fields(path, additions):
    """Add text boxes or checkboxes to an unsigned private snapshot only."""
    import math
    from pyhanko.pdf_utils import generic as g
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    if not isinstance(additions, list) or len(additions) > 100:
        raise ValueError("Add at most 100 text boxes at a time.")
    if verify_signatures(path):
        raise ValueError("Add text boxes before the document has any signatures.")
    geo, _ = page_geometry(path)
    data = io.BytesIO()
    with open(path, "rb") as source:
        writer = IncrementalPdfFileWriter(source)
        acro = cac_sign._get(writer.root, "/AcroForm")
        if acro is None:
            acro = g.DictionaryObject({g.NameObject("/Fields"): g.ArrayObject()})
            writer.root[g.NameObject("/AcroForm")] = writer.add_object(acro)
            writer.update_root()
        fields = cac_sign._get(acro, "/Fields")
        if fields is None:
            fields = g.ArrayObject()
            acro[g.NameObject("/Fields")] = fields
        names = set(cac_sign.walk_fields(acro))
        for item in additions:
            if not isinstance(item, dict):
                raise ValueError("A new text box must describe its name, page and rectangle.")
            name = item.get("name")
            if not isinstance(name, str) or not re.fullmatch(r"AddedText[1-9][0-9]{0,3}", name) or name in names:
                raise ValueError("A new text box has an invalid or duplicate name.")
            kind, label = item.get("kind", "text"), item.get("label", name)
            if kind not in ("text", "checkbox") or not isinstance(label, str) or len(label) > 240:
                raise ValueError("A new field must be a text box or checkbox with a short label.")
            page, box = item.get("page"), item.get("box")
            if (type(page) is not int or not 0 <= page < len(geo) or not isinstance(box, list)
                    or len(box) != 4 or any(type(v) not in (int, float) or not math.isfinite(v) for v in box)
                    or not (0 <= box[0] < box[2] <= 1 and 0 <= box[1] < box[3] <= 1)):
                raise ValueError("Draw each text box inside a document page.")
            rect = from_display(geo[page], box)
            page_ref, _ = writer.find_page_for_modification(page)
            widget = g.DictionaryObject({
                g.NameObject("/Type"): g.NameObject("/Annot"),
                g.NameObject("/Subtype"): g.NameObject("/Widget"),
                g.NameObject("/FT"): g.NameObject("/Btn" if kind == "checkbox" else "/Tx"),
                g.NameObject("/T"): g.TextStringObject(name),
                g.NameObject("/TU"): g.TextStringObject(label),
                g.NameObject("/V"): g.TextStringObject(""),
                g.NameObject("/DA"): g.TextStringObject("/Helv 0 Tf 0 g"),
                g.NameObject("/F"): g.NumberObject(4),
                g.NameObject("/Rect"): g.ArrayObject(g.FloatObject(v) for v in rect),
                g.NameObject("/P"): page_ref,
            })
            width, height = rect[2] - rect[0], rect[3] - rect[1]
            if kind == "checkbox":
                def appearance(checked):
                    ops = b"q Q"
                    if checked:
                        inset = min(width, height) * 0.18
                        ops = (f"q 0 G 0.9 w {inset} {inset} m {width-inset} {height-inset} l "
                               f"{inset} {height-inset} m {width-inset} {inset} l S Q").encode("ascii")
                    return writer.add_object(g.StreamObject({
                        g.NameObject("/Type"): g.NameObject("/XObject"),
                        g.NameObject("/Subtype"): g.NameObject("/Form"),
                        g.NameObject("/BBox"): g.ArrayObject(g.FloatObject(v) for v in (0, 0, width, height)),
                        g.NameObject("/Resources"): g.DictionaryObject(),
                    }, stream_data=ops))
                widget[g.NameObject("/V")] = g.NameObject("/Off")
                widget[g.NameObject("/AS")] = g.NameObject("/Off")
                widget[g.NameObject("/AP")] = g.DictionaryObject({g.NameObject("/N"): g.DictionaryObject({
                    g.NameObject("/Off"): appearance(False), g.NameObject("/Yes"): appearance(True)})})
            else:
                empty = writer.add_object(g.StreamObject({
                    g.NameObject("/Type"): g.NameObject("/XObject"),
                    g.NameObject("/Subtype"): g.NameObject("/Form"),
                    g.NameObject("/BBox"): g.ArrayObject(g.FloatObject(v) for v in (0, 0, width, height)),
                    g.NameObject("/Resources"): g.DictionaryObject(),
                }, stream_data=b"q Q"))
                widget[g.NameObject("/AP")] = g.DictionaryObject({g.NameObject("/N"): empty})
            # Rotated text entry needs its own text orientation; refuse until supported.
            if geo[page]["rot"]:
                raise ValueError("Add text currently needs an unrotated page.")
            ref = writer.add_object(widget)
            fields.append(ref)
            writer.register_annotation(page_ref, ref)
            names.add(name)
        writer.update_container(fields)
        writer.update_container(acro)
        writer.write(data)
    with open(path, "wb") as dest:
        dest.write(data.getvalue())


def _blank(f, v):
    return (not v) if f["kind"] == "checkbox" else (v is None or str(v).strip() == "")


def _say_value(f, v):
    if f["kind"] == "checkbox":
        return "checked" if v else "unchecked"
    if f["kind"] == "radio":
        return v or "none"
    return str(v) if v not in (None, "") else "(blank)"


def describe_changes(model, values):
    """Every edit the signer is about to sign, with plain-language flags for the surprising ones.

    Flags: it replaces something already on the form; it sits in an earlier signer's
    part of the form (above their signature) and the profile does not fill it; or the
    form has a profile and this is not one of the boxes it fills.
    """
    fields = {f["name"]: f for f in model["fields"]}
    signed_areas = [(w["page"], w["box"][1], f["signature"]["signer"]) for f in model["fields"]
                    if f["kind"] == "signature" and f.get("signature") for w in f["widgets"]]
    prof = model.get("profile_fields")
    out = []
    for name, new in (values or {}).items():
        f = fields.get(name)
        if f is None:
            continue
        flags = []
        if not _blank(f, f["value"]):
            flags.append("replaces what the form already had")
        in_profile = prof is not None and name in prof
        w = f["widgets"][0] if f["widgets"] else None
        if w and not in_profile:
            for page, top, signer in signed_areas:
                if w["page"] < page or (w["page"] == page and w["box"][1] < top):
                    flags.append(f"in {signer}'s part of the form, above their signature")
                    break
        if prof is not None and not in_profile:
            flags.append("not one of the boxes this form's profile fills")
        out.append({"name": f.get("label") or name, "from": _say_value(f, f["value"]), "to": _say_value(f, new), "flags": flags})
    return out


def prepare(path, request, stage_dir, extra_args=()):
    """Snapshot the PDF, stage the edits on the snapshot for review, and pin down what will be signed.

    Returns (result for the page, pending dict or None).
    """
    snap, snap_sha = _snapshot(path, stage_dir)
    stage = os.path.join(stage_dir, f"stage-{secrets.token_hex(6)}.pdf")

    def fail(text):
        _remove(snap, stage)
        return {"ok": False, "text": text}, None

    model = form_model(snap)
    if model["blocked"]:
        return fail(model["blocked"])
    if request.get("additions"):
        try:
            _add_text_fields(snap, request["additions"])
        except ValueError as e:
            return fail(str(e))
        snap_sha = _sha256(snap)
        model = form_model(snap)
        if model["blocked"]:
            return fail(model["blocked"])
    sets, err = _sets(model, request.get("values"))
    if err:
        return fail(err)
    if request.get("mode") == "place":
        page, box = int(request["page"]), [float(v) for v in request["box"]]
        if not 0 <= page < len(model["pages"]):
            return fail("That page is not in the document.")
        geo, _ = page_geometry(snap)
        rect = from_display(geo[page], box)
        names = {f["name"] for f in model["fields"]}
        field = next(f"Signature{i}" for i in range(1, 1000) if f"Signature{i}" not in names)
        sign_args = ["--place", f"{page + 1}:{rect[0]},{rect[1]},{rect[2]},{rect[3]}", "--field", field]
        where = {"page": page, "box": box}
    else:
        target = request.get("field") or model["sign_target"]
        f = next((x for x in model["fields"] if x["name"] == target and x["kind"] == "signature"), None)
        if f is None or f["signature"] or f["locked"] or not f["widgets"]:
            return fail("Pick an empty signature box, or draw one on the page.")
        sign_args = ["--field", target]
        where = {"page": f["widgets"][0]["page"], "box": f["widgets"][0]["box"]}
    if sets:  # the same fill code the signing step runs, written to a private copy for review
        rc, text = run_cli([snap, "--no-profile-fill", *sets, "--no-sign", "-o", stage, *extra_args])
        if rc != 0 or not os.path.exists(stage):
            return fail(_shown(text, snap, stage))
    else:
        shutil.copyfile(snap, stage)
    n = len(page_geometry(stage)[0])
    pending = {"src": path, "snap": snap, "snap_sha": snap_sha, "sets": sets, "sign_args": sign_args,
               "stage": stage, "where": where, "next_step": model.get("next_step"),
               "reference": [render_page(stage, i, scale=1.0) for i in range(n)]}
    changes = describe_changes(model, request.get("values"))
    warnings = model.get("warnings") or []
    # enforced in commit(), not just by the page
    pending["needs_ack"] = any(c["flags"] for c in changes) or bool(warnings)
    return {"ok": True, "pages": n, "where": where, "changes": changes, "warnings": warnings,
            "signed_before": model["signed_before"]}, pending


ACK_REFUSAL = "The flagged changes or warnings were not acknowledged, so nothing was signed."


def commit(pending, extra_args=(), ack=False):
    """Sign the reviewed snapshot (re-checked by hash); then prove the result looks as reviewed.

    Flagged changes and viewer warnings must be acknowledged (`ack`). The page's tickbox
    is only the way to say so; this is where it is enforced.
    """
    if pending.get("needs_ack") and ack is not True:
        return {"ok": False, "text": ACK_REFUSAL, "needs_ack": True}
    src, snap = pending["src"], pending["snap"]
    try:
        if not os.path.exists(snap) or _sha256(snap) != pending["snap_sha"]:
            return {"ok": False, "text": "The reviewed copy changed before signing, so nothing was signed. "
                                         "Review it again."}
        out = free_output(src, "_signed")
        rc, text = run_cli([snap, "--no-profile-fill", *pending["sets"], *pending["sign_args"], "-o", out,
                            *extra_args])
        shown = _shown(text, snap, out, src).replace(os.path.basename(snap), os.path.basename(src))
        res = {"ok": rc == 0, "text": shown, "cancelled": "cancelled" in shown and "STOP" in shown}
        failed = out[:-4] + ".FAILED.pdf"
        if rc == 0 and os.path.exists(out):
            page = _visual_diff(pending["reference"], out, pending["where"])
            if page is not None:
                os.replace(out, failed)
                return {"ok": False, "output": failed, "name": os.path.basename(failed),
                        "text": f"The signed file does not look like what you reviewed (page {page + 1}). "
                                f"It was kept as {os.path.basename(failed)}; do not send it."}
            res["output"], res["name"] = out, os.path.basename(out)
            res["signatures"] = [{"field": s["field"], "signer": s["signer"], "when": s["when"], "ok": s["ok"],
                                  "detail": s["detail"]} for s in verify_signatures(out)]
            res["next_step"] = pending.get("next_step")
        elif os.path.exists(failed):
            res["output"], res["name"] = failed, os.path.basename(failed)
        return res
    finally:
        _remove(snap, pending["stage"])


def sign_direct(path, request, extra_args=()):
    """prepare + commit without the review step (scripts and tests; the page always reviews)."""
    stage_dir = tempfile.mkdtemp(prefix="cac-sign-")
    try:
        res, pending = prepare(path, request, stage_dir, extra_args)
        # no review step here by design, so nobody else is there to acknowledge
        return commit(pending, extra_args, ack=True) if pending else res
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)


# ---------- the local server ----------

def review_model(path):
    """Suggest fields without changing the source or relaxing any signing gate."""
    model = form_model(path)
    try:
        from field_detection import suggest_fields
        fields, signatures = suggest_fields(path, model, page_geometry(path)[0], to_display)
    except Exception:
        model["notes"] = [*(model.get("notes") or []),
                          "Automatic field detection could not read this file. Use Add text to place fields manually."]
        return model
    if fields or signatures:
        model["fields"].extend(fields)
        if len(signatures) == 1 and not model.get("sign_target"):
            model["suggested_signature"] = signatures[0]
        model["notes"] = [*(model.get("notes") or []),
                          f"Detected {len(fields)} fields and {len(signatures)} signature line(s). "
                          "Check their positions and labels; remove any incorrect fields. Checkbox choices start unchecked."]
    return model

class Editor:
    def __init__(self, files, extra_args=(), idle=IDLE_SECONDS):
        from document_inputs import pdf_input, conversion_note
        sources = [os.path.abspath(p) for p in files]
        self.files = [pdf_input(p) for p in sources]
        self.input_notes = {i: conversion_note(src, pdf) for i, (src, pdf) in enumerate(zip(sources, self.files))
                            if src.lower().endswith(".docx")}
        self.extra = list(extra_args)
        self.token = secrets.token_urlsafe(32)
        self.idle = idle
        self.last = time.time()
        self.closed = False
        self.outputs = {}
        self.stage_dir = tempfile.mkdtemp(prefix="cac-sign-")  # 0700: staging copies to sign
        self.pending = None
        self.httpd = HTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self.httpd.server_address[1]

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/#{self.token}"

    def serve(self):
        self.httpd.timeout = 2
        try:
            while not self.closed and time.time() - self.last < self.idle:
                self.httpd.handle_request()
        finally:
            self.httpd.server_close()
            shutil.rmtree(self.stage_dir, ignore_errors=True)

    def discard(self):
        if self.pending:
            _remove(self.pending.get("snap"), self.pending.get("stage"))
        self.pending = None

    def _handler(self):
        ed = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="application/json", extra=None):
                body = body if isinstance(body, bytes) else body.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _json(self, obj, code=200):
                self._send(code, json.dumps(obj))

            def _host_ok(self):
                return self.headers.get("Host") in (f"127.0.0.1:{ed.port}", f"localhost:{ed.port}")

            def _authorized(self):
                got = self.headers.get("X-CAC-Token", "")
                return self._host_ok() and hmac.compare_digest(got.encode(), ed.token.encode())

            def _file(self, q):
                i = int(q.get("f", ["0"])[0])
                if not 0 <= i < len(ed.files):
                    raise IndexError("no such file")
                return i, ed.files[i]

            def do_GET(self):
                ed.last = time.time()
                u = urlparse(self.path)
                q = parse_qs(u.query)
                if u.path == "/":
                    if not self._host_ok():
                        return self._send(403, "forbidden", "text/plain")
                    nonce = secrets.token_urlsafe(16)
                    html = open(PAGE_HTML, encoding="utf-8").read().replace("{{NONCE}}", nonce)
                    csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                           "img-src blob:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
                           "frame-ancestors 'none'")
                    return self._send(200, html, "text/html; charset=utf-8", {"Content-Security-Policy": csp})
                if not self._authorized():
                    return self._send(403, "forbidden", "text/plain")
                try:
                    if u.path == "/api/files":
                        return self._json([{"index": i, "name": os.path.basename(p)} for i, p in enumerate(ed.files)])
                    if u.path == "/api/form":
                        i, path = self._file(q)
                        model = review_model(path)
                        if i in ed.input_notes:
                            model["notes"] = [ed.input_notes[i], *(model.get("notes") or [])]
                        return self._json(model)
                    if u.path == "/api/page":
                        return self._send(200, render_page(self._file(q)[1], int(q.get("p", ["0"])[0])), "image/png")
                    if u.path == "/api/preview":
                        if not ed.pending:
                            return self._send(404, "nothing to review", "text/plain")
                        return self._send(200, render_page(ed.pending["stage"], int(q.get("p", ["0"])[0])), "image/png")
                    if u.path == "/api/ping":
                        return self._json({"ok": True})
                except Exception as e:
                    return self._json({"error": cac_sign.printable(f"{type(e).__name__}: {e}")}, 500)
                return self._send(404, "not found", "text/plain")

            def do_POST(self):
                ed.last = time.time()
                if not self._authorized():
                    return self._send(403, "forbidden", "text/plain")
                n = int(self.headers.get("Content-Length", "0"))
                if n > MAX_BODY:
                    return self._send(413, "too large", "text/plain")
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                    u = urlparse(self.path)
                    if u.path == "/api/fill":
                        i, path = self._file({"f": [str(body.get("f", 0))]})
                        res = fill_copy(path, body, ed.extra)
                        if res.get("output"):
                            ed.outputs[i] = res.pop("output")  # the page never needs full paths
                        return self._json(res)
                    if u.path == "/api/prepare":
                        ed.discard()
                        i, path = self._file({"f": [str(body.get("f", 0))]})
                        res, pending = prepare(path, body, ed.stage_dir, ed.extra)
                        if pending:
                            ed.pending = dict(pending, f=i)
                        return self._json(res)
                    if u.path == "/api/commit":
                        if not ed.pending:
                            return self._json({"ok": False, "text": "Nothing has been reviewed yet."})
                        if ed.pending.get("needs_ack") and body.get("ack") is not True:
                            # keep the review open: tick the box and try again
                            return self._json({"ok": False, "text": ACK_REFUSAL, "needs_ack": True})
                        pending, ed.pending = ed.pending, None
                        res = commit(pending, ed.extra, ack=body.get("ack") is True)
                        if res.get("output"):
                            ed.outputs[pending["f"]] = res.pop("output")
                        return self._json(res)
                    if u.path == "/api/discard":
                        ed.discard()
                        return self._json({"ok": True})
                    if u.path == "/api/reveal":
                        out = ed.outputs.get(int(body.get("f", 0)))
                        if out and os.path.exists(out):
                            subprocess.run(["open", "-R", out])
                        return self._json({"ok": bool(out)})
                    if u.path == "/api/close":
                        ed.closed = True
                        return self._json({"ok": True})
                except Exception as e:
                    return self._json({"error": cac_sign.printable(f"{type(e).__name__}: {e}")}, 500)
                return self._send(404, "not found", "text/plain")

            def do_OPTIONS(self):  # no CORS: cross-origin pages cannot call this
                self._send(405, "no", "text/plain")

        return Handler


def main(argv=None):
    ap = argparse.ArgumentParser(description="Convert DOCX locally, enter information and CAC-sign a PDF in the browser.")
    ap.add_argument("files", nargs="+")
    ap.add_argument("--background", action="store_true", help="Start the page server and return at once.")
    ap.add_argument("--no-browser", action="store_true", help="Print the URL instead of opening it.")
    ap.add_argument("--opensc", action="store_true", help="Sign through OpenSC instead of macOS (see cac_sign.py).")
    args = ap.parse_args(argv)
    from document_inputs import supported
    files = [os.path.abspath(p) for p in args.files]
    if any(not supported(p) or not os.path.isfile(p) for p in files):
        ap.error("give existing PDF or DOCX files")
    if args.background and args.no_browser:
        ap.error("--no-browser prints the page's key, so it only runs in the foreground")
    extra = ["--opensc"] if args.opensc else []
    if args.background:
        # errors only, never the URL; the log is private to this user
        fd = os.open(os.path.expanduser("~/Library/Logs/cac-sign-editor.log"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        child = [sys.executable, os.path.abspath(__file__), *extra, *files]
        subprocess.Popen(child, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=fd,
                         start_new_session=True, cwd=HERE)
        os.close(fd)
        return 0
    try:
        ed = Editor(files, extra)
    except Exception as e:
        from cac_sign_finder import notify
        text = cac_sign.printable(f"Could not open the document: {e}")
        if not args.no_browser:
            notify(text)
        ap.error(text)
    if args.no_browser:
        print(ed.url, flush=True)
    else:
        open_browser(ed.url)
    ed.serve()
    return 0


def open_browser(url):
    """Open the page without putting its token in any process's arguments."""
    try:
        from AppKit import NSWorkspace
        from Foundation import NSURL
        if NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(url)):
            return
    except ImportError:
        pass
    subprocess.run(["osascript"], input=f'open location "{url}"', text=True)  # script on stdin, not argv


if __name__ == "__main__":
    sys.exit(main())
