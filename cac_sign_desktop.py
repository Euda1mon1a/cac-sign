#!/usr/bin/env python3
"""
Sealed JSON-RPC worker for CACSign.app (macOS native desktop app).

Modes:
  python3 -I -B cac_sign_desktop.py          # one-shot JSON request on stdin -> JSON envelope on stdout
  python3 -I -B cac_sign_desktop.py --serve  # persistent line-delimited JSON RPC session on stdin/stdout

Never opens network listeners, never forwards proxy/site env, and never logs PINs.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import importlib.metadata
import io
import json
import os
import platform
import re
import secrets
import shutil
import sys
import tempfile
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import cac_sign  # noqa: E402
import cac_sign_editor  # noqa: E402
from cac_sign_finder import run_cli  # noqa: E402
import cac_signing  # noqa: E402
from cac_signing import printable, verify_signatures  # noqa: E402
import document_inputs  # noqa: E402
from pdf_readers import reader_problems, render_page, viewer_warnings  # noqa: E402

PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
PERSONA_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _app_support_dir() -> str:
    env = os.environ.get("CAC_SIGN_APP_SUPPORT")
    if env:
        return os.path.expanduser(env)
    if cac_sign.APP_SUPPORT != cac_sign._DEFAULT_APP_SUPPORT:
        return cac_sign.APP_SUPPORT
    default_ident = os.path.join(cac_sign._DEFAULT_APP_SUPPORT, "identity.json")
    if cac_sign.IDENTITY_FILE != default_ident:
        return os.path.dirname(os.path.abspath(cac_sign.IDENTITY_FILE))
    return cac_sign.APP_SUPPORT


def _identity_file() -> str:
    default_ident = os.path.join(cac_sign._DEFAULT_APP_SUPPORT, "identity.json")
    if cac_sign.IDENTITY_FILE != default_ident:
        return cac_sign.IDENTITY_FILE
    return os.path.join(_app_support_dir(), "identity.json")


def _personas_file() -> str:
    return os.path.join(_app_support_dir(), "personas.json")


def _user_profiles_dir() -> str:
    default_prof = os.path.join(cac_sign._DEFAULT_APP_SUPPORT, "profiles")
    if cac_sign.USER_PROFILE_DIR != default_prof:
        return cac_sign.USER_PROFILE_DIR
    return os.path.join(_app_support_dir(), "profiles")


def _ledger_file() -> str:
    default_ledger = os.path.join(cac_sign._DEFAULT_APP_SUPPORT, "ledger.jsonl")
    if cac_sign.LEDGER != default_ledger:
        return cac_sign.LEDGER
    return os.path.join(_app_support_dir(), "ledger.jsonl")


@contextlib.contextmanager
def _synced_cac_sign_paths():
    """Keep cac_sign module globals aligned with CAC_SIGN_APP_SUPPORT or monkeypatches."""
    saved = (cac_sign.APP_SUPPORT, cac_sign.IDENTITY_FILE, cac_sign.USER_PROFILE_DIR, cac_sign.LEDGER)
    cac_sign.APP_SUPPORT = _app_support_dir()
    cac_sign.IDENTITY_FILE = _identity_file()
    cac_sign.USER_PROFILE_DIR = _user_profiles_dir()
    cac_sign.LEDGER = _ledger_file()
    try:
        yield
    finally:
        cac_sign.APP_SUPPORT, cac_sign.IDENTITY_FILE, cac_sign.USER_PROFILE_DIR, cac_sign.LEDGER = saved


@contextlib.contextmanager
def _scoped_pin(pin: str | None):
    """Inject CAC_PIN strictly in-memory for the duration of a single OpenSC call."""
    if not pin:
        yield
        return
    had_prev = "CAC_PIN" in os.environ
    prev = os.environ.get("CAC_PIN")
    os.environ["CAC_PIN"] = str(pin)
    try:
        yield
    finally:
        if had_prev and prev is not None:
            os.environ["CAC_PIN"] = prev
        else:
            os.environ.pop("CAC_PIN", None)


def _scrub_secret(text: str, secret: str | None) -> str:
    if secret and len(secret) >= 2 and secret in text:
        return text.replace(secret, "[REDACTED]")
    return text


def _atomic_write_json_0600(dest: str, payload: object) -> None:
    parent = os.path.dirname(os.path.abspath(dest))
    os.makedirs(parent, mode=0o700, exist_ok=True)
    os.chmod(parent, 0o700)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        os.replace(tmp, dest)
        os.chmod(dest, 0o600)
    except BaseException:
        if fd is not None:
            os.close(fd)
        if os.path.lexists(tmp):
            os.remove(tmp)
        raise


# ---------- personas & identity ----------

def _clean_persona_fields(raw_fields: object, *, strict_npi: bool = False) -> dict[str, str]:
    if not isinstance(raw_fields, dict):
        raise ValueError("Persona fields must be a JSON object.")
    cleaned: dict[str, str] = {}
    for raw_k, raw_v in raw_fields.items():
        if not isinstance(raw_k, str):
            continue
        key = raw_k.strip()
        if not key or len(key) > 64 or any(ord(c) < 32 for c in key):
            continue
        if isinstance(raw_v, (int, float)) and not isinstance(raw_v, bool):
            raw_v = str(raw_v)
        if not isinstance(raw_v, str):
            continue
        val = raw_v.strip()
        if not val:
            continue
        if len(val) > 240 or any(ord(c) < 32 for c in val):
            raise ValueError(f'Invalid value for identity field "{key}".')
        if key.lower() == "npi":
            digits = re.sub(r"\D+", "", val)
            if len(digits) == 10:
                val = digits
            elif strict_npi:
                raise ValueError("NPI must be a 10-digit number.")
        cleaned[key] = val
    return cleaned


def _load_personas_state() -> dict:
    pfile = _personas_file()
    if os.path.isfile(pfile):
        with open(pfile, encoding="utf-8") as handle:
            raw = json.load(handle)
        raw_list = raw.get("personas") if isinstance(raw, dict) else None
        if isinstance(raw_list, list) and raw_list:
            personas = []
            seen_ids = set()
            for idx, item in enumerate(raw_list):
                if not isinstance(item, dict):
                    continue
                pid = str(item.get("id") or f"persona-{idx + 1}").strip()
                if not pid or pid in seen_ids:
                    continue
                seen_ids.add(pid)
                name = str(item.get("name") or pid).strip() or pid
                fields = _clean_persona_fields(item.get("fields") or {}, strict_npi=False)
                personas.append({"id": pid, "name": name, "fields": fields})
            if personas:
                active_id = str(raw.get("active_id") or personas[0]["id"])
                if not any(p["id"] == active_id for p in personas):
                    active_id = personas[0]["id"]
                state = {"active_id": active_id, "personas": personas}
                ifile = _identity_file()
                if os.path.isfile(ifile):
                    try:
                        if os.path.getmtime(ifile) > os.path.getmtime(pfile) + 0.001:
                            with open(ifile, encoding="utf-8") as ih:
                                ident_raw = json.load(ih)
                            if isinstance(ident_raw, dict):
                                ext_fields = _clean_persona_fields(ident_raw, strict_npi=False)
                                active_p = _active_persona(state)
                                merged = dict(active_p["fields"])
                                merged.update(ext_fields)
                                if merged != active_p["fields"]:
                                    active_p["fields"] = merged
                                    _atomic_write_json_0600(pfile, state)
                    except (OSError, ValueError):
                        pass
                return state

    ifile = _identity_file()
    seeded_fields: dict[str, str] = {}
    has_identity_file = os.path.isfile(ifile)
    if has_identity_file:
        with open(ifile, encoding="utf-8") as handle:
            ident_raw = json.load(handle)
        if isinstance(ident_raw, dict):
            seeded_fields = _clean_persona_fields(ident_raw, strict_npi=False)
    state = {
        "active_id": "default",
        "personas": [{"id": "default", "name": "Primary Signer", "fields": seeded_fields}],
    }
    if has_identity_file:
        _atomic_write_json_0600(pfile, state)
    return state


def _active_persona(state: dict) -> dict:
    active_id = state.get("active_id")
    for persona in state["personas"]:
        if persona["id"] == active_id:
            return persona
    return state["personas"][0]


def _personas_response(state: dict) -> dict:
    active = _active_persona(state)
    return {
        "active_id": active["id"],
        "personas": state["personas"],
        "autocomplete": cac_sign_editor._autocomplete_items(active["fields"]),
    }


def _op_personas_get() -> dict:
    return _personas_response(_load_personas_state())


def _op_personas_save(req: dict) -> dict:
    raw_personas = req.get("personas")
    if not isinstance(raw_personas, list) or not raw_personas:
        raise ValueError("personas_save requires at least one persona.")
    personas = []
    seen_ids = set()
    for item in raw_personas[:32]:
        if not isinstance(item, dict):
            raise ValueError("Each persona must be an object.")
        pid = str(item.get("id") or "").strip()
        if not PERSONA_ID_RE.fullmatch(pid):
            raise ValueError(f'Invalid persona id "{pid}".')
        if pid in seen_ids:
            raise ValueError(f'Duplicate persona id "{pid}".')
        seen_ids.add(pid)
        name = str(item.get("name") or "").strip()
        if not name or len(name) > 120 or any(ord(c) < 32 for c in name):
            raise ValueError(f'Invalid persona name for "{pid}".')
        fields = _clean_persona_fields(item.get("fields") or {}, strict_npi=True)
        personas.append({"id": pid, "name": name, "fields": fields})
    active_id = str(req.get("active_id") or personas[0]["id"]).strip()
    if not any(p["id"] == active_id for p in personas):
        active_id = personas[0]["id"]
    state = {"active_id": active_id, "personas": personas}
    _atomic_write_json_0600(_personas_file(), state)
    _atomic_write_json_0600(_identity_file(), dict(_active_persona(state)["fields"]))
    return _personas_response(state)


def _op_identity_autosave(req: dict) -> dict:
    updates = req.get("updates")
    if not isinstance(updates, dict):
        raise ValueError("updates must be an object.")
    state = _load_personas_state()
    active = _active_persona(state)
    cleaned: dict[str, str] = {}
    for raw_k, raw_v in list(updates.items())[:16]:
        if not isinstance(raw_k, str) or not isinstance(raw_v, str):
            continue
        k = raw_k.strip().lower()
        v = raw_v.strip()
        if k not in cac_sign_editor.SAVEABLE_IDENTITY_KEYS or not v or len(v) > 120:
            continue
        if v.upper() in ("N/A", "NA", "NONE") or any(ord(c) < 32 for c in v):
            continue
        if k == "npi":
            digits = re.sub(r"\D+", "", v)
            if len(digits) != 10:
                continue
            v = digits
        cleaned[k] = v
    if cleaned:
        active["fields"].update(cleaned)
        _atomic_write_json_0600(_personas_file(), state)
        _atomic_write_json_0600(_identity_file(), dict(active["fields"]))
    return _personas_response(state)


# ---------- profiles ----------

def _normalize_profile_spec(prof: dict) -> dict:
    pid = str(prof.get("id") or "").strip()
    raw_fill = prof.get("fill") or {}
    norm_fill: dict[str, dict] = {}
    if isinstance(raw_fill, dict):
        for fname, spec in raw_fill.items():
            if isinstance(spec, dict):
                norm_fill[str(fname)] = {
                    "value": str(spec.get("value", "")),
                    "if_empty": bool(spec.get("if_empty", False)),
                }
            else:
                norm_fill[str(fname)] = {"value": str(spec), "if_empty": False}
    return {
        "id": pid,
        "description": str(prof.get("description") or pid),
        "source": str(prof.get("source") or "bundled"),
        "match_fields": [str(x) for x in (prof.get("match_fields") or []) if str(x).strip()],
        "require_signed": [str(x) for x in (prof.get("require_signed") or []) if str(x).strip()],
        "sign_field": (str(prof["sign_field"]).strip() or None) if prof.get("sign_field") else None,
        "reason": (str(prof["reason"]).strip() or None) if prof.get("reason") else None,
        "fill": norm_fill,
        "next_step": (str(prof["next_step"]).strip() or None) if prof.get("next_step") else None,
    }


def _op_profiles_list() -> dict:
    with _synced_cac_sign_paths():
        return {"profiles": [_normalize_profile_spec(p) for p in cac_sign.load_profiles()]}


def _op_profile_save(req: dict) -> dict:
    raw = req.get("profile")
    if not isinstance(raw, dict):
        raise ValueError("profile must be a JSON object.")
    raw_id = str(raw.get("id") or "").strip().lower()
    if not PROFILE_ID_RE.fullmatch(raw_id) or raw_id == "none":
        raise ValueError(f'Invalid profile id "{raw_id}" (use lowercase letters, numbers, "-" or "_").')
    match_fields = [str(x).strip() for x in (raw.get("match_fields") or []) if isinstance(x, str) and x.strip()]
    if not match_fields:
        raise ValueError("Profile must include at least one match_fields entry.")
    require_signed = [str(x).strip() for x in (raw.get("require_signed") or []) if isinstance(x, str) and x.strip()]
    raw_fill = raw.get("fill") or {}
    if not isinstance(raw_fill, dict):
        raise ValueError("Profile fill must be a JSON object.")
    clean_fill: dict[str, dict] = {}
    for fname, spec in raw_fill.items():
        key = str(fname).strip()
        if not key:
            continue
        if isinstance(spec, dict):
            clean_fill[key] = {
                "value": str(spec.get("value", "")),
                "if_empty": bool(spec.get("if_empty", False)),
            }
        else:
            clean_fill[key] = {"value": str(spec), "if_empty": False}
    saved_obj: dict[str, object] = {
        "id": raw_id,
        "description": str(raw.get("description") or raw_id).strip() or raw_id,
        "match_fields": match_fields,
        "require_signed": require_signed,
        "fill": clean_fill,
    }
    for opt_key in ("sign_field", "reason", "next_step"):
        val = str(raw.get(opt_key) or "").strip()
        if val:
            saved_obj[opt_key] = val
    dest = os.path.join(_user_profiles_dir(), f"{raw_id}.json")
    _atomic_write_json_0600(dest, saved_obj)
    return _op_profiles_list()


def _op_profile_delete(req: dict) -> dict:
    pid = str(req.get("id") or "").strip()
    if not PROFILE_ID_RE.fullmatch(pid):
        raise ValueError(f'Invalid profile id "{pid}".')
    user_path = os.path.join(_user_profiles_dir(), f"{pid}.json")
    if os.path.isfile(user_path):
        os.remove(user_path)
        return _op_profiles_list()
    with _synced_cac_sign_paths():
        existing = {p["id"]: p for p in cac_sign.load_profiles()}
    if pid in existing and existing[pid].get("source") == "bundled":
        raise ValueError(f'Cannot delete bundled profile "{pid}".')
    raise ValueError(f'Profile "{pid}" not found.')


# ---------- document inspection, verification, ledger & diagnostics ----------

def _normalize_field(raw: dict) -> dict:
    item = dict(raw)
    kind = str(item.get("kind") or "text")
    item["kind"] = kind
    item["readonly"] = bool(item.get("readonly", False))
    item["locked"] = bool(item.get("locked", False))
    item["required"] = bool(item.get("required", False))
    item["multiline"] = bool(item.get("multiline", False))
    item["editable"] = bool(item.get("editable", True))
    item["widgets"] = list(item.get("widgets") or [])
    if kind == "choice":
        raw_opts = item.pop("options", None)
        if raw_opts is None:
            raw_opts = item.get("choice_options") or []
        choice_opts = []
        for opt in raw_opts:
            if isinstance(opt, dict):
                v = str(opt.get("value", ""))
                lbl = str(opt.get("label", v))
                choice_opts.append({"value": v, "label": lbl})
            else:
                choice_opts.append({"value": str(opt), "label": str(opt)})
        item["choice_options"] = choice_opts
    elif kind == "radio":
        item["options"] = [str(opt) for opt in (item.get("options") or [])]
        item.pop("choice_options", None)
    else:
        item.pop("options", None)
        item.pop("choice_options", None)
    if kind == "signature" and isinstance(item.get("signature"), dict):
        sig = dict(item["signature"])
        sig.setdefault("field", item.get("name"))
        item["signature"] = sig
    return item


def _ensure_active_identity_synced() -> dict[str, str]:
    state = _load_personas_state()
    active_fields = dict(_active_persona(state)["fields"])
    if os.path.isfile(_personas_file()):
        cur_ident = {}
        if os.path.isfile(_identity_file()):
            try:
                with open(_identity_file(), encoding="utf-8") as handle:
                    cur_ident = json.load(handle)
            except (OSError, ValueError):
                cur_ident = {}
        if cur_ident != active_fields:
            _atomic_write_json_0600(_identity_file(), active_fields)
    return active_fields


def _op_inspect(req: dict) -> dict:
    paths = req.get("paths")
    if not isinstance(paths, list) or not paths:
        raise ValueError("inspect requires a non-empty paths array.")
    active_fields = _ensure_active_identity_synced()
    docs = []
    with _synced_cac_sign_paths():
        for raw_path in paths:
            src_path = os.path.abspath(str(raw_path))
            pdf_path = os.path.abspath(document_inputs.pdf_input(src_path))
            conv_note = (
                document_inputs.conversion_note(src_path, pdf_path)
                if src_path.lower().endswith(".docx")
                else None
            )
            raw_model = cac_sign_editor.review_model(pdf_path)
            notes = list(raw_model.get("notes") or [])
            if conv_note and conv_note not in notes:
                notes.insert(0, conv_note)
            raw_sug = raw_model.get("suggested_signature")
            suggested_sig = None
            if isinstance(raw_sug, dict) and "page" in raw_sug and "box" in raw_sug:
                suggested_sig = {
                    "page": int(raw_sug["page"]),
                    "box": [float(v) for v in raw_sug["box"]],
                    "label": str(raw_sug["label"]) if raw_sug.get("label") else None,
                }
            sha = cac_sign.file_sha256(pdf_path)
            signed_before = raw_model.get("signed_before")
            if signed_before is None:
                signed_before = [
                    {"when": cac_sign._when(e.get("when")), "name": os.path.basename(e.get("output", "?"))}
                    for e in cac_sign.signed_before(sha)[:3]
                ]
            docs.append({
                "source_path": src_path,
                "pdf_path": pdf_path,
                "file": os.path.basename(pdf_path),
                "conversion_note": conv_note,
                "sha256": sha,
                "pages": [{"w": float(p["w"]), "h": float(p["h"])} for p in (raw_model.get("pages") or [])],
                "profile": raw_model.get("profile"),
                "reason": raw_model.get("reason"),
                "next_step": raw_model.get("next_step"),
                "fields": [_normalize_field(f) for f in (raw_model.get("fields") or [])],
                "sign_target": raw_model.get("sign_target"),
                "suggested_signature": suggested_sig,
                "blocked": raw_model.get("blocked"),
                "notes": notes,
                "warnings": list(raw_model.get("warnings") or []),
                "profile_fields": raw_model.get("profile_fields"),
                "autocomplete": raw_model.get("autocomplete") or cac_sign_editor._autocomplete_items(active_fields),
                "signed_before": list(signed_before),
            })
    return {"documents": docs}


def _op_fill_copy(req: dict) -> dict:
    raw_pdf = req.get("pdf_path")
    if not isinstance(raw_pdf, str) or not raw_pdf.strip():
        raise ValueError("fill_copy requires pdf_path.")
    pdf_path = os.path.abspath(raw_pdf)
    _ensure_active_identity_synced()
    body = {
        "values": req.get("values") or {},
        "additions": req.get("additions") or [],
    }
    with _synced_cac_sign_paths():
        res = cac_sign_editor.fill_copy(pdf_path, body)
    out = os.path.abspath(res["output"]) if res.get("output") else None
    return {
        "saved": bool(res.get("ok")),
        "output": out,
        "name": res.get("name"),
        "text": str(res.get("text") or ""),
    }


def _op_verify(req: dict) -> dict:
    raw_path = req.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("verify requires path.")
    abs_path = os.path.abspath(raw_path)
    if not os.path.isfile(abs_path):
        raise ValueError(f"File not found: {abs_path}")
    probs = list(reader_problems(abs_path))
    try:
        warns = list(viewer_warnings(abs_path))
    except Exception as exc:
        warns = [f"could not check viewer warnings ({type(exc).__name__})"]
    sigs = [
        {
            "field": s.get("field"),
            "signer": str(s.get("signer", "?")),
            "when": str(s.get("when", "?")),
            "ok": bool(s.get("ok")),
            "detail": str(s.get("detail", "")),
        }
        for s in verify_signatures(abs_path)
    ]
    return {
        "file": os.path.basename(abs_path),
        "reader_problems": probs,
        "warnings": warns,
        "signatures": sigs,
    }


def _op_ledger_list(req: dict) -> dict:
    raw_limit = req.get("limit", 100)
    limit = max(1, min(1000, int(raw_limit))) if isinstance(raw_limit, int) else 100
    with _synced_cac_sign_paths():
        entries = cac_sign.newest_first(cac_sign.ledger_entries())[:limit]
    records = []
    for e in entries:
        out_path = str(e.get("output") or "")
        records.append({
            "when": str(e.get("when") or ""),
            "when_display": cac_sign._when(e.get("when")),
            "input_sha256": str(e.get("input_sha256") or ""),
            "output": out_path,
            "output_sha256": str(e.get("output_sha256") or ""),
            "field": str(e.get("field") or "?"),
            "signer": str(e.get("signer") or "?"),
            "exists": bool(out_path and os.path.exists(out_path)),
        })
    return {"entries": records}


def _find_soffice() -> str | None:
    finder = getattr(document_inputs, "find_soffice", None)
    if callable(finder):
        return finder()
    return next(
        (
            p
            for p in (
                "/Applications/LibreOffice.app/Contents/MacOS/soffice",
                os.path.expanduser("~/Applications/LibreOffice.app/Contents/MacOS/soffice"),
                shutil.which("soffice"),
            )
            if p and os.path.isfile(p) and os.access(p, os.X_OK)
        ),
        None,
    )


def _pkg_version(dist_name: str, mod_name: str) -> str:
    try:
        return importlib.metadata.version(dist_name)
    except Exception:
        try:
            mod = __import__(mod_name)
            return str(getattr(mod, "__version__", "unknown"))
        except Exception:
            return "unknown"


def _op_render_page(req: dict) -> dict:
    raw_path = req.get("pdf_path") or req.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("render_page requires pdf_path.")
    abs_path = os.path.abspath(raw_path)
    page_idx = int(req.get("page", 0))
    scale = max(0.5, min(3.0, float(req.get("scale", 2.0))))
    png_bytes = render_page(abs_path, page_idx, scale=scale)
    return {"png_base64": base64.b64encode(png_bytes).decode("ascii")}


def _op_diagnostics() -> dict:
    try:
        opensc_path = cac_signing.find_pkcs11_lib()
    except (SystemExit, Exception):
        opensc_path = None
    dod_cas = []
    for cert in cac_signing.keychain_ca_certs():
        try:
            subj = cert.subject.human_friendly.lower()
        except Exception:
            continue
        if "dod" in subj:
            dod_cas.append((cert, subj))
    dod_roots_trusted = any(bool(getattr(c, "self_issued", False)) or "root" in subj for c, subj in dod_cas)
    return {
        "python_version": platform.python_version(),
        "pyhanko_version": _pkg_version("pyHanko", "pyhanko"),
        "pikepdf_version": _pkg_version("pikepdf", "pikepdf"),
        "libreoffice_path": _find_soffice(),
        "opensc_path": opensc_path,
        "dod_roots_trusted": dod_roots_trusted,
        "dod_ca_count": len(dod_cas),
        "app_support_path": _app_support_dir(),
    }


# ---------- stateful staging, signing & RPC server ----------

def _classify_sign_failure(ok: bool, text: str, explicit_cancelled: bool = False) -> tuple[bool, bool]:
    if ok:
        return False, False
    lower = (text or "").lower()
    if explicit_cancelled or "cancelled" in lower or "canceled" in lower:
        return True, False
    is_stop = text.lstrip().startswith("STOP") or "\nSTOP " in (text or "")
    card_markers = (
        "the card did not sign",
        "smart card",
        "smart-card",
        "cryptotokenkit",
        "pkcs#11",
        "pkcs11",
        "opensc",
        "no cac signing certificate",
        "no signing certificate found on the card",
        "pin",
        "could not read token",
        "deviceremoved",
        "deviceerror",
        "tokennotpresent",
        "tokennotrecognised",
        "nosuchtoken",
        "usernotloggedin",
    )
    return False, bool(is_stop or any(m in lower for m in card_markers))


def _build_sign_extra_args(req: dict, fallback_opensc: bool = False) -> list[str]:
    extra: list[str] = []
    reason = req.get("reason")
    if isinstance(reason, str) and reason.strip():
        extra += ["--reason", reason.strip()]
    p12_path = req.get("p12_path")
    if isinstance(p12_path, str) and p12_path.strip():
        extra += ["--p12", p12_path.strip()]
        if req.get("p12_pass") is not None:
            extra += ["--p12-pass", str(req["p12_pass"])]
    elif req.get("opensc") or fallback_opensc:
        extra.append("--opensc")
    return extra


class DesktopWorker:
    def __init__(self) -> None:
        self.stage_dir = tempfile.mkdtemp(prefix="cac-sign-desktop-")
        os.chmod(self.stage_dir, 0o700)
        self.pending_jobs: dict[str, dict] = {}

    def close(self) -> None:
        self.discard(None)
        shutil.rmtree(self.stage_dir, ignore_errors=True)

    def discard(self, job_id: str | None = None) -> dict:
        if job_id:
            pending = self.pending_jobs.pop(str(job_id), None)
            if pending:
                cac_sign_editor._remove(pending.get("snap"), pending.get("stage"))
        else:
            for pending in self.pending_jobs.values():
                cac_sign_editor._remove(pending.get("snap"), pending.get("stage"))
            self.pending_jobs.clear()
        return {"discarded": True}

    def prepare(self, req: dict) -> dict:
        raw_pdf = req.get("pdf_path")
        if not isinstance(raw_pdf, str) or not raw_pdf.strip():
            raise ValueError("prepare requires pdf_path.")
        pdf_path = os.path.abspath(raw_pdf)
        _ensure_active_identity_synced()
        extra = ["--opensc"] if req.get("opensc") else []
        with _synced_cac_sign_paths():
            res, pending = cac_sign_editor.prepare(pdf_path, req, self.stage_dir, extra)
        if not res.get("ok") or pending is None:
            return {
                "ready": False, "job_id": None, "stage_path": None, "pages": None,
                "where": None, "changes": None, "warnings": None, "signed_before": None,
                "needs_ack": None, "text": str(res.get("text") or "Could not prepare document."),
            }
        job_id = secrets.token_hex(8)
        self.pending_jobs[job_id] = dict(pending, opensc=bool(req.get("opensc")))
        return {
            "ready": True,
            "job_id": job_id,
            "stage_path": os.path.abspath(pending["stage"]),
            "pages": int(res["pages"]),
            "where": res.get("where"),
            "changes": list(res.get("changes") or []),
            "warnings": list(res.get("warnings") or []),
            "signed_before": list(res.get("signed_before") or []),
            "needs_ack": bool(pending.get("needs_ack")),
            "text": None,
        }

    def commit(self, req: dict) -> dict:
        job_id = str(req.get("job_id") or "").strip()
        pending = self.pending_jobs.get(job_id)
        if not pending:
            return {
                "signed": False, "cancelled": False, "card_error": False, "needs_ack": False,
                "text": "Nothing has been prepared for that job_id. Review the document again.",
                "output": None, "name": None, "sha256": None, "signatures": None, "next_step": None,
            }
        if pending.get("needs_ack") and req.get("ack") is not True:
            return {
                "signed": False, "cancelled": False, "card_error": False, "needs_ack": True,
                "text": cac_sign_editor.ACK_REFUSAL,
                "output": None, "name": None, "sha256": None, "signatures": None, "next_step": None,
            }
        self.pending_jobs.pop(job_id, None)
        extra = _build_sign_extra_args(req, fallback_opensc=bool(pending.get("opensc")))
        pin = req.get("pin") if isinstance(req.get("pin"), str) else None
        with _synced_cac_sign_paths(), _scoped_pin(pin):
            res = cac_sign_editor.commit(pending, extra, ack=req.get("ack") is True)
        shown = _scrub_secret(str(res.get("text") or ""), pin)
        ok = bool(res.get("ok"))
        cancelled, card_error = _classify_sign_failure(ok, shown, bool(res.get("cancelled")))
        out_path = os.path.abspath(res["output"]) if res.get("output") else None
        sha = cac_sign.file_sha256(out_path) if (ok and out_path and os.path.isfile(out_path)) else None
        return {
            "signed": ok,
            "cancelled": cancelled,
            "card_error": card_error,
            "needs_ack": bool(res.get("needs_ack", False)),
            "text": shown,
            "output": out_path,
            "name": res.get("name"),
            "sha256": sha,
            "signatures": res.get("signatures"),
            "next_step": res.get("next_step"),
        }

    def sign_quick(self, req: dict) -> dict:
        paths = req.get("paths")
        if not isinstance(paths, list) or not paths:
            raise ValueError("sign_quick requires a non-empty paths array.")
        dry_run = bool(req.get("dry_run", False))
        pin = req.get("pin") if isinstance(req.get("pin"), str) else None
        extra = _build_sign_extra_args(req)
        _ensure_active_identity_synced()
        items: list[dict] = []
        plan_args = SimpleNamespace(
            out=None, suffix="_signed", no_sign=False, force=False, profile=None,
            no_profile_fill=False, set=None, place=None, field=None, reason=None,
        )
        with _synced_cac_sign_paths():
            profiles, identity = cac_sign.load_profiles(), cac_sign.load_identity()
            planned_outs: dict[int, str] = {}
            for idx, raw_p in enumerate(paths):
                abs_p = os.path.abspath(str(raw_p))
                fname = os.path.basename(abs_p)
                if abs_p.lower().endswith(".docx"):
                    items.append({
                        "path": abs_p, "file": fname, "can_sign": False,
                        "summary": "Word documents must be reviewed interactively before signing.",
                        "warnings": [], "signed": False, "output": None,
                        "error": "Word documents require interactive review.",
                    })
                    continue
                if not os.path.isfile(abs_p):
                    items.append({
                        "path": abs_p, "file": fname, "can_sign": False,
                        "summary": "File not found.", "warnings": [],
                        "signed": False, "output": None, "error": "File not found.",
                    })
                    continue
                try:
                    pl = cac_sign.make_plan(abs_p, plan_args, profiles, identity)
                except Exception as exc:
                    pl = cac_sign.Plan(abs_p)
                    pl.skip = f"could not read it ({type(exc).__name__}: {exc})"
                if not pl.skip and pl.out and os.path.exists(pl.out):
                    pl.skip = f"{os.path.basename(pl.out)} already exists"
                if not pl.skip and not pl.fills and not pl.sign_field:
                    pl.skip = "nothing to do"
                warns = [
                    n.removeprefix("WARNING: ").removeprefix("NOTE: ")
                    for n in pl.notes
                    if n.startswith(("WARNING: ", "NOTE: "))
                ]
                desc = cac_sign.describe(pl)
                summary = "\n".join(line.strip() for line in desc[1:]) or desc[0]
                can_sign = pl.skip is None and bool(pl.sign_field)
                if pl.out:
                    planned_outs[idx] = pl.out
                items.append({
                    "path": abs_p, "file": fname, "can_sign": can_sign, "summary": summary,
                    "warnings": warns, "signed": False, "output": None,
                    "error": None if can_sign else pl.skip,
                })
            if dry_run:
                return {"items": items, "stopped_early": False}
            stopped_early = False
            for idx, item in enumerate(items):
                if not item["can_sign"]:
                    continue
                if stopped_early:
                    item["error"] = "Not signed: batch stopped after earlier smart-card cancellation or error."
                    continue
                abs_p = item["path"]
                expected_out = planned_outs.get(idx) or f"{os.path.splitext(abs_p)[0]}_signed.pdf"
                with _scoped_pin(pin):
                    rc, raw_out = run_cli([abs_p, *extra])
                shown = _scrub_secret(cac_sign_editor._shown(raw_out, abs_p, expected_out), pin)
                if rc == 0 and os.path.isfile(expected_out):
                    item["signed"] = True
                    item["output"] = os.path.abspath(expected_out)
                    item["summary"] = shown
                    item["error"] = None
                else:
                    cancelled, card_error = _classify_sign_failure(False, shown)
                    item["signed"] = False
                    item["error"] = shown or "Signing failed."
                    if cancelled or card_error:
                        stopped_early = True
            return {"items": items, "stopped_early": stopped_early}

    def dispatch(self, req: dict) -> dict:
        if not isinstance(req, dict):
            raise ValueError("Request must be a JSON object.")
        op = req.get("op")
        if op == "inspect":
            return _op_inspect(req)
        if op == "fill_copy":
            return _op_fill_copy(req)
        if op == "prepare":
            return self.prepare(req)
        if op == "commit":
            return self.commit(req)
        if op == "discard":
            return self.discard(req.get("job_id"))
        if op == "sign_quick":
            return self.sign_quick(req)
        if op == "verify":
            return _op_verify(req)
        if op == "personas_get":
            return _op_personas_get()
        if op == "personas_save":
            return _op_personas_save(req)
        if op == "identity_autosave":
            return _op_identity_autosave(req)
        if op == "profiles_list":
            return _op_profiles_list()
        if op == "profile_save":
            return _op_profile_save(req)
        if op == "profile_delete":
            return _op_profile_delete(req)
        if op == "ledger_list":
            return _op_ledger_list(req)
        if op == "render_page":
            return _op_render_page(req)
        if op == "diagnostics":
            return _op_diagnostics()
        raise ValueError(f'Unknown op "{op}".')


def _execute_envelope(worker: DesktopWorker, raw_text: str) -> dict | None:
    stripped = raw_text.strip()
    if not stripped:
        return None
    req_id = None
    pin_secret = None
    try:
        req = json.loads(stripped)
        if isinstance(req, dict):
            req_id = req.get("id")
            if isinstance(req.get("pin"), str):
                pin_secret = req["pin"]
        sink = io.StringIO()
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            data = worker.dispatch(req)
        env: dict = {"ok": True, "data": data}
    except SystemExit as exc:
        msg = str(exc.code) if exc.code not in (None, 0) else "Operation aborted."
        env = {"ok": False, "error": _scrub_secret(printable(msg), pin_secret)}
    except Exception as exc:
        env = {"ok": False, "error": _scrub_secret(printable(str(exc)), pin_secret)}
    if req_id is not None:
        env["id"] = req_id
    return env


def main(argv: list[str] | None = None) -> int:
    for proxy_key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "SSL_CERT_FILE",
                      "http_proxy", "https_proxy", "all_proxy", "ssl_cert_file"):
        os.environ.pop(proxy_key, None)
    ap = argparse.ArgumentParser(prog="cac-sign-desktop", description="Sealed JSON-RPC desktop worker for CACSign.app.")
    ap.add_argument("--serve", action="store_true", help="Run persistent line-delimited JSON-RPC loop on stdin/stdout.")
    args = ap.parse_args(argv)

    proto_out = sys.stdout
    worker = DesktopWorker()
    try:
        if args.serve:
            while True:
                line = sys.stdin.readline()
                if not line:
                    break
                env = _execute_envelope(worker, line)
                if env is not None:
                    proto_out.write(json.dumps(env, ensure_ascii=False) + "\n")
                    proto_out.flush()
        else:
            payload = sys.stdin.read()
            env = _execute_envelope(worker, payload)
            if env is None:
                env = {"ok": False, "error": "Empty JSON request on stdin."}
            proto_out.write(json.dumps(env, ensure_ascii=False) + "\n")
            proto_out.flush()
    finally:
        worker.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
