"""cac_sign.py end to end on synthetic forms, signed with throwaway software certs.

Run:  uv run --with pytest pytest -q tests
"""
import datetime, io, json, os, sys

import pikepdf
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from reportlab.pdfgen import canvas

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cac_sign  # noqa: E402
import cac_signing  # noqa: E402
from cac_signing import add_empty_sig_field, verify_signatures  # noqa: E402


@pytest.fixture(autouse=True)
def private_ledger(tmp_path, monkeypatch):
    """No test may write to the real signing log or read the real identity."""
    monkeypatch.setattr(cac_sign, "LEDGER", str(tmp_path / "ledger" / "ledger.jsonl"))
    monkeypatch.setattr(cac_sign, "IDENTITY_FILE", str(tmp_path / "no-identity" / "identity.json"))


def test_personal_data_lives_outside_the_code_folder():
    """Updating or reinstalling replaces the code folder; identity and log must survive it."""
    import subprocess
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = subprocess.run([sys.executable, "-c", "import cac_sign as c; print(c.HERE); print(c.IDENTITY_FILE); "
                          "print(c.LEDGER)"], cwd=here, capture_output=True, text=True, check=True).stdout
    code, *data = out.splitlines()
    for path in data:
        assert path.startswith(os.path.expanduser("~/Library/Application Support/cac-sign/"))
        assert not path.startswith(code)


def make_p12(path, cn):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=True, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                         crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .sign(key, hashes.SHA256()))
    from cryptography.hazmat.primitives.serialization import pkcs12
    with open(path, "wb") as f:
        f.write(pkcs12.serialize_key_and_certificates(b"t", key, cert, None,
                                                      serialization.BestAvailableEncryption(b"test")))
    return str(path)


def pyhanko_sign(src, dst, field, p12, certify_no_changes=False):
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.sign import signers
    from pyhanko.sign.fields import MDPPerm
    s = signers.SimpleSigner.load_pkcs12(p12, passphrase=b"test")
    meta = signers.PdfSignatureMetadata(field_name=field, certify=certify_no_changes,
                                        docmdp_permissions=MDPPerm.NO_CHANGES)
    with open(src, "rb") as f, open(dst, "wb") as o:
        signers.PdfSigner(meta, signer=s).sign_pdf(IncrementalPdfFileWriter(f), existing_fields_only=True, output=o)


def trust(monkeypatch, *p12s):
    """Make throwaway certs trusted, the way DoD roots are on the Mac (verify_signatures requires trust)."""
    from asn1crypto import x509 as asn1_x509
    from cryptography.hazmat.primitives.serialization import pkcs12
    roots = []
    for p in p12s:
        _, cert, _ = pkcs12.load_key_and_certificates(open(p, "rb").read(), b"test")
        roots.append(asn1_x509.Certificate.load(cert.public_bytes(serialization.Encoding.DER)))
    monkeypatch.setattr(cac_signing, "EXTRA_TRUST_ROOTS", roots)


@pytest.fixture
def certs(tmp_path, monkeypatch):
    c = {"applicant": make_p12(tmp_path / "a.p12", "APPLICANT.TEST.0000000001"),
         "supervisor": make_p12(tmp_path / "s.p12", "SUPERVISOR.TEST.0000000002")}
    trust(monkeypatch, *c.values())
    return c


@pytest.fixture
def blank_form(tmp_path):
    """A form with text, checkbox, radio, read-only text, and two empty signature fields."""
    raw, out = tmp_path / "raw.pdf", tmp_path / "form0.pdf"
    c = canvas.Canvas(str(raw))
    f = c.acroForm
    f.textfield(name="Name", x=72, y=700, width=200, height=20)
    f.textfield(name="Phone", x=72, y=670, width=200, height=20)
    f.textfield(name="Fixed", x=72, y=640, width=200, height=20, value="locked", fieldFlags="readOnly")
    f.checkbox(name="Agree", x=72, y=610, size=12)
    f.radio(name="Choice", value="A", x=72, y=580, size=12)
    f.radio(name="Choice", value="B", x=100, y=580, size=12)
    c.showPage()
    c.save()
    mid = tmp_path / "mid.pdf"
    add_empty_sig_field(str(raw), str(mid), (72, 500, 300, 522), field="Applicant Signature")
    add_empty_sig_field(str(mid), str(out), (72, 450, 300, 472), field="Signature")
    return out


@pytest.fixture
def applicant_signed(tmp_path, blank_form, certs):
    out = tmp_path / "form.pdf"
    pyhanko_sign(blank_form, out, "Applicant Signature", certs["applicant"])
    return out


def run(*argv):
    return cac_sign.main(list(map(str, argv)))


def values(path):
    pdf = pikepdf.open(path)
    out = {}
    for fld in pdf.Root.AcroForm.Fields:
        out[str(fld.T)] = str(fld.get("/V")) if "/V" in fld else None
    return out


def test_fill_and_sign_keeps_prior_signature_valid(tmp_path, applicant_signed, certs):
    rc = run(applicant_signed, "--set", "Name=Pat Example", "--set", "Agree=yes", "--set", "Choice=B",
             "--field", "Signature", "--p12", certs["supervisor"], "--p12-pass", "test")
    assert rc == 0
    out = tmp_path / "form_signed.pdf"
    sigs = verify_signatures(out)
    assert [s["field"] for s in sigs] == ["Applicant Signature", "Signature"]
    assert all(s["intact"] and s["valid"] and s["trusted"] for s in sigs)
    assert sigs[1]["ok"] and sigs[1]["coverage"] == "ENTIRE_FILE"
    # pyHanko cannot justify radio-widget /AS updates (reports OTHER), so the applicant's
    # full verdict fails closed; the object audit is what proves the update was clean
    assert not sigs[0]["changes_ok"] and not sigs[0]["ok"]
    assert cac_sign.audit_last_revision(out, ["Name", "Agree", "Choice", "Signature"]) == []
    v = values(out)
    assert v["Name"] == "Pat Example" and v["Agree"] != "/Off" and v["Choice"] == "/B"
    # original is untouched, and the output is an append-only update of it
    assert open(out, "rb").read().startswith(open(applicant_signed, "rb").read())


def test_profile_fills_from_identity_and_respects_if_empty(tmp_path, applicant_signed, certs, monkeypatch):
    prof_dir = tmp_path / "profiles"; prof_dir.mkdir()
    (prof_dir / "demo.json").write_text(json.dumps({
        "id": "demo", "match_fields": ["Applicant Signature", "Signature", "Phone", "Choice"],
        "require_signed": ["Applicant Signature"], "sign_field": "Signature",
        "fill": {"Agree": "yes", "Name": {"value": "${full_name}", "if_empty": True}, "Phone": "${duty_phone}"}}))
    ident = tmp_path / "identity.local.json"
    ident.write_text(json.dumps({"full_name": "Pat Example", "duty_phone": "+1-555-0123"}))
    monkeypatch.setattr(cac_sign, "PROFILE_DIR", str(prof_dir))
    monkeypatch.setattr(cac_sign, "IDENTITY_FILE", str(ident))
    assert run(applicant_signed, "--p12", certs["supervisor"], "--p12-pass", "test") == 0
    v = values(tmp_path / "form_signed.pdf")
    assert v["Name"] == "Pat Example" and v["Phone"] == "+1-555-0123"
    assert all(s["ok"] for s in verify_signatures(tmp_path / "form_signed.pdf"))


def test_a_planted_symlink_cannot_redirect_the_output(tmp_path, applicant_signed, certs):
    victim = tmp_path / "victim.txt"
    victim.write_text("ORIGINAL\n")
    os.symlink(victim, tmp_path / ".form_signed.pdf.partial")  # where the output used to be staged
    assert run(applicant_signed, "--field", "Signature", "--p12", certs["supervisor"], "--p12-pass", "test") == 0
    assert victim.read_text() == "ORIGINAL\n"
    assert not (tmp_path / "form_signed.pdf").is_symlink()
    assert not [p for p in os.listdir(tmp_path) if p.endswith(".partial") and p != ".form_signed.pdf.partial"]


def test_profile_requires_applicant_signature_first(tmp_path, blank_form, certs, monkeypatch):
    prof_dir = tmp_path / "profiles"; prof_dir.mkdir()
    (prof_dir / "demo.json").write_text(json.dumps({
        "id": "demo", "match_fields": ["Applicant Signature", "Signature"],
        "require_signed": ["Applicant Signature"], "sign_field": "Signature", "fill": {}}))
    monkeypatch.setattr(cac_sign, "PROFILE_DIR", str(prof_dir))
    assert run(blank_form, "--p12", certs["supervisor"], "--p12-pass", "test") == 1
    assert not (tmp_path / "form0_signed.pdf").exists()


def test_refuses_when_existing_signature_is_broken(tmp_path, applicant_signed, certs):
    data = bytearray(open(applicant_signed, "rb").read())
    i = data.index(b"/Name")  # inside the applicant's signed byte range
    data[i + 1] = ord("M")
    bad = tmp_path / "bad.pdf"; bad.write_bytes(bytes(data))
    assert run(bad, "--verify") == 1
    assert run(bad, "--field", "Signature", "--p12", certs["supervisor"], "--p12-pass", "test") == 1
    assert not (tmp_path / "bad_signed.pdf").exists()


def test_refuses_read_only_and_already_signed(tmp_path, applicant_signed, certs):
    p = ["--p12", certs["supervisor"], "--p12-pass", "test"]
    assert run(applicant_signed, "--set", "Fixed=changed", "--field", "Signature", *p) == 1
    assert run(applicant_signed, "--field", "Applicant Signature", *p) == 1
    assert not (tmp_path / "form_signed.pdf").exists()


def test_certified_no_changes_blocks_filling(tmp_path, blank_form, certs):
    cert_doc = tmp_path / "cert.pdf"
    pyhanko_sign(blank_form, cert_doc, "Applicant Signature", certs["applicant"], certify_no_changes=True)
    assert run(cert_doc, "--set", "Name=x", "--field", "Signature",
               "--p12", certs["supervisor"], "--p12-pass", "test") == 1


def test_place_new_field_on_plain_pdf(tmp_path, certs):
    plain = tmp_path / "memo.pdf"
    c = canvas.Canvas(str(plain)); c.drawString(72, 720, "MEMORANDUM FOR RECORD"); c.save()
    assert run(plain, "--p12", certs["supervisor"], "--p12-pass", "test") == 1  # no field: told to --place
    assert run(plain, "--place", "1:350,60,580,90", "--p12", certs["supervisor"], "--p12-pass", "test") == 0
    sigs = verify_signatures(tmp_path / "memo_signed.pdf")
    assert len(sigs) == 1 and sigs[0]["ok"] and sigs[0]["field"] == "Signature1"


def test_dry_run_writes_nothing(tmp_path, applicant_signed):
    assert run(applicant_signed, "--set", "Name=x", "--field", "Signature", "--dry-run") == 0
    assert sorted(p.name for p in tmp_path.glob("*_signed*")) == []


def test_no_sign_fill_only(tmp_path, applicant_signed):
    assert run(applicant_signed, "--set", "Phone=+1-555-0123", "--no-sign") == 0
    out = tmp_path / "form_filled.pdf"
    assert values(out)["Phone"] == "+1-555-0123"
    assert [s["ok"] for s in verify_signatures(out)] == [True]


def test_text_and_checkbox_fill_is_form_filling(tmp_path, applicant_signed, certs):
    assert run(applicant_signed, "--set", "Name=Pat Example", "--set", "Agree=yes", "--field", "Signature",
               "--p12", certs["supervisor"], "--p12-pass", "test") == 0
    sigs = verify_signatures(tmp_path / "form_signed.pdf")
    assert sigs[0]["modification"] == "FORM_FILLING" and sigs[0]["changes_ok"]


def test_audit_catches_a_stray_overwrite(tmp_path, applicant_signed):
    """An update that rewrites something the tool never meant to touch is flagged."""
    from pyhanko.pdf_utils import generic
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    out = tmp_path / "stray.pdf"
    with open(applicant_signed, "rb") as f, open(out, "wb") as o:
        w = IncrementalPdfFileWriter(f)
        fields = cac_sign.walk_fields(w.root["/AcroForm"])
        fixed = fields["Fixed"]
        fixed.obj["/V"] = generic.TextStringObject("tampered")
        w.mark_update(fixed.ref)
        w.write(o)
    assert cac_sign.audit_last_revision(out, ["Name"]) != []
    assert cac_sign.audit_last_revision(out, ["Fixed"]) == []


def test_untrusted_applicant_cert_is_refused_unless_forced(tmp_path, blank_form, certs, capsys):
    """A self-made cert carrying the applicant's name is intact but not trusted: not OK."""
    stranger = make_p12(tmp_path / "x.p12", "APPLICANT.TEST.0000000001")  # same CN, not a trusted root
    forged = tmp_path / "forged.pdf"
    pyhanko_sign(blank_form, forged, "Applicant Signature", stranger)
    [sig] = verify_signatures(forged)
    assert sig["intact"] and sig["valid"] and not sig["trusted"] and not sig["ok"]
    assert run(forged, "--verify") == 1
    p = ["--field", "Signature", "--p12", certs["supervisor"], "--p12-pass", "test"]
    assert run(forged, *p) == 1
    assert not (tmp_path / "forged_signed.pdf").exists()
    assert "not chain to a root trusted" in capsys.readouterr().out
    assert run(forged, *p, "--force") == 0  # checked by hand, signed anyway


def test_content_changed_after_signing_is_refused(tmp_path, applicant_signed, certs):
    """Page content rewritten after the applicant signed: the signed bytes are intact, the form is not."""
    from pyhanko.pdf_utils import generic
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    altered = tmp_path / "altered.pdf"
    with open(applicant_signed, "rb") as f, open(altered, "wb") as o:
        w = IncrementalPdfFileWriter(f)
        page_ref = w.root["/Pages"]["/Kids"][0]
        page = page_ref.get_object()
        stream = generic.StreamObject(stream_data=b"BT /F1 24 Tf 72 400 Td (APPROVED 365 DAYS) Tj ET")
        page["/Contents"] = w.add_object(stream)
        w.update_container(page)
        w.write(o)
    [sig] = verify_signatures(altered)
    assert sig["intact"] and not sig["changes_ok"] and not sig["ok"]
    assert run(altered, "--verify") == 1
    assert run(altered, "--field", "Signature", "--p12", certs["supervisor"], "--p12-pass", "test") == 1
    assert not (tmp_path / "altered_signed.pdf").exists()


def test_printable_escapes_terminal_controls():
    assert cac_signing.printable("ok\x1b[2K\rDONE\nx\u202ey\u2028z") == "ok\\x1b[2K\\rDONE\\nx\\u202ey\\u2028z"
    assert cac_signing.printable("Doe, Jane  (APD)") == "Doe, Jane  (APD)"


def test_pdf_text_cannot_drive_the_terminal(tmp_path, blank_form, monkeypatch, capsys):
    """Escape sequences and newlines in field values or the signer's name come out escaped."""
    evil = "x\x1b[1A\x1b[2K\nDONE  fake_signed.pdf  -- all 9 signature(s) verified intact"
    form = tmp_path / "evil.pdf"
    with pikepdf.open(blank_form) as pdf:
        for fld in pdf.Root.AcroForm.Fields:
            if str(fld.T) == "Name":
                fld.V = pikepdf.String(evil)
        pdf.save(form)
    signer = make_p12(tmp_path / "e.p12", "SIGNER\x1b[2K\nDONE")
    trust(monkeypatch, signer)
    signed = tmp_path / "evil2.pdf"
    pyhanko_sign(form, signed, "Applicant Signature", signer)
    run(signed, "--list")
    run(signed, "--verify")
    run(signed, "--set", "Phone=1", "--no-sign", "--dry-run")
    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert not any(line.startswith("DONE") for line in out.splitlines())
    assert "\\x1b[2K\\nDONE" in out


def test_issuer_chain_walks_to_the_root():
    """The CAC holds only its own certs; the issuing CA chain comes from the keychain pool."""
    from asn1crypto import x509 as asn1_x509
    now = datetime.datetime.now(datetime.timezone.utc)

    def make(cn, issuer=None, ca=True):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
        iss_name, iss_key = (issuer[1], issuer[2]) if issuer else (name, key)
        b = (x509.CertificateBuilder().subject_name(name).issuer_name(iss_name).public_key(key.public_key())
             .serial_number(x509.random_serial_number()).not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
             .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
             .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
             .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(iss_key.public_key()), critical=False))
        cert = b.sign(iss_key, hashes.SHA256())
        return asn1_x509.Certificate.load(cert.public_bytes(serialization.Encoding.DER)), name, key

    root = make("TEST ROOT")
    other_root = make("OTHER ROOT")
    inter = make("TEST EMAIL CA", issuer=root)
    leaf = make("SIGNER.TEST", issuer=inter, ca=False)
    pool = [other_root[0], root[0], inter[0]]
    assert cac_signing.issuer_chain(leaf[0], pool) == [inter[0], root[0]]
    assert cac_signing.issuer_chain(leaf[0], [other_root[0]]) == []


def test_certified_no_changes_cannot_be_forced(tmp_path, blank_form, certs):
    cert_doc = tmp_path / "cert.pdf"
    pyhanko_sign(blank_form, cert_doc, "Applicant Signature", certs["applicant"], certify_no_changes=True)
    assert run(cert_doc, "--field", "Signature", "--force", "--p12", certs["supervisor"], "--p12-pass", "test") == 1
    assert not (tmp_path / "cert_signed.pdf").exists()


# ---------- cac_sign_finder.py: the Quick Action front end ----------

import cac_sign_finder  # noqa: E402


@pytest.fixture
def dialogs(monkeypatch):
    """Stub the two dialogs; record what they were shown."""
    seen = {"confirm": [], "show": []}
    monkeypatch.setattr(cac_sign_finder, "confirm", lambda text: seen["confirm"].append(text))
    monkeypatch.setattr(cac_sign_finder, "show",
                        lambda text, ok, reveal=(): seen["show"].append((text, ok, list(reveal))))
    return seen


def test_finder_flow_shows_plan_then_signs(tmp_path, applicant_signed, certs, dialogs):
    rc = cac_sign_finder.main([str(applicant_signed), "--field", "Signature",
                               "--p12", certs["supervisor"], "--p12-pass", "test"])
    assert rc == 0
    [plan] = dialogs["confirm"]
    assert "will sign:  Signature" in plan and plan.endswith("macOS will ask for your CAC PIN.")
    [(text, ok, reveal)] = dialogs["show"]
    out = tmp_path / "form_signed.pdf"
    assert ok and text.startswith("DONE  form_signed.pdf") and reveal == [str(out)]
    assert all(s["intact"] and s["valid"] for s in verify_signatures(out))


def test_finder_cancel_signs_nothing(tmp_path, applicant_signed, certs, dialogs, monkeypatch):
    def cancel(text):
        raise cac_sign_finder.Cancelled()
    monkeypatch.setattr(cac_sign_finder, "confirm", cancel)
    assert cac_sign_finder.main([str(applicant_signed), "--field", "Signature",
                                 "--p12", certs["supervisor"], "--p12-pass", "test"]) == 1
    assert not (tmp_path / "form_signed.pdf").exists() and dialogs["show"] == []


def test_finder_nothing_to_sign_explains_without_asking_for_pin(tmp_path, dialogs):
    plain = tmp_path / "memo.pdf"
    c = canvas.Canvas(str(plain)); c.drawString(72, 720, "MEMO"); c.save()
    assert cac_sign_finder.main([str(plain)]) == 1
    assert dialogs["confirm"] == []
    [(text, ok, reveal)] = dialogs["show"]
    assert "SKIP: no empty signature field" in text and not ok and reveal == []


def test_finder_uses_selection_when_given_no_files(tmp_path, applicant_signed, certs, dialogs, monkeypatch):
    monkeypatch.setattr(cac_sign_finder, "finder_selection", lambda: [str(applicant_signed), str(tmp_path)])
    assert cac_sign_finder.main(["--field", "Signature", "--p12", certs["supervisor"], "--p12-pass", "test"]) == 0
    assert (tmp_path / "form_signed.pdf").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="osascript")
def test_dialog_text_is_data_not_applescript(tmp_path):
    canary = tmp_path / "pwned"
    evil = f'x" & (do shell script "touch {canary}") & "'
    echo = ["on run argv", "return item 1 of argv", "end run"]
    assert cac_sign_finder._osascript(echo, evil) == evil
    assert not canary.exists()


# ---------- native macOS signing (system PIN window) ----------

def software_seckey_signer(p12):
    """A KeychainSigner over a software SecKey: the same Security.framework calls as the CAC, no PIN."""
    import Security
    from asn1crypto import x509 as asn1_x509
    from cryptography.hazmat.primitives.serialization import pkcs12
    key, cert, _ = pkcs12.load_key_and_certificates(open(p12, "rb").read(), b"test")
    der = key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.TraditionalOpenSSL,
                            serialization.NoEncryption())
    seckey, err = Security.SecKeyCreateWithData(der, {Security.kSecAttrKeyType: Security.kSecAttrKeyTypeRSA,
                                                      Security.kSecAttrKeyClass: Security.kSecAttrKeyClassPrivate},
                                                None)
    assert seckey is not None, err
    asn1_cert = asn1_x509.Certificate.load(cert.public_bytes(serialization.Encoding.DER))
    return cac_signing._keychain_signer_class()(seckey, asn1_cert)


@pytest.mark.skipif(sys.platform != "darwin", reason="Security.framework")
def test_native_signer_is_the_default_on_a_mac(tmp_path, applicant_signed, certs, monkeypatch):
    monkeypatch.setattr(cac_sign, "build_keychain_signer", lambda: software_seckey_signer(certs["supervisor"]))
    assert run(applicant_signed, "--set", "Name=Pat Example", "--field", "Signature") == 0  # no --p12, no --opensc
    sigs = verify_signatures(tmp_path / "form_signed.pdf")
    assert [s["ok"] for s in sigs] == [True, True]
    assert sigs[1]["signer"] == "SUPERVISOR.TEST.0000000002" and sigs[1]["coverage"] == "ENTIRE_FILE"
    from pyhanko.pdf_utils.reader import PdfFileReader
    with open(tmp_path / "form_signed.pdf", "rb") as f:
        certs_in_cms = [c.chosen.dump() for c in PdfFileReader(f).embedded_signatures[-1].signed_data["certificates"]]
    assert len(certs_in_cms) == len(set(certs_in_cms))  # no certificate embedded twice


def test_cancelled_pin_stops_the_batch_and_leaves_nothing(tmp_path, applicant_signed, certs, monkeypatch, capsys):
    from pyhanko.sign import signers

    class CancelAtPin(signers.SimpleSigner):
        async def async_sign_raw(self, data, digest_algorithm, dry_run=False):
            if dry_run:
                return await super().async_sign_raw(data, digest_algorithm, dry_run=True)
            raise cac_signing.SigningCancelled("PIN entry was cancelled; nothing was signed")

    second = tmp_path / "second.pdf"
    second.write_bytes(applicant_signed.read_bytes())
    def cancel_signer(args):
        base = signers.SimpleSigner.load_pkcs12(args.p12, passphrase=b"test")
        return CancelAtPin(signing_cert=base.signing_cert, signing_key=base.signing_key,
                           cert_registry=base.cert_registry)
    monkeypatch.setattr(cac_sign, "build_pkcs12_signer", cancel_signer)
    rc = run(applicant_signed, second, "--field", "Signature", "--p12", certs["supervisor"], "--p12-pass", "test")
    out = capsys.readouterr().out
    assert rc == 1 and "STOP" in out and "1 more file(s) not signed" in out
    assert sorted(p.name for p in tmp_path.iterdir() if "signed" in p.name or p.name.endswith(".partial")) == []


# ---------- cac_sign_editor.py: the review page ----------

import threading  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402

import cac_sign_editor  # noqa: E402
import pdf_readers  # noqa: E402


@pytest.fixture
def editor(applicant_signed, certs):
    ed = cac_sign_editor.Editor([str(applicant_signed)], ["--p12", certs["supervisor"], "--p12-pass", "test"])
    t = threading.Thread(target=ed.serve, daemon=True)
    t.start()
    yield ed
    ed.closed = True
    t.join(timeout=5)


def call(ed, path, body=None, token=True, host=None):
    req = urllib.request.Request(f"http://127.0.0.1:{ed.port}{path}",
                                 data=None if body is None else json.dumps(body).encode(), method="POST" if body is not None else "GET")
    if token:
        req.add_header("X-CAC-Token", ed.token)
    if host:
        req.add_header("Host", host)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status, r.headers.get("Content-Type"), r.read()


def test_editor_model_maps_fields_onto_the_page(applicant_signed):
    m = cac_sign_editor.form_model(str(applicant_signed), profiles=[], identity={})
    by = {f["name"]: f for f in m["fields"]}
    assert by["Applicant Signature"]["signature"]["ok"] and by["Applicant Signature"]["value"] == ""
    assert by["Fixed"]["readonly"] and by["Choice"]["options"] == ["A", "B"]
    assert m["sign_target"] == "Signature" and m["blocked"] is None
    for f in m["fields"]:
        for w in f["widgets"]:
            l, t, r, b = w["box"]
            assert 0 <= l < r <= 1 and 0 <= t < b <= 1 and w["page"] == 0
    # Name sits at y=700..720 points (reportlab's default page is A4): top = (h - 720) / h
    h = m["pages"][0]["h"]
    assert by["Name"]["widgets"][0]["box"][1] == pytest.approx((h - 720) / h, abs=1e-4)


@pytest.mark.parametrize("rot", [0, 90, 180, 270])
def test_display_mapping_round_trips(rot):
    g = {"crop": (10.0, 20.0, 622.0, 812.0), "rot": rot}
    g["w"], g["h"] = (792.0, 612.0) if rot in (90, 270) else (612.0, 792.0)
    rect = [100.0, 200.0, 300.0, 240.0]
    assert cac_sign_editor.from_display(g, cac_sign_editor.to_display(g, rect)) == pytest.approx(rect, abs=0.05)


def test_editor_signs_edits_through_the_same_checks(tmp_path, editor):
    status, _, body = call(editor, "/api/form?f=0")
    assert status == 200 and json.loads(body)["file"] == "form.pdf"
    prep = json.loads(call(editor, "/api/prepare", {"f": 0, "mode": "sign", "field": "Signature",
                                                    "values": {"Name": "Pat Example", "Agree": True, "Choice": "B"}})[2])
    assert prep["ok"] and prep["pages"] == 1 and prep["where"]["page"] == 0
    status, ctype, png = call(editor, "/api/preview?p=0")  # the staged file, i.e. exactly what gets signed
    assert ctype == "image/png" and png[:4] == b"\x89PNG"
    res = json.loads(call(editor, "/api/commit", {"ack": True})[2])
    assert res["ok"] and res["name"] == "form_signed.pdf" and "output" not in res
    out = tmp_path / "form_signed.pdf"
    v = values(out)
    assert v["Name"] == "Pat Example" and v["Agree"] != "/Off" and v["Choice"] == "/B"
    assert [s["field"] for s in res["signatures"]] == ["Applicant Signature", "Signature"]
    # a second run never overwrites: it picks the next free name
    res2 = json.loads(call(editor, "/api/fill", {"f": 0, "values": {"Phone": "+1-555-0123"}})[2])
    assert res2["ok"] and res2["name"] == "form_filled.pdf"
    assert list(os.scandir(editor.stage_dir)) == []  # staging copies are cleaned up


def test_editor_can_place_a_new_signature_box(tmp_path, certs):
    plain = tmp_path / "memo.pdf"
    c = canvas.Canvas(str(plain)); c.drawString(72, 720, "MEMORANDUM FOR RECORD"); c.save()
    res = cac_sign_editor.sign_direct(str(plain), {"mode": "place", "page": 0, "box": [0.55, 0.85, 0.95, 0.9],
                                                   "values": {}}, ["--p12", certs["supervisor"], "--p12-pass", "test"])
    assert res["ok"], res["text"]
    [sig] = verify_signatures(tmp_path / "memo_signed.pdf")
    assert sig["field"] == "Signature1" and sig["ok"]


def test_editor_refuses_without_token_or_with_a_foreign_host(editor):
    for kwargs in ({"token": False}, {"host": "evil.example:80"}):
        with pytest.raises(urllib.error.HTTPError) as e:
            call(editor, "/api/form?f=0", **kwargs)
        assert e.value.code == 403
    with pytest.raises(urllib.error.HTTPError) as e:
        call(editor, "/api/form?f=7")
    assert e.value.code == 500  # only the files it was started with


def test_editor_page_is_locked_down(editor):
    status, ctype, body = call(editor, "/", token=False)
    html = body.decode()
    assert status == 200 and ctype.startswith("text/html")
    assert "{{NONCE}}" not in html and "innerHTML" not in html and "eval(" not in html


@pytest.mark.skipif(sys.platform != "darwin", reason="Quartz")
def test_editor_renders_pages(editor):
    status, ctype, body = call(editor, "/api/page?f=0&p=0")
    assert status == 200 and ctype == "image/png" and body[:8] == b"\x89PNG\r\n\x1a\n"


def test_signing_needs_the_review_step_and_signs_the_reviewed_bytes(tmp_path, editor, applicant_signed):
    with pytest.raises(urllib.error.HTTPError) as e:  # there is no one-shot signing endpoint
        call(editor, "/api/apply", {"f": 0, "mode": "sign", "field": "Signature", "values": {}})
    assert e.value.code == 404
    assert json.loads(call(editor, "/api/commit", {"ack": True})[2])["ok"] is False  # nothing reviewed yet
    reviewed = applicant_signed.read_bytes()
    assert json.loads(call(editor, "/api/prepare", {"f": 0, "mode": "sign", "field": "Signature",
                                                    "values": {"Name": "Pat Example"}})[2])["ok"]
    snap, stage = editor.pending["snap"], editor.pending["stage"]
    with open(applicant_signed, "ab") as f:  # the file on disk changes after the review...
        f.write(b"\n% swapped after review\n")
    res = json.loads(call(editor, "/api/commit", {"ack": True})[2])
    assert res["ok"], res["text"]
    out = (tmp_path / "form_signed.pdf").read_bytes()
    assert out.startswith(reviewed) and b"swapped after review" not in out  # ...but the reviewed bytes are signed
    assert not os.path.exists(snap) and not os.path.exists(stage)


def test_a_tampered_snapshot_is_never_signed(tmp_path, editor):
    assert json.loads(call(editor, "/api/prepare", {"f": 0, "mode": "sign", "field": "Signature",
                                                    "values": {"Name": "Pat Example"}})[2])["ok"]
    with open(editor.pending["snap"], "ab") as f:
        f.write(b"\n% changed in the private folder\n")
    res = json.loads(call(editor, "/api/commit", {"ack": True})[2])
    assert not res["ok"] and "changed before signing" in res["text"]
    assert not (tmp_path / "form_signed.pdf").exists()


def test_readers_must_agree_before_the_page_signs(tmp_path, applicant_signed):
    assert cac_sign_editor.reader_problems(str(applicant_signed)) == []
    data = applicant_signed.read_bytes()
    i = data.rindex(b"startxref") + len(b"startxref")
    damaged = tmp_path / "damaged.pdf"   # a wrong xref offset: qpdf repairs, pyHanko strict refuses
    damaged.write_bytes(data[:i] + b"\n12\n%%EOF\n")
    with pikepdf.open(applicant_signed) as pdf:
        pdf.Root.AcroForm.XFA = pikepdf.String("<xdp:xdp xmlns:xdp='http://ns.adobe.com/xdp/'/>")
        pdf.save(tmp_path / "xfa.pdf")
    hybrid = tmp_path / "hybrid.pdf"
    hybrid.write_bytes(data.replace(b"trailer", b"trailer\n<< /XRefStm 0 >>", 1) if b"trailer" in data
                       else data + b"\n% /XRefStm\n")
    for bad, why in ((damaged, "repair"), (tmp_path / "xfa.pdf", "XFA"), (hybrid, "hybrid")):
        problems = cac_sign_editor.reader_problems(str(bad))
        assert problems, why
        m = cac_sign_editor.form_model(str(bad), profiles=[], identity={})
        assert m["blocked"] and m["blocked"].startswith("Not signing this file here"), why


def test_signed_file_must_look_like_what_was_reviewed(tmp_path, applicant_signed, certs):
    """If the signed result renders differently from the review (outside the signature box), it fails."""
    from PIL import Image
    stage_dir = tmp_path / "stage"; stage_dir.mkdir()
    extra = ["--p12", certs["supervisor"], "--p12-pass", "test"]
    res, pending = cac_sign_editor.prepare(str(applicant_signed), {"mode": "sign", "field": "Signature",
                                           "values": {"Name": "Pat Example"}}, str(stage_dir), extra)
    assert res["ok"]
    w, h = Image.open(io.BytesIO(pending["reference"][0])).size
    blank = io.BytesIO(); Image.new("RGB", (w, h), "white").save(blank, "PNG")
    pending["reference"] = [blank.getvalue()]  # pretend the reviewer saw an empty page
    res = cac_sign_editor.commit(pending, extra, ack=True)
    assert not res["ok"] and "does not look like what you reviewed" in res["text"]
    assert (tmp_path / "form_signed.FAILED.pdf").exists() and not (tmp_path / "form_signed.pdf").exists()


def _annotated_pdf(path, flags=0, javascript=False):
    """A page with a black square drawn only by an annotation appearance (no page content)."""
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    pdf.pages[0].obj.Contents = pdf.make_stream(b"q Q")  # pikepdf's blank page has an empty Flate stream pyHanko rejects
    ap = pdf.make_stream(b"0 0 0 rg 0 0 100 100 re f")
    ap.Type, ap.Subtype, ap.BBox = pikepdf.Name.XObject, pikepdf.Name.Form, [0, 0, 100, 100]
    annot = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Square,
                                                 Rect=[100, 600, 200, 700], F=flags, AP=pikepdf.Dictionary(N=ap)))
    pdf.pages[0].obj.Annots = pdf.make_indirect(pikepdf.Array([annot]))
    if javascript:
        pdf.Root.OpenAction = pikepdf.Dictionary(S=pikepdf.Name.JavaScript, JS=pikepdf.String("app.alert(1)"))
    pdf.save(path)
    return str(path)


@pytest.mark.skipif(sys.platform != "darwin", reason="Quartz")
def test_page_render_shows_annotations_other_viewers_show(tmp_path):
    from PIL import Image
    shown = _annotated_pdf(tmp_path / "shown.pdf", flags=4)          # Print: visible
    hidden = _annotated_pdf(tmp_path / "hidden.pdf", flags=4 | 32)   # NoView: hidden on screen
    def pixel(path):
        img = Image.open(io.BytesIO(cac_sign_editor.render_page(path, 0, scale=1.0))).convert("L")
        return img.getpixel((150, 792 - 650))
    assert pixel(shown) < 40 and pixel(hidden) > 200


def test_viewer_warnings_flag_scripts_and_redrawn_fields(tmp_path):
    w = cac_sign_editor.viewer_warnings(_annotated_pdf(tmp_path / "a.pdf", flags=4, javascript=True))
    assert any("scripts" in x for x in w)
    assert cac_sign_editor.viewer_warnings(_annotated_pdf(tmp_path / "b.pdf", flags=4)) == []
    with pikepdf.open(tmp_path / "b.pdf") as pdf:
        pdf.Root.AcroForm = pikepdf.Dictionary(Fields=pikepdf.Array(), NeedAppearances=True)
        pdf.save(tmp_path / "c.pdf")
    assert any("redraw" in x for x in cac_sign_editor.viewer_warnings(str(tmp_path / "c.pdf")))


@pytest.mark.parametrize("flags", [4 | 2, 4 | 32])  # Print + Hidden, Print + NoView
def test_content_hidden_on_screen_but_printed_is_never_signed(tmp_path, flags):
    """The review shows the screen view; a printout is what gets filed. Hide one from the other: refused."""
    hidden = _annotated_pdf(tmp_path / "hidden.pdf", flags=flags)
    assert any("hidden on screen but print" in p for p in pdf_readers.reader_problems(hidden))
    assert cac_sign_editor.form_model(hidden, profiles=[], identity={})["blocked"]
    assert pdf_readers.reader_problems(_annotated_pdf(tmp_path / "shown.pdf", flags=4)) == []


@pytest.mark.parametrize("names_its_page", [True, False])
def test_form_boxes_missing_from_the_page(tmp_path, blank_form, names_its_page):
    """A widget only in /AcroForm: refused if it names a page (a viewer may draw it), else a warning."""
    with pikepdf.open(blank_form, allow_overwriting_input=True) as pdf:
        ap = pdf.make_stream(b"BT /Helv 12 Tf 0 0 Td (APPROVED) Tj ET")
        ap.Type, ap.Subtype, ap.BBox = pikepdf.Name.XObject, pikepdf.Name.Form, [0, 0, 200, 20]
        ghost = pikepdf.Dictionary(Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Widget, FT=pikepdf.Name.Tx,
                                   T=pikepdf.String("Ghost"), Rect=[72, 300, 272, 320], F=4,
                                   AP=pikepdf.Dictionary(N=ap))
        if names_its_page:
            ghost.P = pdf.pages[0].obj
        pdf.Root.AcroForm.Fields.append(pdf.make_indirect(ghost))
        pdf.save(blank_form)
    problems = pdf_readers.reader_problems(str(blank_form))
    warnings = cac_sign_editor.viewer_warnings(str(blank_form))
    if names_its_page:
        assert any("missing from it" in p for p in problems)
    else:
        assert problems == [] and any("on no page" in w for w in warnings)


def test_form_boxes_buried_too_deep_are_refused(tmp_path, blank_form):
    with pikepdf.open(blank_form, allow_overwriting_input=True) as pdf:
        node = pdf.make_indirect(pikepdf.Dictionary(Subtype=pikepdf.Name.Widget, Rect=[72, 300, 272, 320], F=4))
        for i in range(40):
            node = pdf.make_indirect(pikepdf.Dictionary(T=pikepdf.String(f"n{i}"), Kids=pikepdf.Array([node])))
        pdf.Root.AcroForm.Fields.append(node)
        pdf.save(blank_form)
    with pikepdf.open(blank_form) as pdf:
        assert pdf_readers._orphan_widgets(pdf)[0] >= 1


def test_form_boxes_on_the_wrong_page_are_refused(tmp_path, blank_form):
    """/P names page 2, /Annots puts it on page 1: viewers that place by either disagree."""
    with pikepdf.open(blank_form, allow_overwriting_input=True) as pdf:
        pdf.add_blank_page(page_size=(612, 792))
        pdf.pages[1].obj.Contents = pdf.make_stream(b"q Q")
        w = next(a for a in pdf.pages[0].obj.Annots if str(a.get("/T", "")) == "Name")
        w.P = pdf.pages[1].obj
        pdf.save(blank_form)
    assert any("different page" in p for p in pdf_readers.reader_problems(str(blank_form)))


def test_a_form_tree_that_cannot_be_walked_is_refused_not_crashed(tmp_path, blank_form):
    with pikepdf.open(blank_form, allow_overwriting_input=True) as pdf:
        pdf.Root.AcroForm.Fields.append(pdf.make_indirect(pikepdf.Dictionary(
            T=pikepdf.String("odd"), Kids=pikepdf.Array([7]))))
        pdf.save(blank_form)
    problems = pdf_readers.reader_problems(str(blank_form))
    assert problems and all(isinstance(p, str) for p in problems)


def _covered_pdf(path, fill, over_text=True, rotate=0):
    """Page text, and a screen-only (no Print flag) box drawn over it, or over an empty area."""
    raw = str(path) + ".raw.pdf"
    c = canvas.Canvas(raw, pagesize=(612, 792))
    c.setFont("Helvetica-Bold", 14)
    c.drawString(80, 600, "APPROVED: 90 DAYS EXCESS LEAVE")
    c.showPage(); c.save()
    pdf = pikepdf.open(raw)
    ap = pdf.make_stream(f"{fill} 0 0 340 26 re f".encode())
    ap.Type, ap.Subtype, ap.BBox = pikepdf.Name.XObject, pikepdf.Name.Form, [0, 0, 340, 26]
    y = 593 if over_text else 300
    box = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Square,
                                               Rect=[70, y, 410, y + 26], AP=pikepdf.Dictionary(N=ap)))
    pdf.pages[0].obj.Annots = pdf.make_indirect(pikepdf.Array([box]))
    if rotate:
        pdf.pages[0].obj.Rotate = rotate
    pdf.save(path)
    return str(path)


@pytest.mark.skipif(sys.platform != "darwin", reason="Quartz")
@pytest.mark.parametrize("fill,rotate", [("1 g", 0), ("0 g", 0), ("1 g", 90), ("0.5 g", 270)])
def test_a_screen_only_box_over_printed_text_is_never_signed(tmp_path, fill, rotate):
    """The review shows the box and hides the text; a printout drops the box and shows the text."""
    covered = _covered_pdf(tmp_path / "covered.pdf", fill, rotate=rotate)
    assert any("shown on screen but not printed" in p for p in pdf_readers.reader_problems(covered))


@pytest.mark.skipif(sys.platform != "darwin", reason="Quartz")
def test_a_screen_only_button_over_an_empty_area_is_fine(tmp_path):
    assert pdf_readers.reader_problems(_covered_pdf(tmp_path / "button.pdf", "0.8 g", over_text=False)) == []


@pytest.mark.skipif(sys.platform != "darwin", reason="Quartz")
def test_faint_printed_text_under_a_screen_only_box_is_found(tmp_path):
    raw = str(tmp_path / "faint.raw.pdf")
    c = canvas.Canvas(raw, pagesize=(612, 792))
    c.setFillGray(0.9)  # light grey: easy to miss on paper, still printed
    c.setFont("Helvetica-Bold", 14)
    c.drawString(80, 600, "APPROVED: 90 DAYS EXCESS LEAVE")
    c.showPage(); c.save()
    pdf = pikepdf.open(raw)
    ap = pdf.make_stream(b"1 g 0 0 340 26 re f")
    ap.Type, ap.Subtype, ap.BBox = pikepdf.Name.XObject, pikepdf.Name.Form, [0, 0, 340, 26]
    pdf.pages[0].obj.Annots = pdf.make_indirect(pikepdf.Array([pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Square, Rect=[70, 593, 410, 619], AP=pikepdf.Dictionary(N=ap)))]))
    pdf.save(tmp_path / "faint.pdf")
    assert any("shown on screen but not printed" in p for p in pdf_readers.reader_problems(str(tmp_path / "faint.pdf")))


@pytest.mark.skipif(sys.platform != "darwin", reason="Quartz")
def test_text_as_bright_as_its_background_under_a_cover_is_found(tmp_path):
    """Orange text on a grey of the same brightness: flat in luminance, plain in colour."""
    raw = str(tmp_path / "iso.raw.pdf")
    c = canvas.Canvas(raw, pagesize=(612, 792))
    c.setFillColorRGB(0.5, 0.5, 0.5)
    c.rect(60, 580, 380, 50, stroke=0, fill=1)
    c.setFillColorRGB(0.78, 0.39, 0.235)  # luminance ~0.49, close to the grey's 0.5
    c.setFont("Helvetica-Bold", 14)
    c.drawString(80, 600, "APPROVED: 90 DAYS EXCESS LEAVE")
    c.showPage(); c.save()
    pdf = pikepdf.open(raw)
    ap = pdf.make_stream(b"1 g 0 0 340 26 re f")
    ap.Type, ap.Subtype, ap.BBox = pikepdf.Name.XObject, pikepdf.Name.Form, [0, 0, 340, 26]
    pdf.pages[0].obj.Annots = pdf.make_indirect(pikepdf.Array([pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Square, Rect=[70, 593, 410, 619], AP=pikepdf.Dictionary(N=ap)))]))
    pdf.save(tmp_path / "iso.pdf")
    assert any("shown on screen but not printed" in p for p in pdf_readers.reader_problems(str(tmp_path / "iso.pdf")))


def _with_annotation(src, dst, **entries):
    pdf = pikepdf.open(src)
    annot = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Annot, Rect=[100, 100, 300, 130], F=4, **entries))
    pg = pdf.pages[0].obj
    pg.Annots = pdf.make_indirect(pikepdf.Array(list(pg.get("/Annots", [])) + [annot]))
    annot.P = pg
    pdf.save(dst)
    return str(dst)


@pytest.mark.parametrize("entries,expect", [
    (dict(Subtype=pikepdf.Name.FreeText, Contents=pikepdf.String("APPROVED"), DA=pikepdf.String("/Helv 12 Tf 0 g")),
     "no drawing of their own"),
    (dict(Subtype=pikepdf.Name.Popup, Open=True), "open by default"),
])
def test_annotations_other_viewers_draw_but_the_review_cannot_are_refused(tmp_path, entries, expect):
    path = _with_annotation(_annotated_pdf(tmp_path / "a.pdf", flags=4), tmp_path / "b.pdf", **entries)
    assert any(expect in p for p in pdf_readers.reader_problems(path))


def test_a_field_value_with_no_drawing_is_refused(tmp_path, blank_form):
    with pikepdf.open(blank_form, allow_overwriting_input=True) as pdf:
        w = next(a for a in pdf.pages[0].obj.Annots if str(a.get("/T", "")) == "Name")
        w.V = pikepdf.String("APPROVED")
        del w["/AP"]
        pdf.save(blank_form)
    assert any("a value but no drawing" in p for p in pdf_readers.reader_problems(str(blank_form)))


def test_screen_only_boxes_on_a_redrawn_form_are_refused(tmp_path):
    path = _covered_pdf(tmp_path / "redrawn.pdf", "0.8 g", over_text=False)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.Root.AcroForm = pikepdf.Dictionary(Fields=pikepdf.Array(), NeedAppearances=True)
        pdf.save(path)
    assert any("redraw" in p for p in pdf_readers.reader_problems(path))


def test_a_layer_that_does_not_print_is_never_signed(tmp_path):
    """A cover drawn in page content inside a layer that is on for screen but marked not to print."""
    path = _annotated_pdf(tmp_path / "oc.pdf", flags=4)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        ocg = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.OCG, Name=pikepdf.String("cover"),
                                                   Usage=pikepdf.Dictionary(Print=pikepdf.Dictionary(PrintState=pikepdf.Name.OFF))))
        pdf.Root.OCProperties = pikepdf.Dictionary(OCGs=pikepdf.Array([ocg]), D=pikepdf.Dictionary(ON=pikepdf.Array([ocg])))
        pdf.save(path)
    assert any("layers" in p for p in pdf_readers.reader_problems(path))


def _layer_cover_pdf(path, usage, declare, table=True):
    """Text, then a white cover in page content inside a layer referenced only from the page's resources."""
    raw = str(path) + ".raw.pdf"
    c = canvas.Canvas(raw, pagesize=(612, 792))
    c.setFont("Helvetica-Bold", 14)
    c.drawString(80, 600, "APPROVED: 90 DAYS EXCESS LEAVE")
    c.showPage(); c.save()
    pdf = pikepdf.open(raw)
    ocg = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.OCG, Name=pikepdf.String("cover"), **usage))
    pg = pdf.pages[0]
    pg.obj.Resources.Properties = pikepdf.Dictionary(cover=ocg)
    pg.contents_add(pdf.make_stream(b"/OC /cover BDC 1 g 70 593 340 26 re f EMC"), prepend=False)
    if table:
        pdf.Root.OCProperties = pikepdf.Dictionary(OCGs=pikepdf.Array([ocg] if declare else []),
                                                   D=pikepdf.Dictionary(ON=pikepdf.Array([ocg])))
    pdf.save(path)
    return str(path)


PRINT_OFF = dict(Usage=pikepdf.Dictionary(Print=pikepdf.Dictionary(PrintState=pikepdf.Name.OFF)))


@pytest.mark.parametrize("declare,table", [(True, True), (False, True), (False, False)])
def test_a_do_not_print_layer_is_refused_wherever_it_is_defined(tmp_path, declare, table):
    path = _layer_cover_pdf(tmp_path / "l.pdf", PRINT_OFF, declare, table)
    assert any("layers" in p for p in pdf_readers.reader_problems(path))


def test_layer_walk_has_no_blind_spots(tmp_path):
    """A resource named /P, a group with no /Type (judged by where it is used), a 300-deep chain, and a
    non-dictionary in the layer table: each is still judged."""
    def build(name, mutate):
        path = _layer_cover_pdf(tmp_path / f"{name}.pdf", {}, declare=False, table=False)
        with pikepdf.open(path, allow_overwriting_input=True) as pdf:
            mutate(pdf)
            pdf.save(path)
        return path
    def named_p(pdf):
        props = pdf.pages[0].obj.Resources.Properties
        props.P = pdf.make_indirect(pikepdf.Dictionary(Usage=pikepdf.Dictionary(Print=pikepdf.Dictionary(PrintState=pikepdf.Name.OFF))))
    def untyped(pdf):
        props = pdf.pages[0].obj.Resources.Properties
        props.cover = pdf.make_indirect(pikepdf.Dictionary(Name=pikepdf.String("x"), Intent=pikepdf.Name.Design))
    def deep(pdf):
        node = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.OCG, **PRINT_OFF))
        for _ in range(300):
            node = pdf.make_indirect(pikepdf.Dictionary(Next=node))
        pdf.pages[0].obj.Extra = node
    def odd_entry(pdf):
        pdf.Root.OCProperties = pikepdf.Dictionary(OCGs=pikepdf.Array([7]), D=pikepdf.Dictionary())
    for name, mutate in (("named_p", named_p), ("untyped", untyped), ("deep", deep), ("odd", odd_entry)):
        assert any("layers" in p for p in pdf_readers.reader_problems(build(name, mutate))), name


def test_layer_rules_the_review_renderer_cannot_resolve_are_refused(tmp_path):
    def build(name, mutate):
        path = _layer_cover_pdf(tmp_path / f"{name}.pdf", {}, declare=True)
        with pikepdf.open(path, allow_overwriting_input=True) as pdf:
            mutate(pdf, pdf.pages[0].obj.Resources.Properties.cover)
            pdf.save(path)
        return path
    def ve(pdf, g):
        pdf.pages[0].obj.Resources.Properties.cover = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.OCMD, OCGs=g, VE=pikepdf.Array([pikepdf.Name.Not, g])))
    def alloff(pdf, g):
        pdf.pages[0].obj.Resources.Properties.cover = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.OCMD, OCGs=g, P=pikepdf.Name.AllOff))
    def radio(pdf, g):
        h = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.OCG, Name=pikepdf.String("h")))
        pdf.Root.OCProperties.OCGs.append(h)
        pdf.Root.OCProperties.D.RBGroups = pikepdf.Array([pikepdf.Array([g, h])])
    def configs(pdf, g):
        pdf.Root.OCProperties.Configs = pikepdf.Array([pikepdf.Dictionary(OFF=pikepdf.Array([g]))])
    def single(pdf, g):  # an OCMD whose /OCGs is one untyped dictionary, not an array
        pdf.pages[0].obj.Resources.Properties.cover = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.OCMD, OCGs=pdf.make_indirect(pikepdf.Dictionary(Intent=pikepdf.Name.Design))))
    for name, mutate in (("ve", ve), ("alloff", alloff), ("radio", radio), ("configs", configs), ("single", single)):
        assert any("layers" in p for p in pdf_readers.reader_problems(build(name, mutate))), name


def test_a_plain_layer_left_out_of_the_table_is_only_a_warning(tmp_path):
    """Acrobat's Headers/Footers leave per-page groups out of /OCGs; that alone is not refused."""
    path = _layer_cover_pdf(tmp_path / "hf.pdf", dict(Usage=pikepdf.Dictionary(PageElement=pikepdf.Dictionary(
        Subtype=pikepdf.Name.HF))), declare=False)
    assert pdf_readers.reader_problems(path) == []
    assert any("has layers" in w for w in pdf_readers.viewer_warnings(path))


def test_an_open_note_is_refused(tmp_path):
    path = _annotated_pdf(tmp_path / "n.pdf", flags=4)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.pages[0].obj.Annots[0].Open = True
        pdf.save(path)
    assert any("open by default" in p for p in pdf_readers.reader_problems(path))


def test_a_field_value_chain_that_loops_is_refused(tmp_path, blank_form):
    with pikepdf.open(blank_form, allow_overwriting_input=True) as pdf:
        w = next(a for a in pdf.pages[0].obj.Annots if str(a.get("/T", "")) == "Name")
        del w["/AP"]
        if "/V" in w:
            del w["/V"]  # so the value has to come from the (looping) parents
        a_ = pdf.make_indirect(pikepdf.Dictionary(T=pikepdf.String("a")))
        b_ = pdf.make_indirect(pikepdf.Dictionary(T=pikepdf.String("b"), Parent=a_))
        a_.Parent = b_
        w.Parent = a_
        pdf.save(blank_form)
    with pikepdf.open(blank_form) as pdf:
        assert any("a value but no drawing" in p for p in pdf_readers._undrawn_problems(pdf))


def test_rich_text_comments_count_as_comments(tmp_path):
    path = _annotated_pdf(tmp_path / "rc.pdf", flags=4)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.pages[0].obj.Annots[0].RC = pikepdf.String("<body><p>approved</p></body>")
        pdf.save(path)
    assert any("comment(s) carry text" in w for w in pdf_readers.viewer_warnings(path))


def test_comment_text_is_a_warning(tmp_path):
    path = _annotated_pdf(tmp_path / "c.pdf", flags=4)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.pages[0].obj.Annots[0].Contents = pikepdf.String("approved for 90 days")
        pdf.save(path)
    assert any("comment(s) carry text" in w for w in pdf_readers.viewer_warnings(path))


def test_content_outside_the_crop_box_is_a_warning(tmp_path):
    path = _annotated_pdf(tmp_path / "cropped.pdf", flags=4)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        pdf.pages[0].obj.CropBox = [0, 100, 612, 792]
        pdf.save(path)
    assert any("crop box" in w for w in cac_sign_editor.viewer_warnings(path))


def test_page_text_never_carries_folder_paths(tmp_path):
    out = tmp_path / "deep" / "form_signed.pdf"
    text = f"form.pdf\n    output:     {out}\n"
    assert str(tmp_path) not in cac_sign_editor._shown(text, out) and "form_signed.pdf" in cac_sign_editor._shown(text, out)


def test_layers_switched_off_are_never_signed(tmp_path):
    path = _annotated_pdf(tmp_path / "layers.pdf", flags=4)
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        ocg = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.OCG, Name=pikepdf.String("secret")))
        pdf.Root.OCProperties = pikepdf.Dictionary(OCGs=pikepdf.Array([ocg]), D=pikepdf.Dictionary(
            OFF=pikepdf.Array([ocg])))
        pdf.save(path)
    assert any("layers" in p for p in pdf_readers.reader_problems(path))


def test_viewer_warnings_need_acknowledgement_and_reach_the_cli_plan(tmp_path, blank_form, certs, capsys):
    """A form with Acrobat scripts can be signed, but only once the signer has seen the warning."""
    with pikepdf.open(blank_form, allow_overwriting_input=True) as pdf:
        pdf.Root.OpenAction = pikepdf.Dictionary(S=pikepdf.Name.JavaScript, JS=pikepdf.String("1"))
        pdf.save(blank_form)
    scripted = tmp_path / "scripted.pdf"
    pyhanko_sign(blank_form, scripted, "Applicant Signature", certs["applicant"])
    stage = tmp_path / "stage"; stage.mkdir()
    extra = ["--p12", certs["supervisor"], "--p12-pass", "test"]
    res, pending = cac_sign_editor.prepare(str(scripted), {"mode": "sign", "field": "Signature", "values": {}},
                                           str(stage), extra)
    assert res["ok"] and res["changes"] == [] and any("scripts" in x for x in res["warnings"])
    refused = cac_sign_editor.commit(pending, extra, ack=False)
    assert not refused["ok"] and refused["needs_ack"] and not (tmp_path / "scripted_signed.pdf").exists()
    assert cac_sign_editor.commit(pending, extra, ack=True)["ok"]
    capsys.readouterr()
    run(scripted, "--field", "Signature", "--suffix", "_again", "--dry-run", *extra)
    assert "WARNING: This PDF contains Acrobat scripts" in capsys.readouterr().out


def test_editor_cli_keeps_secrets_out_of_argv_and_logs(tmp_path, applicant_signed, monkeypatch):
    with pytest.raises(SystemExit):
        cac_sign_editor.main(["--p12-pass", "secret", str(applicant_signed)])  # no such option any more
    with pytest.raises(SystemExit):
        cac_sign_editor.main(["--background", "--no-browser", str(applicant_signed)])
    seen = {}
    monkeypatch.setattr(cac_sign_editor.subprocess, "Popen", lambda argv, **kw: seen.update(argv=argv, kw=kw))
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Library" / "Logs").mkdir(parents=True)
    assert cac_sign_editor.main(["--background", str(applicant_signed)]) == 0
    assert seen["argv"][2:] == [str(applicant_signed)] and seen["kw"]["stdout"] == cac_sign_editor.subprocess.DEVNULL
    log = tmp_path / "Library" / "Logs" / "cac-sign-editor.log"
    assert oct(log.stat().st_mode & 0o777) == "0o600"


def test_escaped_hybrid_key_is_still_caught(tmp_path, applicant_signed):
    """`/XRef#53tm` is /XRefStm to every reader but slips past a byte search."""
    data = applicant_signed.read_bytes()
    i = data.rindex(b"trailer")
    j = data.index(b">>", i)
    sneaky = tmp_path / "sneaky.pdf"
    sneaky.write_bytes(data[:j] + b" /XRef#53tm 0 " + data[j:])
    assert b"/XRefStm" not in sneaky.read_bytes()
    assert cac_sign_editor.reader_problems(str(sneaky))
    assert cac_sign_editor.form_model(str(sneaky), profiles=[], identity={})["blocked"]


@pytest.mark.skipif(sys.platform != "darwin", reason="CoreGraphics")
def test_three_readers_see_the_same_pages_and_annotations(applicant_signed):
    import io as _io
    import pikepdf as _pk
    from pyhanko.pdf_utils.reader import PdfFileReader
    data = applicant_signed.read_bytes()
    with _pk.open(_io.BytesIO(data)) as pdf:
        q = pdf_readers._q_view(pdf)
    h = pdf_readers._h_view(pdf_readers._pyhanko_pages(PdfFileReader(_io.BytesIO(data), strict=True)))
    cg = pdf_readers._cg_view(pdf_readers._cg_doc(data))
    assert q == h == cg
    annots = q[0][1]
    assert len(annots) >= 6 and all(a[4] for a in annots if a[0] == "/Widget" and a[3] != "")  # states resolved


def test_any_reader_disagreeing_blocks_signing(applicant_signed, monkeypatch):
    pdf_readers._reader_problems.cache_clear()
    real = pdf_readers._cg_view
    monkeypatch.setattr(pdf_readers, "_cg_view", lambda doc: [("other", [])] + real(doc)[1:])
    try:
        problems = cac_sign_editor.reader_problems(str(applicant_signed))
        assert any("Apple's PDF reader and qpdf disagree" in p for p in problems)
    finally:
        pdf_readers._reader_problems.cache_clear()


def _damaged_copy(src, dst):
    data = src.read_bytes()
    i = data.rindex(b"startxref") + len(b"startxref")
    dst.write_bytes(data[:i] + b"\n12\n%%EOF\n")  # wrong xref offset: qpdf must repair it
    return dst


def test_cli_refuses_to_sign_what_readers_disagree_on(tmp_path, applicant_signed, certs, capsys):
    damaged = _damaged_copy(applicant_signed, tmp_path / "damaged.pdf")
    rc = run(damaged, "--field", "Signature", "--p12", certs["supervisor"], "--p12-pass", "test")
    assert rc == 1 and not (tmp_path / "damaged_signed.pdf").exists()
    assert "PDF readers do not agree" in capsys.readouterr().out


def test_quick_action_refuses_what_readers_disagree_on(tmp_path, applicant_signed, certs, dialogs):
    damaged = _damaged_copy(applicant_signed, tmp_path / "damaged.pdf")
    assert cac_sign_finder.main([str(damaged), "--field", "Signature",
                                 "--p12", certs["supervisor"], "--p12-pass", "test"]) == 1
    assert dialogs["confirm"] == []
    [(text, ok, _)] = dialogs["show"]
    assert "PDF readers do not agree" in text and not ok


def test_signing_log_records_and_warns_on_resigning(tmp_path, applicant_signed, certs, capsys):
    p = ["--field", "Signature", "--p12", certs["supervisor"], "--p12-pass", "test"]
    assert run(applicant_signed, *p) == 0
    [e] = cac_sign.ledger_entries()
    assert set(e) == {"when", "input_sha256", "output", "output_sha256", "field", "signer"}  # no form content
    assert e["input_sha256"] == cac_sign.file_sha256(applicant_signed) and e["output"].endswith("form_signed.pdf")
    assert e["output_sha256"] == cac_sign.file_sha256(tmp_path / "form_signed.pdf") and e["field"] == "Signature"
    assert oct(os.stat(cac_sign.LEDGER).st_mode & 0o777) == "0o600"
    capsys.readouterr()
    assert run(applicant_signed, *p, "-o", tmp_path / "again.pdf") == 0
    out = capsys.readouterr().out
    assert "NOTE: you already signed this exact file" in out and "form_signed.pdf" in out
    assert run("--history") == 0
    hist = capsys.readouterr().out.splitlines()
    assert "again.pdf" in hist[0] and "form_signed.pdf" in hist[1]  # newest first


def test_review_lists_changes_and_flags_surprises(applicant_signed):
    m = cac_sign_editor.form_model(str(applicant_signed), profiles=[], identity={})
    ch = {c["name"]: c for c in cac_sign_editor.describe_changes(m, {"Name": "Pat", "Agree": True})}
    assert (ch["Name"]["from"], ch["Name"]["to"]) == ("(blank)", "Pat") and ch["Agree"]["to"] == "checked"
    # every box on the synthetic form sits above the applicant's signature
    assert any("APPLICANT.TEST.0000000001's part of the form" in f for f in ch["Name"]["flags"])
    prof = [{"id": "demo", "match_fields": ["Applicant Signature", "Signature"], "fill": {"Name": "x"}}]
    m2 = cac_sign_editor.form_model(str(applicant_signed), profiles=prof, identity={})
    ch2 = {c["name"]: c for c in cac_sign_editor.describe_changes(m2, {"Name": "Pat", "Phone": "1"})}
    assert ch2["Name"]["flags"] == []  # the profile fills it: expected, not flagged
    assert "not one of the boxes this form's profile fills" in ch2["Phone"]["flags"]
    assert any("part of the form" in f for f in ch2["Phone"]["flags"])


def test_review_warns_when_this_file_was_signed_before(tmp_path, applicant_signed, certs):
    extra = ["--p12", certs["supervisor"], "--p12-pass", "test"]
    req = {"mode": "sign", "field": "Signature", "values": {}}
    assert cac_sign_editor.sign_direct(str(applicant_signed), req, extra)["ok"]
    assert cac_sign_editor.form_model(str(applicant_signed))["signed_before"][0]["name"] == "form_signed.pdf"
    stage = tmp_path / "stage"; stage.mkdir()
    res, pending = cac_sign_editor.prepare(str(applicant_signed), req, str(stage), extra)
    try:
        assert res["ok"] and res["changes"] == [] and res["signed_before"][0]["name"] == "form_signed.pdf"
    finally:
        cac_sign_editor._remove(pending["snap"], pending["stage"])


def test_flagged_changes_need_acknowledgement_on_the_server(tmp_path, editor):
    """The tickbox is the page's way to say so; the server enforces it."""
    prep = json.loads(call(editor, "/api/prepare", {"f": 0, "mode": "sign", "field": "Signature",
                                                    "values": {"Name": "Pat Example"}})[2])
    assert prep["ok"] and any(c["flags"] for c in prep["changes"])
    for body in ({}, {"ack": False}, {"ack": "yes"}):  # a bare call, a no, and a not-quite-yes
        res = json.loads(call(editor, "/api/commit", body)[2])
        assert not res["ok"] and res.get("needs_ack") and editor.pending is not None  # review stays open
    assert not (tmp_path / "form_signed.pdf").exists()
    assert json.loads(call(editor, "/api/commit", {"ack": True})[2])["ok"]


def test_unflagged_signing_needs_no_acknowledgement(tmp_path, editor):
    prep = json.loads(call(editor, "/api/prepare", {"f": 0, "mode": "sign", "field": "Signature", "values": {}})[2])
    assert prep["ok"] and prep["changes"] == []
    assert json.loads(call(editor, "/api/commit", {})[2])["ok"]


# ---------- the packaged `cac-sign` command (pip, uv, Homebrew) ----------
import cac_sign_launch  # noqa: E402


def test_launcher_routes_to_each_front_end(monkeypatch):
    seen = []
    monkeypatch.setattr(cac_sign, "main", lambda argv: seen.append(("cli", argv)) or 0)
    monkeypatch.setattr(cac_sign_finder, "main", lambda argv: seen.append(("finder", argv)) or 0)
    monkeypatch.setattr(cac_sign_editor, "main", lambda argv: seen.append(("editor", argv)) or 0)
    assert cac_sign_launch.cli(["form.pdf", "--dry-run"]) == 0
    assert cac_sign_launch.cli(["--quick-action", "a.pdf"]) == 0
    assert cac_sign_launch.cli(["--review", "--background", "a.pdf"]) == 0
    assert seen == [("cli", ["form.pdf", "--dry-run"]), ("finder", ["a.pdf"]), ("editor", ["--background", "a.pdf"])]


def test_setup_installs_quick_actions_that_call_the_installed_command(tmp_path, monkeypatch):
    import plistlib, shlex
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cac_sign_launch.setup([], refresh=False) == 0
    services = tmp_path / "Library" / "Services"
    for name, (args, script) in cac_sign_launch.ACTIONS.items():
        with open(services / name / "Contents" / "document.wflow", "rb") as f:
            cmd = plistlib.load(f)["actions"][0]["action"]["ActionParameters"]["COMMAND_STRING"]
        assert script in cmd  # how install.sh and --remove recognise it as ours
        assert f'exec {shlex.quote(cac_sign_launch._command())} {args} "$@"' in cmd
    assert cac_sign_launch.setup(["--remove"], refresh=False) == 0
    assert not any(services.iterdir())


def test_setup_leaves_someone_elses_workflow_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    theirs = tmp_path / "Library" / "Services" / "CAC Sign.workflow" / "Contents"
    theirs.mkdir(parents=True)
    (theirs / "document.wflow").write_text("my own automator thing")
    assert cac_sign_launch.setup([], refresh=False) == 1
    assert (theirs / "document.wflow").read_text() == "my own automator thing"
