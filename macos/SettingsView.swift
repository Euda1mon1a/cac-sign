import AppKit
import SwiftUI

public struct SettingsView: View {
    @ObservedObject public var vm: WorkspaceViewModel
    @ObservedObject private var tokenMonitor = TokenMonitor.shared

    public init(vm: WorkspaceViewModel) {
        self.vm = vm
    }

    public var body: some View {
        TabView {
            PersonasSettingsTab(vm: vm)
                .tabItem {
                    Label("Signer Personas", systemImage: "person.text.rectangle")
                }

            FormProfilesSettingsTab(vm: vm)
                .tabItem {
                    Label("Form Profiles", systemImage: "doc.badge.gearshape")
                }

            SigningHistorySettingsTab(vm: vm)
                .tabItem {
                    Label("Signing History", systemImage: "clock.badge.checkmark")
                }

            CardDiagnosticsSettingsTab(vm: vm, tokenMonitor: tokenMonitor)
                .tabItem {
                    Label("Card & Diagnostics", systemImage: "creditcard.and.123")
                }
        }
        .frame(minWidth: 680, minHeight: 480)
        .sheet(item: $vm.verifyResultSheet) { res in
            VerifySignatureResultSheet(result: res)
        }
    }
}

extension VerifyResult: Identifiable {
    public var id: String { "\(file)|\(signatures.count)|\(reader_problems.count)" }
}

// MARK: - Tab 1: Signer Personas

private struct PersonasSettingsTab: View {
    @ObservedObject var vm: WorkspaceViewModel
    @State private var draftPersonas: [SignerPersona] = []
    @State private var draftActiveID: String = "default"
    @State private var selectedPersonaID: String = "default"
    @State private var savedFlash: Bool = false

    var body: some View {
        HStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 8) {
                Text("Personas")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 12)
                    .padding(.top, 12)

                List(selection: $selectedPersonaID) {
                    ForEach(draftPersonas) { persona in
                        HStack {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(persona.name)
                                    .font(.subheadline.weight(.medium))
                                if let title = persona.fields["title"], !title.isEmpty {
                                    Text(title)
                                        .font(.caption2)
                                        .foregroundStyle(.secondary)
                                }
                            }
                            Spacer()
                            if persona.id == draftActiveID {
                                Text("Active")
                                    .font(.caption2.weight(.bold))
                                    .foregroundStyle(.green)
                                    .padding(.horizontal, 6)
                                    .padding(.vertical, 2)
                                    .background(Capsule().fill(Color.green.opacity(0.15)))
                            }
                        }
                        .tag(persona.id)
                    }
                }
                .listStyle(.sidebar)

                HStack(spacing: 6) {
                    Button {
                        addPersona()
                    } label: {
                        Label("New", systemImage: "plus")
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.small)

                    Button(role: .destructive) {
                        deleteSelectedPersona()
                    } label: {
                        Image(systemName: "trash")
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                    .disabled(draftPersonas.count <= 1)

                    Spacer()
                }
                .padding(10)
            }
            .frame(width: 220)

            Divider()

            VStack(spacing: 0) {
                if let pIdx = draftPersonas.firstIndex(where: { $0.id == selectedPersonaID }) {
                    Form {
                        Section("Persona Profile") {
                            HStack {
                                TextField("Persona Name", text: $draftPersonas[pIdx].name)
                                if draftPersonas[pIdx].id != draftActiveID {
                                    Button("Set Active") {
                                        draftActiveID = draftPersonas[pIdx].id
                                    }
                                    .buttonStyle(.bordered)
                                    .controlSize(.small)
                                }
                            }
                        }

                        Section("Signer Identity Fields (0600 synced to identity.json)") {
                            ForEach(SignerPersona.supportedKeys, id: \.key) { spec in
                                TextField(
                                    spec.label,
                                    text: Binding(
                                        get: { draftPersonas[pIdx].fields[spec.key] ?? "" },
                                        set: { draftPersonas[pIdx].fields[spec.key] = $0 }
                                    ),
                                    prompt: Text(spec.placeholder)
                                )
                            }
                        }
                    }
                    .formStyle(.grouped)
                } else {
                    Spacer()
                    Text("Select or create a signer persona.")
                        .foregroundStyle(.secondary)
                    Spacer()
                }

                Divider()

                HStack {
                    Text("Stored locally in Application Support (0600). Never written into form profiles.")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                    Spacer()
                    if savedFlash {
                        Label("Saved", systemImage: "checkmark.circle.fill")
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(.green)
                    }
                    Button("Save Personas") {
                        Task {
                            await vm.savePersonasState(activeID: draftActiveID, personas: draftPersonas)
                            savedFlash = true
                        }
                    }
                    .buttonStyle(.borderedProminent)
                }
                .padding(12)
            }
        }
        .onAppear(perform: syncFromVM)
        .onChange(of: vm.personasState) { _, _ in
            syncFromVM()
        }
    }

    private func syncFromVM() {
        if vm.personasState.personas.isEmpty {
            let fallback = SignerPersona(id: "default", name: "Primary Signer", fields: [:])
            draftPersonas = [fallback]
            draftActiveID = "default"
            selectedPersonaID = "default"
        } else {
            draftPersonas = vm.personasState.personas
            draftActiveID = vm.personasState.active_id
            selectedPersonaID = vm.personasState.active_id
        }
    }

    private func addPersona() {
        let newID = "persona-\(UUID().uuidString.prefix(8).lowercased())"
        let p = SignerPersona(id: newID, name: "Additional Role", fields: [:])
        draftPersonas.append(p)
        selectedPersonaID = newID
    }

    private func deleteSelectedPersona() {
        guard draftPersonas.count > 1,
              let idx = draftPersonas.firstIndex(where: { $0.id == selectedPersonaID }) else { return }
        let removedID = draftPersonas[idx].id
        draftPersonas.remove(at: idx)
        let nextID = draftPersonas.first?.id ?? "default"
        selectedPersonaID = nextID
        if draftActiveID == removedID {
            draftActiveID = nextID
        }
    }
}

// MARK: - Tab 2: Form Profiles

private struct FormProfilesSettingsTab: View {
    @ObservedObject var vm: WorkspaceViewModel
    @State private var selectedProfileID: String?

    var body: some View {
        HStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 8) {
                List(selection: $selectedProfileID) {
                    ForEach(vm.profiles) { prof in
                        HStack {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(prof.id)
                                    .font(.caption.monospaced().weight(.semibold))
                                Text(prof.description)
                                    .font(.caption2)
                                    .foregroundStyle(.secondary)
                                    .lineLimit(1)
                            }
                            Spacer()
                            let isBundled = (prof.source == "bundled")
                            Text(isBundled ? "Bundled" : "User")
                                .font(.system(size: 9, weight: .bold))
                                .foregroundStyle(isBundled ? Color.secondary : Color.blue)
                                .padding(.horizontal, 5)
                                .padding(.vertical, 2)
                                .background(Capsule().fill((isBundled ? Color.secondary : Color.blue).opacity(0.14)))
                        }
                        .tag(prof.id)
                    }
                }
                .listStyle(.sidebar)

                HStack {
                    Button {
                        revealProfilesFolder()
                    } label: {
                        Label("Reveal Folder", systemImage: "folder")
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                    Spacer()
                }
                .padding(10)
            }
            .frame(width: 240)

            Divider()

            if let id = selectedProfileID, let prof = vm.profiles.first(where: { $0.id == id }) {
                ScrollView {
                    VStack(alignment: .leading, spacing: 14) {
                        HStack {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(prof.id)
                                    .font(.title3.monospaced().weight(.semibold))
                                Text(prof.description)
                                    .font(.subheadline)
                                    .foregroundStyle(.secondary)
                            }
                            Spacer()
                            if prof.source != "bundled" {
                                Button(role: .destructive) {
                                    Task {
                                        await vm.deleteFormProfile(id: prof.id)
                                        selectedProfileID = vm.profiles.first?.id
                                    }
                                } label: {
                                    Label("Delete Profile", systemImage: "trash")
                                }
                                .buttonStyle(.bordered)
                            } else {
                                Text("Bundled (read-only)")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                        }

                        Divider()

                        Group {
                            detailRow("Sign Target", value: prof.sign_field ?? "(Manual placement)")
                            detailRow("Require Signed", value: prof.require_signed.isEmpty ? "None" : prof.require_signed.joined(separator: ", "))
                            detailRow("Reason", value: prof.reason ?? "—")
                            detailRow("Next Step", value: prof.next_step ?? "—")
                            detailRow("Match Fields", value: prof.match_fields.joined(separator: ", "))
                        }

                        if !prof.fill.isEmpty {
                            Divider()
                            Text("Template Fill Rules")
                                .font(.caption.weight(.semibold))
                                .foregroundStyle(.secondary)
                            ForEach(prof.fill.keys.sorted(), id: \.self) { key in
                                if let rule = prof.fill[key] {
                                    HStack {
                                        Text(key)
                                            .font(.caption.monospaced())
                                        Spacer()
                                        Text(rule.value)
                                            .font(.caption.monospaced().weight(.semibold))
                                            .foregroundStyle(.blue)
                                        if rule.if_empty {
                                            Text("if empty")
                                                .font(.caption2)
                                                .foregroundStyle(.secondary)
                                        }
                                    }
                                    .padding(.vertical, 2)
                                }
                            }
                        }
                    }
                    .padding(18)
                }
            } else {
                VStack {
                    Spacer()
                    Text("Select a profile to inspect matching & template rules.")
                        .foregroundStyle(.secondary)
                    Spacer()
                }
                .frame(maxWidth: .infinity)
            }
        }
        .onAppear {
            if selectedProfileID == nil {
                selectedProfileID = vm.profiles.first?.id
            }
        }
    }

    private func detailRow(_ title: String, value: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title)
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
            Text(value)
                .font(.callout)
                .textSelection(.enabled)
        }
    }

    private func revealProfilesFolder() {
        let base = vm.diagnostics?.app_support_path ?? (NSHomeDirectory() + "/Library/Application Support/cac-sign")
        let profilesDir = (base as NSString).appendingPathComponent("profiles")
        let fm = FileManager.default
        try? fm.createDirectory(atPath: base, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        try? fm.setAttributes([.posixPermissions: 0o700], ofItemAtPath: base)
        try? fm.createDirectory(atPath: profilesDir, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        try? fm.setAttributes([.posixPermissions: 0o700], ofItemAtPath: profilesDir)
        NSWorkspace.shared.selectFile(nil, inFileViewerRootedAtPath: profilesDir)
    }
}

// MARK: - Tab 3: Signing History (Ledger)

private struct SigningHistorySettingsTab: View {
    @ObservedObject var vm: WorkspaceViewModel
    @State private var searchText: String = ""

    private var filteredLedger: [LedgerRecord] {
        let q = searchText.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !q.isEmpty else { return vm.ledger }
        return vm.ledger.filter { rec in
            rec.output.lowercased().contains(q) ||
            rec.signer.lowercased().contains(q) ||
            rec.field.lowercased().contains(q) ||
            rec.when_display.lowercased().contains(q) ||
            rec.output_sha256.lowercased().contains(q)
        }
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 10) {
                Image(systemName: "magnifyingglass")
                    .foregroundStyle(.secondary)
                TextField("Filter signing history by filename, signer, field, or SHA-256…", text: $searchText)
                    .textFieldStyle(.plain)
                if !searchText.isEmpty {
                    Button { searchText = "" } label: {
                        Image(systemName: "xmark.circle.fill")
                            .foregroundStyle(.secondary)
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(10)
            .background(Color(nsColor: .controlBackgroundColor))

            Divider()

            if filteredLedger.isEmpty {
                Spacer()
                Text(vm.ledger.isEmpty ? "No CAC signatures recorded in ledger.jsonl yet." : "No matching ledger entries.")
                    .foregroundStyle(.secondary)
                Spacer()
            } else {
                List(filteredLedger) { rec in
                    let outURL = URL(fileURLWithPath: rec.output)
                    HStack(spacing: 12) {
                        VStack(alignment: .leading, spacing: 3) {
                            HStack(spacing: 6) {
                                Text(outURL.lastPathComponent)
                                    .font(.subheadline.weight(.semibold))
                                Text(rec.exists ? "On Disk" : "Missing")
                                    .font(.system(size: 9, weight: .bold))
                                    .foregroundStyle(rec.exists ? Color.green : Color.secondary)
                                    .padding(.horizontal, 5)
                                    .padding(.vertical, 1.5)
                                    .background(Capsule().fill((rec.exists ? Color.green : Color.secondary).opacity(0.14)))
                            }
                            Text("\(rec.when_display) • Signer: \(rec.signer) • Target: \(rec.field)")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                            Text("SHA-256: \(rec.output_sha256.prefix(16))…")
                                .font(.caption2.monospaced())
                                .foregroundStyle(.tertiary)
                        }
                        Spacer()
                        Button("Verify Signatures") {
                            Task { await vm.verifySignedFile(path: rec.output) }
                        }
                        .buttonStyle(.bordered)
                        .controlSize(.small)
                        .disabled(!rec.exists)

                        Button("Reveal in Finder") {
                            NSWorkspace.shared.activateFileViewerSelecting([outURL])
                        }
                        .buttonStyle(.bordered)
                        .controlSize(.small)
                        .disabled(!rec.exists)
                    }
                    .padding(.vertical, 4)
                }
            }
        }
    }
}

// MARK: - Tab 4: Card & Diagnostics

private struct CardDiagnosticsSettingsTab: View {
    @ObservedObject var vm: WorkspaceViewModel
    @ObservedObject var tokenMonitor: TokenMonitor

    var body: some View {
        Form {
            Section("Smart Card & DoD Trust Store") {
                HStack(spacing: 10) {
                    Image(systemName: statusIcon)
                        .font(.title3)
                        .foregroundStyle(statusColor)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(statusHeadline)
                            .font(.subheadline.weight(.semibold))
                        Text(tokenMonitor.status.dodRootsTrusted
                             ? "DoD Root CAs trusted in macOS Keychain (\(vm.diagnostics?.dod_ca_count ?? 0) CA certs detected)"
                             : "DoD Root CAs not detected as trusted in macOS Keychain")
                            .font(.caption)
                            .foregroundStyle(tokenMonitor.status.dodRootsTrusted ? Color.secondary : Color.orange)
                    }
                    Spacer()
                    Button("Refresh Diagnostics") {
                        Task { await vm.refreshDiagnostics() }
                    }
                    .buttonStyle(.bordered)
                }

                if tokenMonitor.status.certificates.isEmpty {
                    Text("Insert a DoD CAC reader and smart card to inspect PIV certificates.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else {
                    ForEach(tokenMonitor.status.certificates) { cert in
                        VStack(alignment: .leading, spacing: 3) {
                            HStack {
                                Text(cert.commonName)
                                    .font(.caption.weight(.semibold))
                                if cert.isPreferredSigningCert {
                                    Text("Preferred Signing Cert")
                                        .font(.system(size: 9, weight: .bold))
                                        .foregroundStyle(.green)
                                        .padding(.horizontal, 5)
                                        .padding(.vertical, 1.5)
                                        .background(Capsule().fill(Color.green.opacity(0.15)))
                                }
                                Spacer()
                                Text("Expires: \(cert.expiresDisplay)")
                                    .font(.caption2)
                                    .foregroundStyle((cert.daysRemaining ?? 999) < 30 ? Color.orange : Color.secondary)
                            }
                            Text("Issuer: \(cert.issuer) • KeyUsage: \(cert.keyUsage)")
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                        }
                        .padding(.vertical, 2)
                    }
                }
            }

            Section("PKCS#11 Batch Mode") {
                Toggle("Enable OpenSC Single-PIN Batch Mode", isOn: $vm.openscEnabled)
                Text("By default, macOS CryptoTokenKit prompts for your CAC PIN per document without CACSign ever seeing your PIN. Enable OpenSC mode only when signing multi-file batches with a single PIN session.")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                if let oscPath = vm.diagnostics?.opensc_path, !oscPath.isEmpty {
                    Text("OpenSC module: \(oscPath)")
                        .font(.caption2.monospaced())
                        .foregroundStyle(.secondary)
                } else {
                    Text("OpenSC module: Not installed (native macOS PIV driver active)")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }

            Section("Local Toolchain & Optional Capabilities") {
                LabeledContent("Python / pyHanko / pikepdf") {
                    if let d = vm.diagnostics {
                        Text("Python \(d.python_version) • pyHanko \(d.pyhanko_version) • pikepdf \(d.pikepdf_version)")
                            .font(.caption.monospaced())
                    } else {
                        Text("Loading…")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                LabeledContent("LibreOffice (.docx intake)") {
                    if let lo = vm.diagnostics?.libreoffice_path, !lo.isEmpty {
                        Text("Installed (\(lo))")
                            .font(.caption)
                            .foregroundStyle(.green)
                    } else {
                        Text("Not installed (PDF-only mode)")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                LabeledContent("On-Device Advisory AI") {
                    let fmAvail = AdvisoryFillController.isFoundationModelsAvailable || tokenMonitor.status.foundationModelsAvailable
                    Text(fmAvail ? "Apple Foundation Models Ready" : "Unavailable (macOS 26+ Apple Intelligence required)")
                        .font(.caption)
                        .foregroundStyle(fmAvail ? Color.green : Color.secondary)
                }
                if let supportPath = vm.diagnostics?.app_support_path, !supportPath.isEmpty {
                    LabeledContent("Application Support (0700)") {
                        Button("Reveal in Finder") {
                            NSWorkspace.shared.selectFile(nil, inFileViewerRootedAtPath: supportPath)
                        }
                        .buttonStyle(.bordered)
                        .controlSize(.small)
                    }
                }
            }
        }
        .formStyle(.grouped)
    }

    private var statusIcon: String {
        switch tokenMonitor.status.readiness {
        case .ready: return "checkmark.shield.fill"
        case .expiringSoon: return "exclamationmark.shield.fill"
        case .noCard: return "creditcard.trianglebadge.exclamationmark"
        case .untrustedRoots, .error: return "xmark.shield.fill"
        }
    }

    private var statusColor: Color {
        switch tokenMonitor.status.readiness {
        case .ready: return .green
        case .expiringSoon: return .orange
        case .noCard: return .secondary
        case .untrustedRoots, .error: return .red
        }
    }

    private var statusHeadline: String {
        switch tokenMonitor.status.readiness {
        case .ready:
            return "CAC Ready — \(tokenMonitor.status.preferredCert?.commonName ?? "Signing Certificate Available")"
        case .expiringSoon(let days):
            return "CAC Certificate Expiring in \(days) day\(days == 1 ? "" : "s")"
        case .noCard:
            return "No CAC Smart Card Detected"
        case .untrustedRoots:
            return "DoD Root CAs Untrusted in macOS Keychain"
        case .error(let msg):
            return "Card Status Error: \(msg)"
        }
    }
}

// MARK: - Shared Signature Verification Sheet

public struct VerifySignatureResultSheet: View {
    public let result: VerifyResult
    @Environment(\.dismiss) private var dismiss

    public init(result: VerifyResult) {
        self.result = result
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Label("Signature Verification: \(result.file)", systemImage: "checkmark.seal")
                    .font(.headline)
                Spacer()
                Button("Done") { dismiss() }
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.cancelAction)
            }

            Divider()

            if !result.reader_problems.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    Label("Reader Agreement Problems", systemImage: "xmark.octagon.fill")
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(.red)
                    ForEach(result.reader_problems, id: \.self) { prob in
                        Text("• \(prob)")
                            .font(.caption)
                    }
                }
                .padding(10)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: 8).fill(Color.red.opacity(0.12)))
            }

            if !result.warnings.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    Label("Viewer Warnings", systemImage: "exclamationmark.triangle.fill")
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(.orange)
                    ForEach(result.warnings, id: \.self) { warn in
                        Text("• \(warn)")
                            .font(.caption)
                    }
                }
                .padding(10)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: 8).fill(Color.orange.opacity(0.12)))
            }

            if result.signatures.isEmpty {
                Text("No digital signatures found in this document.")
                    .foregroundStyle(.secondary)
                    .padding(.vertical, 12)
            } else {
                List(result.signatures) { sig in
                    HStack(alignment: .top, spacing: 10) {
                        Image(systemName: sig.ok ? "checkmark.seal.fill" : "exclamationmark.triangle.fill")
                            .foregroundStyle(sig.ok ? .green : .red)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(sig.signer)
                                .font(.subheadline.weight(.semibold))
                            Text("Field: \(sig.field ?? "Signature") • Signed: \(sig.when)")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                            if !sig.detail.isEmpty {
                                Text(sig.detail)
                                    .font(.caption2)
                                    .foregroundStyle(sig.ok ? Color.secondary : Color.red)
                            }
                        }
                    }
                    .padding(.vertical, 3)
                }
                .frame(minHeight: 160)
            }
        }
        .padding(18)
        .frame(minWidth: 540, minHeight: 340)
    }
}
