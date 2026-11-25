import Foundation
import SwiftUI

public struct ProfileBuilderSheet: View {
    @ObservedObject public var vm: WorkspaceViewModel
    @Environment(\.dismiss) private var dismiss

    private struct FillDraftRow: Identifiable {
        let id: String // field name
        let label: String
        var enabled: Bool
        var templateValue: String
        var ifEmpty: Bool
        var looksPersonalWarning: Bool
    }

    @State private var profileID: String = ""
    @State private var profileDescription: String = ""
    @State private var selectedMatchFields: Set<String> = []
    @State private var selectedRequireSigned: Set<String> = []
    @State private var selectedSignField: String = ""
    @State private var reasonText: String = ""
    @State private var nextStepText: String = ""
    @State private var fillRows: [FillDraftRow] = []

    public init(vm: WorkspaceViewModel) {
        self.vm = vm
    }

    public var body: some View {
        VStack(spacing: 0) {
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Label("Remember This Form…", systemImage: "bookmark.fill")
                        .font(.title3.weight(.semibold))
                    Text("Save a reusable local profile using signer variables (${full_name}, ${today}) without storing patient PHI.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
            }
            .padding(16)
            .background(.ultraThinMaterial)

            Divider()

            Form {
                Section("Profile Identity") {
                    TextField("Profile ID (slug)", text: $profileID)
                        .font(.system(.body, design: .monospaced))
                    TextField("Description", text: $profileDescription)
                }

                if let doc = vm.currentSession {
                    Section("Signature & Gate Rules") {
                        Picker("Sign Target Field", selection: $selectedSignField) {
                            Text("None / Manual Placement").tag("")
                            ForEach(doc.signatureFields) { sf in
                                Text(sf.displayLabel).tag(sf.name)
                            }
                        }

                        let otherSigs = doc.signatureFields.filter { $0.name != selectedSignField }
                        if !otherSigs.isEmpty {
                            VStack(alignment: .leading, spacing: 4) {
                                Text("Require Already Signed Before Counter-Signing:")
                                    .font(.caption.weight(.semibold))
                                    .foregroundStyle(.secondary)
                                ForEach(otherSigs) { sf in
                                    Toggle(sf.displayLabel, isOn: Binding(
                                        get: { selectedRequireSigned.contains(sf.name) },
                                        set: { on in
                                            if on { selectedRequireSigned.insert(sf.name) }
                                            else { selectedRequireSigned.remove(sf.name) }
                                        }
                                    ))
                                    .controlSize(.small)
                                }
                            }
                        }

                        TextField("Signing Reason (optional)", text: $reasonText)
                        TextField("Post-Sign Next Step Hint (optional)", text: $nextStepText)
                    }

                    Section("Form Recognition Fields (\(selectedMatchFields.count) selected)") {
                        ScrollView {
                            LazyVStack(alignment: .leading, spacing: 4) {
                                ForEach(doc.inspected.fields.filter { $0.added != true }) { field in
                                    Toggle(isOn: Binding(
                                        get: { selectedMatchFields.contains(field.name) },
                                        set: { on in
                                            if on { selectedMatchFields.insert(field.name) }
                                            else { selectedMatchFields.remove(field.name) }
                                        }
                                    )) {
                                        HStack {
                                            Text(field.name)
                                                .font(.caption.monospaced())
                                            if field.displayLabel != field.name {
                                                Text("(\(field.displayLabel))")
                                                    .font(.caption2)
                                                    .foregroundStyle(.secondary)
                                            }
                                        }
                                    }
                                    .controlSize(.small)
                                }
                            }
                        }
                        .frame(maxHeight: 130)
                    }

                    Section("Auto-Fill Rules & Template Expressions") {
                        if fillRows.isEmpty {
                            Text("Fill signer fields on the form first to generate ${full_name}, ${title}, ${duty_phone}, ${npi}, ${fax}, or ${today} templates.")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        } else {
                            ForEach($fillRows) { $row in
                                VStack(alignment: .leading, spacing: 4) {
                                    HStack(spacing: 8) {
                                        Toggle("", isOn: $row.enabled)
                                            .labelsHidden()
                                        Text(row.label)
                                            .font(.caption.weight(.semibold))
                                            .frame(width: 150, alignment: .leading)
                                            .lineLimit(1)
                                        TextField("Value or ${template}", text: $row.templateValue)
                                            .textFieldStyle(.roundedBorder)
                                            .font(.caption.monospaced())
                                            .disabled(!row.enabled)
                                        Toggle("Only if empty", isOn: $row.ifEmpty)
                                            .controlSize(.small)
                                            .font(.caption2)
                                            .disabled(!row.enabled)
                                    }
                                    if row.enabled && row.looksPersonalWarning && !row.templateValue.contains("${") {
                                        Text("Literal text detected — consider replacing with a ${persona_key} variable so no personal/patient text is stored.")
                                            .font(.caption2)
                                            .foregroundStyle(.orange)
                                            .padding(.leading, 26)
                                    }
                                }
                            }
                        }
                    }
                }
            }
            .formStyle(.grouped)

            Divider()

            HStack {
                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Spacer()
                Button("Save Form Profile") {
                    Task { await saveProfile() }
                }
                .buttonStyle(.borderedProminent)
                .disabled(profileID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || selectedMatchFields.isEmpty)
                .keyboardShortcut(.defaultAction)
            }
            .padding(14)
            .background(.ultraThinMaterial)
        }
        .frame(minWidth: 640, minHeight: 580)
        .onAppear(perform: seedFromCurrentDocument)
    }

    private func seedFromCurrentDocument() {
        guard let doc = vm.currentSession else { return }

        let stem = URL(fileURLWithPath: doc.inspected.file)
            .deletingPathExtension()
            .lastPathComponent
            .replacingOccurrences(of: "_filled", with: "")
            .replacingOccurrences(of: "_signed", with: "")

        let slug = stem
            .lowercased()
            .components(separatedBy: CharacterSet.alphanumerics.inverted)
            .filter { !$0.isEmpty }
            .joined(separator: "-")

        profileID = doc.inspected.profile?.id ?? (slug.isEmpty ? "custom-form" : slug)
        profileDescription = doc.inspected.profile?.description ?? stem.replacingOccurrences(of: "_", with: " ").replacingOccurrences(of: "-", with: " ")
        selectedSignField = doc.selectedSignField ?? doc.inspected.sign_target ?? ""
        reasonText = vm.signReason.isEmpty ? (doc.inspected.reason ?? "") : vm.signReason
        nextStepText = doc.inspected.next_step ?? ""

        let nativeFields = doc.inspected.fields.filter { $0.added != true }
        if let profFields = doc.inspected.profile_fields, !profFields.isEmpty {
            selectedMatchFields = Set(profFields)
        } else {
            selectedMatchFields = Set(nativeFields.prefix(6).map(\.name))
        }

        let alreadySignedSigs = doc.signatureFields
            .filter { $0.signature != nil && $0.name != selectedSignField }
            .map(\.name)
        selectedRequireSigned = Set(alreadySignedSigs)

        let persona = vm.activePersonaFields
        let todayStrings = Self.todayFormattedCandidates()

        var drafted: [FillDraftRow] = []
        for field in nativeFields where field.kind != "signature" {
            let curVal = doc.currentValue(for: field)
            guard !curVal.isBlank(forKind: field.kind) else { continue }

            let party = PatientClipboard.classifyParty(for: field)
            if party == .patient || party == .sponsor || doc.patientApplied[field.name] != nil {
                continue
            }

            let rawText = field.kind == "checkbox" ? "x" : curVal.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !rawText.isEmpty else { continue }

            let converted = Self.templatize(
                rawValue: rawText,
                field: field,
                personaFields: persona,
                todayStrings: todayStrings
            )
            let isTemplate = converted.contains("${") || field.kind == "checkbox"
            drafted.append(FillDraftRow(
                id: field.name,
                label: field.displayLabel,
                enabled: isTemplate,
                templateValue: converted,
                ifEmpty: converted != "${today}" && field.kind != "checkbox",
                looksPersonalWarning: !isTemplate && rawText.count > 3
            ))
        }
        fillRows = drafted
    }

    private static func todayFormattedCandidates() -> Set<String> {
        let now = Date()
        let formats = ["dd MMM yyyy", "d MMM yyyy", "MM/dd/yyyy", "M/d/yyyy", "yyyy-MM-dd"]
        var out = Set<String>()
        let df = DateFormatter()
        df.locale = Locale(identifier: "en_US_POSIX")
        for fmt in formats {
            df.dateFormat = fmt
            let s = df.string(from: now)
            out.insert(s.lowercased())
        }
        return out
    }

    private static func templatize(
        rawValue: String,
        field: FormFieldModel,
        personaFields: [String: String],
        todayStrings: Set<String>
    ) -> String {
        if field.kind == "checkbox" {
            return "x"
        }
        let norm = rawValue.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()

        let orderedKeys = ["full_name", "title", "duty_phone", "npi", "fax", "dea", "license", "clinic", "unit", "email"]
        for key in orderedKeys {
            if let pVal = personaFields[key]?.trimmingCharacters(in: .whitespacesAndNewlines),
               !pVal.isEmpty,
               pVal.lowercased() == norm {
                return "${\(key)}"
            }
        }

        let labelLower = "\(field.name) \(field.label ?? "")".lowercased()
        let isBirthOrExp = labelLower.contains("birth") || labelLower.contains("dob") || labelLower.contains("expir")
        if !isBirthOrExp && todayStrings.contains(norm) {
            return "${today}"
        }
        return rawValue
    }

    private func saveProfile() async {
        let cleanID = profileID
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()
            .components(separatedBy: CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-_")).inverted)
            .filter { !$0.isEmpty }
            .joined(separator: "-")

        guard !cleanID.isEmpty else { return }

        var fillMap: [String: ProfileFillSpec] = [:]
        for row in fillRows where row.enabled {
            let trimmed = row.templateValue.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !trimmed.isEmpty else { continue }
            fillMap[row.id] = ProfileFillSpec(value: trimmed, if_empty: row.ifEmpty)
        }

        let cleanReason = reasonText.trimmingCharacters(in: .whitespacesAndNewlines)
        let cleanNext = nextStepText.trimmingCharacters(in: .whitespacesAndNewlines)

        let spec = FormProfileSpec(
            id: cleanID,
            description: profileDescription.trimmingCharacters(in: .whitespacesAndNewlines),
            source: "user",
            match_fields: selectedMatchFields.sorted(),
            require_signed: selectedRequireSigned.sorted(),
            sign_field: selectedSignField.isEmpty ? nil : selectedSignField,
            reason: cleanReason.isEmpty ? nil : cleanReason,
            fill: fillMap,
            next_step: cleanNext.isEmpty ? nil : cleanNext
        )

        if await vm.saveFormProfile(spec) {
            dismiss()
        }
    }
}
