import AppKit
import Combine
import Foundation
import SwiftUI

// MARK: - Document Session State

public struct DocumentSession: Identifiable, Equatable {
    public typealias UndoSnapshot = ([String: FormValue], [FormFieldModel], SuggestedSignatureBox?)

    public let id: UUID
    public var inspected: InspectedDocument
    public var values: [String: FormValue]
    public var originalValues: [String: FormValue]
    public var patientApplied: [String: FormValue]
    public var advisoryFields: Set<String>
    public var selectedSignField: String?
    public var placedSignature: SuggestedSignatureBox?
    public var signedOutputURL: URL?
    public var signedSummaries: [SignatureSummary]
    public var lastStatusText: String?
    public var undoStack: [UndoSnapshot]
    public var redoStack: [UndoSnapshot]

    public init(id: UUID = UUID(), inspected: InspectedDocument) {
        self.id = id
        self.inspected = inspected

        var initialValues: [String: FormValue] = [:]
        var origValues: [String: FormValue] = [:]
        for field in inspected.fields where field.kind != "signature" {
            origValues[field.name] = field.value
            initialValues[field.name] = field.isInteractiveInput ? (field.proposed ?? field.value) : field.value
        }
        self.values = initialValues
        self.originalValues = origValues
        self.patientApplied = [:]
        self.advisoryFields = []
        self.selectedSignField = inspected.sign_target
        if inspected.sign_target == nil {
            self.placedSignature = inspected.suggested_signature
        } else {
            self.placedSignature = nil
        }
        self.signedOutputURL = nil
        self.signedSummaries = []
        self.lastStatusText = nil
        self.undoStack = []
        self.redoStack = []
    }

    public static func == (lhs: DocumentSession, rhs: DocumentSession) -> Bool {
        lhs.id == rhs.id &&
        lhs.inspected == rhs.inspected &&
        lhs.values == rhs.values &&
        lhs.originalValues == rhs.originalValues &&
        lhs.patientApplied == rhs.patientApplied &&
        lhs.advisoryFields == rhs.advisoryFields &&
        lhs.selectedSignField == rhs.selectedSignField &&
        lhs.placedSignature == rhs.placedSignature &&
        lhs.signedOutputURL == rhs.signedOutputURL &&
        lhs.signedSummaries == rhs.signedSummaries &&
        lhs.lastStatusText == rhs.lastStatusText &&
        lhs.undoStack.count == rhs.undoStack.count &&
        lhs.redoStack.count == rhs.redoStack.count
    }

    public var isBlocked: Bool {
        inspected.blocked != nil
    }

    public var hasSignTarget: Bool {
        if let sf = selectedSignField, !sf.isEmpty {
            return true
        }
        return placedSignature != nil
    }

    public var isReadyForBatchWithoutAck: Bool {
        !isBlocked &&
        hasSignTarget &&
        signedOutputURL == nil &&
        inspected.warnings.isEmpty &&
        inspected.signed_before.isEmpty &&
        advisoryFields.isEmpty
    }

    public var signatureFields: [FormFieldModel] {
        inspected.fields.filter { $0.kind == "signature" }
    }

    public var interactiveFields: [FormFieldModel] {
        inspected.fields.filter { $0.kind != "signature" }
    }

    public var additions: [FieldAddition] {
        inspected.fields.compactMap { field in
            guard field.added == true, let widget = field.widgets.first else { return nil }
            return FieldAddition(
                name: field.name,
                kind: field.kind,
                label: field.displayLabel,
                page: widget.page,
                box: widget.box
            )
        }
    }

    public func currentValue(for field: FormFieldModel) -> FormValue {
        values[field.name] ?? field.value
    }

    public func isValueChanged(for field: FormFieldModel) -> Bool {
        let cur = values[field.name] ?? field.value
        let orig = originalValues[field.name] ?? field.value
        return cur != orig
    }

    public var changedValues: [String: FormValue] {
        var out: [String: FormValue] = [:]
        for field in inspected.fields where field.isInteractiveInput && isValueChanged(for: field) {
            if let v = values[field.name] {
                out[field.name] = v
            }
        }
        return out
    }

    public var blankInteractiveCount: Int {
        interactiveFields.filter { field in
            field.isInteractiveInput && currentValue(for: field).isBlank(forKind: field.kind)
        }.count
    }
}

// MARK: - Workspace ViewModel

@MainActor
public final class WorkspaceViewModel: ObservableObject {
    @Published public var sessions: [DocumentSession] = []
    @Published public var selectedSessionID: UUID?
    @Published public var personasState: PersonasState = PersonasState(active_id: "default", personas: [], autocomplete: [])
    @Published public var profiles: [FormProfileSpec] = []
    @Published public var ledger: [LedgerRecord] = []
    @Published public var diagnostics: SystemDiagnostics?
    @Published public var patientContext: PatientContext?

    @Published public var activeTool: CanvasTool = .select
    @Published public var zoomScale: CGFloat = 1.0
    @Published public var selectedFieldName: String?
    @Published public var inspectorBlanksOnly: Bool = false

    @Published public var openscEnabled: Bool = false
    @Published public var openscPIN: String = ""
    @Published public var signReason: String = ""

    @Published public var isBusy: Bool = false
    @Published public var busyMessage: String?
    @Published public var bannerError: String?
    @Published public var bannerInfo: String?

    @Published public var activePrepareResult: PrepareResult?
    @Published public var ackChecked: Bool = false
    @Published public var showPreSignSheet: Bool = false
    @Published public var showProfileBuilderSheet: Bool = false
    @Published public var showAdvisoryNoteSheet: Bool = false
    @Published public var advisoryNoteDraft: String = ""
    @Published public var showPatientPasteSheet: Bool = false
    @Published public var patientPasteDraft: String = ""
    @Published public var showWarningsSheet: Bool = false

    @Published public var quickSignPlan: QuickSignResult?
    @Published public var quickSignPendingPaths: [String] = []
    @Published public var showQuickSignConfirmSheet: Bool = false
    @Published public var quickSignSummaryAlert: String?
    @Published public var verifyResultSheet: VerifyResult?

    private var overlaySequence: Int = 1

    public init() {}

    public var currentSessionIndex: Int? {
        guard let id = selectedSessionID else { return sessions.indices.first }
        return sessions.firstIndex(where: { $0.id == id })
    }

    public var currentSession: DocumentSession? {
        guard let idx = currentSessionIndex, sessions.indices.contains(idx) else { return nil }
        return sessions[idx]
    }

    public var activePersonaFields: [String: String] {
        personasState.activePersona?.fields ?? [:]
    }

    public var combinedAutocomplete: [AutocompleteItem] {
        var seen = Set<String>()
        var result: [AutocompleteItem] = []
        for item in (personasState.autocomplete ?? []) + (currentSession?.inspected.autocomplete ?? []) {
            if seen.insert(item.id).inserted {
                result.append(item)
            }
        }
        return result
    }

    public var batchReadyCount: Int {
        sessions.filter(\.isReadyForBatchWithoutAck).count
    }

    public var canUndo: Bool {
        !(currentSession?.undoStack.isEmpty ?? true)
    }

    public var canRedo: Bool {
        !(currentSession?.redoStack.isEmpty ?? true)
    }

    // MARK: - Bootstrap & Intake

    public func loadBootstrapState() async {
        TokenMonitor.shared.refreshNow()
        do {
            async let pTask = DesktopBridge.shared.getPersonas()
            async let profTask = DesktopBridge.shared.listProfiles()
            async let ledTask = DesktopBridge.shared.listLedger(limit: 100)
            async let diagTask = DesktopBridge.shared.diagnostics()

            let (p, prof, led, diag) = try await (pTask, profTask, ledTask, diagTask)
            self.personasState = p
            self.profiles = prof
            self.ledger = led
            self.diagnostics = diag
        } catch {
            self.bannerError = error.localizedDescription
        }
    }

    public func openURLs(_ urls: [URL]) async {
        guard !urls.isEmpty else { return }
        bannerError = nil
        isBusy = true
        busyMessage = "Inspecting documents…"
        defer {
            isBusy = false
            busyMessage = nil
        }

        do {
            let collected = try FileIntake.collectSupportedFiles(from: urls)
            guard !collected.isEmpty else { return }
            let docs = try await DesktopBridge.shared.inspect(paths: collected.map(\.path))
            var firstNewID: UUID?
            for doc in docs {
                var session = DocumentSession(inspected: doc)
                if let ctx = patientContext {
                    applyPatient(ctx, to: &session)
                }
                if firstNewID == nil { firstNewID = session.id }
                sessions.append(session)
            }
            if let id = firstNewID {
                selectSession(id: id)
            }
        } catch {
            bannerError = error.localizedDescription
        }
    }

    public func selectSession(id: UUID) {
        selectedSessionID = id
        selectedFieldName = nil
        if let doc = currentSession {
            signReason = doc.inspected.reason ?? ""
        }
    }

    public func removeSession(id: UUID) {
        guard let idx = sessions.firstIndex(where: { $0.id == id }) else { return }
        let wasSelected = (selectedSessionID == id)
        sessions.remove(at: idx)
        if wasSelected {
            if sessions.indices.contains(idx) {
                selectSession(id: sessions[idx].id)
            } else if let last = sessions.last {
                selectSession(id: last.id)
            } else {
                selectedSessionID = nil
                selectedFieldName = nil
            }
        }
    }

    public func adjustZoom(delta: CGFloat) {
        let clamped = min(2.5, max(0.6, ((zoomScale + delta) * 20).rounded() / 20))
        zoomScale = clamped
    }

    public func resetZoom() {
        zoomScale = 1.0
    }

    // MARK: - Ephemeral Patient Clipboard & Fill Matched

    public func pastePatientFromClipboard() {
        bannerError = nil
        if let ctx = PatientClipboard.readFromGeneralPasteboard() {
            revertUntouchedPatientValues()
            patientContext = ctx
            applyActivePatientToOpenSessions()
            bannerInfo = "Applied ephemeral patient context (\(ctx.displayName)) in RAM."
        } else if let raw = NSPasteboard.general.string(forType: .string),
                  !raw.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            applyPatientText(raw)
        } else {
            showPatientPasteSheet = true
        }
    }

    public func applyPatientText(_ raw: String) {
        bannerError = nil
        revertUntouchedPatientValues()
        guard let ctx = PatientClipboard.parse(raw) else {
            patientContext = nil
            bannerError = "Clipboard text did not contain recognizable patient demographics."
            return
        }
        patientContext = ctx
        applyActivePatientToOpenSessions()
        bannerInfo = "Applied ephemeral patient context (\(ctx.displayName)) in RAM."
    }

    public func clearPatient() {
        revertUntouchedPatientValues()
        patientContext = nil
        bannerInfo = "Cleared ephemeral patient demographics from memory."
    }

    private func revertUntouchedPatientValues() {
        for idx in sessions.indices {
            for (fieldName, appliedVal) in sessions[idx].patientApplied {
                if sessions[idx].values[fieldName] == appliedVal {
                    let fallback = sessions[idx].originalValues[fieldName] ?? .text("")
                    sessions[idx].values[fieldName] = fallback
                }
            }
            sessions[idx].patientApplied.removeAll()
        }
    }

    private func applyActivePatientToOpenSessions() {
        guard let ctx = patientContext else { return }
        for idx in sessions.indices {
            applyPatient(ctx, to: &sessions[idx])
        }
    }

    private func applyPatient(_ ctx: PatientContext, to session: inout DocumentSession) {
        guard !session.isBlocked, session.signedOutputURL == nil else { return }
        for field in session.inspected.fields {
            guard field.isInteractiveInput,
                  field.kind != "checkbox",
                  PatientClipboard.classifyParty(for: field) == .patient else { continue }
            let cur = session.values[field.name] ?? field.value
            guard cur.isBlank(forKind: field.kind) else { continue }
            if let top = PatientClipboard.patientSuggestions(for: field, patient: ctx).first,
               !top.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                let applied: FormValue = .text(top)
                session.values[field.name] = applied
                session.patientApplied[field.name] = applied
            }
        }
    }

    public func fillAllMatched() {
        guard let idx = currentSessionIndex,
              !sessions[idx].isBlocked,
              sessions[idx].signedOutputURL == nil else { return }
        pushUndoSnapshot(at: idx)
        var filledCount = 0
        let personaMap = activePersonaFields
        let autoItems = combinedAutocomplete
        let patCtx = patientContext

        for field in sessions[idx].inspected.fields {
            guard field.isInteractiveInput, field.kind != "checkbox" else { continue }
            let cur = sessions[idx].values[field.name] ?? field.value
            guard cur.isBlank(forKind: field.kind) else { continue }
            let suggestions = PatientClipboard.fieldSuggestions(
                for: field,
                personaFields: personaMap,
                autocomplete: autoItems,
                patient: patCtx
            )
            if let top = suggestions.first, !top.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                let newVal: FormValue = .text(top)
                sessions[idx].values[field.name] = newVal
                if PatientClipboard.classifyParty(for: field) == .patient, patCtx != nil {
                    sessions[idx].patientApplied[field.name] = newVal
                }
                filledCount += 1
            }
        }
        bannerInfo = filledCount > 0 ? "Filled \(filledCount) matched blank field\(filledCount == 1 ? "" : "s")." : "No additional blank fields had matching suggestions."
    }

    // MARK: - Field Edits, Overlays, Signature Placement & Undo/Redo

    private func pushUndoSnapshot(at idx: Int) {
        let snap: DocumentSession.UndoSnapshot = (
            sessions[idx].values,
            sessions[idx].inspected.fields,
            sessions[idx].placedSignature
        )
        sessions[idx].undoStack.append(snap)
        if sessions[idx].undoStack.count > 50 {
            sessions[idx].undoStack.removeFirst()
        }
        sessions[idx].redoStack.removeAll()
    }

    public func undo() {
        guard let idx = currentSessionIndex,
              let prev = sessions[idx].undoStack.popLast() else { return }
        let curSnap: DocumentSession.UndoSnapshot = (
            sessions[idx].values,
            sessions[idx].inspected.fields,
            sessions[idx].placedSignature
        )
        sessions[idx].redoStack.append(curSnap)
        sessions[idx].values = prev.0
        sessions[idx].inspected.fields = prev.1
        sessions[idx].placedSignature = prev.2
    }

    public func redo() {
        guard let idx = currentSessionIndex,
              let next = sessions[idx].redoStack.popLast() else { return }
        let curSnap: DocumentSession.UndoSnapshot = (
            sessions[idx].values,
            sessions[idx].inspected.fields,
            sessions[idx].placedSignature
        )
        sessions[idx].undoStack.append(curSnap)
        sessions[idx].values = next.0
        sessions[idx].inspected.fields = next.1
        sessions[idx].placedSignature = next.2
    }

    public func setFieldValue(_ fieldName: String, value: FormValue, recordUndo: Bool = true) {
        guard let idx = currentSessionIndex else { return }
        let existing = sessions[idx].values[fieldName]
        if existing != value {
            if recordUndo {
                pushUndoSnapshot(at: idx)
            }
            sessions[idx].values[fieldName] = value
            if let patVal = sessions[idx].patientApplied[fieldName], patVal != value {
                sessions[idx].patientApplied.removeValue(forKey: fieldName)
            }
        }
        sessions[idx].advisoryFields.remove(fieldName)
    }

    public func confirmAdvisoryField(_ fieldName: String) {
        guard let idx = currentSessionIndex else { return }
        sessions[idx].advisoryFields.remove(fieldName)
    }

    public func addOverlayField(kind: String, page: Int, box: [Double]) {
        guard let idx = currentSessionIndex,
              !sessions[idx].isBlocked,
              sessions[idx].signedOutputURL == nil else { return }
        pushUndoSnapshot(at: idx)
        let normalizedKind = (kind == "checkbox") ? "checkbox" : "text"
        var candidateName = ""
        let existingNames = Set(sessions[idx].inspected.fields.map(\.name))
        repeat {
            candidateName = "AddedText\(overlaySequence)"
            overlaySequence += 1
        } while existingNames.contains(candidateName)

        let label = normalizedKind == "checkbox" ? "Added Checkbox" : "Added Text"
        let initVal: FormValue = normalizedKind == "checkbox" ? .bool(false) : .text("")
        let newField = FormFieldModel(
            name: candidateName,
            label: label,
            kind: normalizedKind,
            value: initVal,
            widgets: [WidgetPlacement(page: page, box: box)],
            editable: true,
            added: true
        )
        sessions[idx].inspected.fields.append(newField)
        sessions[idx].values[candidateName] = initVal
        sessions[idx].originalValues[candidateName] = initVal
        selectedFieldName = candidateName
        activeTool = .select
    }

    public func updateAddedFieldBox(name: String, page: Int, box: [Double], recordUndo: Bool = false) {
        guard let idx = currentSessionIndex,
              let fIdx = sessions[idx].inspected.fields.firstIndex(where: { $0.name == name && $0.added == true }) else { return }
        if recordUndo {
            pushUndoSnapshot(at: idx)
        }
        sessions[idx].inspected.fields[fIdx].widgets = [WidgetPlacement(page: page, box: box)]
    }

    public func removeAddedField(name: String) {
        guard let idx = currentSessionIndex,
              sessions[idx].inspected.fields.contains(where: { $0.name == name && $0.added == true }) else { return }
        pushUndoSnapshot(at: idx)
        sessions[idx].inspected.fields.removeAll { $0.name == name }
        sessions[idx].values.removeValue(forKey: name)
        sessions[idx].originalValues.removeValue(forKey: name)
        sessions[idx].advisoryFields.remove(name)
        if selectedFieldName == name { selectedFieldName = nil }
    }

    public func setPlacedSignature(page: Int, box: [Double], recordUndo: Bool = true) {
        guard let idx = currentSessionIndex else { return }
        if recordUndo {
            pushUndoSnapshot(at: idx)
        }
        sessions[idx].selectedSignField = nil
        sessions[idx].placedSignature = SuggestedSignatureBox(page: page, box: box, label: "Placed Signature")
        activeTool = .select
    }

    public func clearPlacedSignature() {
        guard let idx = currentSessionIndex else { return }
        pushUndoSnapshot(at: idx)
        sessions[idx].placedSignature = nil
    }

    public func selectSignatureField(_ fieldName: String?) {
        guard let idx = currentSessionIndex else { return }
        sessions[idx].selectedSignField = fieldName
    }

    // MARK: - Save Copy, Pre-Sign Prepare, Commit & Batch Signing

    public func saveFilledCopy() async {
        guard let doc = currentSession else { return }
        bannerError = nil
        isBusy = true
        busyMessage = "Saving filled PDF copy…"
        defer { isBusy = false; busyMessage = nil }

        do {
            let res = try await DesktopBridge.shared.fillCopy(
                pdfPath: doc.inspected.pdf_path,
                values: doc.changedValues,
                additions: doc.additions
            )
            if res.saved {
                bannerInfo = res.text.isEmpty ? "Saved filled copy: \(res.name ?? "")" : res.text
                if let idx = currentSessionIndex {
                    sessions[idx].lastStatusText = res.text
                }
            } else {
                bannerError = res.text.isEmpty ? "Could not save filled copy." : res.text
            }
        } catch {
            bannerError = error.localizedDescription
        }
    }

    public func beginSignReview() async {
        guard let doc = currentSession else { return }
        if let blocked = doc.inspected.blocked {
            bannerError = blocked
            return
        }
        let mode: String
        let fieldArg: String?
        let pageArg: Int?
        let boxArg: [Double]?

        if let sf = doc.selectedSignField, !sf.isEmpty {
            mode = "field"
            fieldArg = sf
            pageArg = nil
            boxArg = nil
        } else if let placed = doc.placedSignature {
            mode = "place"
            fieldArg = nil
            pageArg = placed.page
            boxArg = placed.box
        } else {
            bannerError = "Select a signature field or draw a signature box first."
            return
        }

        bannerError = nil
        isBusy = true
        busyMessage = "Staging document & running PDF reader checks…"
        defer { isBusy = false; busyMessage = nil }

        do {
            let prep = try await DesktopBridge.shared.prepare(
                pdfPath: doc.inspected.pdf_path,
                values: doc.changedValues,
                additions: doc.additions,
                mode: mode,
                field: fieldArg,
                page: pageArg,
                box: boxArg,
                opensc: openscEnabled
            )
            if prep.ready {
                activePrepareResult = prep
                ackChecked = !(prep.needs_ack ?? false)
                if signReason.isEmpty, let r = doc.inspected.reason {
                    signReason = r
                }
                showPreSignSheet = true
            } else {
                bannerError = prep.text ?? "Document could not be prepared for signing."
            }
        } catch {
            bannerError = error.localizedDescription
        }
    }

    public func cancelSignReview() async {
        let jobID = activePrepareResult?.job_id
        showPreSignSheet = false
        activePrepareResult = nil
        await DesktopBridge.shared.discard(jobID: jobID)
    }

    public func autosaveSignerFieldsIfChanged() async {
        guard let doc = currentSession else { return }
        let extracted = PatientClipboard.extractSaveableSignerUpdates(
            fields: doc.inspected.fields,
            values: doc.values,
            patientApplied: doc.patientApplied,
            patient: patientContext
        )
        let currentMap = activePersonaFields
        var delta: [String: String] = [:]
        for (k, v) in extracted {
            let trimmed = v.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !trimmed.isEmpty, currentMap[k] != trimmed else { continue }
            delta[k] = trimmed
        }
        guard !delta.isEmpty else { return }
        if let updated = try? await DesktopBridge.shared.autosaveIdentity(updates: delta) {
            personasState = updated
        }
    }

    public func confirmAndCommitSign() async {
        guard let prep = activePrepareResult, let jobID = prep.job_id else { return }
        if prep.needs_ack == true && !ackChecked {
            bannerError = "Please acknowledge the flagged changes and warnings before signing."
            return
        }

        bannerError = nil
        isBusy = true
        busyMessage = "Waiting for CAC smart-card signature…"
        defer { isBusy = false; busyMessage = nil }

        await autosaveSignerFieldsIfChanged()

        do {
            let trimmedReason = signReason.trimmingCharacters(in: .whitespacesAndNewlines)
            let pinArg = (openscEnabled && !openscPIN.isEmpty) ? openscPIN : nil
            let res = try await DesktopBridge.shared.commit(
                jobID: jobID,
                ack: ackChecked,
                reason: trimmedReason.isEmpty ? nil : trimmedReason,
                opensc: openscEnabled,
                pin: pinArg
            )
            if res.signed {
                if let idx = currentSessionIndex {
                    if let outPath = res.output {
                        sessions[idx].signedOutputURL = URL(fileURLWithPath: outPath)
                    }
                    sessions[idx].signedSummaries = res.signatures ?? []
                    sessions[idx].lastStatusText = res.text
                }
                showPreSignSheet = false
                activePrepareResult = nil
                bannerInfo = res.text.isEmpty ? "Signed & verified \(res.name ?? "")." : res.text
                if let led = try? await DesktopBridge.shared.listLedger(limit: 100) {
                    ledger = led
                }
            } else if res.needs_ack == true {
                ackChecked = false
                bannerError = res.text.isEmpty ? "Acknowledgment required before signing." : res.text
            } else if res.cancelled {
                showPreSignSheet = false
                activePrepareResult = nil
                await DesktopBridge.shared.discard(jobID: jobID)
                bannerInfo = "CAC PIN entry cancelled."
            } else {
                if res.card_error {
                    openscPIN = ""
                }
                showPreSignSheet = false
                activePrepareResult = nil
                await DesktopBridge.shared.discard(jobID: jobID)
                bannerError = res.text.isEmpty ? "CAC signing failed." : res.text
            }
        } catch {
            bannerError = error.localizedDescription
        }
    }

    public func batchSignReady() async {
        let candidateIDs = sessions.filter(\.isReadyForBatchWithoutAck).map(\.id)
        guard !candidateIDs.isEmpty else { return }

        bannerError = nil
        isBusy = true
        busyMessage = "Batch signing \(candidateIDs.count) ready document(s)…"
        defer { isBusy = false; busyMessage = nil }

        var signedCount = 0
        var skippedForAck = 0

        for sessionID in candidateIDs {
            guard let idx = sessions.firstIndex(where: { $0.id == sessionID }) else { continue }
            let doc = sessions[idx]
            let mode: String
            let fieldArg: String?
            let pageArg: Int?
            let boxArg: [Double]?
            if let sf = doc.selectedSignField, !sf.isEmpty {
                mode = "field"; fieldArg = sf; pageArg = nil; boxArg = nil
            } else if let placed = doc.placedSignature {
                mode = "place"; fieldArg = nil; pageArg = placed.page; boxArg = placed.box
            } else {
                continue
            }

            do {
                let prep = try await DesktopBridge.shared.prepare(
                    pdfPath: doc.inspected.pdf_path,
                    values: doc.changedValues,
                    additions: doc.additions,
                    mode: mode,
                    field: fieldArg,
                    page: pageArg,
                    box: boxArg,
                    opensc: openscEnabled
                )
                guard prep.ready, let jobID = prep.job_id else { continue }
                if prep.needs_ack == true || !(prep.warnings ?? []).isEmpty {
                    skippedForAck += 1
                    await DesktopBridge.shared.discard(jobID: jobID)
                    continue
                }

                let pinArg = (openscEnabled && !openscPIN.isEmpty) ? openscPIN : nil
                let res = try await DesktopBridge.shared.commit(
                    jobID: jobID,
                    ack: false,
                    reason: doc.inspected.reason,
                    opensc: openscEnabled,
                    pin: pinArg
                )

                // CRITICAL 3-strike CAC lockout protection: halt immediately on cancel or card error!
                if res.cancelled || res.card_error {
                    if res.card_error { openscPIN = "" }
                    await DesktopBridge.shared.discard(jobID: jobID)
                    bannerError = res.cancelled
                        ? "Batch signing stopped after user cancellation (\(signedCount) signed)."
                        : "Batch signing halted immediately on CAC card/PIN error to prevent 3-strike lockout: \(res.text)"
                    break
                }

                if res.signed {
                    signedCount += 1
                    if let outPath = res.output {
                        sessions[idx].signedOutputURL = URL(fileURLWithPath: outPath)
                    }
                    sessions[idx].signedSummaries = res.signatures ?? []
                    sessions[idx].lastStatusText = res.text
                }
            } catch {
                bannerError = "Batch signing stopped: \(error.localizedDescription)"
                break
            }
        }

        if let led = try? await DesktopBridge.shared.listLedger(limit: 100) {
            ledger = led
        }
        if bannerError == nil {
            let note = skippedForAck > 0 ? " (\(skippedForAck) skipped for manual warning review)" : ""
            bannerInfo = "Batch signed \(signedCount) document\(signedCount == 1 ? "" : "s")\(note)."
        }
    }

    // MARK: - Quick Sign Service, Advisory Fill & Settings Operations

    public func quickSignFromService(_ urls: [URL]) async {
        guard !urls.isEmpty else { return }
        bannerError = nil
        isBusy = true
        busyMessage = "Preparing Quick Sign plan…"
        defer { isBusy = false; busyMessage = nil }

        do {
            let collected = try FileIntake.collectSupportedFiles(from: urls)
            let filePaths = collected.map(\.path)
            guard !filePaths.isEmpty else { return }
            let plan = try await DesktopBridge.shared.signQuick(
                paths: filePaths,
                dryRun: true,
                opensc: openscEnabled,
                pin: nil
            )
            quickSignPendingPaths = filePaths
            quickSignPlan = plan
            showQuickSignConfirmSheet = true
        } catch {
            bannerError = error.localizedDescription
        }
    }

    public func confirmQuickSignService() async {
        guard !quickSignPendingPaths.isEmpty else { return }
        bannerError = nil
        isBusy = true
        busyMessage = "Signing with CAC…"
        defer { isBusy = false; busyMessage = nil }

        do {
            let pinArg = (openscEnabled && !openscPIN.isEmpty) ? openscPIN : nil
            let res = try await DesktopBridge.shared.signQuick(
                paths: quickSignPendingPaths,
                dryRun: false,
                opensc: openscEnabled,
                pin: pinArg
            )
            showQuickSignConfirmSheet = false
            quickSignPlan = nil
            quickSignPendingPaths = []

            let signedNames = res.items.filter(\.signed).map { $0.output.map { URL(fileURLWithPath: $0).lastPathComponent } ?? $0.file }
            let failedItems = res.items.filter { !$0.signed }
            var lines: [String] = []
            if !signedNames.isEmpty {
                lines.append("Signed (\(signedNames.count)): \(signedNames.joined(separator: ", "))")
            }
            for f in failedItems {
                lines.append("\(f.file): \(f.error ?? f.summary)")
            }
            if res.stopped_early {
                openscPIN = ""
                lines.append("Stopped early to protect CAC PIN retry counter.")
            }
            quickSignSummaryAlert = lines.isEmpty ? "Quick Sign completed." : lines.joined(separator: "\n")
            if let led = try? await DesktopBridge.shared.listLedger(limit: 100) {
                ledger = led
            }
        } catch {
            bannerError = error.localizedDescription
        }
    }

    public func cancelQuickSignService() {
        showQuickSignConfirmSheet = false
        quickSignPlan = nil
        quickSignPendingPaths = []
    }

    public func runAdvisoryNoteFill(noteText: String) async {
        let trimmed = noteText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty,
              let idx = currentSessionIndex,
              !sessions[idx].isBlocked,
              sessions[idx].signedOutputURL == nil else { return }

        let unfilled = sessions[idx].inspected.fields.filter { f in
            f.isInteractiveInput &&
            f.kind == "text" &&
            (sessions[idx].values[f.name] ?? f.value).isBlank(forKind: f.kind)
        }
        guard !unfilled.isEmpty else {
            bannerInfo = "No blank text fields remain to populate."
            showAdvisoryNoteSheet = false
            return
        }

        bannerError = nil
        isBusy = true
        busyMessage = "Analyzing clinical note locally…"
        defer { isBusy = false; busyMessage = nil }

        do {
            let candidates = try await AdvisoryFillController.suggestFromClinicalNote(
                noteText: trimmed,
                unfilledFields: unfilled
            )
            guard !candidates.isEmpty else {
                bannerInfo = "No advisory field candidates matched the clinical note."
                showAdvisoryNoteSheet = false
                return
            }
            pushUndoSnapshot(at: idx)
            var applied = 0
            for c in candidates {
                let text = c.suggestedText.trimmingCharacters(in: .whitespacesAndNewlines)
                guard !text.isEmpty else { continue }
                sessions[idx].values[c.fieldName] = .text(text)
                sessions[idx].advisoryFields.insert(c.fieldName)
                applied += 1
            }
            showAdvisoryNoteSheet = false
            bannerInfo = "Populated \(applied) advisory field\(applied == 1 ? "" : "s") — review amber highlights before signing."
        } catch {
            bannerError = error.localizedDescription
        }
    }

    public func selectActivePersona(id: String) async {
        guard personasState.personas.contains(where: { $0.id == id }) else { return }
        do {
            personasState = try await DesktopBridge.shared.savePersonas(
                activeID: id,
                personas: personasState.personas
            )
        } catch {
            bannerError = error.localizedDescription
        }
    }

    public func savePersonasState(activeID: String, personas: [SignerPersona]) async {
        do {
            personasState = try await DesktopBridge.shared.savePersonas(activeID: activeID, personas: personas)
            bannerInfo = "Saved signer personas to Application Support."
        } catch {
            bannerError = error.localizedDescription
        }
    }

    public func saveFormProfile(_ profile: FormProfileSpec) async -> Bool {
        do {
            profiles = try await DesktopBridge.shared.saveProfile(profile)
            bannerInfo = "Saved form profile '\(profile.id)'."
            return true
        } catch {
            bannerError = error.localizedDescription
            return false
        }
    }

    public func deleteFormProfile(id: String) async {
        do {
            profiles = try await DesktopBridge.shared.deleteProfile(id: id)
        } catch {
            bannerError = error.localizedDescription
        }
    }

    public func verifySignedFile(path filePath: String) async {
        do {
            verifyResultSheet = try await DesktopBridge.shared.verify(path: filePath)
        } catch {
            bannerError = error.localizedDescription
        }
    }

    public func refreshDiagnostics() async {
        TokenMonitor.shared.refreshNow()
        do {
            diagnostics = try await DesktopBridge.shared.diagnostics()
            ledger = try await DesktopBridge.shared.listLedger(limit: 100)
        } catch {
            bannerError = error.localizedDescription
        }
    }
}
