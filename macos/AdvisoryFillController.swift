import Foundation
#if canImport(FoundationModels)
import FoundationModels
#endif

public struct AdvisoryFillError: LocalizedError, Sendable {
    public let message: String
    public init(_ message: String) { self.message = message }
    public var errorDescription: String? { message }
}

#if canImport(FoundationModels)
@available(macOS 26.0, *)
@Generable
struct ClinicalAdvisoryBatch {
    @Guide(description: "Concise text values explicitly stated in the clinical note for matching unfilled text fields; omit unknown fields")
    var items: [ClinicalAdvisoryEntry]
}

@available(macOS 26.0, *)
@Generable
struct ClinicalAdvisoryEntry {
    @Guide(description: "Exact fieldName from the supplied unfilled text fields list")
    var fieldName: String
    @Guide(description: "Concise text value directly stated in the note; never guess and leave empty if absent")
    var suggestedText: String
    @Guide(description: "Short supporting phrase from the clinical note")
    var evidence: String
}
#endif

public enum AdvisoryFillController {
    private static let disallowedPlaceholders: Set<String> = [
        "", "n/a", "na", "none", "nil", "null", "unknown",
        "not stated", "not specified", "not applicable", "unspecified", "tbd", "-", "--"
    ]

    public static var isFoundationModelsAvailable: Bool {
        #if canImport(FoundationModels)
        if #available(macOS 26.0, *) {
            if case .available = SystemLanguageModel.default.availability {
                return true
            }
        }
        #endif
        return false
    }

    /// Strictly restricts advisory extraction to interactive text inputs (never checkboxes, radios, choices, or signatures).
    public static func eligibleTextFields(from unfilledFields: [FormFieldModel]) -> [FormFieldModel] {
        unfilledFields.filter { field in
            field.isInteractiveInput && field.kind == "text"
        }
    }

    /// Sanitizes raw model proposals against eligible text fields, discarding placeholders and unknown field names.
    public static func sanitizeCandidates(
        _ rawItems: [(fieldName: String, suggestedText: String, source: String)],
        eligibleFields: [FormFieldModel]
    ) -> [AdvisoryCandidate] {
        var byName: [String: FormFieldModel] = [:]
        for field in eligibleFields {
            byName[field.name] = field
        }

        var seen = Set<String>()
        var results: [AdvisoryCandidate] = []
        for item in rawItems {
            let key = item.fieldName.trimmingCharacters(in: .whitespacesAndNewlines)
            guard let target = byName[key], seen.insert(key).inserted else { continue }

            var text = item.suggestedText.trimmingCharacters(in: .whitespacesAndNewlines)
            if !target.multiline {
                text = text.replacingOccurrences(of: "\r", with: " ")
                    .replacingOccurrences(of: "\n", with: " ")
                    .trimmingCharacters(in: .whitespacesAndNewlines)
            }
            let lowered = text.lowercased()
            guard !disallowedPlaceholders.contains(lowered) else { continue }

            if let maxLen = target.maxlen, maxLen > 0, text.count > maxLen {
                text = String(text.prefix(maxLen)).trimmingCharacters(in: .whitespacesAndNewlines)
            }
            guard !text.isEmpty else { continue }

            let cleanSource = item.source.trimmingCharacters(in: .whitespacesAndNewlines)
            results.append(
                AdvisoryCandidate(
                    fieldName: target.name,
                    label: target.displayLabel,
                    suggestedText: text,
                    source: cleanSource.isEmpty ? "Apple Intelligence (on-device)" : cleanSource
                )
            )
        }
        return results
    }

    public static func suggestFromClinicalNote(
        noteText: String,
        unfilledFields: [FormFieldModel]
    ) async throws -> [AdvisoryCandidate] {
        let trimmedNote = noteText.trimmingCharacters(in: .whitespacesAndNewlines)
        let eligible = eligibleTextFields(from: unfilledFields)
        guard !trimmedNote.isEmpty, !eligible.isEmpty else {
            return []
        }

        #if canImport(FoundationModels)
        if #available(macOS 26.0, *) {
            guard case .available = SystemLanguageModel.default.availability else {
                throw AdvisoryFillError("On-device Apple Intelligence is not currently available on this Mac.")
            }

            let fieldLines = eligible.prefix(40).map { field -> String in
                let maxHint = field.maxlen.map { " (max \($0) chars)" } ?? ""
                return "- fieldName: \"\(field.name)\" | label: \"\(field.displayLabel)\"\(maxHint)"
            }.joined(separator: "\n")

            let instructions = """
            You extract concise, factual form text values strictly from a clinician's pasted note.
            Rules:
            1. Only propose values explicitly stated in the note. Never invent, infer, or guess.
            2. Omit any field not directly answered in the note. Never output "N/A", "none", or "unknown".
            3. Use the exact fieldName provided in the field list.
            """

            let prompt = """
            Unfilled text fields:
            \(fieldLines)

            Clinical note:
            \(String(trimmedNote.prefix(8000)))
            """

            let session = LanguageModelSession(instructions: instructions)
            // Note: Explicitly pass sampling: nil so macOS 27 SDK binds to macOS 26.0's
            // init(sampling:temperature:maximumResponseTokens:) instead of 27-only init(samplingMode:...).
            let response = try await session.respond(
                to: prompt,
                generating: ClinicalAdvisoryBatch.self,
                options: GenerationOptions(sampling: nil, temperature: 0.1, maximumResponseTokens: 700)
            )
            let raw = response.content.items.map {
                (fieldName: $0.fieldName, suggestedText: $0.suggestedText, source: $0.evidence)
            }
            return sanitizeCandidates(raw, eligibleFields: eligible)
        }
        #endif

        throw AdvisoryFillError("On-device FoundationModels requires macOS 26 or newer with Apple Intelligence enabled.")
    }
}
