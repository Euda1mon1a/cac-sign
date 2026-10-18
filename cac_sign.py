#!/usr/bin/env python3
"""
CAC-sign any existing PDF on this Mac -- no AVHE, no Adobe.

Fills form fields, signs a signature field with the CAC, and verifies the result
before calling it done. Everything is written as an incremental update (appended,
never rewritten), so signatures already on the form -- e.g. the applicant's --
stay valid. A form you sign often gets a profile in profiles/ that knows which
boxes to fill and which field to sign; it is picked automatically.

Usage:
  cac_sign.py FORM.pdf [MORE.pdf ...]          # auto: profile -> fill -> sign -> verify
  cac_sign.py FORM.pdf --list                  # fields, signatures, page sizes; changes nothing
  cac_sign.py FORM.pdf --verify                # check every signature; exit 1 if any fails
  cac_sign.py FORM.pdf --set "Date=14 OCT 2026" --set "Yes=x" --field Signature
  cac_sign.py MEMO.pdf --place 1:400,60,580,100   # no signature field: create one, then sign
  cac_sign.py FORM.pdf --dry-run               # show what would happen
  cac_sign.py --list-certs                     # certificates on the inserted CAC

Output defaults to "<name>_signed.pdf" next to the input. If verification fails the
file is kept as "<name>_signed.FAILED.pdf" and the exit code is 1.

The PIN: on a Mac, macOS asks for it in its own smart-card window, once per file,
and this program never sees it. --opensc instead types one PIN here for a whole
batch, through OpenSC.
"""
from __future__ import annotations
import argparse, datetime, glob, json, os, re, sys

from cac_signing import (SigningCancelled, SigningError, build_keychain_signer, build_pkcs11_signer,
                         build_pkcs12_signer, find_pkcs11_lib, list_keychain_certs, list_pkcs11_certs, printable,
                         read_pin, stamp_style, verify_signatures)

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(HERE, "profiles")
# Your data lives here, apart from the code, so reinstalling or updating never touches it.
APP_SUPPORT = os.path.expanduser("~/Library/Application Support/cac-sign")
IDENTITY_FILE = os.path.join(APP_SUPPORT, "identity.json")


def say(*lines):
    """Print each line through printable(): text from a PDF must not drive the terminal."""
    for line in lines:
        print(printable(line))


FF_READONLY = 1 << 0
FF_MULTILINE = 1 << 12
FF_RADIO = 1 << 15
FF_PUSHBUTTON = 1 << 16
FF_COMB = 1 << 24
TRUTHY = {"x", "yes", "y", "true", "on", "1", "checked"}
FALSY = {"", "no", "n", "false", "off", "0", "unchecked"}

# (ascent, descent) of each standard font's bbox, used to centre text vertically.
FONT_METRICS = {"Helvetica": (0.931, 0.225), "Times-Roman": (0.898, 0.218), "Courier": (0.805, 0.250)}


def today_mil():
    return datetime.date.today().strftime("%d %b %Y").upper().lstrip("0")


def _get(d, key, default=None):
    """dict.get that dereferences indirect objects (pyHanko's .get does not)."""
    if d is None or key not in d:
        return default
    return d[key]


def norm(s):
    return " ".join(str(s).split()).lower()


# ---------- reading the form ----------
class FormField:
    """One terminal form field: its dictionary, reference, widgets, inherited attributes."""

    def __init__(self, name, obj, ref, widgets, inherited):
        self.name, self.obj, self.ref, self.widgets, self.inh = name, obj, ref, widgets, inherited

    @property
    def ft(self):
        return str(self.inh.get("/FT", ""))

    @property
    def flags(self):
        return int(self.inh.get("/Ff", 0))

    @property
    def value(self):
        v = _get(self.obj, "/V", self.inh.get("/V"))
        return None if v is None else str(v)

    @property
    def kind(self):
        if self.ft == "/Tx":
            return "text"
        if self.ft == "/Ch":
            return "choice"
        if self.ft == "/Sig":
            return "signature"
        if self.ft == "/Btn":
            if self.flags & FF_PUSHBUTTON:
                return "button"
            return "radio" if self.flags & FF_RADIO else "checkbox"
        return self.ft or "?"


def walk_fields(acroform):
    """Map fully-qualified field name -> FormField. Works on pyHanko reader or writer objects."""
    out = {}

    def visit(ref, parent_name, inherited):
        obj = ref.get_object()
        t = _get(obj, "/T")
        name = f"{parent_name}.{t}" if (parent_name and t is not None) else (str(t) if t is not None else parent_name)
        inh = dict(inherited)
        for k in ("/FT", "/Ff", "/DA", "/Q", "/V", "/MaxLen"):
            if k in obj:
                inh[k] = obj[k]
        kids = _get(obj, "/Kids")
        kid_refs = [kids.raw_get(i) for i in range(len(kids))] if kids is not None else []
        if any("/T" in r.get_object() for r in kid_refs):
            for r in kid_refs:
                visit(r, name, inh)
        else:
            widgets = kid_refs if kid_refs else [ref]
            out[name] = FormField(name, obj, ref, widgets, inh)

    fields = _get(acroform, "/Fields")
    if fields is not None:
        for i in range(len(fields)):
            visit(fields.raw_get(i), "", {})
    return out


def find_field(fields, name):
    if name in fields:
        return fields[name]
    hits = [f for n, f in fields.items() if norm(n) == norm(name)]
    if len(hits) == 1:
        return hits[0]
    raise LookupError(f'no field named "{name}"' + (" (ambiguous)" if hits else ""))


def locked_fields(root, fields):
    """Fields the existing signatures forbid changing: (set of names, 'all' flag, reason or None)."""
    locked, lock_all, no_changes = set(), False, None
    perms = _get(root, "/Perms")
    if perms is not None and "/DocMDP" in perms:
        for ref in _get(perms["/DocMDP"], "/Reference", []) or []:
            ref = ref.get_object() if hasattr(ref, "get_object") else ref
            if str(_get(ref, "/TransformMethod")) == "/DocMDP":
                p = int(_get(_get(ref, "/TransformParams", {}), "/P", 2))
                if p == 1:
                    no_changes = "the document is certified with 'no changes allowed'"
    for f in fields.values():
        if f.kind != "signature" or "/V" not in f.obj:
            continue
        for ref in _get(f.obj["/V"], "/Reference", []) or []:
            ref = ref.get_object() if hasattr(ref, "get_object") else ref
            if str(_get(ref, "/TransformMethod")) != "/FieldMDP":
                continue
            tp = _get(ref, "/TransformParams", {})
            action, names = str(_get(tp, "/Action", "")), [str(n) for n in _get(tp, "/Fields", [])]
            if action == "/All":
                lock_all = True
            elif action == "/Include":
                locked.update(names)
            elif action == "/Exclude":
                locked.update(n for n in fields if n not in names)
    return locked, lock_all, no_changes


# ---------- appearance streams ----------
def _font_for(acroform, da, writer, cache):
    """Resolve the DA font to (resource name, font object or ref, metrics name)."""
    from pyhanko.pdf_utils import generic
    m = re.search(r"/([^\s/\[\]()<>]+)\s+[\d.]+\s+Tf", da or "")
    name = m.group(1) if m else "Helv"
    dr = _get(acroform, "/DR")
    fonts = _get(dr, "/Font")
    if fonts is not None and f"/{name}" in fonts:
        fref = fonts.raw_get(f"/{name}")
        base = str(_get(fonts[f"/{name}"], "/BaseFont", ""))
    else:
        if "helv" not in cache:
            cache["helv"] = writer.add_object(generic.DictionaryObject({
                generic.NameObject("/Type"): generic.NameObject("/Font"),
                generic.NameObject("/Subtype"): generic.NameObject("/Type1"),
                generic.NameObject("/BaseFont"): generic.NameObject("/Helvetica"),
                generic.NameObject("/Encoding"): generic.NameObject("/WinAnsiEncoding")}))
        name, fref, base = "Helv", cache["helv"], "Helvetica"
    metrics = "Times-Roman" if "Tim" in base or name.startswith("TiRo") else \
              "Courier" if "Cour" in base or name.startswith("Cour") else "Helvetica"
    return name, fref, metrics


def _pdf_str(s):
    b = s.encode("cp1252")  # WinAnsi; raises on characters a standard font cannot show
    return b.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def text_appearance(writer, acroform, field, widget, text, cache):
    from pyhanko.pdf_utils import generic
    from reportlab.pdfbase.pdfmetrics import stringWidth
    x1, y1, x2, y2 = [float(v) for v in widget["/Rect"]]
    w, h = abs(x2 - x1), abs(y2 - y1)
    da = str(field.inh.get("/DA") or _get(acroform, "/DA") or "/Helv 0 Tf 0 g")
    fname, fref, metrics = _font_for(acroform, da, writer, cache)
    m = re.search(r"([\d.]+)\s+Tf", da)
    size = float(m.group(1)) if m else 0.0
    cm = re.findall(r"((?:[\d.]+\s+){1,4}(?:g|rg|k))\b", da)
    color = cm[-1].strip() if cm else "0 g"
    asc, desc = FONT_METRICS[metrics]
    multiline = bool(field.flags & FF_MULTILINE)
    lines = text.split("\n") if multiline else [text.replace("\n", " ")]
    if size == 0:  # auto: what Acrobat does -- fill the box height, then shrink to fit the width
        size = (h - 2) * 0.741 if not multiline else 12.0
        widest = max((stringWidth(l, metrics, 1) for l in lines), default=0)
        if widest:
            size = min(size, (w - 4) / widest)
        size = max(size, 4.0)
    q = int(field.inh.get("/Q", 0))
    ops = [b"/Tx BMC", b"q", f"1 1 {w - 2:.2f} {h - 2:.2f} re W n".encode(), b"BT",
           f"/{fname} {size:.3f} Tf".encode(), color.encode()]
    maxlen = int(field.inh.get("/MaxLen", 0) or 0)
    if field.flags & FF_COMB and maxlen and not multiline:
        cell = w / maxlen
        y = (h - size * (asc + desc)) / 2 + size * desc
        for i, ch in enumerate(lines[0][:maxlen]):
            x = i * cell + (cell - stringWidth(ch, metrics, size)) / 2
            ops.append(f"1 0 0 1 {x:.3f} {y:.3f} Tm".encode() + b" (" + _pdf_str(ch) + b") Tj")
    else:
        lead = size * 1.15
        y = (h - 2 - size * asc) if multiline else (h - size * (asc + desc)) / 2 + size * desc
        for line in lines:
            tw = stringWidth(line, metrics, size)
            x = 2 if q == 0 else (w - tw) / 2 if q == 1 else w - 2 - tw
            ops.append(f"1 0 0 1 {x:.3f} {y:.3f} Tm".encode() + b" (" + _pdf_str(line) + b") Tj")
            y -= lead
    ops += [b"ET", b"Q", b"EMC"]
    stream = generic.StreamObject({
        generic.NameObject("/Type"): generic.NameObject("/XObject"),
        generic.NameObject("/Subtype"): generic.NameObject("/Form"),
        generic.NameObject("/BBox"): generic.ArrayObject([generic.FloatObject(0), generic.FloatObject(0),
                                                          generic.FloatObject(round(w, 3)), generic.FloatObject(round(h, 3))]),
        generic.NameObject("/Resources"): generic.DictionaryObject({
            generic.NameObject("/Font"): generic.DictionaryObject({generic.NameObject(f"/{fname}"): fref})}),
    }, stream_data=b"\n".join(ops) + b"\n")
    return writer.add_object(stream)


def _on_states(widget):
    n = _get(_get(widget, "/AP"), "/N")
    return [str(k) for k in n.keys() if str(k) != "/Off"] if n is not None else []


# ---------- filling ----------
def _touch(writer, ref):
    from pyhanko.pdf_utils import generic
    if isinstance(ref, generic.IndirectObject):
        writer.mark_update(ref)
    else:  # direct object inside a container: mark the container
        writer.update_container(ref)


def set_field(writer, acroform, field, value, cache):
    """Set one field's value and regenerate its appearance. Returns a short description."""
    from pyhanko.pdf_utils import generic
    kind = field.kind
    if kind in ("text", "choice"):
        field.obj["/V"] = generic.TextStringObject(value)
        _touch(writer, field.ref)
        for wref in field.widgets:
            wd = wref.get_object()
            wd["/AP"] = generic.DictionaryObject({generic.NameObject("/N"):
                                                  text_appearance(writer, acroform, field, wd, value, cache)})
            _touch(writer, wref)
        return value
    if kind in ("checkbox", "radio"):
        v = value.strip()
        if kind == "checkbox":
            if v.lower() in TRUTHY:
                states = [s for wref in field.widgets for s in _on_states(wref.get_object())]
                if not states:
                    raise ValueError("checkbox has no 'on' appearance")
                target = states[0]
            elif v.lower() in FALSY:
                target = "/Off"
            else:
                raise ValueError(f'checkbox value must be yes/no, got "{value}"')
        else:
            want = "/" + v.lstrip("/")
            names = {s.lower(): s for wref in field.widgets for s in _on_states(wref.get_object())}
            if v.lower() in FALSY:
                target = "/Off"
            elif want.lower() in names:
                target = names[want.lower()]
            else:
                raise ValueError(f'radio choice must be one of {sorted(n.lstrip("/") for n in names.values())}')
        field.obj["/V"] = generic.NameObject(target)
        _touch(writer, field.ref)
        for wref in field.widgets:
            wd = wref.get_object()
            wd["/AS"] = generic.NameObject(target if target in _on_states(wd) else "/Off")
            _touch(writer, wref)
        return "checked" if target != "/Off" else "unchecked"
    raise ValueError(f"cannot fill a {kind} field")


# ---------- profiles ----------
def load_profiles():
    out = []
    for p in sorted(glob.glob(os.path.join(PROFILE_DIR, "*.json"))):
        with open(p) as f:
            prof = json.load(f)
        prof.setdefault("id", os.path.splitext(os.path.basename(p))[0])
        out.append(prof)
    return out


def detect_profile(profiles, fields, forced=None):
    if forced == "none":
        return None
    if forced:
        for p in profiles:
            if p["id"] == forced:
                return p
        raise SystemExit(f'No profile "{forced}". Have: {", ".join(p["id"] for p in profiles) or "none"}')
    names = {norm(n) for n in fields}
    hits = [p for p in profiles if all(norm(m) in names for m in p.get("match_fields", []))]
    return max(hits, key=lambda p: len(p["match_fields"])) if hits else None


def load_identity():
    if not os.path.exists(IDENTITY_FILE):
        return {}
    with open(IDENTITY_FILE) as f:
        return json.load(f)


def expand(value, identity):
    def sub(m):
        key = m.group(1)
        if key == "today":
            return today_mil()
        if key not in identity:
            raise SystemExit(f'Profile needs "{key}" -- add it to {IDENTITY_FILE} '
                             f'(see identity.example.json), or re-run install.sh to be asked.')
        return str(identity[key])
    return re.sub(r"\$\{(\w+)\}", sub, value)


# ---------- the signing log ----------
# One JSON line per signature this tool made: when, the original's and the output's
# SHA-256, the output path, signer and field. No form content. It lets the tool say
# "you already signed this exact file", and `--history` lists it. Private to the user.
LEDGER = os.path.join(APP_SUPPORT, "ledger.jsonl")


def file_sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ledger_entries():
    try:
        with open(LEDGER, encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue  # a damaged line never hides the rest
    return out


def ledger_record(entry):
    os.makedirs(os.path.dirname(LEDGER), mode=0o700, exist_ok=True)
    fd = os.open(LEDGER, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")


def _when(iso):
    try:
        return datetime.datetime.fromisoformat(iso).astimezone().strftime("%d %b %Y %H:%M")
    except (TypeError, ValueError):
        return "?"


def newest_first(entries):
    """Newest first; within the same second, the later line in the log is newer."""
    return [e for _, e in sorted(enumerate(entries), key=lambda ie: (ie[1].get("when", ""), ie[0]), reverse=True)]


def signed_before(sha):
    """Earlier signatures this tool made on a file with exactly these bytes, newest first."""
    return newest_first([e for e in ledger_entries() if e.get("input_sha256") == sha])


# ---------- planning ----------
class Plan:
    def __init__(self, path):
        self.path, self.out = path, None
        self.profile = None
        self.fills = []          # (field name, new value, old value)
        self.sign_field = None
        self.place = None        # (page index, rect)
        self.existing = []       # verify_signatures() of the input
        self.skip = None         # reason this file is skipped
        self.notes = []


def parse_place(spec):
    m = re.fullmatch(r"\s*(\d+)\s*:\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*", spec)
    if not m:
        raise SystemExit('--place must look like PAGE:X1,Y1,X2,Y2 (points from the bottom-left, page 1 = first)')
    return int(m.group(1)) - 1, tuple(float(m.group(i)) for i in range(2, 6))


def make_plan(path, args, profiles, identity):
    from pyhanko.pdf_utils.reader import PdfFileReader
    plan = Plan(path)
    stem, _ = os.path.splitext(path)
    plan.out = args.out or f"{stem}{args.suffix}.pdf"
    if not args.no_sign and not args.force:
        from pdf_readers import reader_problems  # imported here: pdf_readers imports this module
        disagree = reader_problems(path)
        if disagree:
            plan.skip = ("PDF readers do not agree on this file (" + "; ".join(disagree) + "), so what you check "
                         "here might not be what Acrobat shows -- check it in Acrobat; --force signs anyway")
            return plan
    if not args.no_sign:
        from pdf_readers import viewer_warnings
        try:
            warns = viewer_warnings(path)
        except Exception as e:  # only reachable with --force, which skipped the readers gate
            warns = [f"could not check how other viewers show this file ({type(e).__name__})"]
        plan.notes += [f"WARNING: {w}" for w in warns]
        for e in signed_before(file_sha256(path))[:2]:
            plan.notes.append(f"NOTE: you already signed this exact file on {_when(e.get('when'))} "
                              f"-> {os.path.basename(e.get('output', '?'))}")
    with open(path, "rb") as f:
        r = PdfFileReader(f, strict=False)
        acro = _get(r.root, "/AcroForm")
        fields = walk_fields(acro) if acro is not None else {}
        npages = int(r.root["/Pages"]["/Count"])
        plan.existing = verify_signatures(path)
        plan.profile = detect_profile(profiles, fields, args.profile)
        prof = plan.profile or {}

        broken = [s for s in plan.existing if not s["ok"]]
        if broken and not args.force:
            plan.skip = ("an existing signature does not check out (" +
                         "; ".join(f'{s["field"]}: {s["detail"]}' for s in broken) +
                         ") -- start from a clean copy, or check it in Acrobat and use --force")
            return plan
        signed = {s["field"] for s in plan.existing}
        for need in prof.get("require_signed", []):
            if need not in signed:
                plan.skip = f'"{need}" is not signed yet -- the profile expects it first'
                return plan

        # requested fills: profile first, then --set (which wins)
        requested = []
        for fname, spec in ({} if getattr(args, "no_profile_fill", False) else prof.get("fill", {})).items():
            spec = spec if isinstance(spec, dict) else {"value": spec}
            requested.append((fname, spec["value"], bool(spec.get("if_empty")), True))
        for kv in args.set or []:
            if "=" not in kv:
                raise SystemExit(f'--set needs NAME=VALUE, got "{kv}"')
            k, v = kv.split("=", 1)
            requested = [x for x in requested if norm(x[0]) != norm(k)] + [(k, v, False, False)]
        locked, lock_all, no_changes = locked_fields(r.root, fields)
        if no_changes:  # never forceable: any change voids the certification
            plan.skip = f"{no_changes}; any fill or signature would break that certification"
            return plan
        for fname, value, if_empty, from_profile in requested:
            try:
                fld = find_field(fields, fname)
            except LookupError as e:
                plan.skip = str(e)
                return plan
            old = fld.value
            if if_empty and old not in (None, "", "/Off"):
                plan.notes.append(f'kept {fld.name} = "{old}"')
                continue
            if fld.kind == "signature":
                plan.skip = f'"{fld.name}" is a signature field; use --field to sign it'
                return plan
            if fld.flags & FF_READONLY and not args.force:
                plan.skip = f'"{fld.name}" is read-only (--force to fill anyway)'
                return plan
            if no_changes or lock_all or fld.name in locked:
                plan.skip = f'"{fld.name}" is locked by an existing signature' + (f" ({no_changes})" if no_changes else "")
                return plan
            # expand ${...} only now, so identity keys are needed only for boxes actually filled
            plan.fills.append((fld.name, expand(value, identity) if from_profile else value, old))

        if args.no_sign:
            return plan
        if args.place:
            pi, rect = parse_place(args.place)
            if not 0 <= pi < npages:
                raise SystemExit(f"--place page {pi + 1} is out of range (document has {npages})")
            plan.place, plan.sign_field = (pi, rect), args.field or "Signature1"
            if plan.sign_field in fields:
                plan.skip = f'a field named "{plan.sign_field}" already exists; pick another --field'
            return plan
        want = args.field or prof.get("sign_field")
        empty = [f.name for f in fields.values() if f.kind == "signature" and "/V" not in f.obj]
        if want:
            try:
                fld = find_field(fields, want)
            except LookupError as e:
                plan.skip = str(e)
                return plan
            if fld.kind != "signature":
                plan.skip = f'"{fld.name}" is not a signature field'
            elif "/V" in fld.obj:
                who = next((s["signer"] for s in plan.existing if s["field"] == fld.name), "someone")
                plan.skip = f'"{fld.name}" is already signed by {who}'
            else:
                plan.sign_field = fld.name
        elif len(empty) == 1:
            plan.sign_field = empty[0]
        elif not empty:
            plan.skip = "no empty signature field -- use --place PAGE:X1,Y1,X2,Y2 to add one (see --list for page sizes)"
        else:
            plan.skip = f"several empty signature fields ({', '.join(empty)}) -- pick one with --field"
    return plan


def describe(plan):
    head = os.path.basename(plan.path)
    if plan.profile:
        head += f"  [{plan.profile.get('description', plan.profile['id'])}]"
    lines = [head]
    for s in plan.existing:
        state = "valid" if s["ok"] else f"PROBLEM: {s['detail']}"
        lines.append(f"    already signed: {s['field']} by {s['signer']}, {s['when']} ({state})")
    if plan.skip:
        lines.append(f"    SKIP: {plan.skip}")
        return lines
    for n in plan.notes:
        lines.append(f"    {n}")
    if plan.fills:
        lines.append("    will fill:  " + ";  ".join(f"{n} = {v}" for n, v, _ in plan.fills))
    if plan.sign_field:
        where = f" (new field on page {plan.place[0] + 1})" if plan.place else ""
        lines.append(f"    will sign:  {plan.sign_field}{where}")
    lines.append(f"    output:     {plan.out}")
    return lines


# ---------- doing it ----------
def audit_last_revision(path, touched, place_page=None):
    """Existing objects the newest revision overwrote that it had no business touching.

    Allowed: the fields named in `touched` (filled + signed) and their widgets, the
    document-level objects pyHanko's signer always rewrites (catalog, AcroForm and its
    /Fields array, document info, XMP metadata), and -- for --place -- the target page
    and its /Annots array. New objects are always fine; that is how an update adds.
    Returns a list of short descriptions; empty means clean.
    """
    from pyhanko.pdf_utils import generic
    from pyhanko.pdf_utils.reader import PdfFileReader

    def key(x):
        if isinstance(x, generic.IndirectObject):
            x = x.reference
        return (x.idnum, x.generation) if isinstance(x, generic.Reference) else None

    with open(path, "rb") as f:
        r = PdfFileReader(f, strict=False)
        allowed = {key(r.root_ref)}
        for obj, k in ((r.trailer, "/Info"), (r.root, "/Metadata"), (r.root, "/AcroForm")):
            if k in obj:
                allowed.add(key(obj.raw_get(k)))
        acro = _get(r.root, "/AcroForm")
        if acro is not None and "/Fields" in acro:
            allowed.add(key(acro.raw_get("/Fields")))
        fields = walk_fields(acro) if acro is not None else {}
        for name in touched:
            if name in fields:
                allowed.add(key(fields[name].ref))
                allowed.update(key(wr) for wr in fields[name].widgets)
        if place_page is not None:
            pages = _get(r.root, "/Pages")
            # walk the page tree to the target page (flat trees are the norm for forms)
            kids = [_get(pages, "/Kids").raw_get(i) for i in range(len(_get(pages, "/Kids")))]
            stack, flat = list(kids), []
            while stack:
                ref = stack.pop(0)
                node = ref.get_object()
                if str(_get(node, "/Type")) == "/Pages":
                    allowed.add(key(ref))
                    stack[:0] = [_get(node, "/Kids").raw_get(i) for i in range(len(_get(node, "/Kids")))]
                else:
                    flat.append(ref)
            allowed.add(key(r.root.raw_get("/Pages")))
            if place_page < len(flat):
                pg = flat[place_page]
                allowed.add(key(pg))
                if "/Annots" in pg.get_object():
                    allowed.add(key(pg.get_object().raw_get("/Annots")))
        allowed.discard(None)
        last = r.xrefs.total_revisions - 1
        bad = []
        for ref in r.xrefs.explicit_refs_in_revision(last):
            if r.xrefs.get_introducing_revision(ref) < last and (ref.idnum, ref.generation) not in allowed:
                obj = r.get_object(ref)
                label = next((str(obj.raw_get(k)) for k in ("/T", "/Type", "/Subtype")
                              if hasattr(obj, "raw_get") and k in obj), type(obj).__name__)
                bad.append(f"object {ref.idnum} ({label})")
        return bad


def execute(plan, signer, reason):
    """Write fills + signature as one incremental update, verify, then move into place."""
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.sign import fields as sigfields, signers as _s
    import tempfile
    d, base = os.path.split(plan.out)
    in_sha = file_sha256(plan.path)
    # A random name, created exclusively and written through its descriptor, so a symlink planted
    # at a predictable name cannot redirect the write.
    fd, tmp = tempfile.mkstemp(dir=d or ".", prefix=f".{base}.", suffix=".partial")
    try:
        umask = os.umask(0)
        os.umask(umask)
        os.fchmod(fd, 0o666 & ~umask)  # what a plain open() would have given the output
        with open(plan.path, "rb") as inf, os.fdopen(fd, "wb") as outf:
            fd = None  # outf owns it now
            w = IncrementalPdfFileWriter(inf, strict=False)
            acro = w.root["/AcroForm"] if "/AcroForm" in w.root else None
            cache = {}
            if plan.fills:
                fields = walk_fields(acro)
                for name, value, _ in plan.fills:
                    set_field(w, acro, fields[name], value, cache)
            spec = None
            if plan.place:
                spec = sigfields.SigFieldSpec(sig_field_name=plan.sign_field, box=plan.place[1], on_page=plan.place[0])
            if signer is None:
                if spec:
                    sigfields.append_signature_field(w, spec)
                w.write(outf)
            else:
                meta = _s.PdfSignatureMetadata(field_name=plan.sign_field, reason=reason)
                _s.PdfSigner(meta, signer=signer, new_field_spec=spec, stamp_style=stamp_style()).sign_pdf(
                    w, existing_fields_only=spec is None, output=outf)
    except BaseException:
        if fd is not None:
            os.close(fd)
        if os.path.lexists(tmp):
            os.remove(tmp)
        raise
    result = verify_signatures(tmp)
    # Integrity is the gate here. Trust and earlier changes were checked on the input
    # (make_plan), and this revision's own changes are checked by audit_last_revision,
    # which unlike pyHanko accepts radio-button fills.
    problems = [f'{s["field"]}: {s["detail"]}' for s in result if not (s["intact"] and s["valid"])]
    if signer is not None:
        new = [s for s in result if s["field"] == plan.sign_field]
        if not new:
            problems.append(f"{plan.sign_field}: new signature not found in output")
        elif new[0]["coverage"] != "ENTIRE_FILE":
            problems.append(f"{plan.sign_field}: new signature does not cover the whole file ({new[0]['coverage']})")
    stray = audit_last_revision(tmp, [n for n, _, _ in plan.fills] + ([plan.sign_field] if plan.sign_field else []),
                                plan.place[0] if plan.place else None)
    if stray:
        problems.append("update touched objects it should not have: " + ", ".join(stray))
    if problems:
        failed = plan.out[:-4] + ".FAILED.pdf"
        os.replace(tmp, failed)
        return False, problems, failed
    os.replace(tmp, plan.out)
    if signer is not None:
        new = [s for s in result if s["field"] == plan.sign_field]
        try:
            ledger_record({"when": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                           "input_sha256": in_sha, "output": os.path.abspath(plan.out),
                           "output_sha256": file_sha256(plan.out), "field": plan.sign_field,
                           "signer": new[0]["signer"] if new else "?"})
        except OSError as e:  # the signature is done and verified; the log is a convenience
            plan.notes.append(f"could not write the signing log ({e})")
            plan.ledger_error = str(e)
    return True, result, plan.out


def show_fields(path):
    from pyhanko.pdf_utils.reader import PdfFileReader
    with open(path, "rb") as f:
        r = PdfFileReader(f, strict=False)
        acro = _get(r.root, "/AcroForm")
        fields = walk_fields(acro) if acro is not None else {}
        say(os.path.basename(path))
        import pikepdf
        with pikepdf.open(path) as pk:
            say(f"  pages: {len(pk.pages)}  (for --place: points, origin bottom-left)")
            for i, pg in enumerate(pk.pages):
                mb = [round(float(v)) for v in pg.mediabox]
                say(f"    page {i + 1}: {mb[2] - mb[0]} x {mb[3] - mb[1]}")
        sigs = {s["field"]: s for s in verify_signatures(path)}
        say(f"  fields: {len(fields)}")
        for n, fld in fields.items():
            if fld.kind == "signature":
                s = sigs.get(n)
                val = (f"signed by {s['signer']}, {s['when']} ({'valid' if s['ok'] else 'PROBLEM: ' + s['detail']})"
                       if s else "(empty, signable)")
            else:
                v = fld.value
                val = "(empty)" if v in (None, "") else f'"{v}"'
            flags = " read-only" if fld.flags & FF_READONLY else ""
            say(f"    {n:40.40}  {fld.kind:9} {val}{flags}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="CAC-sign existing PDFs on this Mac (fill, sign, verify).",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("Usage:")[1])
    ap.add_argument("files", nargs="*", help="PDF(s) to sign")
    ap.add_argument("--list", action="store_true", help="Show fields and signatures, change nothing.")
    ap.add_argument("--verify", action="store_true",
                    help="Check every signature (intact, trusted, later changes form filling only); exit 1 if any fails.")
    ap.add_argument("--set", action="append", metavar="NAME=VALUE", help="Fill a field (repeatable). Checkboxes: yes/no.")
    ap.add_argument("--field", help="Signature field to sign (default: profile's, or the only empty one).")
    ap.add_argument("--place", metavar="PAGE:X1,Y1,X2,Y2", help="Create a new signature field there, then sign it.")
    ap.add_argument("--profile", help='Force a profile id from profiles/, or "none".')
    ap.add_argument("--no-sign", action="store_true", help="Fill only; write without signing.")
    ap.add_argument("--dry-run", action="store_true", help="Show the plan and stop.")
    ap.add_argument("--no-profile-fill", action="store_true",
                    help="Use the profile's checks and signature field, but fill only what --set says "
                         "(the review page sends every value itself).")
    ap.add_argument("-o", "--out", help="Output path (single input only).")
    ap.add_argument("--suffix", default=None, help='Output name suffix (default "_signed", or "_filled" with --no-sign).')
    ap.add_argument("--force", action="store_true",
                    help="Overwrite output; fill read-only fields; sign even though an existing signature fails its "
                         "check or PDF readers disagree on the file.")
    ap.add_argument("--reason", default=None, help="Signature reason (default: the profile's).")
    ap.add_argument("--list-certs", action="store_true", help="List certificates on the inserted CAC, then exit.")
    ap.add_argument("--history", action="store_true", help="List the signatures this tool has made (newest first), then exit.")
    # signer options
    ap.add_argument("--opensc", action="store_true",
                    help="Sign through OpenSC: type one PIN here for the whole batch, instead of macOS asking per file.")
    ap.add_argument("--pkcs11-lib", default=None, help="PKCS#11 module for --opensc (default: auto-detected).")
    ap.add_argument("--slot", type=int, default=None)
    ap.add_argument("--token-label", default=None)
    ap.add_argument("--cert-label", default=None)
    ap.add_argument("--key-label", default=None)
    ap.add_argument("--cert-id", default=None)
    ap.add_argument("--key-id", default=None)
    ap.add_argument("--p12", help="PKCS#12 software cert for testing instead of a CAC.")
    ap.add_argument("--p12-pass", default=None)
    args = ap.parse_args(argv)
    if args.suffix is None:
        args.suffix = "_filled" if args.no_sign else "_signed"

    use_pkcs11 = args.opensc or args.pkcs11_lib or sys.platform != "darwin"
    if args.history:
        entries = newest_first(ledger_entries())
        if not entries:
            say("No signatures recorded yet.")
        for e in entries[:50]:
            out = e.get("output", "?")
            gone = "" if os.path.exists(out) else "  (file no longer there)"
            say(f"{_when(e.get('when'))}  {os.path.basename(out)}  -- {e.get('field', '?')}, {e.get('signer', '?')}{gone}")
        return 0
    if args.list_certs:
        if use_pkcs11:
            list_pkcs11_certs(find_pkcs11_lib(args.pkcs11_lib), os.environ.get("CAC_PIN"))
        else:
            list_keychain_certs()
        return 0
    if not args.files:
        ap.error("give one or more PDFs")
    if args.out and len(args.files) > 1:
        ap.error("-o/--out works with a single input; use --suffix for batches")
    missing = [p for p in args.files if not os.path.isfile(p)]
    if missing:
        ap.error("not found: " + ", ".join(missing))

    if args.list:
        for p in args.files:
            show_fields(p)
        return 0
    if args.verify:
        bad = 0
        for p in args.files:
            try:
                res = verify_signatures(p)
            except Exception as e:
                say(os.path.basename(p), f"    PROBLEM: could not read its signatures ({type(e).__name__}: {e})")
                bad += 1
                continue
            say(os.path.basename(p) + ("" if res else "  (no signatures)"))
            for s in res:
                verdict = "OK" if s["ok"] else f"PROBLEM: {s['detail']}"
                say(f"    {s['field']}: {s['signer']}, {s['when']} -- {verdict}")
                bad += not s["ok"]
        return 1 if bad else 0

    profiles, identity = load_profiles(), load_identity()
    plans = []
    for p in args.files:
        try:
            plans.append(make_plan(p, args, profiles, identity))
        except Exception as e:
            pl = Plan(p)
            pl.skip = f"could not read it ({type(e).__name__}: {e})"
            plans.append(pl)
    for pl in plans:
        if not pl.skip and os.path.exists(pl.out) and not args.force:
            pl.skip = f"{os.path.basename(pl.out)} already exists (--force to overwrite)"
        if not pl.skip and not pl.fills and not pl.sign_field:
            pl.skip = "nothing to do"
    for pl in plans:
        say(*describe(pl))
    todo = [pl for pl in plans if not pl.skip]
    if args.dry_run or not todo:
        return 0 if todo else 1  # dry run too: 1 means nothing would be done

    signer = None
    if not args.no_sign:
        if args.p12:
            signer = build_pkcs12_signer(args)
        elif use_pkcs11:
            args.pkcs11_lib = find_pkcs11_lib(args.pkcs11_lib)
            signer = build_pkcs11_signer(args, read_pin())  # one PIN for the whole batch
        else:
            signer = build_keychain_signer()  # macOS asks for the PIN, per signature

    failed = 0
    say("")
    for pl in todo:
        reason = args.reason or (pl.profile or {}).get("reason")
        try:
            ok, info, where = execute(pl, signer, reason)
        except (SigningCancelled, SigningError) as e:
            # a card problem, not a file problem: stop rather than prompt again for the next file
            failed += 1
            rest = len(todo) - todo.index(pl) - 1
            say(f"STOP  {os.path.basename(pl.path)}  -- {e}" + (f"; {rest} more file(s) not signed" if rest else ""))
            break
        except Exception as e:
            ok, info, where = False, [f"{type(e).__name__}: {e}"], None
        if ok:
            new = [s for s in info if s["field"] == pl.sign_field]
            who = f" by {new[0]['signer']}" if new else ""
            say(f"DONE  {os.path.basename(where)}  -- {'signed' + who if signer else 'filled'}, "
                f"all {len(info)} signature(s) verified intact")
            if getattr(pl, "ledger_error", None):
                say(f"      note: could not write the signing log ({pl.ledger_error})")
            for s in info:
                if not s["ok"]:
                    say(f"      note: {s['field']}: {s['detail']}")
            nxt = (pl.profile or {}).get("next_step")
            if nxt:
                say(f"      next: {nxt}")
        else:
            failed += 1
            say(f"FAIL  {os.path.basename(pl.path)}  -- " + "; ".join(info))
            if where:
                say(f"      kept for inspection: {where}")
    skipped = len(plans) - len(todo)
    if skipped:
        say("", f"{skipped} file(s) skipped (see above).")
    return 1 if failed or skipped else 0


if __name__ == "__main__":
    sys.exit(main())
