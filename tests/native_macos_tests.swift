import AppKit
import Foundation

// Deterministic native test suite for Worker B modules:
// Models, FileIntake, PatientClipboard, TokenMonitor, AdvisoryFillController, DesktopBridge, ServicesProvider, MakeIcon.

private func check(_ condition: @autoclosure () -> Bool, _ message: String, file: StaticString = #filePath, line: UInt = #line) {
    if !condition() {
        fputs("FAIL [\(file):\(line)]: \(message)\n", stderr)
        exit(1)
    }
}

private func makeField(
    name: String,
    label: String? = nil,
    kind: String = "text",
    value: FormValue = .text(""),
    readonly: Bool = false,
    locked: Bool = false,
    multiline: Bool = false,
    maxlen: Int? = nil,
    editable: Bool = true
) -> FormFieldModel {
    FormFieldModel(
        name: name,
        label: label,
        kind: kind,
        value: value,
        widgets: [WidgetPlacement(page: 0, box: [0.1, 0.1, 0.4, 0.14])],
        readonly: readonly,
        locked: locked,
        required: false,
        multiline: multiline,
        maxlen: maxlen,
        editable: editable
    )
}

// MARK: - Synthetic DER X.509 Certificate Builder for TokenMonitor Tests

private enum SyntheticDER {
    static func tlv(_ tag: UInt8, _ bytes: [UInt8]) -> [UInt8] {
        var out: [UInt8] = [tag]
        let len = bytes.count
        if len < 0x80 {
            out.append(UInt8(len))
        } else if len <= 0xFF {
            out.append(0x81)
            out.append(UInt8(len))
        } else {
            out.append(0x82)
            out.append(UInt8((len >> 8) & 0xFF))
            out.append(UInt8(len & 0xFF))
        }
        out.append(contentsOf: bytes)
        return out
    }

    static func nameSequence(cn: String) -> [UInt8] {
        let oid = tlv(0x06, [0x55, 0x04, 0x03]) // 2.5.4.3 CommonName
        let val = tlv(0x0C, Array(cn.utf8))     // UTF8String
        let atv = tlv(0x30, oid + val)
        let rdn = tlv(0x31, atv)
        return tlv(0x30, rdn)
    }

    static func generalizedTime(_ date: Date) -> [UInt8] {
        let fmt = DateFormatter()
        fmt.locale = Locale(identifier: "en_US_POSIX")
        fmt.timeZone = TimeZone(secondsFromGMT: 0)
        fmt.dateFormat = "yyyyMMddHHmmss'Z'"
        return tlv(0x18, Array(fmt.string(from: date).utf8))
    }

    static func buildCertificate(
        serial: UInt8,
        subjectCN: String,
        issuerCN: String,
        notBefore: Date,
        notAfter: Date,
        keyUsageByte0: UInt8
    ) -> Data {
        let version = tlv(0xA0, tlv(0x02, [0x02])) // v3
        let serialTLV = tlv(0x02, [serial])
        let sigAlg = tlv(0x30, tlv(0x06, [0x2A, 0x86, 0x48, 0x86, 0xF7, 0x0D, 0x01, 0x01, 0x0B])) // sha256WithRSAEncryption
        let issuer = nameSequence(cn: issuerCN)
        let validity = tlv(0x30, generalizedTime(notBefore) + generalizedTime(notAfter))
        let subject = nameSequence(cn: subjectCN)
        let spki = tlv(0x30, sigAlg + tlv(0x03, [0x00, 0x01, 0x02, 0x03, 0x04]))

        // KeyUsage extension (2.5.29.15)
        let kuOID = tlv(0x06, [0x55, 0x1D, 0x0F])
        let critical = tlv(0x01, [0xFF])
        let kuBits = tlv(0x03, [0x01, keyUsageByte0])
        let kuOctet = tlv(0x04, kuBits)
        let kuExt = tlv(0x30, kuOID + critical + kuOctet)
        let exts = tlv(0xA3, tlv(0x30, kuExt))

        let tbs = tlv(0x30, version + serialTLV + sigAlg + issuer + validity + subject + spki + exts)
        let sigVal = tlv(0x03, [0x00, 0xAA, 0xBB, 0xCC, 0xDD])
        return Data(tlv(0x30, tbs + sigAlg + sigVal))
    }
}

// MARK: - Test Suites

private func testFileIntake() throws {
    let fm = FileManager.default
    let root = fm.temporaryDirectory.appendingPathComponent("cacsign-intake-test-" + UUID().uuidString)
    try fm.createDirectory(at: root, withIntermediateDirectories: true)
    defer { try? fm.removeItem(at: root) }

    let sub = root.appendingPathComponent("Packet Forms/Sub")
    try fm.createDirectory(at: sub, withIntermediateDirectories: true)

    let pdfA = root.appendingPathComponent("alpha.pdf")
    let docxB = sub.appendingPathComponent("bravo.DOCX")
    let ignoredTxt = sub.appendingPathComponent("notes.txt")
    try "%PDF-1.4\n".write(to: pdfA, atomically: true, encoding: .utf8)
    try "PK\u{03}\u{04}".write(to: docxB, atomically: true, encoding: .utf8)
    try "ignore me".write(to: ignoredTxt, atomically: true, encoding: .utf8)

    let collected = try FileIntake.collectSupportedFiles(from: [root, pdfA])
    check(collected.count == 2, "Expected 2 deduplicated supported documents, got \(collected.count)")
    check(collected.map { $0.lastPathComponent } == ["alpha.pdf", "bravo.DOCX"], "Unexpected order: \(collected)")

    var rejectedUnsupported = false
    do {
        _ = try FileIntake.collectSupportedFiles(from: [ignoredTxt])
    } catch is FileIntake.IntakeError {
        rejectedUnsupported = true
    }
    check(rejectedUnsupported, "Direct unsupported .txt file must throw IntakeError")

    var rejectedRemote = false
    do {
        _ = try FileIntake.collectSupportedFiles(from: [URL(string: "https://example.invalid/form.pdf")!])
    } catch is FileIntake.IntakeError {
        rejectedRemote = true
    }
    check(rejectedRemote, "Remote URL must throw IntakeError")
}

private func testPatientClipboardAndSuggestions() {
    // 1. Multi-line GENERAL INFORMATION block
    let block = """
    GENERAL INFORMATION
    Full Name:
    Sample, Jordan K
    Date of Birth:
    04/09/1991
    Age:
    35 yrs
    Sex:
    F
    MRN:
    SYN-90421
    DoD ID:
    1000000042
    SSN:
    900-00-4321
    Home Address:
    100 Maple Street
    Apt: 4B
    Springfield, IL 62704-1234
    Home Phone:
    (555) 555-0144
    Email:
    jordan.sample@example.invalid
    Sponsor Name:
    Taylor R. Sample
    Sponsor Rank:
    CPT / O-3
    Relationship:
    Spouse
    """

    guard let pt = PatientClipboard.parse(block) else {
        check(false, "Failed to parse multi-line GENERAL INFORMATION block")
        return
    }
    check(pt.fields["last_name"] == "Sample", "last_name mismatch: \(String(describing: pt.fields["last_name"]))")
    check(pt.fields["first_name"] == "Jordan", "first_name mismatch: \(String(describing: pt.fields["first_name"]))")
    check(pt.fields["middle_name"] == "K", "middle_name mismatch")
    check(pt.fields["middle_initial"] == "K", "middle_initial mismatch")
    check(pt.fields["full_name"] == "Jordan K Sample", "full_name mismatch: \(String(describing: pt.fields["full_name"]))")
    check(pt.fields["full_name_comma"] == "Sample, Jordan K", "full_name_comma mismatch")
    check(pt.fields["dob"] == "04/09/1991", "dob mismatch")
    check(pt.fields["dob_yyyymmdd"] == "19910409", "dob_yyyymmdd mismatch")
    check(pt.fields["age"] == "35", "age mismatch")
    check(pt.fields["sex"] == "F", "sex mismatch")
    check(pt.fields["mrn"] == "SYN-90421", "mrn mismatch")
    check(pt.fields["dod_id"] == "1000000042", "dod_id mismatch")
    check(pt.fields["ssn_last4"] == "4321", "ssn_last4 must store only last 4 digits")
    check(pt.fields["ssn"] == nil, "Full SSN must never be stored in PatientContext")
    check(pt.fields["street"] == "100 Maple Street, Apt: 4B", "Apt: 4B must stay attached to street: \(String(describing: pt.fields["street"]))")
    check(pt.fields["city"] == "Springfield" && pt.fields["state"] == "IL" && pt.fields["zip"] == "62704-1234", "CSZ mismatch")
    check(pt.fields["phone"] == "555-555-0144", "phone formatting mismatch: \(String(describing: pt.fields["phone"]))")
    check(pt.fields["email"] == "jordan.sample@example.invalid", "email mismatch")
    check(pt.fields["sponsor_name"] == "Taylor R. Sample", "sponsor_name mismatch")
    check(pt.fields["sponsor_rank"] == "CPT / O-3", "sponsor_rank mismatch")
    check(pt.fields["relationship"] == "Spouse", "relationship mismatch")

    // 2. Inline clinical header banner
    let inlineHeader = "Patient: Rivera, Alex M | DOB: 1988-11-03 | Sex: M | MRN: MRN771 | DoD ID: 1000000099 | Phone: 1-555-555-0188"
    let pt2 = PatientClipboard.parse(inlineHeader)
    check(pt2?.fields["last_name"] == "Rivera", "Inline last_name mismatch")
    check(pt2?.fields["middle_initial"] == "M", "Inline middle_initial mismatch")
    check(pt2?.fields["dob"] == "11/03/1988", "Inline ISO DOB normalization mismatch")
    check(pt2?.fields["phone"] == "555-555-0188", "Inline 11-digit phone normalization mismatch")

    // 3. Party classification & strict party isolation
    let ptNameField = makeField(name: "PtName", label: "Patient Name (Last, First)")
    let ptDobCompact = makeField(name: "PtDOB", label: "Date of Birth (YYYYMMDD)")
    let ptCheckbox = makeField(name: "ConsentBox", label: "Patient Name Consent", kind: "checkbox", value: .bool(false))
    let signerNameField = makeField(name: "ProvName", label: "Physician Printed Name")
    let signerPhoneField = makeField(name: "ProvPhone", label: "Provider Duty Phone")
    let signerNPIField = makeField(name: "ProvNPI", label: "Provider NPI")
    let signerUnitField = makeField(name: "ProvUnit", label: "Command / Organization")
    let sponsorNameField = makeField(name: "SponName", label: "Sponsor's Name")
    let sponsorRankField = makeField(name: "SponRank", label: "Sponsor Rank")
    let sponsorPhoneField = makeField(name: "SponPhone", label: "Sponsor Phone")
    let guardianField = makeField(name: "GuardName", label: "Parent/Guardian — Name")

    check(PatientClipboard.classifyParty(for: ptNameField) == .patient, "Expected .patient")
    check(PatientClipboard.classifyParty(for: signerNameField) == .signer, "Expected .signer")
    check(PatientClipboard.classifyParty(for: sponsorNameField) == .sponsor, "Expected .sponsor")
    check(PatientClipboard.classifyParty(for: guardianField) == .other, "Expected .other")

    let persona: [String: String] = [
        "full_name": "CASEY P. CLINICIAN, MD",
        "title": "Staff Physician",
        "duty_phone": "555-555-0100",
        "npi": "1234567890",
        "clinic": "Synthetic Family Medicine Clinic",
        "unit": "Synthetic Medical Center"
    ]
    let ac: [AutocompleteItem] = [
        AutocompleteItem(key: "today", value: "25 NOV 2026"),
        AutocompleteItem(key: "today_us", value: "11/25/2026")
    ]

    check(PatientClipboard.fieldSuggestions(for: ptNameField, personaFields: persona, autocomplete: ac, patient: pt).first == "Sample, Jordan K", "Comma patient name suggestion mismatch")
    check(PatientClipboard.fieldSuggestions(for: ptDobCompact, personaFields: persona, autocomplete: ac, patient: pt).first == "19910409", "Compact DOB suggestion mismatch")
    check(PatientClipboard.fieldSuggestions(for: ptCheckbox, personaFields: persona, autocomplete: ac, patient: pt).isEmpty, "Checkboxes must NEVER receive suggestions")
    check(PatientClipboard.fieldSuggestions(for: ptNameField, personaFields: persona, autocomplete: ac, patient: nil).isEmpty, "Patient field must NEVER receive signer persona when patient is nil")
    check(PatientClipboard.fieldSuggestions(for: signerNameField, personaFields: persona, autocomplete: ac, patient: pt) == ["CASEY P. CLINICIAN, MD"], "Signer name suggestion mismatch")
    check(PatientClipboard.fieldSuggestions(for: signerPhoneField, personaFields: persona, autocomplete: ac, patient: pt) == ["555-555-0100"], "Signer phone must not leak patient phone")
    check(PatientClipboard.fieldSuggestions(for: sponsorNameField, personaFields: persona, autocomplete: ac, patient: pt).isEmpty, "Sponsor name must NOT be populated from patient clipboard")
    check(PatientClipboard.fieldSuggestions(for: sponsorRankField, personaFields: persona, autocomplete: ac, patient: pt).isEmpty, "Sponsor rank must NOT leak patient clipboard or signer title/rank")
    check(PatientClipboard.fieldSuggestions(for: sponsorPhoneField, personaFields: persona, autocomplete: ac, patient: pt).isEmpty, "Sponsor phone must not leak patient or signer phone")
    check(PatientClipboard.fieldSuggestions(for: guardianField, personaFields: persona, autocomplete: ac, patient: pt).isEmpty, "Third-party guardian field must stay empty")

    // 4. Signer autosave extraction (strictly signer party, 10-digit NPI validation)
    let badNPIField = makeField(name: "BadNPI", label: "Provider NPI")
    let updates = PatientClipboard.extractSaveableSignerUpdates(
        fields: [ptNameField, signerNameField, signerNPIField, signerUnitField, sponsorNameField],
        values: [
            "PtName": .text("Sample, Jordan K"),
            "ProvName": .text("CASEY P. CLINICIAN, MD"),
            "ProvNPI": .text("1234567890"),
            "ProvUnit": .text("Synthetic Medical Readiness Command"),
            "SponName": .text("Taylor R. Sample")
        ]
    )
    check(updates["full_name"] == "CASEY P. CLINICIAN, MD", "Autosave full_name missing")
    check(updates["npi"] == "1234567890", "Autosave 10-digit NPI missing")
    check(updates["unit"] == "Synthetic Medical Readiness Command", "Autosave unit missing: \(updates)")
    check(updates.count == 3, "Patient/sponsor fields must never autosave: \(updates)")

    let invalidNPIUpdates = PatientClipboard.extractSaveableSignerUpdates(
        fields: [badNPIField],
        values: ["BadNPI": .text("12345")]
    )
    check(invalidNPIUpdates.isEmpty, "Non-10-digit NPI must be rejected by autosave")

    // Bare "Phone" / "Email" fields must never autosave patient clipboard values or patientApplied fields
    let barePhone = makeField(name: "Phone", label: "Phone")
    let bareEmail = makeField(name: "Email", label: "Email")
    let blockedPtAutosave = PatientClipboard.extractSaveableSignerUpdates(
        fields: [barePhone, bareEmail],
        values: [
            "Phone": .text("555-555-0144"),
            "Email": .text("other@example.invalid")
        ],
        patientApplied: ["Email": .text("other@example.invalid")],
        patient: pt
    )
    check(blockedPtAutosave.isEmpty, "Patient phone/email must NEVER autosave into signer personas: \(blockedPtAutosave)")
}

@MainActor
private func testTokenMonitorAndCertRanking() {
    let now = Date(timeIntervalSince1970: 1_795_000_000)
    let before = now.addingTimeInterval(-86_400 * 30)
    let afterValid = now.addingTimeInterval(86_400 * 180)
    let afterSoon = now.addingTimeInterval(86_400 * 12 + 3600)

    // KeyUsage byte0 bits (RFC 5280): 0x80 = digitalSignature, 0x40 = nonRepudiation, 0x20 = keyEncipherment
    let pivAuthDER = SyntheticDER.buildCertificate(
        serial: 0x01,
        subjectCN: "DOE.CASEY.SYNTHETIC.1000000001",
        issuerCN: "DOD ID CA-73",
        notBefore: before,
        notAfter: afterValid,
        keyUsageByte0: 0x80 // Digital Signature only
    )
    let emailEncDER = SyntheticDER.buildCertificate(
        serial: 0x02,
        subjectCN: "DOE.CASEY.SYNTHETIC.1000000001",
        issuerCN: "DOD EMAIL CA-73",
        notBefore: before,
        notAfter: afterValid,
        keyUsageByte0: 0x20 // Key Encipherment only
    )
    let nonRepudDER = SyntheticDER.buildCertificate(
        serial: 0x03,
        subjectCN: "DOE.CASEY.SIGNING.1000000001",
        issuerCN: "DOD EMAIL CA-73",
        notBefore: before,
        notAfter: afterValid,
        keyUsageByte0: 0x40 // Non-Repudiation
    )

    let records = [
        TokenMonitor.RawCertificateRecord(der: pivAuthDER, label: "PIV Auth"),
        TokenMonitor.RawCertificateRecord(der: emailEncDER, label: "Key Management"),
        TokenMonitor.RawCertificateRecord(der: nonRepudDER, label: "Certificate For Digital Signature")
    ]

    let readyState = TokenMonitor.evaluateStatus(
        records: records,
        dodRootsTrusted: true,
        foundationModelsAvailable: false,
        now: now
    )
    check(readyState.readiness == .ready, "Expected .ready, got \(readyState.readiness)")
    check(readyState.preferredCert?.commonName == "DOE.CASEY.SIGNING.1000000001", "Non-Repudiation cert must rank ahead of Digital Signature")
    check(readyState.preferredCert?.keyUsage.contains("Non-Repudiation") == true, "Expected Non-Repudiation keyUsage")
    check(readyState.certificates.count == 3, "Expected all 3 certs listed")

    let untrustedState = TokenMonitor.evaluateStatus(
        records: records,
        dodRootsTrusted: false,
        foundationModelsAvailable: false,
        now: now
    )
    check(untrustedState.readiness == .untrustedRoots, "Expected .untrustedRoots when DoD Root CA missing")

    let soonDER = SyntheticDER.buildCertificate(
        serial: 0x04,
        subjectCN: "DOE.CASEY.EXPIRING.1000000001",
        issuerCN: "DOD EMAIL CA-73",
        notBefore: before,
        notAfter: afterSoon,
        keyUsageByte0: 0xC0 // Digital Signature + Non-Repudiation
    )
    let soonState = TokenMonitor.evaluateStatus(
        records: [TokenMonitor.RawCertificateRecord(der: soonDER)],
        dodRootsTrusted: true,
        foundationModelsAvailable: true,
        now: now
    )
    check(soonState.readiness == .expiringSoon(days: 12), "Expected .expiringSoon(days: 12), got \(soonState.readiness)")
    check(soonState.foundationModelsAvailable == true, "foundationModelsAvailable flag must propagate")

    let emptyState = TokenMonitor.evaluateStatus(records: [], dodRootsTrusted: true, foundationModelsAvailable: false, now: now)
    check(emptyState.readiness == .noCard, "Empty token list must report .noCard")

    // Live smoke check on TokenMonitor singleton (must not crash whether a card is inserted or not).
    TokenMonitor.shared.refreshNow()
    _ = TokenMonitor.shared.status
}

private func testAdvisoryFillController() async throws {
    let textField = makeField(name: "Diagnosis", label: "Primary Diagnosis", kind: "text", maxlen: 24)
    let checkField = makeField(name: "Consent", label: "Patient Consent", kind: "checkbox", value: .bool(false))
    let sigField = makeField(name: "ProviderSig", label: "Provider Signature", kind: "signature")
    let readonlyField = makeField(name: "LockedID", label: "Form ID", kind: "text", readonly: true)

    let eligible = AdvisoryFillController.eligibleTextFields(from: [textField, checkField, sigField, readonlyField])
    check(eligible.map(\.name) == ["Diagnosis"], "AdvisoryFillController must ONLY accept interactive text fields")

    let sanitized = AdvisoryFillController.sanitizeCandidates(
        [
            (fieldName: "Diagnosis", suggestedText: "Acute ankle sprain, right lateral ligament", source: "Assessment: Acute ankle sprain"),
            (fieldName: "Consent", suggestedText: "yes", source: "never allowed"),
            (fieldName: "Hallucinated", suggestedText: "foo", source: "bar")
        ],
        eligibleFields: eligible
    )
    check(sanitized.count == 1, "Only valid eligible text field should survive sanitization")
    check(sanitized[0].suggestedText.count <= 24, "maxlen must be respected")

    let placeholdersFiltered = AdvisoryFillController.sanitizeCandidates(
        [
            (fieldName: "Diagnosis", suggestedText: "N/A", source: ""),
            (fieldName: "Diagnosis", suggestedText: "none", source: "")
        ],
        eligibleFields: eligible
    )
    check(placeholdersFiltered.isEmpty, "N/A and none placeholders must be filtered out")

    let emptyNoteRes = try await AdvisoryFillController.suggestFromClinicalNote(noteText: "   ", unfilledFields: [textField])
    check(emptyNoteRes.isEmpty, "Blank clinical note must short-circuit to empty candidates")
    _ = AdvisoryFillController.isFoundationModelsAvailable
}

@MainActor
private func testServicesProvider() throws {
    let fm = FileManager.default
    let dir = fm.temporaryDirectory.appendingPathComponent("cacsign-services-test-" + UUID().uuidString)
    try fm.createDirectory(at: dir, withIntermediateDirectories: true)
    defer { try? fm.removeItem(at: dir) }

    let pdf = dir.appendingPathComponent("referral.pdf")
    try "%PDF-1.4\n".write(to: pdf, atomically: true, encoding: .utf8)

    let pboard = NSPasteboard.withUniqueName()
    pboard.clearContents()
    _ = pboard.writeObjects([pdf as NSURL])

    let provider = ServicesProvider()
    var reviewed: [URL] = []
    var quickSigned: [URL] = []
    provider.onReviewFiles = { reviewed = $0 }
    provider.onQuickSignFiles = { quickSigned = $0 }

    var serviceErr: NSString? = nil
    provider.reviewWithCACSign(pboard, userData: nil, error: &serviceErr)
    check(serviceErr == nil, "Unexpected service error: \(String(describing: serviceErr))")
    check(reviewed.map(\.lastPathComponent) == ["referral.pdf"], "onReviewFiles did not receive referral.pdf")

    provider.quickSignWithCAC(pboard, userData: nil, error: &serviceErr)
    check(serviceErr == nil, "Unexpected quickSign error")
    check(quickSigned.map(\.lastPathComponent) == ["referral.pdf"], "onQuickSignFiles did not receive referral.pdf")

    let emptyBoard = NSPasteboard.withUniqueName()
    emptyBoard.clearContents()
    var emptyErr: NSString? = nil
    provider.reviewWithCACSign(emptyBoard, userData: nil, error: &emptyErr)
    check(emptyErr != nil, "Empty service pasteboard should populate NSServices error message")
}

private func testDesktopBridge() async throws {
    let fm = FileManager.default
    let workDir = fm.temporaryDirectory.appendingPathComponent("cacsign-bridge-test-" + UUID().uuidString)
    try fm.createDirectory(at: workDir, withIntermediateDirectories: true)
    defer { try? fm.removeItem(at: workDir) }

    let supportDir = workDir.appendingPathComponent("app_support")
    try fm.createDirectory(at: supportDir, withIntermediateDirectories: true)

    let mockScript = workDir.appendingPathComponent("mock_cac_sign_desktop.py")
    let pythonSource = #"""
import json, os, sys, tempfile

assert "--serve" in sys.argv, "Expected --serve flag"
for forbidden in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "SSL_CERT_FILE"):
    assert forbidden not in os.environ, f"Forbidden env leaked: {forbidden}"

stage_dir = tempfile.mkdtemp(prefix="cac-sign-mock-stage-")
jobs = {}

while True:
    raw = sys.stdin.readline()
    if not raw:
        break
    raw = raw.strip()
    if not raw:
        continue
    req = json.loads(raw)
    rid = req.get("id")
    op = req.get("op")
    if op == "inspect":
        data = {"documents": [{
            "source_path": req["paths"][0],
            "pdf_path": req["paths"][0],
            "file": os.path.basename(req["paths"][0]),
            "conversion_note": None,
            "sha256": "deadbeef",
            "pages": [{"w": 612.0, "h": 792.0}],
            "profile": None,
            "reason": "Supervisor approval",
            "next_step": None,
            "fields": [],
            "sign_target": "Signature1",
            "suggested_signature": None,
            "blocked": None,
            "notes": [f"pid={os.getpid()}"],
            "warnings": [],
            "profile_fields": None,
            "autocomplete": [],
            "signed_before": []
        }]}
    elif op == "fill_copy":
        data = {"saved": True, "output": req["pdf_path"] + ".filled.pdf", "name": "filled.pdf", "text": "Saved"}
    elif op == "prepare":
        jid = "job1234567890abcd"
        jobs[jid] = os.path.join(stage_dir, "staged.pdf")
        data = {
            "ready": True, "job_id": jid, "stage_path": jobs[jid], "pages": 1,
            "where": {"page": 0, "box": [0.1, 0.8, 0.4, 0.86]},
            "changes": [], "warnings": [], "signed_before": [], "needs_ack": False, "text": None
        }
    elif op == "commit":
        jid = req["job_id"]
        assert jid in jobs, "commit did not share persistent worker jobs dict"
        data = {
            "signed": True, "cancelled": False, "card_error": False, "needs_ack": False,
            "text": "DONE", "output": "/tmp/out_signed.pdf", "name": "out_signed.pdf",
            "sha256": "cafe1234", "signatures": [], "next_step": None
        }
    elif op == "discard":
        jobs.clear()
        data = {"discarded": True}
    elif op == "sign_quick":
        data = {"items": [{"path": req["paths"][0], "file": "a.pdf", "can_sign": True, "summary": "Ready", "warnings": [], "signed": not req["dry_run"], "output": None, "error": None}], "stopped_early": False}
    elif op == "verify":
        data = {"file": os.path.basename(req["path"]), "reader_problems": [], "warnings": [], "signatures": []}
    elif op in ("personas_get", "personas_save", "identity_autosave"):
        data = {"active_id": req.get("active_id", "default"), "personas": [{"id": "default", "name": "Primary", "fields": req.get("updates", {"npi": "1234567890"})}], "autocomplete": []}
    elif op in ("profiles_list", "profile_save", "profile_delete"):
        data = {"profiles": [{"id": "p1", "description": "Demo", "source": "user", "match_fields": ["F1"], "require_signed": [], "sign_field": "Sig", "reason": nil if False else None, "fill": {}, "next_step": None}]}
    elif op == "ledger_list":
        data = {"entries": []}
    elif op == "render_page":
        data = {"png_base64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+X2ZkAAAAASUVORK5CYII="}
    elif op == "diagnostics":
        data = {
            "python_version": "3.12", "pyhanko_version": "0.25", "pikepdf_version": "9.0",
            "libreoffice_path": None, "opensc_path": None, "dod_roots_trusted": True,
            "dod_ca_count": 4,
            "app_support_path": os.environ.get("CAC_SIGN_APP_SUPPORT", "")
        }
    else:
        sys.stdout.write(json.dumps({"id": rid, "ok": False, "error": f"Unknown op: {op}"}) + "\n")
        sys.stdout.flush()
        continue
    sys.stdout.write(json.dumps({"id": rid, "ok": True, "data": data}) + "\n")
    sys.stdout.flush()
"""#
    try pythonSource.write(to: mockScript, atomically: true, encoding: .utf8)

    // Inject proxy vars into parent env to prove DesktopBridge strips them completely.
    setenv("HTTP_PROXY", "http://127.0.0.1:8899", 1)
    setenv("HTTPS_PROXY", "http://127.0.0.1:8899", 1)
    setenv("ALL_PROXY", "socks5://127.0.0.1:8899", 1)
    setenv("SSL_CERT_FILE", "/tmp/fake-proxy-ca.pem", 1)

    let venvPy = fm.homeDirectoryForCurrentUser.appendingPathComponent("workspace/cac-sign/.venv/bin/python").path
    let pyBin = fm.isExecutableFile(atPath: venvPy) ? venvPy
        : (fm.isExecutableFile(atPath: "/opt/homebrew/bin/python3") ? "/opt/homebrew/bin/python3" : "/usr/bin/python3")
    let bridge = DesktopBridge(pythonPath: pyBin, scriptPath: mockScript.path, appSupportPath: supportDir.path)

    // Concurrent calls to verify actor reentrancy + serial ioQueue safety.
    async let diagTask = bridge.diagnostics()
    async let inspectTask = bridge.inspect(paths: ["/tmp/synthetic.pdf"])
    let (diag, docs) = try await (diagTask, inspectTask)
    check(diag.app_support_path == supportDir.path, "CAC_SIGN_APP_SUPPORT must be forwarded cleanly")
    check(docs.count == 1 && docs[0].sign_target == "Signature1", "inspect response mismatch")
    let firstPIDNote = docs[0].notes.first ?? ""

    // Verify persistent session shares job_id across prepare -> commit.
    let prep = try await bridge.prepare(
        pdfPath: "/tmp/synthetic.pdf",
        values: ["FieldA": .text("Val"), "CheckB": .bool(true)],
        additions: [FieldAddition(name: "AddedText1", kind: "text", label: "Label", page: 0, box: [0.1, 0.2, 0.3, 0.24])],
        mode: "field",
        field: "Signature1",
        page: nil,
        box: nil,
        opensc: false
    )
    check(prep.ready && prep.job_id == "job1234567890abcd", "prepare failed")
    let commitRes = try await bridge.commit(jobID: prep.job_id!, ack: true, reason: "Test", opensc: false, pin: nil)
    check(commitRes.signed && commitRes.sha256 == "cafe1234", "commit failed over persistent stage_dir")

    let docs2 = try await bridge.inspect(paths: ["/tmp/synthetic.pdf"])
    check(docs2[0].notes.first == firstPIDNote, "Worker PID changed mid-session; expected persistent subprocess")

    // Remaining RPC endpoints + respawn after shutdown.
    _ = try await bridge.fillCopy(pdfPath: "/tmp/synthetic.pdf", values: [:], additions: [])
    _ = try await bridge.signQuick(paths: ["/tmp/synthetic.pdf"], dryRun: true, opensc: false, pin: nil)
    _ = try await bridge.verify(path: "/tmp/out_signed.pdf")
    _ = try await bridge.getPersonas()
    _ = try await bridge.savePersonas(activeID: "default", personas: [SignerPersona(id: "default", name: "Primary", fields: [:])])
    let updatedPersonas = try await bridge.autosaveIdentity(updates: ["npi": "1234567890"])
    check(updatedPersonas.activePersona?.fields["npi"] == "1234567890", "autosaveIdentity mismatch")
    let profiles = try await bridge.listProfiles()
    check(profiles.count == 1, "listProfiles mismatch")
    _ = try await bridge.saveProfile(profiles[0])
    _ = try await bridge.deleteProfile(id: "p1")
    _ = try await bridge.listLedger(limit: 25)
    let renderedPNG = try await bridge.renderPagePNG(pdfPath: "/tmp/synthetic.pdf", page: 0, scale: 2.0)
    check(renderedPNG.count > 16, "renderPagePNG must decode base64 PNG bytes")
    await bridge.discard(jobID: prep.job_id)

    await bridge.shutdown()
    // Verify automatic respawn after worker exit/shutdown.
    let docsAfterRespawn = try await bridge.inspect(paths: ["/tmp/synthetic.pdf"])
    check(docsAfterRespawn.count == 1 && docsAfterRespawn[0].notes.first != firstPIDNote, "Expected clean worker respawn with new PID")
    await bridge.shutdown()
}

private func testMakeIcon(worktreeRoot: URL) throws {
    let iconOut = URL(fileURLWithPath: "/tmp/test_cac_icon.png")
    try? FileManager.default.removeItem(at: iconOut)

    let makeIconScript = worktreeRoot.appendingPathComponent("macos/MakeIcon.swift")
    let proc = Process()
    proc.executableURL = URL(fileURLWithPath: "/usr/bin/swift")
    proc.arguments = [makeIconScript.path, iconOut.path]
    try proc.run()
    proc.waitUntilExit()
    check(proc.terminationStatus == 0, "MakeIcon.swift exited with \(proc.terminationStatus)")

    let pngBytes = try Data(contentsOf: iconOut)
    check(pngBytes.count > 4096, "Generated icon PNG unexpectedly small: \(pngBytes.count) bytes")
    check(Array(pngBytes.prefix(8)) == [0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A], "Invalid PNG header")

    guard let rep = NSBitmapImageRep(data: pngBytes) else {
        check(false, "Failed to decode /tmp/test_cac_icon.png via NSBitmapImageRep")
        return
    }
    check(rep.pixelsWide == 1024 && rep.pixelsHigh == 1024, "Expected 1024x1024 icon, got \(rep.pixelsWide)x\(rep.pixelsHigh)")
}

@main
struct NativeMacOSTests {
    static func main() async throws {
        let worktreeRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()

        try testFileIntake()
        testPatientClipboardAndSuggestions()
        testTokenMonitorAndCertRanking()
        try await testAdvisoryFillController()
        try testServicesProvider()
        try await testDesktopBridge()
        try testMakeIcon(worktreeRoot: worktreeRoot)

        print("ALL NATIVE MACOS SWIFT TESTS PASSED (FileIntake, PatientClipboard, TokenMonitor, AdvisoryFillController, ServicesProvider, DesktopBridge, MakeIcon).")
        fflush(stdout)
        Darwin.exit(0)
    }
}
