"""Tests for cac_sign_desktop.py, profile/persona storage, and desktop launcher routing.

Synthetic fixtures only (JANE Q. DOE / 1234567890). Never touches ~/Library/Application Support/cac-sign.
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID
import pikepdf
import pytest
from reportlab.pdfgen import canvas

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import cac_sign  # noqa: E402
import cac_sign_desktop  # noqa: E402
import cac_sign_editor  # noqa: E402
import cac_sign_launch  # noqa: E402
import cac_signing  # noqa: E402
from cac_signing import add_empty_sig_field  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_app_support(tmp_path, monkeypatch):
    """Isolate all App Support files (identity, personas, profiles, ledger) inside tmp_path."""
    sup = tmp_path / "app-support"
    monkeypatch.setenv("CAC_SIGN_APP_SUPPORT", str(sup))
    monkeypatch.setattr(cac_sign, "APP_SUPPORT", str(sup))
    monkeypatch.setattr(cac_sign, "IDENTITY_FILE", str(sup / "identity.json"))
    monkeypatch.setattr(cac_sign, "USER_PROFILE_DIR", str(sup / "profiles"))
    monkeypatch.setattr(cac_sign, "LEDGER", str(sup / "ledger.jsonl"))
    return sup


def make_p12(dest, cn="DOE.JANE.Q.1234567890"):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=True,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    data = pkcs12.serialize_key_and_certificates(
        b"synth", key, cert, None, serialization.BestAvailableEncryption(b"test")
    )
    dest.write_bytes(data)
    return str(dest)


@pytest.fixture
def synth_p12(tmp_path, monkeypatch):
    from asn1crypto import x509 as asn1_x509

    p12_file = make_p12(tmp_path / "jane_doe.p12", "DOE.JANE.Q.1234567890")
    _, cert, _ = pkcs12.load_key_and_certificates(open(p12_file, "rb").read(), b"test")
    der = asn1_x509.Certificate.load(cert.public_bytes(serialization.Encoding.DER))
    monkeypatch.setattr(cac_signing, "EXTRA_TRUST_ROOTS", [der])
    return p12_file


@pytest.fixture
def acroform_pdf(tmp_path):
    """AcroForm PDF containing text, checkbox, radio, choice ([export, label]), and empty signature."""
    raw = tmp_path / "raw_acro.pdf"
    with_sig = tmp_path / "clinic_request.pdf"
    c = canvas.Canvas(str(raw))
    af = c.acroForm
    af.textfield(name="ProviderName", x=72, y=700, width=220, height=20, value="")
    af.textfield(name="ExistingNote", x=72, y=670, width=220, height=20, value="Prior note")
    af.checkbox(name="Approve", x=72, y=640, size=14)
    af.radio(name="Urgency", value="Routine", x=72, y=610, size=14)
    af.radio(name="Urgency", value="Urgent", x=110, y=610, size=14)
    c.showPage()
    c.save()

    add_empty_sig_field(str(raw), str(with_sig), (72, 480, 300, 510), field="Signature")
    with pikepdf.open(with_sig, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0]
        ap_stream = pdf.make_stream(
            b"q Q",
            Type=pikepdf.Name("/XObject"),
            Subtype=pikepdf.Name("/Form"),
            BBox=pikepdf.Array([0, 0, 188, 20]),
            Resources=pikepdf.Dictionary(),
        )
        choice = pdf.make_indirect(
            pikepdf.Dictionary(
                FT=pikepdf.Name("/Ch"),
                Ff=1 << 17,  # combo box
                T=pikepdf.String("Department"),
                V=pikepdf.String(""),
                DA=pikepdf.String("/Helv 10 Tf 0 g"),
                Opt=pikepdf.Array([
                    pikepdf.Array([pikepdf.String("FM"), pikepdf.String("Family Medicine")]),
                    pikepdf.Array([pikepdf.String("IM"), pikepdf.String("Internal Medicine")]),
                ]),
                AP=pikepdf.Dictionary(N=ap_stream),
                Type=pikepdf.Name("/Annot"),
                Subtype=pikepdf.Name("/Widget"),
                Rect=pikepdf.Array([72, 560, 260, 580]),
                F=4,
                P=page.obj,
            )
        )
        page.Annots.append(choice)
        pdf.Root.AcroForm.Fields.append(choice)
        pdf.save(with_sig)
    return with_sig


@pytest.fixture
def flat_pdf(tmp_path):
    """Flat PDF with selectable printed blanks and a signature line (no AcroForm)."""
    out = tmp_path / "flat_referral.pdf"
    c = canvas.Canvas(str(out))
    c.setFont("Helvetica", 12)
    c.drawString(72, 720, "SYNTHETIC MEDICAL REFERRAL FORM")
    c.drawString(72, 680, "Patient Name: ________________________")
    c.drawString(72, 645, "[ ] Routine Follow-Up")
    c.drawString(72, 560, "Provider Signature: ________________________")
    c.showPage()
    c.save()
    return out


def _rpc(worker: cac_sign_desktop.DesktopWorker, payload: dict) -> dict:
    env = cac_sign_desktop._execute_envelope(worker, json.dumps(payload))
    assert env is not None
    return env


def test_personas_lossless_bootstrap_and_multi_persona_sync(isolated_app_support):
    isolated_app_support.mkdir(parents=True, exist_ok=True)
    os.chmod(isolated_app_support, 0o700)
    ident_path = isolated_app_support / "identity.json"
    personas_path = isolated_app_support / "personas.json"

    seed_identity = {
        "full_name": "JANE Q. DOE",
        "title": "Staff Physician",
        "duty_phone": "(555) 555-0100",
        "fax": "(555) 555-0199",
        "npi": "1234567890",
        "custom_dept_code": "SYNTH-FM-01",
    }
    fd = os.open(ident_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(seed_identity, handle)

    worker = cac_sign_desktop.DesktopWorker()
    try:
        res = _rpc(worker, {"op": "personas_get"})
        assert res["ok"] is True
        data = res["data"]
        assert data["active_id"] == "default"
        assert len(data["personas"]) == 1
        assert data["personas"][0]["name"] == "Primary Signer"
        # Every key in identity.json (including custom_dept_code, npi, fax, full_name) preserved losslessly
        assert data["personas"][0]["fields"] == seed_identity
        assert personas_path.exists()
        assert (personas_path.stat().st_mode & 0o777) == 0o600
        ac_map = {item["key"]: item["value"] for item in data["autocomplete"]}
        assert ac_map["full_name"] == "JANE Q. DOE"
        assert ac_map["npi"] == "1234567890"

        # Save multi-persona configuration and switch active_id -> syncs identity.json (0600)
        alt_fields = {
            "full_name": "JANE Q. DOE, MD",
            "title": "Clinic OIC",
            "duty_phone": "(555) 555-0122",
            "npi": "123-456-7890",
        }
        save_res = _rpc(
            worker,
            {
                "op": "personas_save",
                "active_id": "oic-role",
                "personas": [
                    {"id": "default", "name": "Primary Signer", "fields": seed_identity},
                    {"id": "oic-role", "name": "Clinic OIC", "fields": alt_fields},
                ],
            },
        )
        assert save_res["ok"] is True
        assert save_res["data"]["active_id"] == "oic-role"
        disk_ident = json.loads(ident_path.read_text(encoding="utf-8"))
        assert disk_ident == {
            "full_name": "JANE Q. DOE, MD",
            "title": "Clinic OIC",
            "duty_phone": "(555) 555-0122",
            "npi": "1234567890",
        }
        assert (ident_path.stat().st_mode & 0o777) == 0o600
        assert (personas_path.stat().st_mode & 0o777) == 0o600

        # identity_autosave merges into active persona and syncs identity.json
        auto_res = _rpc(worker, {"op": "identity_autosave", "updates": {"fax": "(555) 555-0177", "npi": "bad"}})
        assert auto_res["ok"] is True
        updated_ident = json.loads(ident_path.read_text(encoding="utf-8"))
        assert updated_ident["fax"] == "(555) 555-0177"
        assert updated_ident["npi"] == "1234567890"

        # External manual edit to identity.json with newer mtime merges back instead of getting clobbered
        import time as _time
        ext_ident = dict(updated_ident, custom_cli_note="EXT-KEY-99")
        ident_path.write_text(json.dumps(ext_ident), encoding="utf-8")
        future_ts = personas_path.stat().st_mtime + 2.0
        os.utime(ident_path, (future_ts, future_ts))
        reloaded = _rpc(worker, {"op": "personas_get"})
        assert reloaded["ok"] is True
        active_p = next(p for p in reloaded["data"]["personas"] if p["id"] == "oic-role")
        assert active_p["fields"]["custom_cli_note"] == "EXT-KEY-99"
    finally:
        worker.close()


def test_profiles_list_save_override_and_delete_guards(isolated_app_support):
    worker = cac_sign_desktop.DesktopWorker()
    try:
        listed = _rpc(worker, {"op": "profiles_list"})
        assert listed["ok"] is True
        by_id = {p["id"]: p for p in listed["data"]["profiles"]}
        assert "rts-course-request" in by_id
        rts = by_id["rts-course-request"]
        assert rts["source"] == "bundled"
        assert rts["fill"]["Yes"] == {"value": "x", "if_empty": False}
        assert rts["fill"]["Supervisor Title"] == {"value": "${title}", "if_empty": True}

        # Refuses deleting bundled profile
        del_bundled = _rpc(worker, {"op": "profile_delete", "id": "rts-course-request"})
        assert del_bundled["ok"] is False
        assert "bundled" in del_bundled["error"].lower()

        # Save user profile -> writes 0600 file in USER_PROFILE_DIR
        saved = _rpc(
            worker,
            {
                "op": "profile_save",
                "profile": {
                    "id": "synth-clinic-approval",
                    "description": "Synthetic Clinic Approval",
                    "match_fields": ["ProviderName", "Approve", "Signature"],
                    "require_signed": [],
                    "sign_field": "Signature",
                    "reason": "Clinical review",
                    "fill": {
                        "ProviderName": {"value": "${full_name}", "if_empty": True},
                        "Approve": {"value": "yes", "if_empty": False},
                    },
                    "next_step": "Upload signed PDF to synthetic inbox.",
                },
            },
        )
        assert saved["ok"] is True
        prof_file = isolated_app_support / "profiles" / "synth-clinic-approval.json"
        assert prof_file.is_file()
        assert (prof_file.stat().st_mode & 0o777) == 0o600
        saved_by_id = {p["id"]: p for p in saved["data"]["profiles"]}
        assert saved_by_id["synth-clinic-approval"]["source"] == "user"
        assert any(p["id"] == "synth-clinic-approval" for p in cac_sign.load_profiles())

        # Delete user profile succeeds
        deleted = _rpc(worker, {"op": "profile_delete", "id": "synth-clinic-approval"})
        assert deleted["ok"] is True
        assert not prof_file.exists()
        assert "synth-clinic-approval" not in {p["id"] for p in deleted["data"]["profiles"]}
    finally:
        worker.close()


def test_inspect_acroform_radio_choice_normalization_and_flat_pdf(acroform_pdf, flat_pdf):
    worker = cac_sign_desktop.DesktopWorker()
    try:
        # 1) AcroForm inspection: radio uses "options": [str], choice uses "choice_options": [{value,label}]
        res_acro = _rpc(worker, {"op": "inspect", "paths": [str(acroform_pdf)]})
        assert res_acro["ok"] is True
        doc = res_acro["data"]["documents"][0]
        assert doc["source_path"] == str(acroform_pdf)
        assert doc["pdf_path"] == str(acroform_pdf)
        assert doc["conversion_note"] is None
        assert len(doc["sha256"]) == 64
        assert doc["sign_target"] == "Signature"

        fields = {f["name"]: f for f in doc["fields"]}
        assert fields["Urgency"]["kind"] == "radio"
        assert fields["Urgency"]["options"] == ["Routine", "Urgent"]
        assert "choice_options" not in fields["Urgency"]

        assert fields["Department"]["kind"] == "choice"
        assert "options" not in fields["Department"]
        assert fields["Department"]["choice_options"] == [
            {"value": "FM", "label": "Family Medicine"},
            {"value": "IM", "label": "Internal Medicine"},
        ]

        # 2) Flat PDF inspection: detects blank fields and suggested_signature without altering file
        before_bytes = flat_pdf.read_bytes()
        res_flat = _rpc(worker, {"op": "inspect", "paths": [str(flat_pdf)]})
        assert res_flat["ok"] is True
        flat_doc = res_flat["data"]["documents"][0]
        detected = [f for f in flat_doc["fields"] if f.get("detected")]
        assert len(detected) >= 2
        kinds = {f["kind"] for f in detected}
        assert "text" in kinds
        assert "checkbox" in kinds
        assert flat_doc["suggested_signature"] is not None
        assert flat_doc["suggested_signature"]["page"] == 0
        assert len(flat_doc["suggested_signature"]["box"]) == 4
        assert flat_pdf.read_bytes() == before_bytes
    finally:
        worker.close()


def test_prepare_commit_verify_fill_copy_and_ledger(acroform_pdf, synth_p12):
    worker = cac_sign_desktop.DesktopWorker()
    try:
        # fill_copy returns full output path
        filled = _rpc(
            worker,
            {
                "op": "fill_copy",
                "pdf_path": str(acroform_pdf),
                "values": {"ProviderName": "JANE Q. DOE", "Approve": True},
                "additions": [],
            },
        )
        assert filled["ok"] is True
        assert filled["data"]["saved"] is True
        assert os.path.isabs(filled["data"]["output"])
        assert os.path.isfile(filled["data"]["output"])

        rendered = _rpc(worker, {"op": "render_page", "pdf_path": filled["data"]["output"], "page": 0, "scale": 1.5})
        assert rendered["ok"] is True
        import base64 as _b64
        png_raw = _b64.b64decode(rendered["data"]["png_base64"])
        assert png_raw[:8] == b"\x89PNG\r\n\x1a\n"

        # prepare with an edit replacing ExistingNote triggers needs_ack=True
        prep = _rpc(
            worker,
            {
                "op": "prepare",
                "pdf_path": str(acroform_pdf),
                "values": {"ProviderName": "JANE Q. DOE", "ExistingNote": "Updated clinical note", "Approve": True},
                "additions": [],
                "mode": "field",
                "field": "Signature",
            },
        )
        assert prep["ok"] is True
        pdata = prep["data"]
        assert pdata["ready"] is True
        assert pdata["needs_ack"] is True
        assert os.path.isfile(pdata["stage_path"])
        job_id = pdata["job_id"]

        # commit without ack refuses while keeping job_id staged
        unacked = _rpc(
            worker,
            {"op": "commit", "job_id": job_id, "ack": False, "p12_path": synth_p12, "p12_pass": "test"},
        )
        assert unacked["ok"] is True
        assert unacked["data"]["signed"] is False
        assert unacked["data"]["needs_ack"] is True

        # commit with ack=True signs, verifies, and records ledger entry
        committed = _rpc(
            worker,
            {
                "op": "commit",
                "job_id": job_id,
                "ack": True,
                "reason": "Supervisor approval",
                "p12_path": synth_p12,
                "p12_pass": "test",
            },
        )
        assert committed["ok"] is True
        cdata = committed["data"]
        assert cdata["signed"] is True
        assert cdata["cancelled"] is False
        assert cdata["card_error"] is False
        assert os.path.isfile(cdata["output"])
        assert len(cdata["sha256"]) == 64
        assert cdata["signatures"][0]["ok"] is True
        assert "DOE.JANE.Q.1234567890" in cdata["signatures"][0]["signer"]

        # ledger_list shows newest-first record with exists=True
        ledger = _rpc(worker, {"op": "ledger_list", "limit": 10})
        assert ledger["ok"] is True
        assert len(ledger["data"]["entries"]) == 1
        entry = ledger["data"]["entries"][0]
        assert entry["output"] == cdata["output"]
        assert entry["exists"] is True
        assert entry["when_display"] != "?"

        # verify op checks reader agreement and signature trust/integrity
        ver = _rpc(worker, {"op": "verify", "path": cdata["output"]})
        assert ver["ok"] is True
        assert ver["data"]["reader_problems"] == []
        assert len(ver["data"]["signatures"]) == 1
        assert ver["data"]["signatures"][0]["ok"] is True
    finally:
        worker.close()


def test_sign_quick_and_card_error_halt(tmp_path, acroform_pdf, synth_p12, monkeypatch):
    import shutil as _shutil

    second_pdf = tmp_path / "clinic_request_2.pdf"
    _shutil.copyfile(acroform_pdf, second_pdf)

    worker = cac_sign_desktop.DesktopWorker()
    try:
        dry = _rpc(worker, {"op": "sign_quick", "paths": [str(acroform_pdf), str(second_pdf)], "dry_run": True})
        assert dry["ok"] is True
        assert all(item["can_sign"] for item in dry["data"]["items"])
        assert dry["data"]["stopped_early"] is False

        # Simulate smart-card error on first file -> batch halts immediately before touching second file
        calls = []

        def fake_keychain_signer():
            calls.append(1)
            raise cac_signing.SigningError("the card did not sign (CryptoTokenKit smart card PIN refused)")

        monkeypatch.setattr(cac_sign, "build_keychain_signer", fake_keychain_signer)
        halted = _rpc(worker, {"op": "sign_quick", "paths": [str(acroform_pdf), str(second_pdf)], "dry_run": False})
        assert halted["ok"] is True
        assert halted["data"]["stopped_early"] is True
        assert len(calls) == 1
        assert not halted["data"]["items"][0]["signed"]
        assert not halted["data"]["items"][1]["signed"]

        # Deterministic sign_quick with p12 succeeds on both
        signed = _rpc(
            worker,
            {
                "op": "sign_quick",
                "paths": [str(acroform_pdf), str(second_pdf)],
                "dry_run": False,
                "p12_path": synth_p12,
                "p12_pass": "test",
            },
        )
        assert signed["ok"] is True
        assert signed["data"]["stopped_early"] is False
        assert all(item["signed"] and os.path.isfile(item["output"]) for item in signed["data"]["items"])
    finally:
        worker.close()


def test_serve_subprocess_stripped_env_and_launcher_routing(tmp_path, acroform_pdf, monkeypatch):
    sup = tmp_path / "subproc-support"
    stripped_env = {
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin",
        "CAC_SIGN_APP_SUPPORT": str(sup),
    }
    worker_script = os.path.join(ROOT, "cac_sign_desktop.py")
    proc = subprocess.Popen(
        [sys.executable, "-I", "-B", worker_script, "--serve"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=stripped_env,
    )
    assert proc.stdin is not None and proc.stdout is not None
    try:
        requests = [
            {"id": 1, "op": "diagnostics"},
            {
                "id": 2,
                "op": "personas_save",
                "active_id": "default",
                "personas": [{"id": "default", "name": "Primary Signer", "fields": {"full_name": "JANE Q. DOE", "npi": "1234567890"}}],
            },
            {"id": 3, "op": "profiles_list"},
            {"id": 4, "op": "discard"},
        ]
        for r in requests:
            proc.stdin.write(json.dumps(r) + "\n")
        proc.stdin.flush()

        replies = [json.loads(proc.stdout.readline()) for _ in requests]
        assert [r["id"] for r in replies] == [1, 2, 3, 4]
        assert all(r["ok"] is True for r in replies)
        assert replies[0]["data"]["app_support_path"] == str(sup)
        assert (sup / "identity.json").is_file()
        assert ((sup / "identity.json").stat().st_mode & 0o777) == 0o600
    finally:
        proc.stdin.close()
        assert proc.wait(timeout=10) == 0

    # Verify one-shot mode via cac_sign_launch.py --desktop-worker
    oneshot = subprocess.run(
        [sys.executable, "-I", "-B", os.path.join(ROOT, "cac_sign_launch.py"), "--desktop-worker"],
        input=json.dumps({"op": "personas_get"}),
        capture_output=True,
        text=True,
        env=stripped_env,
        check=True,
    )
    parsed = json.loads(oneshot.stdout.strip())
    assert parsed["ok"] is True
    assert parsed["data"]["personas"][0]["fields"]["full_name"] == "JANE Q. DOE"

    # Verify cac_sign_launch.py GUI routing to CACSign.app unless CAC_SIGN_FORCE_WEB=1 or --no-browser
    fake_app = tmp_path / "CACSign.app"
    fake_app.mkdir()
    opened = []
    monkeypatch.setattr(cac_sign_launch, "DESKTOP_APP", str(fake_app))
    monkeypatch.setattr(cac_sign_launch.subprocess, "run", lambda cmd: opened.append(cmd) or type("R", (), {"returncode": 0})())
    monkeypatch.setattr(cac_sign_editor, "main", lambda argv: opened.append(["web", *argv]) or 0)

    assert cac_sign_launch.cli(["--review", str(acroform_pdf)]) == 0
    assert opened[-1] == ["open", "-a", str(fake_app), os.path.abspath(str(acroform_pdf))]

    assert cac_sign_launch.cli(["--review", "--no-browser", str(acroform_pdf)]) == 0
    assert opened[-1] == ["web", "--no-browser", str(acroform_pdf)]

    monkeypatch.setenv("CAC_SIGN_FORCE_WEB", "1")
    assert cac_sign_launch.cli(["--review", str(acroform_pdf)]) == 0
    assert opened[-1] == ["web", str(acroform_pdf)]


def test_zsh_wrapper_resolution_order(tmp_path):
    zsh_file = os.path.join(ROOT, "cac-sign.zsh")
    subprocess.run(["/bin/zsh", "-n", zsh_file], check=True)

    fake_home = tmp_path / "cac-home"
    fake_home.mkdir()
    fake_app = tmp_path / "FakeCACSign.app"
    app_py = fake_app / "Contents" / "Resources" / "python" / "bin" / "python3"
    app_launcher = fake_app / "Contents" / "Resources" / "app" / "cac_sign_launch.py"
    app_py.parent.mkdir(parents=True)
    app_launcher.parent.mkdir(parents=True)
    app_py.write_text("#!/bin/sh\necho \"APP_PY:$@\"\n", encoding="utf-8")
    app_py.chmod(0o755)
    app_launcher.write_text("# stub\n", encoding="utf-8")

    # Without $CAC_SIGN_HOME/.venv/bin/python, falls back to CACSign.app bundled python
    out_app = subprocess.run(
        ["/bin/zsh", "-c", f'source "{zsh_file}" && cac-sign --version-check'],
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "CAC_SIGN_HOME": str(fake_home), "CAC_SIGN_APP": str(fake_app)},
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert out_app == f"APP_PY:{app_launcher} --version-check"

    # Dev .venv takes priority when present
    venv_py = fake_home / ".venv" / "bin" / "python"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text("#!/bin/sh\necho \"DEV_VENV:$@\"\n", encoding="utf-8")
    venv_py.chmod(0o755)
    out_dev = subprocess.run(
        ["/bin/zsh", "-c", f'source "{zsh_file}" && cac-sign --version-check'],
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "CAC_SIGN_HOME": str(fake_home), "CAC_SIGN_APP": str(fake_app)},
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert out_dev == f"DEV_VENV:{fake_home / 'cac_sign_launch.py'} --version-check"


def test_release_packaging_helpers_and_user_applications_fallback(tmp_path, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "build_macos_app", os.path.join(ROOT, "scripts", "build_macos_app.py")
    )
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)

    # Verify Mach-O filetype classification (MH_EXECUTE=2 vs MH_DYLIB=6 vs non-Mach-O)
    exec_bin = tmp_path / "exec_macho"
    exec_bin.write_bytes(b"\xcf\xfa\xed\xfe" + b"\x00" * 8 + (2).to_bytes(4, "little") + b"\x00" * 16)
    dylib_bin = tmp_path / "lib.dylib"
    dylib_bin.write_bytes(b"\xcf\xfa\xed\xfe" + b"\x00" * 8 + (6).to_bytes(4, "little") + b"\x00" * 16)
    plain_txt = tmp_path / "note.txt"
    plain_txt.write_text("not a macho\n", encoding="utf-8")

    assert builder.macho_kind(exec_bin) == "exec"
    assert builder.macho_kind(dylib_bin) == "lib"
    assert builder.macho_kind(plain_txt) is None

    # MH_DSYM (filetype 10) and PyObjCTest must be excluded so Apple notarization never sees .dSYM bundles
    dsym_bin = tmp_path / "debug.dSYM_dwarf"
    dsym_bin.write_bytes(b"\xcf\xfa\xed\xfe" + b"\x00" * 8 + (10).to_bytes(4, "little") + b"\x00" * 16)
    assert builder.macho_kind(dsym_bin) is None
    assert builder.should_skip_site_entry("PyObjCTest") is True

    # Verify ~/Applications/CACSign.app fallback when /Applications/CACSign.app is absent
    fake_home = tmp_path / "user-home"
    user_app = fake_home / "Applications" / "CACSign.app"
    (user_app / "Contents" / "Resources" / "python" / "bin").mkdir(parents=True)
    (user_app / "Contents" / "Resources" / "app").mkdir(parents=True)
    app_py = user_app / "Contents" / "Resources" / "python" / "bin" / "python3"
    app_launcher = user_app / "Contents" / "Resources" / "app" / "cac_sign_launch.py"
    app_py.write_text("#!/bin/sh\necho \"USER_APP_PY:$@\"\n", encoding="utf-8")
    app_py.chmod(0o755)
    app_launcher.write_text("# stub\n", encoding="utf-8")

    zsh_file = os.path.join(ROOT, "cac-sign.zsh")
    out_user = subprocess.run(
        ["/bin/zsh", "-c", f'source "{zsh_file}" && cac-sign --help-stub'],
        env={
            "HOME": str(fake_home),
            "PATH": "/usr/bin:/bin",
            "CAC_SIGN_HOME": str(fake_home / ".cac-sign"),
            "CAC_SIGN_SYSTEM_APP": str(tmp_path / "absent-system-CACSign.app"),
        },
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert out_user == f"USER_APP_PY:{app_launcher} --help-stub"

    # Verify package-macos.sh syntax and required argument guards
    pkg_script = os.path.join(ROOT, "scripts", "package-macos.sh")
    subprocess.run(["/bin/bash", "-n", pkg_script], check=True)
    bad = subprocess.run(["/bin/bash", pkg_script], capture_output=True, text=True)
    assert bad.returncode != 0
    assert "Developer ID Application" in bad.stderr
