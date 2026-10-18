"""Shared CAC signing helpers: signer construction, PIN entry, signing, verification.

Used by cac_sign.py (and by private form generators that vendor it). The CAC specifics
here were hard-won on the live card (see CLAUDE.md, "CAC specifics") -- keep them:
multi-cert auto-selection, and CKA_ALWAYS_AUTHENTICATE per-signature re-auth.
"""
from __future__ import annotations
import getpass, os, subprocess, sys

# Where OpenSC's PKCS#11 module lands, in order of likelihood on this Mac.
PKCS11_CANDIDATES = (
    "/opt/homebrew/lib/opensc-pkcs11.so",    # Homebrew, Apple Silicon
    "/Library/OpenSC/lib/opensc-pkcs11.so",  # OpenSC .pkg installer
    "/usr/local/lib/opensc-pkcs11.so",       # Homebrew, Intel
)


def find_pkcs11_lib(explicit=None):
    if explicit:
        return explicit
    for p in PKCS11_CANDIDATES:
        if os.path.exists(p):
            return p
    raise SystemExit("No OpenSC PKCS#11 module found (brew install opensc), or pass --pkcs11-lib.")


def read_pin(prompt="CAC PIN: "):
    """CAC_PIN env, else a TTY prompt, else (macOS, no TTY) a hidden-answer dialog.

    The dialog fallback lets signing run where there is no terminal, e.g. a `!`
    command inside Claude Code or a Finder Quick Action.
    """
    pin = os.environ.get("CAC_PIN")
    if pin:
        return pin
    if sys.stdin.isatty():
        return getpass.getpass(prompt)
    if sys.platform == "darwin":
        script = ('text returned of (display dialog "Enter your CAC PIN to sign." default answer "" '
                  'with hidden answer with title "CAC sign" buttons {"Cancel", "Sign"} default button "Sign")')
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit("PIN entry cancelled.")
        return r.stdout.rstrip("\n")
    raise SystemExit("No terminal for the PIN prompt; run in a Terminal window or set CAC_PIN.")


def _select_signing_cert(session):
    """Auto-pick the CAC document-signing cert (non-repudiation), returning (cert_id, desc).

    DoD CACs carry several certs; the signing cert is the one with the
    nonRepudiation (content_commitment) key-usage bit, fall back to digitalSignature.
    """
    import pkcs11
    from cryptography import x509
    nonrepudiation = digital_sig = None
    for obj in session.get_objects({pkcs11.Attribute.CLASS: pkcs11.ObjectClass.CERTIFICATE}):
        try:
            cid = bytes(obj[pkcs11.Attribute.ID])
            cert = x509.load_der_x509_certificate(bytes(obj[pkcs11.Attribute.VALUE]))
            ku = cert.extensions.get_extension_for_class(x509.KeyUsage).value
        except Exception:
            continue
        try:
            desc = cert.subject.rfc4514_string()
        except Exception:
            desc = cid.hex()
        if getattr(ku, "content_commitment", False) and nonrepudiation is None:
            nonrepudiation = (cid, desc)
        if getattr(ku, "digital_signature", False) and digital_sig is None:
            digital_sig = (cid, desc)
    return nonrepudiation or digital_sig


def build_pkcs11_signer(args, pin):
    from pyhanko.sign.pkcs11 import open_pkcs11_session, PKCS11Signer
    session = open_pkcs11_session(args.pkcs11_lib, slot_no=getattr(args, "slot", None),
                                  token_label=getattr(args, "token_label", None), user_pin=pin)
    cert_label = getattr(args, "cert_label", None)
    cert_id = bytes.fromhex(args.cert_id) if getattr(args, "cert_id", None) else None
    key_id = bytes.fromhex(args.key_id) if getattr(args, "key_id", None) else None
    # CACs hold several certs; auto-select the signing cert unless one was named.
    if not cert_label and not cert_id:
        sel = _select_signing_cert(session)
        if sel is None:
            raise SystemExit("No signing certificate found on the card. "
                             "Run --list-pkcs11 and pass --cert-label or --cert-id.")
        cert_id, desc = sel
        print(f"Using signing certificate id={cert_id.hex()}  ({printable(desc)})")
    # The CAC non-repudiation key is CKA_ALWAYS_AUTHENTICATE: the chip requires a
    # context-specific login before every signature. Passing the PIN via
    # base_sign_kwargs makes python-pkcs11 reaffirm credentials per file, so one PIN
    # entry covers the whole batch.
    return PKCS11Signer(session, cert_label=cert_label, key_label=getattr(args, "key_label", None),
                        cert_id=cert_id, key_id=key_id or cert_id, ca_chain=_card_cert_chain(session, cert_id, cert_label),
                        base_sign_kwargs={"pin": pin})


def keychain_ca_certs():
    """CA certificates in the macOS keychains (asn1crypto), for building chains only.

    The CAC holds just its own certs, not the issuing DOD EMAIL CA. Acrobat embeds
    that intermediate; without it a signature cannot be checked offline. The DoD
    intermediates must be in the keychain (installing the DoD certificates puts them
    there). Trust still comes only from the system trust store: these certs are never
    trust anchors.
    """
    global _KEYCHAIN_CAS
    if _KEYCHAIN_CAS is None:
        _KEYCHAIN_CAS = []
        if sys.platform == "darwin":
            import re
            from asn1crypto import pem, x509 as asn1_x509
            try:
                dump = subprocess.run(["security", "find-certificate", "-a", "-p"], capture_output=True,
                                      timeout=20).stdout
            except Exception:
                dump = b""
            for block in re.findall(rb"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", dump, re.S):
                try:
                    c = asn1_x509.Certificate.load(pem.unarmor(block)[2])
                    if c.ca:
                        _KEYCHAIN_CAS.append(c)
                except Exception:
                    continue
    return _KEYCHAIN_CAS


_KEYCHAIN_CAS = None


def issuer_chain(cert, pool):
    """Issuers of `cert` found in `pool`, nearest first, up to a self-issued root."""
    chain, cur = [], cert
    while not cur.self_issued and len(chain) < 8:
        nxt = next((c for c in pool if c.subject == cur.issuer and c not in chain
                    and (cur.authority_key_identifier is None or c.key_identifier == cur.authority_key_identifier)),
                   None)
        if nxt is None:
            break
        chain.append(nxt)
        cur = nxt
    return chain


def _card_cert_chain(session, cert_id, cert_label):
    """The keychain issuer chain of the signing cert on the card, to embed in each signature.

    Best effort: if it cannot be built, the signature is still made, and the post-sign
    check reports the signer as not chained to a trusted root.
    """
    try:
        import pkcs11
        from asn1crypto import x509 as asn1_x509
        query = {pkcs11.Attribute.CLASS: pkcs11.ObjectClass.CERTIFICATE}
        if cert_id:
            query[pkcs11.Attribute.ID] = cert_id
        if cert_label:
            query[pkcs11.Attribute.LABEL] = cert_label
        obj = next(iter(session.get_objects(query)))
        cert = asn1_x509.Certificate.load(bytes(obj[pkcs11.Attribute.VALUE]))
        return issuer_chain(cert, keychain_ca_certs()) or None
    except Exception:
        return None


class SigningCancelled(Exception):
    """The PIN prompt was cancelled. Nothing was signed."""


class SigningError(Exception):
    """The card or macOS refused to sign (wrong PIN, card removed...). Stop the batch."""


def _keychain_signer_class():
    from pyhanko.sign.signers.pdf_cms import Signer

    class KeychainSigner(Signer):
        """Signs with a key held by macOS (the built-in smart-card driver for a CAC).

        macOS shows its own smart-card PIN window, the one Safari and Outlook use,
        and the PIN never passes through this program. The CAC signing key needs the
        PIN for every signature, so a batch asks once per file.
        """

        ALGORITHMS = {"sha256": "kSecKeyAlgorithmRSASignatureMessagePKCS1v15SHA256",
                      "sha384": "kSecKeyAlgorithmRSASignatureMessagePKCS1v15SHA384",
                      "sha512": "kSecKeyAlgorithmRSASignatureMessagePKCS1v15SHA512"}

        def __init__(self, key, cert, chain=()):
            from asn1crypto import algos
            from pyhanko_certvalidator.registry import SimpleCertificateStore
            import Security
            self._key = key
            bits = Security.SecKeyCopyAttributes(key).get(Security.kSecAttrKeySizeInBits, 2048)
            self._sig_len = (int(bits) + 7) // 8
            # pyHanko adds the signing cert to the CMS itself; the registry holds only the chain
            super().__init__(signing_cert=cert, cert_registry=SimpleCertificateStore.from_certs(list(chain)),
                             signature_mechanism=algos.SignedDigestAlgorithm({"algorithm": "sha256_rsa"}))

        async def async_sign_raw(self, data, digest_algorithm, dry_run=False):
            if dry_run:  # pyHanko sizing the signature: never touch the card for this
                return bytes(self._sig_len)
            import Security
            from Foundation import NSData
            alg = getattr(Security, self.ALGORITHMS[digest_algorithm])
            sig, err = Security.SecKeyCreateSignature(self._key, alg, NSData.dataWithBytes_length_(data, len(data)),
                                                      None)
            if sig is None:
                # errSecUserCanceled (-128), or CryptoTokenKit's TKErrorCodeCanceledByUser (-4)
                if err is not None and (err.code() == -128 or (err.domain() == "CryptoTokenKit" and err.code() == -4)):
                    raise SigningCancelled("PIN entry was cancelled; nothing was signed")
                raise SigningError(f"the card did not sign ({err.localizedDescription() if err else 'unknown error'})")
            return bytes(sig)

    return KeychainSigner


def _keychain_identities():
    """(rank, private key, asn1 cert, label) for each smart-card identity macOS can see.

    rank 2 = nonRepudiation ("Certificate For Digital Signature"), 1 = digitalSignature only.
    """
    if sys.platform != "darwin":
        raise SystemExit("Native smart-card signing needs macOS; use --opensc instead.")
    try:
        import Security
    except ImportError:
        raise SystemExit("Missing pyobjc-framework-Security; re-run install.sh, or `uv sync` in the cac-sign folder.")
    from asn1crypto import x509 as asn1_x509
    from cryptography import x509
    query = {Security.kSecClass: Security.kSecClassIdentity,
             Security.kSecAttrAccessGroup: Security.kSecAttrAccessGroupToken,  # smart cards only
             Security.kSecReturnRef: True, Security.kSecReturnAttributes: True,
             Security.kSecMatchLimit: Security.kSecMatchLimitAll}
    err, items = Security.SecItemCopyMatching(query, None)
    out = []
    for item in items or []:
        ident = item["v_Ref"]
        e1, cref = Security.SecIdentityCopyCertificate(ident, None)
        e2, key = Security.SecIdentityCopyPrivateKey(ident, None)
        if e1 or e2 or key is None:
            continue
        der = bytes(Security.SecCertificateCopyData(cref))
        try:
            ku = x509.load_der_x509_certificate(der).extensions.get_extension_for_class(x509.KeyUsage).value
            rank = 2 if ku.content_commitment else 1 if ku.digital_signature else 0
        except Exception:
            rank = 0
        out.append((rank, key, asn1_x509.Certificate.load(der), str(item.get("labl", "?"))))
    return out


def build_keychain_signer():
    """Signer for the CAC through macOS's built-in smart-card support (no OpenSC, no PIN here).

    Picks the nonRepudiation certificate ("Certificate For Digital Signature"), as for PKCS#11.
    """
    signable = [i for i in _keychain_identities() if i[0]]
    if not signable:
        raise SystemExit("No CAC signing certificate found. Is the card in the reader? "
                         "(Or use --opensc.)")
    _, key, cert, label = max(signable, key=lambda i: i[0])
    print(f"Using {printable(label)} (macOS asks for the PIN)")
    return _keychain_signer_class()(key, cert, issuer_chain(cert, keychain_ca_certs()))


def list_keychain_certs():
    ids = _keychain_identities()
    if not ids:
        print("No smart-card certificates visible to macOS. Is the card in the reader?")
    best = max((i[0] for i in ids), default=0)
    for rank, _, cert, label in ids:
        use = {2: "signing (nonRepudiation)", 1: "digitalSignature", 0: "not for signing"}[rank]
        pick = "  <- used to sign" if rank and rank == best else ""
        print(f"  {printable(label)}  [{use}]{pick}")


def build_pkcs12_signer(args):
    from pyhanko.sign import signers as _s
    pw = args.p12_pass.encode() if args.p12_pass else None
    return _s.SimpleSigner.load_pkcs12(args.p12, passphrase=pw)


def list_pkcs11_certs(lib, pin=None):
    import pkcs11
    for tok in pkcs11.lib(lib).get_tokens():
        print(f"\nToken: {tok.label!r}")
        try:
            with tok.open(user_pin=pin) as s:
                any_ = False
                for o in s.get_objects({pkcs11.Attribute.CLASS: pkcs11.ObjectClass.CERTIFICATE}):
                    any_ = True
                    try:  # `Attribute.LABEL in o` raises AttributeTypeInvalid on python-pkcs11 objects
                        lbl = o[pkcs11.Attribute.LABEL]
                    except Exception:
                        lbl = "?"
                    try:
                        cid = bytes(o[pkcs11.Attribute.ID]).hex()
                    except Exception:
                        cid = "?"
                    usage = ""
                    try:
                        from cryptography import x509
                        cert = x509.load_der_x509_certificate(bytes(o[pkcs11.Attribute.VALUE]))
                        ku = cert.extensions.get_extension_for_class(x509.KeyUsage).value
                        fl = []
                        if getattr(ku, "digital_signature", False): fl.append("digitalSignature")
                        if getattr(ku, "content_commitment", False): fl.append("nonRepudiation")
                        if getattr(ku, "key_encipherment", False): fl.append("keyEncipherment")
                        if fl: usage = "  usage: " + ",".join(fl)
                    except Exception:
                        pass
                    print(f"  cert-label: {lbl!r}  id: {cid}{usage}")
                if not any_:
                    print("  (no certs visible; try --pin)")
        except Exception as e:
            print(f"  could not read token: {e}")


def stamp_style():
    """Acrobat-like visible signature: 'Digitally signed by NAME / Date: ...', Helvetica, no box.

    pyHanko's default stamp is Courier in a heavy border over a pyHanko-logo
    watermark -- fine for testing, wrong for a form going up the chain.
    """
    from pyhanko import stamp
    from pyhanko.pdf_utils.font import SimpleFontEngineFactory
    from pyhanko.pdf_utils.layout import AxisAlignment, InnerScaling, Margins, SimpleBoxLayoutRule
    from pyhanko.pdf_utils.text import TextBoxStyle
    return stamp.TextStampStyle(
        stamp_text="Digitally signed by %(signer)s\nDate: %(ts)s",
        timestamp_format="%Y.%m.%d %H:%M:%S %Z",
        border_width=0,
        # box_layout_rule with zero margins: pyHanko's default text box pads 10 pt on
        # every side, which halves the text in a form-height (~22 pt) field.
        text_box_style=TextBoxStyle(
            font=SimpleFontEngineFactory("Helvetica", 0.57), font_size=10, leading=12,
            box_layout_rule=SimpleBoxLayoutRule(x_align=AxisAlignment.ALIGN_MIN, y_align=AxisAlignment.ALIGN_MID,
                                                margins=Margins(0, 0, 0, 0))),
        inner_content_layout=SimpleBoxLayoutRule(
            x_align=AxisAlignment.ALIGN_MIN, y_align=AxisAlignment.ALIGN_MID,
            margins=Margins(left=3, right=2, top=1, bottom=1), inner_content_scaling=InnerScaling.SHRINK_TO_FIT),
    )


# ---------- new signature field (generated documents) ----------
def add_empty_sig_field(in_pdf, out_pdf, rect, page_index=0, field="Signature1"):
    import pikepdf
    pdf = pikepdf.open(in_pdf)
    page = pdf.pages[page_index]
    fo = pdf.make_indirect(pikepdf.Dictionary(
        FT=pikepdf.Name.Sig, T=pikepdf.String(field), Ff=0,
        Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Widget,
        Rect=pikepdf.Array(list(rect)), F=4, P=page.obj,
        MK=pikepdf.Dictionary(BC=pikepdf.Array([0, 0, 0]), BG=pikepdf.Array([0.90, 0.93, 0.97])),
    ))
    page.Annots = (page.Annots if "/Annots" in page else pikepdf.Array())
    page.Annots.append(fo)
    root = pdf.Root
    if "/AcroForm" not in root:
        root.AcroForm = pdf.make_indirect(pikepdf.Dictionary(Fields=pikepdf.Array([fo])))
    elif "/Fields" not in root.AcroForm:
        root.AcroForm.Fields = pikepdf.Array([fo])
    else:
        root.AcroForm.Fields.append(fo)
    root.AcroForm.SigFlags = 3
    pdf.save(out_pdf)


def sign_pdf(in_pdf, out_pdf, rect, signer, page_index=0, field="Signature1", reason=None):
    from pyhanko.sign import signers as _s
    from pyhanko.sign.fields import SigFieldSpec
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    meta = _s.PdfSignatureMetadata(field_name=field, reason=reason)
    pdf_signer = _s.PdfSigner(meta, signer=signer,
                              new_field_spec=SigFieldSpec(sig_field_name=field, box=tuple(rect), on_page=page_index))
    with open(in_pdf, "rb") as inf, open(out_pdf, "wb") as outf:
        pdf_signer.sign_pdf(IncrementalPdfFileWriter(inf), output=outf)


# ---------- verification ----------
def _diff_policy():
    """pyHanko's DEFAULT_DIFF_POLICY with in-place appearance-stream changes disallowed.

    The in-place rule crashes (IndirectObjectExpected) on any form whose checkbox or
    radio /AP /N is a direct state dictionary -- i.e. most real checkboxes -- as soon
    as a later revision exists (pyHanko 0.35.1). Turning it off is stricter, not
    looser: fills done by this tool, and by Acrobat, write new appearance streams.
    """
    from pyhanko.sign.diff_analysis import (CatalogModificationRule, DocInfoRule, DSSCompareRule,
                                            FormUpdatingRule, GenericFieldModificationRule, MetadataUpdateRule,
                                            ModificationLevel, ObjectStreamRule, SigFieldCreationRule,
                                            SigFieldModificationRule, StandardDiffPolicy, XrefStreamRule)
    lta = ModificationLevel.LTA_UPDATES
    return StandardDiffPolicy(
        global_rules=[CatalogModificationRule(), DocInfoRule().as_qualified(lta), XrefStreamRule().as_qualified(lta),
                      ObjectStreamRule().as_qualified(lta), DSSCompareRule().as_qualified(lta),
                      MetadataUpdateRule().as_qualified(lta)],
        form_rule=FormUpdatingRule(field_rules=[
            SigFieldCreationRule(),
            SigFieldModificationRule(allow_in_place_appearance_stream_changes=False),
            GenericFieldModificationRule(allow_in_place_appearance_stream_changes=False)]))


# Trust anchors added to this Mac's trust store when validating (the tests add their
# throwaway roots here). DoD roots come from the keychain once the DoD certificates
# are installed.
EXTRA_TRUST_ROOTS = []


def printable(text):
    """Escape control and invisible format characters before text reaches the terminal.

    Field names, values and signer names come from the PDF. Raw, an ESC sequence or
    a newline in them could erase or fake a line ("DONE ... verified"), and a bidi
    override could reorder one. Spaces stay; everything else non-printing becomes
    a visible escape such as \\x1b or \\u202e.
    """
    import unicodedata
    out = []
    for ch in str(text):
        cat = unicodedata.category(ch)
        out.append(ascii(ch)[1:-1] if cat[0] == "C" or cat in ("Zl", "Zp") else ch)
    return "".join(out)


def verify_signatures(path):
    """Check every signature in a PDF. Returns one dict per signature, in file order.

    `ok` is the full verdict, failing closed. It is pyHanko's bottom line: intact,
    valid, chained to a root trusted on this Mac, timestamps valid, and every later
    change at most form filling/signing. On top of that, the changes must actually
    have been analysed. `detail` says which part failed.

    The parts are also returned separately, because cac_sign.py gates its own
    output differently. `intact` and `valid` are what Acrobat in AVHE got wrong
    (a "digest mismatch"). `changes_ok` can be a false negative: pyHanko cannot
    justify radio-button widget /AS updates and reports them as OTHER. So for the
    revision cac_sign.py writes itself, audit_last_revision is the gate on changes.
    """
    import logging
    from pyhanko.pdf_utils.reader import PdfFileReader
    from pyhanko.sign.diff_analysis import ModificationLevel
    from pyhanko.sign.validation import validate_pdf_signature
    from pyhanko_certvalidator import ValidationContext
    logging.getLogger("pyhanko").setLevel(logging.ERROR)
    logging.getLogger("pyhanko_certvalidator").setLevel(logging.ERROR)
    out, policy = [], _diff_policy()
    with open(path, "rb") as f:
        r = PdfFileReader(f, strict=False)
        for es in r.embedded_signatures:
            try:
                # keychain intermediates help build the path for signers that did not embed it
                vc = ValidationContext(allow_fetching=False, extra_trust_roots=list(EXTRA_TRUST_ROOTS),
                                       other_certs=keychain_ca_certs())
                st = validate_pdf_signature(es, vc, diff_policy=policy)
            except Exception as e:
                out.append({"field": es.field_name, "signer": "?", "when": "?", "ok": False, "intact": False,
                            "valid": False, "trusted": False, "changes_ok": False, "modification": None,
                            "coverage": None, "detail": f"could not validate: {e}"})
                continue
            mod = st.modification_level
            # mod is None only when the diff could not be analysed: unknown, so not ok
            changes_ok = bool(st.docmdp_ok) and mod is not None and mod <= ModificationLevel.FORM_FILLING
            cn = st.signing_cert.subject.native.get("common_name", "?")
            # local time: CMS signingTime is UTC, and "15 Oct 09:30" for a 14 Oct 23:30 HST signing misleads
            when = st.signer_reported_dt.astimezone().strftime("%d %b %Y %H:%M") if st.signer_reported_dt else "?"
            detail = []
            if not st.intact:
                detail.append("document changed after signing (digest mismatch)")
            if not st.valid:
                detail.append("signature does not verify")
            if not st.trusted:
                detail.append("signer's certificate does not chain to a root trusted on this Mac")
            if not changes_ok:
                detail.append(
                    f"changes after this signature are not confirmed as form filling only "
                    f"(pyHanko: {mod.name if mod else 'not analysed'}"
                    f"{'' if st.docmdp_ok else ', outside the signer' + chr(39) + 's permitted changes'})")
            ok = bool(st.bottom_line) and changes_ok
            if not ok and not detail:
                detail.append("rejected by pyHanko (timestamp or signing-constraint check failed)")
            out.append({"field": es.field_name, "signer": cn, "when": when, "ok": ok,
                        "intact": st.intact, "valid": st.valid, "trusted": st.trusted,
                        "changes_ok": changes_ok, "modification": mod.name if mod else None,
                        "coverage": st.coverage.name if st.coverage else None,
                        "detail": "; ".join(detail)})
    return out
