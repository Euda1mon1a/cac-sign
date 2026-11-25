import AppKit
import SwiftUI
import UniformTypeIdentifiers

public struct MainWindowView: View {
    @ObservedObject public var vm: WorkspaceViewModel
    @ObservedObject private var tokenMonitor = TokenMonitor.shared

    @State private var showCardPopover: Bool = false
    @State private var isDropTargeted: Bool = false

    public init(vm: WorkspaceViewModel) {
        self.vm = vm
    }

    public var body: some View {
        VStack(spacing: 0) {
            WorkspaceHeaderToolbar(
                vm: vm,
                tokenMonitor: tokenMonitor,
                showCardPopover: $showCardPopover
            )

            Divider()

            HStack(spacing: 0) {
                QueueSidebarView(vm: vm, isDropTargeted: $isDropTargeted)
                    .frame(width: 236)

                Divider()

                if let session = vm.currentSession {
                    CenterDocumentPane(vm: vm, session: session)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)

                    Divider()

                    RightFieldInspectorView(vm: vm, session: session)
                        .frame(width: 316)
                } else {
                    EmptyDropWorkspaceView(vm: vm, isDropTargeted: $isDropTargeted)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                }
            }
        }
        .onDrop(of: [.fileURL], isTargeted: $isDropTargeted) { providers in
            handleFileDrop(providers: providers)
        }
        .sheet(isPresented: $vm.showPreSignSheet) {
            if let prep = vm.activePrepareResult {
                PreSignReviewSheet(vm: vm, prep: prep)
            }
        }
        .sheet(isPresented: $vm.showProfileBuilderSheet) {
            ProfileBuilderSheet(vm: vm)
        }
        .sheet(isPresented: $vm.showAdvisoryNoteSheet) {
            AdvisoryClinicalNoteSheet(vm: vm)
        }
        .sheet(isPresented: $vm.showPatientPasteSheet) {
            ManualPatientPasteSheet(vm: vm)
        }
        .sheet(isPresented: $vm.showQuickSignConfirmSheet) {
            if let plan = vm.quickSignPlan {
                QuickSignConfirmSheet(vm: vm, plan: plan)
            }
        }
        .sheet(isPresented: $vm.showWarningsSheet) {
            if let session = vm.currentSession {
                DocumentWarningsDetailSheet(session: session)
            }
        }
        .sheet(item: $vm.verifyResultSheet) { res in
            VerifySignatureResultSheet(result: res)
        }
        .alert(
            "Quick Sign Result",
            isPresented: Binding(
                get: { vm.quickSignSummaryAlert != nil },
                set: { if !$0 { vm.quickSignSummaryAlert = nil } }
            )
        ) {
            Button("OK") { vm.quickSignSummaryAlert = nil }
        } message: {
            Text(vm.quickSignSummaryAlert ?? "")
        }
    }

    private func handleFileDrop(providers: [NSItemProvider]) -> Bool {
        guard !providers.isEmpty else { return false }
        for provider in providers {
            _ = provider.loadObject(ofClass: URL.self) { url, _ in
                guard let url else { return }
                Task { @MainActor in
                    await vm.openURLs([url])
                }
            }
        }
        return true
    }
}

// MARK: - Top Header / Action Bar

private struct WorkspaceHeaderToolbar: View {
    @ObservedObject var vm: WorkspaceViewModel
    @ObservedObject var tokenMonitor: TokenMonitor
    @Binding var showCardPopover: Bool

    var body: some View {
        VStack(spacing: 6) {
            HStack(spacing: 8) {
                cardStatusPill

                personaMenu

                patientControl

                Spacer(minLength: 6)

                Button {
                    vm.fillAllMatched()
                } label: {
                    Label("Fill Matched (⌘K)", systemImage: "wand.and.rays")
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .disabled(vm.currentSession == nil || vm.isBusy)

                Button {
                    vm.showAdvisoryNoteSheet = true
                } label: {
                    Label("Suggest from Note…", systemImage: "sparkles")
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .disabled(vm.currentSession == nil || vm.isBusy)

                Button {
                    vm.showProfileBuilderSheet = true
                } label: {
                    Label("Remember Form…", systemImage: "bookmark")
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .disabled(vm.currentSession == nil)

                SettingsLink {
                    Image(systemName: "gearshape")
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .help("Signer Settings & Diagnostics (⌘,)")
            }

            HStack(spacing: 10) {
                Picker("Tool", selection: $vm.activeTool) {
                    Text("Select").tag(CanvasTool.select)
                    Text("Text").tag(CanvasTool.addText)
                    Text("Check").tag(CanvasTool.addCheck)
                    Text("Sig Box").tag(CanvasTool.drawSignature)
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .controlSize(.small)
                .frame(width: 240)
                .disabled(vm.currentSession == nil || (vm.currentSession?.isBlocked ?? false))

                HStack(spacing: 3) {
                    Button { vm.adjustZoom(delta: -0.15) } label: {
                        Image(systemName: "minus.magnifyingglass")
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.small)

                    Button("\(Int((vm.zoomScale * 100).rounded()))%") {
                        vm.resetZoom()
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                    .font(.caption.monospacedDigit())

                    Button { vm.adjustZoom(delta: 0.15) } label: {
                        Image(systemName: "plus.magnifyingglass")
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                }

                if vm.isBusy, let msg = vm.busyMessage {
                    HStack(spacing: 6) {
                        ProgressView().controlSize(.small)
                        Text(msg)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                    }
                }

                Spacer(minLength: 6)

                Button {
                    Task { await vm.saveFilledCopy() }
                } label: {
                    Label("Save Filled Copy", systemImage: "square.and.arrow.down")
                }
                .buttonStyle(.bordered)
                .controlSize(.regular)
                .disabled(vm.currentSession == nil || vm.isBusy)

                Button {
                    Task { await vm.beginSignReview() }
                } label: {
                    Label("Review & Sign…", systemImage: "signature")
                        .fontWeight(.semibold)
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.regular)
                .keyboardShortcut(.return, modifiers: .command)
                .disabled(
                    vm.currentSession == nil ||
                    (vm.currentSession?.isBlocked ?? true) ||
                    !(vm.currentSession?.hasSignTarget ?? false) ||
                    vm.isBusy
                )
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(.ultraThinMaterial)
    }

    private var cardStatusPill: some View {
        let (title, color, icon): (String, Color, String) = {
            switch tokenMonitor.status.readiness {
            case .ready:
                let cn = tokenMonitor.status.preferredCert?.commonName ?? "CAC Ready"
                return (cn, .green, "checkmark.shield.fill")
            case .expiringSoon(let d):
                return ("CAC Expiring (\(d)d)", .orange, "exclamationmark.shield.fill")
            case .noCard:
                return ("No CAC Card", .secondary, "creditcard")
            case .untrustedRoots:
                return ("DoD Roots Untrusted", .red, "xmark.shield.fill")
            case .error:
                return ("Card Error", .red, "exclamationmark.triangle.fill")
            }
        }()

        return Button {
            TokenMonitor.shared.refreshNow()
            showCardPopover.toggle()
        } label: {
            HStack(spacing: 5) {
                Circle().fill(color).frame(width: 7, height: 7)
                Image(systemName: icon)
                    .font(.caption)
                    .foregroundStyle(color)
                Text(title)
                    .font(.caption.weight(.medium))
                    .lineLimit(1)
                    .frame(maxWidth: 165, alignment: .leading)
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 4)
            .background(Capsule().fill(color.opacity(0.12)))
            .overlay(Capsule().strokeBorder(color.opacity(0.35), lineWidth: 1))
        }
        .buttonStyle(.plain)
        .popover(isPresented: $showCardPopover, arrowEdge: .bottom) {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Text("Smart Card Certificates")
                        .font(.subheadline.weight(.semibold))
                    Spacer()
                    Button("Refresh") { TokenMonitor.shared.refreshNow() }
                        .buttonStyle(.bordered)
                        .controlSize(.mini)
                }
                Text(tokenMonitor.status.dodRootsTrusted ? "DoD Root CAs: Trusted" : "DoD Root CAs: Not Trusted")
                    .font(.caption2)
                    .foregroundStyle(tokenMonitor.status.dodRootsTrusted ? .green : .orange)
                Divider()
                if tokenMonitor.status.certificates.isEmpty {
                    Text("No PIV smart-card certificates currently visible.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else {
                    ForEach(tokenMonitor.status.certificates) { cert in
                        VStack(alignment: .leading, spacing: 2) {
                            HStack {
                                Text(cert.commonName)
                                    .font(.caption.weight(.semibold))
                                if cert.isPreferredSigningCert {
                                    Text("Signing")
                                        .font(.system(size: 9, weight: .bold))
                                        .foregroundStyle(.green)
                                }
                            }
                            Text("\(cert.issuer) • \(cert.keyUsage) • Exp \(cert.expiresDisplay)")
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                        }
                    }
                }
            }
            .padding(12)
            .frame(width: 340)
        }
    }

    private var personaMenu: some View {
        Menu {
            ForEach(vm.personasState.personas) { p in
                Button {
                    Task { await vm.selectActivePersona(id: p.id) }
                } label: {
                    if p.id == vm.personasState.active_id {
                        Label(p.name, systemImage: "checkmark")
                    } else {
                        Text(p.name)
                    }
                }
            }
        } label: {
            Label(vm.personasState.activePersona?.name ?? "Primary Signer", systemImage: "person.crop.circle")
                .font(.caption)
        }
        .controlSize(.small)
        .frame(maxWidth: 170)
    }

    @ViewBuilder
    private var patientControl: some View {
        if let pat = vm.patientContext {
            HStack(spacing: 5) {
                Image(systemName: "person.text.rectangle.fill")
                    .font(.caption2)
                    .foregroundStyle(.teal)
                Text("Patient: \(pat.displayName) (RAM Only)")
                    .font(.caption.weight(.semibold))
                    .lineLimit(1)
                Button {
                    vm.clearPatient()
                } label: {
                    Text("×")
                        .font(.caption.weight(.bold))
                        .padding(.horizontal, 4)
                }
                .buttonStyle(.plain)
                .help("Clear ephemeral patient demographics from RAM")
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 3.5)
            .background(Capsule().fill(Color.teal.opacity(0.15)))
            .overlay(Capsule().strokeBorder(Color.teal.opacity(0.45), lineWidth: 1))
        } else {
            Menu {
                Button("Paste Patient from Clipboard") {
                    vm.pastePatientFromClipboard()
                }
                Button("Enter / Paste Demographics Text…") {
                    vm.patientPasteDraft = ""
                    vm.showPatientPasteSheet = true
                }
            } label: {
                Label("Paste Patient", systemImage: "doc.on.clipboard")
                    .font(.caption)
            } primaryAction: {
                vm.pastePatientFromClipboard()
            }
            .controlSize(.small)
        }
    }
}

// MARK: - Left Queue Sidebar & Empty Drop State

private struct QueueSidebarView: View {
    @ObservedObject var vm: WorkspaceViewModel
    @Binding var isDropTargeted: Bool

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text("Queue (\(vm.sessions.count))")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                Spacer()
                Button {
                    CACSignFilePanel.presentOpenPanel(vm: vm)
                } label: {
                    Label("Add Files…", systemImage: "plus")
                        .font(.caption)
                }
                .buttonStyle(.bordered)
                .controlSize(.mini)
            }
            .padding(10)

            Divider()

            ScrollView {
                LazyVStack(spacing: 6) {
                    ForEach(vm.sessions) { item in
                        let selected = (vm.currentSession?.id == item.id)
                        HStack(spacing: 8) {
                            VStack(alignment: .leading, spacing: 3) {
                                Text(item.inspected.file)
                                    .font(.caption.weight(.semibold))
                                    .lineLimit(1)
                                HStack(spacing: 4) {
                                    queueBadge(for: item)
                                    if let prof = item.inspected.profile {
                                        Text(prof.id)
                                            .font(.system(size: 9))
                                            .foregroundStyle(.secondary)
                                            .lineLimit(1)
                                    }
                                }
                            }
                            Spacer(minLength: 2)
                            Button {
                                vm.removeSession(id: item.id)
                            } label: {
                                Image(systemName: "xmark")
                                    .font(.system(size: 9, weight: .bold))
                                    .foregroundStyle(.secondary)
                            }
                            .buttonStyle(.plain)
                        }
                        .padding(8)
                        .background(
                            RoundedRectangle(cornerRadius: 7)
                                .fill(selected ? Color.accentColor.opacity(0.16) : Color.primary.opacity(0.03))
                        )
                        .overlay(
                            RoundedRectangle(cornerRadius: 7)
                                .strokeBorder(selected ? Color.accentColor.opacity(0.55) : Color.clear, lineWidth: 1)
                        )
                        .contentShape(Rectangle())
                        .onTapGesture {
                            vm.selectSession(id: item.id)
                        }
                    }
                }
                .padding(8)
            }

            Divider()

            VStack(spacing: 6) {
                if vm.openscEnabled {
                    SecureField("OpenSC Batch PIN", text: $vm.openscPIN)
                        .textFieldStyle(.roundedBorder)
                        .controlSize(.small)
                }
                let readyN = vm.batchReadyCount
                Button {
                    Task { await vm.batchSignReady() }
                } label: {
                    Label("Batch Sign Ready (\(readyN))", systemImage: "checkmark.seal.fill")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.small)
                .disabled(readyN == 0 || vm.isBusy)
                .help("Sequentially signs queue items that have no unacknowledged warnings; stops immediately on cancel or PIN error.")
            }
            .padding(10)
        }
        .background(Color(nsColor: .windowBackgroundColor))
    }

    private func queueBadge(for item: DocumentSession) -> some View {
        let (label, color): (String, Color) = {
            if item.signedOutputURL != nil { return ("Signed", .green) }
            if item.isBlocked { return ("Blocked", .red) }
            if !item.inspected.warnings.isEmpty || !item.advisoryFields.isEmpty { return ("Review", .orange) }
            if item.hasSignTarget { return ("Ready", .blue) }
            return ("Needs Sig", .secondary)
        }()
        return Text(label)
            .font(.system(size: 9, weight: .bold))
            .foregroundStyle(color)
            .padding(.horizontal, 5)
            .padding(.vertical, 1.5)
            .background(Capsule().fill(color.opacity(0.14)))
    }
}

private struct EmptyDropWorkspaceView: View {
    @ObservedObject var vm: WorkspaceViewModel
    @Binding var isDropTargeted: Bool

    var body: some View {
        VStack(spacing: 18) {
            Image(systemName: "doc.badge.gearshape")
                .font(.system(size: 52, weight: .light))
                .foregroundStyle(isDropTargeted ? Color.accentColor : Color.secondary)

            VStack(spacing: 6) {
                Text("Drop PDF or Word (.docx) Form Here")
                    .font(.title2.weight(.semibold))
                Text("Strict incremental CAC signing • Preserves earlier digital signatures • Ephemeral RAM patient fill")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
            }

            HStack(spacing: 12) {
                Button {
                    CACSignFilePanel.presentOpenPanel(vm: vm)
                } label: {
                    Label("⌘O Open Form…", systemImage: "folder")
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)

                Button {
                    vm.pastePatientFromClipboard()
                } label: {
                    Label("⌘V Paste Patient", systemImage: "doc.on.clipboard")
                }
                .buttonStyle(.bordered)
                .controlSize(.large)

                SettingsLink {
                    Label("⌘, Signer Settings", systemImage: "person.text.rectangle")
                }
                .buttonStyle(.bordered)
                .controlSize(.large)
            }
        }
        .padding(44)
        .frame(maxWidth: 640, maxHeight: 400)
        .background(
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .fill(isDropTargeted ? Color.accentColor.opacity(0.08) : Color(nsColor: .controlBackgroundColor))
                .overlay(
                    RoundedRectangle(cornerRadius: 16, style: .continuous)
                        .strokeBorder(
                            isDropTargeted ? Color.accentColor : Color.secondary.opacity(0.32),
                            style: StrokeStyle(lineWidth: isDropTargeted ? 2.5 : 1.5, dash: [8, 5])
                        )
                )
        )
        .padding(32)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(nsColor: .underPageBackgroundColor))
    }
}

private struct CenterDocumentPane: View {
    @ObservedObject var vm: WorkspaceViewModel
    let session: DocumentSession

    var body: some View {
        VStack(spacing: 0) {
            if let err = vm.bannerError {
                HStack(spacing: 8) {
                    Image(systemName: "exclamationmark.octagon.fill")
                    Text(err).font(.caption.weight(.medium)).lineLimit(2)
                    Spacer()
                    Button { vm.bannerError = nil } label: { Image(systemName: "xmark") }.buttonStyle(.plain)
                }
                .foregroundStyle(.white)
                .padding(.horizontal, 12)
                .padding(.vertical, 6)
                .background(Color.red.opacity(0.9))
            } else if let info = vm.bannerInfo {
                HStack(spacing: 8) {
                    Image(systemName: "info.circle.fill")
                    Text(info).font(.caption).lineLimit(2)
                    Spacer()
                    Button { vm.bannerInfo = nil } label: { Image(systemName: "xmark") }.buttonStyle(.plain)
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 6)
                .background(Color.accentColor.opacity(0.12))
            }

            if let blocked = session.inspected.blocked {
                HStack(spacing: 8) {
                    Image(systemName: "hand.raised.fill")
                        .foregroundStyle(.red)
                    Text("Signing Refused: \(blocked)")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.red)
                    Spacer()
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .background(Color.red.opacity(0.12))
            }

            if let conv = session.inspected.conversion_note, !conv.isEmpty {
                HStack(spacing: 6) {
                    Image(systemName: "arrow.triangle.2.circlepath.doc.on.clipboard")
                        .font(.caption)
                    Text(conv).font(.caption)
                    Spacer()
                }
                .foregroundStyle(.secondary)
                .padding(.horizontal, 12)
                .padding(.vertical, 5)
                .background(Color.secondary.opacity(0.08))
            }

            PostSignActionBar(session: session)

            PDFPageCanvasView(vm: vm, session: session)
        }
    }
}

// MARK: - Right Field & Signature Inspector

private struct RightFieldInspectorView: View {
    @ObservedObject var vm: WorkspaceViewModel
    let session: DocumentSession

    private var displayedFields: [FormFieldModel] {
        let base = session.interactiveFields
        guard vm.inspectorBlanksOnly else { return base }
        return base.filter { f in
            f.isInteractiveInput && session.currentValue(for: f).isBlank(forKind: f.kind)
        }
    }

    var body: some View {
        VStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 8) {
                Text("Signature Target")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)

                HStack(spacing: 6) {
                    Picker("Target", selection: Binding(
                        get: { session.selectedSignField ?? "__placed__" },
                        set: { choice in
                            if choice == "__placed__" {
                                vm.selectSignatureField(nil)
                            } else {
                                vm.selectSignatureField(choice)
                            }
                        }
                    )) {
                        ForEach(session.signatureFields.filter { $0.signature == nil }) { sf in
                            Text(sf.displayLabel).tag(sf.name)
                        }
                        if let p = session.placedSignature {
                            Text("Placed Box (Page \(p.page + 1))").tag("__placed__")
                        } else {
                            Text("No Target Selected").tag("__placed__")
                        }
                    }
                    .labelsHidden()
                    .controlSize(.small)

                    Button {
                        vm.activeTool = .drawSignature
                    } label: {
                        Image(systemName: "rectangle.dashed")
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                    .help("Draw custom signature placement box on page")
                    .disabled(session.isBlocked || session.signedOutputURL != nil)
                }

                TextField("Signing Reason (optional)", text: $vm.signReason)
                    .textFieldStyle(.roundedBorder)
                    .controlSize(.small)
                    .font(.caption)

                let totalNotices = session.inspected.warnings.count + session.inspected.notes.count + session.inspected.signed_before.count
                if totalNotices > 0 {
                    Button {
                        vm.showWarningsSheet = true
                    } label: {
                        HStack(spacing: 6) {
                            Image(systemName: session.inspected.warnings.isEmpty ? "info.circle.fill" : "exclamationmark.triangle.fill")
                                .foregroundStyle(session.inspected.warnings.isEmpty ? .blue : .orange)
                            Text("\(session.inspected.warnings.count) warning(s) • \(session.inspected.notes.count + session.inspected.signed_before.count) note(s)")
                                .font(.caption.weight(.semibold))
                            Spacer()
                            Image(systemName: "chevron.right")
                                .font(.caption2)
                        }
                        .padding(7)
                        .background(RoundedRectangle(cornerRadius: 6).fill((session.inspected.warnings.isEmpty ? Color.blue : Color.orange).opacity(0.12)))
                    }
                    .buttonStyle(.plain)
                }

                Picker("Filter", selection: $vm.inspectorBlanksOnly) {
                    Text("All (\(session.interactiveFields.count))").tag(false)
                    Text("Blanks Only (\(session.blankInteractiveCount))").tag(true)
                }
                .pickerStyle(.segmented)
                .controlSize(.small)
            }
            .padding(10)

            Divider()

            ScrollView {
                LazyVStack(spacing: 8) {
                    ForEach(displayedFields) { field in
                        InspectorFieldRow(vm: vm, session: session, field: field)
                    }
                }
                .padding(10)
            }
        }
        .background(Color(nsColor: .windowBackgroundColor))
    }
}

private struct InspectorFieldRow: View {
    @ObservedObject var vm: WorkspaceViewModel
    let session: DocumentSession
    let field: FormFieldModel

    private var isSelected: Bool { vm.selectedFieldName == field.name }
    private var isAdvisory: Bool { session.advisoryFields.contains(field.name) }
    private var curVal: FormValue { session.currentValue(for: field) }
    private var interactive: Bool { field.isInteractiveInput && !session.isBlocked && session.signedOutputURL == nil }

    var body: some View {
        let party = PatientClipboard.classifyParty(for: field)
        let suggestions = interactive && field.kind != "checkbox"
            ? PatientClipboard.fieldSuggestions(
                for: field,
                personaFields: vm.activePersonaFields,
                autocomplete: vm.combinedAutocomplete,
                patient: vm.patientContext
            )
            : []

        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 4) {
                Text(field.displayLabel)
                    .font(.caption.weight(.semibold))
                    .lineLimit(1)
                if party != .other {
                    Text(party.rawValue.capitalized)
                        .font(.system(size: 8.5, weight: .bold))
                        .foregroundStyle(party == .patient ? .teal : .blue)
                        .padding(.horizontal, 4)
                        .padding(.vertical, 1)
                        .background(Capsule().fill((party == .patient ? Color.teal : Color.blue).opacity(0.14)))
                }
                if isAdvisory {
                    Button {
                        vm.confirmAdvisoryField(field.name)
                    } label: {
                        Text("Advisory — Verify")
                            .font(.system(size: 8.5, weight: .bold))
                            .foregroundStyle(.white)
                            .padding(.horizontal, 5)
                            .padding(.vertical, 1)
                            .background(Capsule().fill(Color.orange))
                    }
                    .buttonStyle(.plain)
                }
                Spacer(minLength: 2)
                if interactive && field.kind == "text" {
                    Button("N/A") {
                        vm.selectedFieldName = field.name
                        vm.setFieldValue(field.name, value: .text("N/A"))
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.mini)
                    .font(.system(size: 9, weight: .semibold))
                }
            }

            if field.kind == "checkbox" {
                Toggle("Checked", isOn: Binding(
                    get: { curVal.boolValue },
                    set: { vm.setFieldValue(field.name, value: .bool($0)) }
                ))
                .controlSize(.small)
                .font(.caption)
                .disabled(!interactive)
            } else if field.kind == "radio", let opts = field.options, !opts.isEmpty {
                Picker("", selection: Binding(
                    get: { curVal.stringValue },
                    set: { vm.setFieldValue(field.name, value: .text($0)) }
                )) {
                    Text("(None)").tag("")
                    ForEach(opts, id: \.self) { opt in
                        Text(opt).tag(opt)
                    }
                }
                .labelsHidden()
                .controlSize(.small)
                .disabled(!interactive)
            } else if field.kind == "choice" {
                let choices: [(val: String, label: String)] = {
                    if let co = field.choice_options, !co.isEmpty {
                        return co.map { ($0.value, $0.label) }
                    }
                    return (field.options ?? []).map { ($0, $0) }
                }()
                Picker("", selection: Binding(
                    get: { curVal.stringValue },
                    set: { vm.setFieldValue(field.name, value: .text($0)) }
                )) {
                    Text("(Blank)").tag("")
                    ForEach(choices, id: \.val) { item in
                        Text(item.label).tag(item.val)
                    }
                }
                .labelsHidden()
                .controlSize(.small)
                .disabled(!interactive)
            } else {
                TextField(
                    field.readonly || field.locked ? "Locked" : "Enter value…",
                    text: Binding(
                        get: { curVal.stringValue },
                        set: { vm.setFieldValue(field.name, value: .text($0)) }
                    ),
                    axis: field.multiline ? .vertical : .horizontal
                )
                .textFieldStyle(.roundedBorder)
                .controlSize(.small)
                .font(.caption)
                .disabled(!interactive)
            }

            if !suggestions.isEmpty {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 4) {
                        ForEach(suggestions.prefix(5), id: \.self) { chip in
                            Button {
                                vm.selectedFieldName = field.name
                                vm.setFieldValue(field.name, value: .text(chip))
                            } label: {
                                Text(chip)
                                    .font(.system(size: 10, weight: .medium))
                                    .lineLimit(1)
                                    .padding(.horizontal, 6)
                                    .padding(.vertical, 2)
                                    .background(Capsule().fill(Color.accentColor.opacity(0.12)))
                            }
                            .buttonStyle(.plain)
                        }
                    }
                }
            }
        }
        .padding(8)
        .background(
            RoundedRectangle(cornerRadius: 7)
                .fill(isAdvisory ? Color.orange.opacity(0.09) : (isSelected ? Color.accentColor.opacity(0.09) : Color(nsColor: .controlBackgroundColor)))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 7)
                .strokeBorder(isAdvisory ? Color.orange.opacity(0.6) : (isSelected ? Color.accentColor.opacity(0.6) : Color.secondary.opacity(0.16)), lineWidth: 1)
        )
        .onTapGesture {
            vm.selectedFieldName = field.name
        }
    }
}

// MARK: - Pre-Sign Review & Confirm Sheet

private struct PreSignReviewSheet: View {
    @ObservedObject var vm: WorkspaceViewModel
    let prep: PrepareResult

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Label("Review Staged PDF Before CAC Signature", systemImage: "checkmark.shield.fill")
                        .font(.title3.weight(.semibold))
                    Text("What you see below is the exact private snapshot that will be cryptographically hashed and signed.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
            }
            .padding(14)
            .background(.ultraThinMaterial)

            Divider()

            HStack(spacing: 0) {
                stagedPreviewPane
                    .frame(minWidth: 420, maxWidth: .infinity)

                Divider()

                stagedAuditSidebar
                    .frame(width: 380)
            }

            Divider()

            HStack {
                Button("Cancel") {
                    Task { await vm.cancelSignReview() }
                }
                .keyboardShortcut(.cancelAction)
                .disabled(vm.isBusy)

                Spacer()

                if vm.isBusy {
                    ProgressView().controlSize(.small)
                    Text(vm.busyMessage ?? "Signing…")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }

                Button {
                    Task { await vm.confirmAndCommitSign() }
                } label: {
                    Label("Sign with CAC", systemImage: "signature")
                        .fontWeight(.semibold)
                }
                .buttonStyle(.borderedProminent)
                .disabled(((prep.needs_ack ?? false) && !vm.ackChecked) || vm.isBusy)
                .keyboardShortcut(.defaultAction)
            }
            .padding(14)
            .background(.ultraThinMaterial)
        }
        .frame(minWidth: 860, minHeight: 620)
    }

    @ViewBuilder
    private var stagedPreviewPane: some View {
        if let stagePath = prep.stage_path {
            let stageURL = URL(fileURLWithPath: stagePath)
            let pageCount = max(1, prep.pages ?? CGPDFDocumentCache.shared.pageCount(for: stageURL))
            let docPages = vm.currentSession?.inspected.pages ?? []
            ScrollView {
                LazyVStack(spacing: 16) {
                    ForEach(0..<pageCount, id: \.self) { idx in
                        let pm = docPages.indices.contains(idx) ? docPages[idx] : PageSizeModel(w: 612, h: 792)
                        let pSize = CGSize(width: pm.w * 0.72, height: pm.h * 0.72)
                        ZStack(alignment: .topLeading) {
                            CGPDFPageView(pdfURL: stageURL, pageIndex: idx)
                                .frame(width: pSize.width, height: pSize.height)
                            if let whereBox = prep.`where`, whereBox.page == idx {
                                let r = DisplayBoxGeometry.viewRect(for: whereBox.box, in: pSize)
                                RoundedRectangle(cornerRadius: 3)
                                    .fill(Color.indigo.opacity(0.18))
                                    .overlay(
                                        RoundedRectangle(cornerRadius: 3)
                                            .strokeBorder(Color.indigo, style: StrokeStyle(lineWidth: 2, dash: [5, 3]))
                                    )
                                    .frame(width: r.width, height: r.height)
                                    .offset(x: r.minX, y: r.minY)
                            }
                        }
                        .background(Color.white)
                        .shadow(color: .black.opacity(0.14), radius: 5, y: 2)
                    }
                }
                .padding(18)
            }
            .background(Color(nsColor: .underPageBackgroundColor))
        } else {
            Text("Staged snapshot path unavailable.")
                .foregroundStyle(.secondary)
        }
    }

    private var stagedAuditSidebar: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                if let prior = prep.signed_before, !prior.isEmpty {
                    VStack(alignment: .leading, spacing: 4) {
                        Label("Previously Signed Input Bytes", systemImage: "clock.badge.exclamationmark")
                            .font(.caption.weight(.bold))
                            .foregroundStyle(.orange)
                        ForEach(prior) { p in
                            Text("• \(p.when): \(p.name)")
                                .font(.caption2)
                        }
                    }
                    .padding(8)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(RoundedRectangle(cornerRadius: 7).fill(Color.orange.opacity(0.12)))
                }

                if let warnings = prep.warnings, !warnings.isEmpty {
                    VStack(alignment: .leading, spacing: 4) {
                        Label("Viewer Warnings", systemImage: "exclamationmark.triangle.fill")
                            .font(.caption.weight(.bold))
                            .foregroundStyle(.orange)
                        ForEach(warnings, id: \.self) { w in
                            Text("• \(w)").font(.caption2)
                        }
                    }
                    .padding(8)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(RoundedRectangle(cornerRadius: 7).fill(Color.orange.opacity(0.12)))
                }

                VStack(alignment: .leading, spacing: 6) {
                    Text("Field Changes (\(prep.changes?.count ?? 0))")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                    if let changes = prep.changes, !changes.isEmpty {
                        ForEach(changes) { ch in
                            let flagged = !ch.flags.isEmpty
                            VStack(alignment: .leading, spacing: 3) {
                                HStack {
                                    Text(ch.name)
                                        .font(.caption.weight(.semibold))
                                    Spacer()
                                    ForEach(ch.flags, id: \.self) { fl in
                                        Text(fl)
                                            .font(.system(size: 9, weight: .bold))
                                            .foregroundStyle(.white)
                                            .padding(.horizontal, 5)
                                            .padding(.vertical, 1)
                                            .background(Capsule().fill(Color.orange))
                                    }
                                }
                                Text("\(ch.from) → \(ch.to)")
                                    .font(.caption2.monospaced())
                                    .foregroundStyle(.secondary)
                            }
                            .padding(7)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .background(RoundedRectangle(cornerRadius: 6).fill(flagged ? Color.orange.opacity(0.15) : Color(nsColor: .controlBackgroundColor)))
                            .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(flagged ? Color.orange : Color.clear, lineWidth: 1))
                        }
                    } else {
                        Text("No form values modified (signing existing values).")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }

                if prep.needs_ack == true {
                    Toggle("I meant to make these flagged changes / reviewed the warnings", isOn: $vm.ackChecked)
                        .font(.caption.weight(.semibold))
                        .padding(8)
                        .background(RoundedRectangle(cornerRadius: 7).fill(Color.orange.opacity(0.12)))
                }

                if vm.openscEnabled {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("CAC PIN (OpenSC PKCS#11 Session)")
                            .font(.caption.weight(.semibold))
                        SecureField("Enter CAC PIN", text: $vm.openscPIN)
                            .textFieldStyle(.roundedBorder)
                    }
                }
            }
            .padding(14)
        }
    }
}

// MARK: - Auxiliary Modal Sheets

private struct AdvisoryClinicalNoteSheet: View {
    @ObservedObject var vm: WorkspaceViewModel
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Label("Suggest Blank Fields from Clinical Note", systemImage: "sparkles")
                .font(.headline)
            Text(AdvisoryFillController.isFoundationModelsAvailable
                 ? "Apple Foundation Models (On-Device): Active. Suggestions run strictly locally and highlight in amber for clinician verification."
                 : "Apple Intelligence is currently unavailable on this Mac. Enable Apple Intelligence (macOS 26+) to use note-to-field extraction.")
                .font(.caption)
                .foregroundStyle(.secondary)
            TextEditor(text: $vm.advisoryNoteDraft)
                .font(.system(.body, design: .monospaced))
                .frame(minHeight: 180)
                .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(Color.secondary.opacity(0.25)))
            HStack {
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
                Spacer()
                Button("Populate Advisory Suggestions") {
                    Task { await vm.runAdvisoryNoteFill(noteText: vm.advisoryNoteDraft) }
                }
                .buttonStyle(.borderedProminent)
                .disabled(vm.advisoryNoteDraft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || vm.isBusy)
            }
        }
        .padding(18)
        .frame(width: 540, height: 340)
    }
}

private struct ManualPatientPasteSheet: View {
    @ObservedObject var vm: WorkspaceViewModel
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Label("Ephemeral Patient Demographics (RAM Only)", systemImage: "person.text.rectangle")
                .font(.headline)
            Text("Paste an EHR patient banner or demographics block. Stored strictly in volatile RAM and never saved to disk.")
                .font(.caption)
                .foregroundStyle(.secondary)
            TextEditor(text: $vm.patientPasteDraft)
                .font(.system(.body, design: .monospaced))
                .frame(minHeight: 150)
                .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(Color.secondary.opacity(0.25)))
            HStack {
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
                Spacer()
                Button("Apply Patient in RAM") {
                    vm.applyPatientText(vm.patientPasteDraft)
                    dismiss()
                }
                .buttonStyle(.borderedProminent)
                .disabled(vm.patientPasteDraft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }
        .padding(18)
        .frame(width: 500, height: 310)
    }
}

private struct QuickSignConfirmSheet: View {
    @ObservedObject var vm: WorkspaceViewModel
    let plan: QuickSignResult

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Label("Confirm Quick Sign Plan", systemImage: "signature")
                .font(.headline)
            List(plan.items) { item in
                VStack(alignment: .leading, spacing: 3) {
                    HStack {
                        Image(systemName: item.can_sign ? "checkmark.circle.fill" : "xmark.octagon.fill")
                            .foregroundStyle(item.can_sign ? .green : .red)
                        Text(item.file).font(.subheadline.weight(.semibold))
                    }
                    Text(item.summary).font(.caption).foregroundStyle(.secondary)
                    ForEach(item.warnings, id: \.self) { w in
                        Text("⚠ \(w)").font(.caption2).foregroundStyle(.orange)
                    }
                }
            }
            .frame(minHeight: 160)
            if vm.openscEnabled {
                SecureField("CAC PIN (OpenSC Session)", text: $vm.openscPIN)
                    .textFieldStyle(.roundedBorder)
            }
            HStack {
                Button("Cancel") { vm.cancelQuickSignService() }.keyboardShortcut(.cancelAction)
                Spacer()
                Button("Sign Ready Files with CAC") {
                    Task { await vm.confirmQuickSignService() }
                }
                .buttonStyle(.borderedProminent)
                .disabled(!plan.items.contains(where: \.can_sign) || vm.isBusy)
            }
        }
        .padding(18)
        .frame(width: 540, height: 360)
    }
}

private struct DocumentWarningsDetailSheet: View {
    let session: DocumentSession
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Label("Document Warnings & Inspection Notes", systemImage: "exclamationmark.triangle")
                    .font(.headline)
                Spacer()
                Button("Done") { dismiss() }.buttonStyle(.borderedProminent)
            }
            ScrollView {
                VStack(alignment: .leading, spacing: 10) {
                    ForEach(session.inspected.warnings, id: \.self) { w in
                        Text("⚠ \(w)").font(.callout).foregroundStyle(.orange)
                    }
                    ForEach(session.inspected.signed_before) { s in
                        Text("• Signed earlier (\(s.when)): \(s.name)").font(.callout)
                    }
                    ForEach(session.inspected.notes, id: \.self) { n in
                        Text("• \(n)").font(.callout).foregroundStyle(.secondary)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .padding(18)
        .frame(width: 500, height: 300)
    }
}
