import CoreGraphics
import Foundation

// MARK: - Core Value & Party Primitives

public enum FieldParty: String, Codable, CaseIterable, Sendable {
    case patient
    case signer
    case sponsor
    case other
}

public enum FormValue: Codable, Equatable, Hashable, Sendable {
    case text(String)
    case bool(Bool)

    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if let b = try? container.decode(Bool.self) {
            self = .bool(b)
        } else if let s = try? container.decode(String.self) {
            self = .text(s)
        } else if container.decodeNil() {
            self = .text("")
        } else {
            self = .text("")
        }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .text(let s): try container.encode(s)
        case .bool(let b): try container.encode(b)
        }
    }

    public var stringValue: String {
        switch self {
        case .text(let s): return s
        case .bool(let b): return b ? "yes" : ""
        }
    }

    public var boolValue: Bool {
        switch self {
        case .bool(let b): return b
        case .text(let s):
            let norm = s.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            return ["x", "yes", "y", "true", "on", "1", "checked"].contains(norm)
        }
    }

    public func isBlank(forKind kind: String) -> Bool {
        if kind == "checkbox" { return !boolValue }
        return stringValue.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    public var jsonObject: Any {
        switch self {
        case .text(let s): return s
        case .bool(let b): return b
        }
    }
}

// MARK: - Document & Form Inspection Models

public struct PageSizeModel: Codable, Equatable, Hashable, Sendable {
    public let w: Double
    public let h: Double
    public init(w: Double, h: Double) { self.w = w; self.h = h }
}

public struct ChoiceOption: Codable, Equatable, Hashable, Sendable {
    public let value: String
    public let label: String
    public init(value: String, label: String) { self.value = value; self.label = label }
}

public struct WidgetPlacement: Codable, Equatable, Hashable, Sendable {
    public var page: Int
    /// Normalized display fractions: [left, top, right, bottom] in 0...1
    public var box: [Double]
    public var option: String?

    public init(page: Int, box: [Double], option: String? = nil) {
        self.page = page
        self.box = box
        self.option = option
    }
}

public struct SignatureSummary: Codable, Equatable, Hashable, Identifiable, Sendable {
    public var id: String { "\(field ?? "sig")|\(signer)|\(when)" }
    public let field: String?
    public let signer: String
    public let when: String
    public let ok: Bool
    public let detail: String

    public init(field: String? = nil, signer: String, when: String, ok: Bool, detail: String) {
        self.field = field
        self.signer = signer
        self.when = when
        self.ok = ok
        self.detail = detail
    }
}

public struct FormFieldModel: Codable, Equatable, Hashable, Identifiable, Sendable {
    public var id: String { name }
    public var name: String
    public var label: String?
    public var kind: String
    public var value: FormValue
    public var widgets: [WidgetPlacement]
    public var readonly: Bool
    public var locked: Bool
    public var required: Bool
    public var multiline: Bool
    public var maxlen: Int?
    public var editable: Bool
    public var options: [String]?
    public var choice_options: [ChoiceOption]?
    public var custom: Bool?
    public var signature: SignatureSummary?
    public var proposed: FormValue?
    public var added: Bool?
    public var detected: Bool?

    public init(
        name: String,
        label: String? = nil,
        kind: String,
        value: FormValue,
        widgets: [WidgetPlacement],
        readonly: Bool = false,
        locked: Bool = false,
        required: Bool = false,
        multiline: Bool = false,
        maxlen: Int? = nil,
        editable: Bool = true,
        options: [String]? = nil,
        choice_options: [ChoiceOption]? = nil,
        custom: Bool? = nil,
        signature: SignatureSummary? = nil,
        proposed: FormValue? = nil,
        added: Bool? = nil,
        detected: Bool? = nil
    ) {
        self.name = name
        self.label = label
        self.kind = kind
        self.value = value
        self.widgets = widgets
        self.readonly = readonly
        self.locked = locked
        self.required = required
        self.multiline = multiline
        self.maxlen = maxlen
        self.editable = editable
        self.options = options
        self.choice_options = choice_options
        self.custom = custom
        self.signature = signature
        self.proposed = proposed
        self.added = added
        self.detected = detected
    }

    public var displayLabel: String {
        if let label, !label.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return label
        }
        return name
    }

    public var isInteractiveInput: Bool {
        kind != "signature" && !readonly && !locked && editable
    }
}

public struct MatchedProfileSummary: Codable, Equatable, Hashable, Sendable {
    public let id: String
    public let description: String
    public init(id: String, description: String) { self.id = id; self.description = description }
}

public struct AutocompleteItem: Codable, Equatable, Hashable, Identifiable, Sendable {
    public var id: String { "\(key)|\(value)" }
    public let key: String
    public let value: String
    public init(key: String, value: String) { self.key = key; self.value = value }
}

public struct PriorSignNote: Codable, Equatable, Hashable, Identifiable, Sendable {
    public var id: String { "\(when)|\(name)" }
    public let when: String
    public let name: String
    public init(when: String, name: String) { self.when = when; self.name = name }
}

public struct SuggestedSignatureBox: Codable, Equatable, Hashable, Sendable {
    public var page: Int
    public var box: [Double]
    public var label: String?
    public init(page: Int, box: [Double], label: String? = nil) {
        self.page = page
        self.box = box
        self.label = label
    }
}

public struct InspectedDocument: Codable, Equatable, Sendable {
    public let source_path: String
    public let pdf_path: String
    public let file: String
    public let conversion_note: String?
    public let sha256: String
    public let pages: [PageSizeModel]
    public let profile: MatchedProfileSummary?
    public let reason: String?
    public let next_step: String?
    public var fields: [FormFieldModel]
    public var sign_target: String?
    public var suggested_signature: SuggestedSignatureBox?
    public let blocked: String?
    public var notes: [String]
    public let warnings: [String]
    public let profile_fields: [String]?
    public var autocomplete: [AutocompleteItem]
    public let signed_before: [PriorSignNote]
}

public struct InspectBatchResponse: Codable, Equatable, Sendable {
    public let documents: [InspectedDocument]
}

// MARK: - Execution & Signing RPC Payloads

public struct FieldAddition: Codable, Equatable, Hashable, Sendable {
    public let name: String
    public let kind: String
    public let label: String
    public let page: Int
    public let box: [Double]

    public init(name: String, kind: String, label: String, page: Int, box: [Double]) {
        self.name = name
        self.kind = kind
        self.label = label
        self.page = page
        self.box = box
    }

    public var dictionaryObject: [String: Any] {
        ["name": name, "kind": kind, "label": label, "page": page, "box": box]
    }
}

public struct PreparedChange: Codable, Equatable, Hashable, Identifiable, Sendable {
    public var id: String { "\(name)|\(from)|\(to)" }
    public let name: String
    public let from: String
    public let to: String
    public let flags: [String]
}

public struct FillCopyResult: Codable, Equatable, Sendable {
    public let saved: Bool
    public let output: String?
    public let name: String?
    public let text: String
}

public struct PrepareResult: Codable, Equatable, Sendable {
    public let ready: Bool
    public let job_id: String?
    public let stage_path: String?
    public let pages: Int?
    public let `where`: SuggestedSignatureBox?
    public let changes: [PreparedChange]?
    public let warnings: [String]?
    public let signed_before: [PriorSignNote]?
    public let needs_ack: Bool?
    public let text: String?
}

public struct CommitResult: Codable, Equatable, Sendable {
    public let signed: Bool
    public let cancelled: Bool
    public let card_error: Bool
    public let needs_ack: Bool?
    public let text: String
    public let output: String?
    public let name: String?
    public let sha256: String?
    public let signatures: [SignatureSummary]?
    public let next_step: String?
}

public struct QuickSignItem: Codable, Equatable, Identifiable, Sendable {
    public var id: String { path }
    public let path: String
    public let file: String
    public let can_sign: Bool
    public let summary: String
    public let warnings: [String]
    public let signed: Bool
    public let output: String?
    public let error: String?
}

public struct QuickSignResult: Codable, Equatable, Sendable {
    public let items: [QuickSignItem]
    public let stopped_early: Bool
}

public struct VerifyResult: Codable, Equatable, Sendable {
    public let file: String
    public let reader_problems: [String]
    public let warnings: [String]
    public let signatures: [SignatureSummary]
}

// MARK: - Settings, Personas, Profiles, Ledger & Diagnostics

public struct SignerPersona: Codable, Equatable, Hashable, Identifiable, Sendable {
    public var id: String
    public var name: String
    public var fields: [String: String]

    public init(id: String, name: String, fields: [String: String]) {
        self.id = id
        self.name = name
        self.fields = fields
    }

    public static let supportedKeys: [(key: String, label: String, placeholder: String)] = [
        ("full_name", "Full Name", "JANE Q. DOE, MD"),
        ("title", "Title / Role", "Staff Physician"),
        ("duty_phone", "Duty Phone", "(555) 555-0100"),
        ("fax", "Fax Number", "(555) 555-0199"),
        ("npi", "NPI (10-digit)", "1234567890"),
        ("dea", "DEA Number", "BD1234567"),
        ("license", "State License", "MD-12345"),
        ("clinic", "Clinic / Department", "Family Medicine Clinic"),
        ("unit", "Command / MTF", "Medical Treatment Facility"),
        ("email", "Duty Email", "jane.q.doe.mil@health.mil")
    ]
}

public struct PersonasState: Codable, Equatable, Sendable {
    public var active_id: String
    public var personas: [SignerPersona]
    public var autocomplete: [AutocompleteItem]?

    public init(active_id: String, personas: [SignerPersona], autocomplete: [AutocompleteItem]? = nil) {
        self.active_id = active_id
        self.personas = personas
        self.autocomplete = autocomplete
    }

    public var activePersona: SignerPersona? {
        personas.first(where: { $0.id == active_id }) ?? personas.first
    }
}

public struct ProfileFillSpec: Codable, Equatable, Hashable, Sendable {
    public var value: String
    public var if_empty: Bool
    public init(value: String, if_empty: Bool = true) {
        self.value = value
        self.if_empty = if_empty
    }
}

public struct FormProfileSpec: Codable, Equatable, Hashable, Identifiable, Sendable {
    public var id: String
    public var description: String
    public var source: String? // "bundled" or "user"
    public var match_fields: [String]
    public var require_signed: [String]
    public var sign_field: String?
    public var reason: String?
    public var fill: [String: ProfileFillSpec]
    public var next_step: String?

    public init(
        id: String,
        description: String,
        source: String? = "user",
        match_fields: [String],
        require_signed: [String] = [],
        sign_field: String? = nil,
        reason: String? = nil,
        fill: [String: ProfileFillSpec] = [:],
        next_step: String? = nil
    ) {
        self.id = id
        self.description = description
        self.source = source
        self.match_fields = match_fields
        self.require_signed = require_signed
        self.sign_field = sign_field
        self.reason = reason
        self.fill = fill
        self.next_step = next_step
    }
}

public struct ProfilesListResponse: Codable, Equatable, Sendable {
    public let profiles: [FormProfileSpec]
}

public struct LedgerRecord: Codable, Equatable, Identifiable, Sendable {
    public var id: String { "\(when)|\(output_sha256)|\(output)" }
    public let when: String
    public let when_display: String
    public let input_sha256: String
    public let output: String
    public let output_sha256: String
    public let field: String
    public let signer: String
    public let exists: Bool
}

public struct LedgerListResponse: Codable, Equatable, Sendable {
    public let entries: [LedgerRecord]
}

public struct RenderPageResponse: Codable, Equatable, Sendable {
    public let png_base64: String
}

public struct SystemDiagnostics: Codable, Equatable, Sendable {
    public let python_version: String
    public let pyhanko_version: String
    public let pikepdf_version: String
    public let libreoffice_path: String?
    public let opensc_path: String?
    public let dod_roots_trusted: Bool
    public let dod_ca_count: Int
    public let app_support_path: String
}

// MARK: - Native Runtime & UI Domain Models

public struct CACCertificateInfo: Equatable, Identifiable, Sendable {
    public let id: String
    public let commonName: String
    public let issuer: String
    public let keyUsage: String
    public let expires: Date?
    public let expiresDisplay: String
    public let daysRemaining: Int?
    public let isPreferredSigningCert: Bool

    public init(
        id: String,
        commonName: String,
        issuer: String,
        keyUsage: String,
        expires: Date?,
        expiresDisplay: String,
        daysRemaining: Int?,
        isPreferredSigningCert: Bool
    ) {
        self.id = id
        self.commonName = commonName
        self.issuer = issuer
        self.keyUsage = keyUsage
        self.expires = expires
        self.expiresDisplay = expiresDisplay
        self.daysRemaining = daysRemaining
        self.isPreferredSigningCert = isPreferredSigningCert
    }
}

public struct CACCardStatus: Equatable, Sendable {
    public enum Readiness: Equatable, Sendable {
        case noCard
        case ready
        case expiringSoon(days: Int)
        case untrustedRoots
        case error(String)
    }

    public let readiness: Readiness
    public let preferredCert: CACCertificateInfo?
    public let certificates: [CACCertificateInfo]
    public let dodRootsTrusted: Bool
    public let foundationModelsAvailable: Bool

    public init(
        readiness: Readiness,
        preferredCert: CACCertificateInfo? = nil,
        certificates: [CACCertificateInfo] = [],
        dodRootsTrusted: Bool = false,
        foundationModelsAvailable: Bool = false
    ) {
        self.readiness = readiness
        self.preferredCert = preferredCert
        self.certificates = certificates
        self.dodRootsTrusted = dodRootsTrusted
        self.foundationModelsAvailable = foundationModelsAvailable
    }

    public static let initial = CACCardStatus(readiness: .noCard)
}

public struct PatientContext: Equatable, Sendable {
    public let displayName: String
    public let fields: [String: String]

    public init(displayName: String, fields: [String: String]) {
        self.displayName = displayName
        self.fields = fields
    }
}

public enum CanvasTool: String, CaseIterable, Equatable, Sendable {
    case select
    case addText
    case addCheck
    case drawSignature

    public var label: String {
        switch self {
        case .select: return "Select"
        case .addText: return "Add Text"
        case .addCheck: return "Add Check"
        case .drawSignature: return "Draw Signature"
        }
    }

    public var systemImage: String {
        switch self {
        case .select: return "cursorarrow"
        case .addText: return "character.textbox"
        case .addCheck: return "checkmark.square"
        case .drawSignature: return "signature"
        }
    }
}

public struct AdvisoryCandidate: Equatable, Identifiable, Sendable {
    public var id: String { fieldName }
    public let fieldName: String
    public let label: String
    public let suggestedText: String
    public let source: String

    public init(fieldName: String, label: String, suggestedText: String, source: String) {
        self.fieldName = fieldName
        self.label = label
        self.suggestedText = suggestedText
        self.source = source
    }
}

public enum DisplayBoxGeometry {
    /// Convert normalized [left, top, right, bottom] (0...1) into a CGRect inside a top-left-origin flipped view of `pageSize`.
    public static func viewRect(for box: [Double], in pageSize: CGSize) -> CGRect {
        guard box.count == 4 else { return .zero }
        let x0 = min(box[0], box[2]) * pageSize.width
        let y0 = min(box[1], box[3]) * pageSize.height
        let x1 = max(box[0], box[2]) * pageSize.width
        let y1 = max(box[1], box[3]) * pageSize.height
        return CGRect(x: x0, y: y0, width: max(2, x1 - x0), height: max(2, y1 - y0))
    }

    /// Convert a CGRect in a top-left-origin flipped view of `pageSize` back into clamped [left, top, right, bottom] in 0...1.
    public static func normalizedBox(for rect: CGRect, in pageSize: CGSize) -> [Double] {
        guard pageSize.width > 0, pageSize.height > 0 else { return [0.1, 0.1, 0.3, 0.14] }
        let l = max(0.0, min(0.99, Double(rect.minX / pageSize.width)))
        let t = max(0.0, min(0.99, Double(rect.minY / pageSize.height)))
        let r = max(l + 0.01, min(1.0, Double(rect.maxX / pageSize.width)))
        let b = max(t + 0.01, min(1.0, Double(rect.maxY / pageSize.height)))
        return [
            (l * 100000).rounded() / 100000,
            (t * 100000).rounded() / 100000,
            (r * 100000).rounded() / 100000,
            (b * 100000).rounded() / 100000
        ]
    }
}
