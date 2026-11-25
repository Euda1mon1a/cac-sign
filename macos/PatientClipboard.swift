import AppKit
import Foundation

public enum PatientClipboard {
    private static let governedAutocompleteKeys: Set<String> = [
        "today", "today_us", "today_iso_compact", "duty_phone", "phone", "fax", "npi", "dea",
        "license", "email", "title", "rank", "full_name", "name", "unit", "clinic", "facility", "department"
    ]

    private static let knownPatientHeaderKeys: Set<String> = [
        "full name", "patient name", "patient", "name", "first name", "given name",
        "middle name", "middle initial", "mi", "last name", "surname", "age",
        "date of birth", "dob", "birth date", "birthdate", "sex", "gender", "race", "ethnicity",
        "marital status", "language", "mrn", "medical record number", "medical record #", "fin",
        "ssn", "social security number", "ssn last 4", "last 4 ssn", "last 4",
        "dod id", "dod id #", "dod id number", "edipi", "dod benefits number", "dbn",
        "sponsor ssn", "sponsor", "sponsor name", "sponsor's name",
        "sponsor rank", "sponsor grade", "sponsor rank/grade", "rank/pay grade",
        "relationship", "relationship to sponsor", "fmp", "family member prefix",
        "religion", "home address", "mailing address", "street address",
        "address", "city", "state", "zip", "zip code", "county",
        "home phone", "cell phone", "mobile phone", "daytime phone", "work phone",
        "bus phone", "business phone", "patient phone", "contact phone", "phone", "telephone",
        "email", "e-mail", "patient email",
        "insurance", "primary insurance", "primary insurance name", "insurance name",
        "insurance number", "insurance id", "member id", "subscriber id", "policy number", "benefits number"
    ]

    private static let sponsorPartyRegex = try! NSRegularExpression(
        pattern: #"^(?:sponsor)\s+—|\b(sponsor|service\s*member|soldier|active\s+duty\s+spouse|spouse\s+ssn|spouse'?s\s+name|relationship\s+to\s+sponsor|family\s+member\s+prefix|\bfmp\b)\b"#,
        options: [.caseInsensitive]
    )

    private static let otherThirdPartyDenyRegex = try! NSRegularExpression(
        pattern: #"^(?:admin|parent\/guardian)\s+—|\b(case\s+manager|administrative\s+certification|for\s+administrative\s+use|military\s+personnel\s+division|screening\s+coordinator|parent\/guardian|emergency\s+contact)\b"#,
        options: [.caseInsensitive]
    )

    private static let thirdPartyDenyRegex = try! NSRegularExpression(
        pattern: #"^(?:sponsor|admin|parent\/guardian)\s+—|\b(sponsor|soldier|service\s*member|active\s+duty\s+spouse|spouse\s+ssn|spouse'?s\s+name|case\s+manager|administrative\s+certification|for\s+administrative\s+use|military\s+personnel\s+division|screening\s+coordinator|parent\/guardian)\b"#,
        options: [.caseInsensitive]
    )

    private static let providerLabelRegex = try! NSRegularExpression(
        pattern: #"^(?:provider|signer|physician)\s+—|\b(physician|provider|practitioner|doctor|clinician|prescriber|signer|supervisor|attending|referring|examiner|evaluator|author|oic|commander|npi|dea|duty\s*phone)\b"#,
        options: [.caseInsensitive]
    )

    private static let nonSignerQualifierRegex = try! NSRegularExpression(
        pattern: #"\b(patient|family\s+member|family\s+home|member|subscriber|sponsor|beneficiary|veteran|child|student|emergency|spouse|parent|mother|father|insurance|carrier|plan|pharmacy|vendor|recipient)\b"#,
        options: [.caseInsensitive]
    )

    // MARK: - Party Classification

    public static func classifyParty(for field: FormFieldModel) -> FieldParty {
        let lbl = fieldLabelText(for: field)
        guard !lbl.isEmpty else { return .other }

        if matches(lbl, #"^(?:provider|signer|physician)\s+—"#) {
            return .signer
        }
        if matches(lbl, sponsorPartyRegex) {
            return .sponsor
        }
        if matches(lbl, otherThirdPartyDenyRegex) {
            return .other
        }
        if matches(lbl, #"^patient\s+—"#) {
            return .patient
        }

        // Explicit patient fields take priority before generic words like "phone", "email", "address", "name".
        if matches(lbl, #"\b(?:family\s+member\/patient|family\s+member|patient|member|subscriber|veteran|beneficiary|recipient|child|student)\b"#)
            || matches(lbl, #"\b(date\s+of\s+birth|birth\s*date|birthdate|dob|mrn|medical\s+record|dod\s+id|edipi|ssn|social\s+security|last\s*4|sex|gender|marital\s+status)\b"#)
            || matches(lbl, #"^\s*age\s*$"#)
            || matches(lbl, #"\b(home\s+address|mailing\s+address|residential\s+address|current\s+address|street\s+address|home\s+phone|cell\s+phone|mobile\s+phone|daytime\s+phone|insurance|policy\s+(?:id|number|no\.?|#)|benefits?\s+(?:id|number|no\.?|#)|dbn)\b"#)
            || (matches(lbl, #"^\s*(?:full\s*name|first\s*name|given\s*name|last\s*name|surname|middle\s*(?:name|initial)|mi|city|state|zip(?:\s*code)?|postal\s*code|address|street)\s*$"#) && !matches(lbl, providerLabelRegex)) {
            return .patient
        }

        // Signer / clinician / provider fields.
        if matches(lbl, providerLabelRegex) {
            return .signer
        }
        if !matches(lbl, nonSignerQualifierRegex) {
            if matches(lbl, #"\b(?:state\s+)?licen[sc]e\b"#) && !matches(lbl, #"\b(driver|plate|vehicle)\b"#) {
                return .signer
            }
            if matches(lbl, #"\b(clinic|facility|department|mtf|unit|command|organization|hospital|fax)\b"#) {
                return .signer
            }
            if matches(lbl, #"\b(title|position|rank|role|specialty)\b"#) && !matches(lbl, #"\b(course|document|form|note)\b"#) {
                return .signer
            }
            if matches(lbl, #"^\s*(?:phone|tel|telephone|e-?mail|printed\s+name|typed\s+or\s+printed\s+name|date\s+signed|signature\s+date)\s*:?\s*$"#) {
                return .signer
            }
        }

        return .other
    }

    // MARK: - Pasteboard & Text Parsing

    public static func readFromGeneralPasteboard() -> PatientContext? {
        guard let raw = NSPasteboard.general.string(forType: .string), !raw.isEmpty else {
            return nil
        }
        return parse(raw)
    }

    public static func parse(_ rawText: String) -> PatientContext? {
        let normalizedNewlines = rawText
            .replacingOccurrences(of: "\r\n", with: "\n")
            .replacingOccurrences(of: "\r", with: "\n")
        guard !normalizedNewlines.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            return nil
        }

        // Expand pipe-separated clinical banner lines ("Patient: ... | DOB: ... | MRN: ...") while keeping normal lines intact.
        var lines: [String] = []
        for rawLine in normalizedNewlines.components(separatedBy: "\n") {
            if rawLine.contains("|") && rawLine.contains(":") {
                let parts = rawLine.components(separatedBy: "|")
                if parts.filter({ $0.contains(":") }).count >= 2 {
                    lines.append(contentsOf: parts)
                    continue
                }
            }
            lines.append(rawLine)
        }

        var map: [String: [String]] = [:]
        var curKey: String? = nil
        let headerRegex = try! NSRegularExpression(pattern: #"^([A-Za-z][A-Za-z0-9 '/#()-]{1,36}):(?:\s*(.*))?$"#)

        for rawLine in lines {
            let line = rawLine.trimmingCharacters(in: .whitespacesAndNewlines)
            if matches(line, #"^(general\s+information|patient\s+information|demographics)$"#) {
                curKey = nil
                continue
            }
            if let match = firstMatch(headerRegex, in: line) {
                let keyRaw = substring(line, range: match.range(at: 1))
                let normKey = collapseSpaces(keyRaw).lowercased()
                let inline = match.range(at: 2).location != NSNotFound
                    ? substring(line, range: match.range(at: 2)).trimmingCharacters(in: .whitespacesAndNewlines)
                    : ""
                if inline.isEmpty || knownPatientHeaderKeys.contains(normKey) {
                    curKey = normKey
                    if map[normKey] == nil { map[normKey] = [] }
                    if !inline.isEmpty {
                        map[normKey, default: []].append(inline)
                    }
                    continue
                }
            }
            if let activeKey = curKey, !line.isEmpty {
                map[activeKey, default: []].append(line)
            }
        }

        return buildPatientContext(from: map)
    }

    private static func buildPatientContext(from map: [String: [String]]) -> PatientContext? {
        func first(_ keys: String...) -> String {
            for key in keys {
                if let vals = map[key], let firstVal = vals.first {
                    let trimmed = firstVal.trimmingCharacters(in: .whitespacesAndNewlines)
                    if !trimmed.isEmpty { return trimmed }
                }
            }
            return ""
        }

        var out: [String: String] = [:]
        let rawName = first("full name", "patient name", "patient", "name")
        if !rawName.isEmpty {
            if let commaIdx = rawName.firstIndex(of: ",") {
                let last = String(rawName[..<commaIdx]).trimmingCharacters(in: .whitespacesAndNewlines)
                let given = collapseSpaces(String(rawName[rawName.index(after: commaIdx)...]))
                if !last.isEmpty { out["last_name"] = last }
                if !given.isEmpty {
                    let parts = given.components(separatedBy: " ")
                    out["first_name"] = parts[0]
                    out["first_only"] = parts[0]
                    if parts.count > 1 {
                        let mid = parts.dropFirst().joined(separator: " ")
                        out["middle_name"] = mid
                        if let ch = mid.first, ch.isLetter {
                            out["middle_initial"] = String(ch).uppercased()
                        }
                    }
                    out["full_name"] = [given, last].filter { !$0.isEmpty }.joined(separator: " ")
                    out["full_name_comma"] = [last, given].filter { !$0.isEmpty }.joined(separator: ", ")
                }
            } else {
                let parts = collapseSpaces(rawName).components(separatedBy: " ").filter { !$0.isEmpty }
                out["full_name"] = parts.joined(separator: " ")
                if parts.count >= 2 {
                    let last = parts.last!
                    out["last_name"] = last
                    out["first_name"] = parts[0]
                    out["first_only"] = parts[0]
                    if parts.count > 2 {
                        let mid = parts[1..<(parts.count - 1)].joined(separator: " ")
                        out["middle_name"] = mid
                        if let ch = mid.first, ch.isLetter {
                            out["middle_initial"] = String(ch).uppercased()
                        }
                    }
                    let given = parts.dropLast().joined(separator: " ")
                    out["full_name_comma"] = "\(last), \(given)"
                } else if let single = parts.first {
                    out["first_name"] = single
                    out["first_only"] = single
                }
            }
        }

        let explicitFirst = first("first name", "given name")
        if !explicitFirst.isEmpty {
            let parts = collapseSpaces(explicitFirst).components(separatedBy: " ")
            out["first_name"] = explicitFirst
            out["first_only"] = parts.first ?? explicitFirst
        }
        let explicitLast = first("last name", "surname")
        if !explicitLast.isEmpty { out["last_name"] = explicitLast }
        let explicitMid = first("middle name")
        if !explicitMid.isEmpty {
            out["middle_name"] = explicitMid
            if let ch = explicitMid.first, ch.isLetter {
                out["middle_initial"] = String(ch).uppercased()
            }
        }
        let explicitMI = first("middle initial", "mi")
        if !explicitMI.isEmpty {
            let cleanMI = explicitMI.replacingOccurrences(of: ".", with: "").trimmingCharacters(in: .whitespaces)
            if !cleanMI.isEmpty {
                out["middle_initial"] = String(cleanMI.prefix(1)).uppercased()
                if out["middle_name"] == nil { out["middle_name"] = out["middle_initial"] }
            }
        }
        if out["full_name"] == nil, (out["first_name"] != nil || out["last_name"] != nil) {
            let given = [out["first_name"], out["middle_name"]].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " ")
            let last = out["last_name"] ?? ""
            out["full_name"] = [given, last].filter { !$0.isEmpty }.joined(separator: " ")
            if !last.isEmpty && !given.isEmpty {
                out["full_name_comma"] = "\(last), \(given)"
            }
        }

        // Date of Birth
        let rawDob = first("date of birth", "dob", "birth date", "birthdate")
        if !rawDob.isEmpty {
            let usRegex = try! NSRegularExpression(pattern: #"^(\d{1,2})\/(\d{1,2})\/(\d{4})\b"#)
            let isoRegex = try! NSRegularExpression(pattern: #"^(\d{4})-(\d{2})-(\d{2})\b"#)
            let ymdRegex = try! NSRegularExpression(pattern: #"^(\d{8})\b"#)
            if let m = firstMatch(usRegex, in: rawDob) {
                let mm = String(format: "%02d", Int(substring(rawDob, range: m.range(at: 1))) ?? 1)
                let dd = String(format: "%02d", Int(substring(rawDob, range: m.range(at: 2))) ?? 1)
                let yyyy = substring(rawDob, range: m.range(at: 3))
                out["dob"] = "\(mm)/\(dd)/\(yyyy)"
                out["dob_yyyymmdd"] = "\(yyyy)\(mm)\(dd)"
            } else if let m = firstMatch(isoRegex, in: rawDob) {
                let yyyy = substring(rawDob, range: m.range(at: 1))
                let mm = substring(rawDob, range: m.range(at: 2))
                let dd = substring(rawDob, range: m.range(at: 3))
                out["dob"] = "\(mm)/\(dd)/\(yyyy)"
                out["dob_yyyymmdd"] = "\(yyyy)\(mm)\(dd)"
            } else if let m = firstMatch(ymdRegex, in: rawDob) {
                let ymd = substring(rawDob, range: m.range(at: 1))
                let yyyy = String(ymd.prefix(4))
                let mm = String(ymd.dropFirst(4).prefix(2))
                let dd = String(ymd.dropFirst(6).prefix(2))
                out["dob_yyyymmdd"] = ymd
                out["dob"] = "\(mm)/\(dd)/\(yyyy)"
            } else {
                out["dob"] = rawDob.components(separatedBy: .whitespaces).first ?? rawDob
            }
        }

        let rawAge = first("age")
        if !rawAge.isEmpty {
            out["age_raw"] = rawAge
            let digits = String(rawAge.prefix(while: \.isNumber))
            if !digits.isEmpty { out["age"] = digits }
        }
        let sex = first("sex", "gender")
        if !sex.isEmpty { out["sex"] = sex }

        // IDs (DoD ID / EDIPI, SSN Last 4 only, MRN, Insurance)
        let dodID = first("dod id", "dod id #", "dod id number", "edipi", "dod benefits number", "dbn")
        if !dodID.isEmpty {
            let digits = dodID.filter(\.isNumber)
            out["dod_id"] = digits.count >= 10 ? digits : dodID
        }
        let rawSSN = first("ssn last 4", "last 4 ssn", "last 4", "ssn", "social security number")
        if !rawSSN.isEmpty {
            let digits = rawSSN.filter(\.isNumber)
            if digits.count >= 4 {
                out["ssn_last4"] = String(digits.suffix(4))
            }
        }
        let mrn = first("mrn", "medical record number", "medical record #", "fin")
        if !mrn.isEmpty { out["mrn"] = mrn }

        // Address
        let addrLines = map["home address"] ?? map["mailing address"] ?? map["address"] ?? []
        if !addrLines.isEmpty {
            let lastLine = addrLines.last!
            let cszRegex = try! NSRegularExpression(pattern: #"^(.+?)[,\s]+([A-Za-z]{2})\s+(\d{5})(?:-?(\d{4}))?$"#)
            if addrLines.count >= 2, let m = firstMatch(cszRegex, in: lastLine) {
                let city = substring(lastLine, range: m.range(at: 1))
                    .trimmingCharacters(in: CharacterSet(charactersIn: ", ").union(.whitespaces))
                let state = substring(lastLine, range: m.range(at: 2)).uppercased()
                let z5 = substring(lastLine, range: m.range(at: 3))
                let z4 = m.range(at: 4).location != NSNotFound ? substring(lastLine, range: m.range(at: 4)) : ""
                let zip = z4.isEmpty ? z5 : "\(z5)-\(z4)"
                out["city"] = city
                out["state"] = state
                out["zip"] = zip
                out["city_state_zip"] = "\(city), \(state) \(zip)"
                out["street"] = addrLines.dropLast().joined(separator: ", ")
            } else {
                out["street"] = addrLines.joined(separator: ", ")
            }
            let full = [out["street"], out["city_state_zip"]].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: ", ")
            if !full.isEmpty {
                out["address"] = full
                out["full_address"] = full
            }
        }
        let explicitStreet = first("street address")
        if !explicitStreet.isEmpty { out["street"] = explicitStreet }
        let explicitCity = first("city")
        if !explicitCity.isEmpty { out["city"] = explicitCity }
        let explicitState = first("state")
        if !explicitState.isEmpty { out["state"] = explicitState }
        let explicitZip = first("zip", "zip code")
        if !explicitZip.isEmpty { out["zip"] = explicitZip }
        if out["city_state_zip"] == nil, (out["city"] != nil || out["state"] != nil || out["zip"] != nil) {
            let cs = [out["city"], out["state"]].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: ", ")
            let z = out["zip"].map { " \($0)" } ?? ""
            let combined = (cs + z).trimmingCharacters(in: .whitespaces)
            if !combined.isEmpty { out["city_state_zip"] = combined }
        }
        if out["address"] == nil, (out["street"] != nil || out["city_state_zip"] != nil) {
            let combined = [out["street"], out["city_state_zip"]].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: ", ")
            if !combined.isEmpty {
                out["address"] = combined
                out["full_address"] = combined
            }
        }

        // Phone, Email, Insurance, Sponsor
        let ph = first("home phone", "cell phone", "mobile phone", "daytime phone", "patient phone", "contact phone", "phone", "telephone", "bus phone", "work phone", "business phone")
        if !ph.isEmpty {
            let formatted = formatPhone(ph)
            out["phone"] = formatted
            out["patient_phone"] = formatted
        }
        let email = first("email", "e-mail", "patient email")
        if !email.isEmpty {
            out["email"] = email
            out["patient_email"] = email
        }
        let insID = first("insurance number", "insurance id", "member id", "subscriber id", "policy number", "benefits number")
        if !insID.isEmpty { out["insurance_id"] = insID }
        let insName = first("primary insurance name", "primary insurance", "insurance name", "insurance")
        if !insName.isEmpty { out["insurance_name"] = insName }

        let sponsorName = first("sponsor name", "sponsor's name", "sponsor")
        if !sponsorName.isEmpty { out["sponsor_name"] = sponsorName }
        let sponsorRank = first("sponsor rank", "sponsor grade", "sponsor rank/grade", "rank/pay grade")
        if !sponsorRank.isEmpty { out["sponsor_rank"] = sponsorRank }
        let rel = first("relationship", "relationship to sponsor", "fmp", "family member prefix")
        if !rel.isEmpty { out["relationship"] = rel }

        guard !out.isEmpty else { return nil }
        let display = out["full_name_comma"] ?? out["full_name"] ?? out["last_name"] ?? out["mrn"] ?? "Patient"
        return PatientContext(displayName: display, fields: out)
    }

    // MARK: - Patient & Signer Suggestions

    public static func patientSuggestions(for field: FormFieldModel, patient: PatientContext?) -> [String] {
        guard let patient,
              field.isInteractiveInput,
              field.kind != "checkbox",
              field.kind != "radio",
              field.kind != "choice",
              field.kind != "signature" else {
            return []
        }

        let lbl = fieldLabelText(for: field)
        guard !lbl.isEmpty else { return [] }
        let party = classifyParty(for: field)

        var out: [String] = []
        var seen = Set<String>()
        func add(_ val: String?) {
            guard let v = val?.trimmingCharacters(in: .whitespacesAndNewlines), !v.isEmpty else { return }
            if seen.insert(v).inserted { out.append(v) }
        }

        let pf = patient.fields
        guard party == .patient else { return [] }

        let compactDate = matches(lbl, #"\byyyymmdd\b|y\s+y\s+y\s+y\s+m\s+m\s+d\s+d"#)
        let commaName = matches(lbl, #"\blast\s*,\s*first\b"#)

        if matches(lbl, #"\b(?:family\s+member\/patient|family\s+member|patient|member|subscriber|veteran|beneficiary|recipient|child|student)(?:'s)?\s*(?:full\s*)?name\b|\bname\s+of\s+(?:patient|family\s+member)\b|^patient\s+—.*\bname\b|\bfamily\s+members\b.*\bname\b|\bfamily\s+member\s+screening\b.*\bname\b|^\s*full\s*name\s*$"#) {
            if commaName { add(pf["full_name_comma"]); add(pf["full_name"]) }
            else { add(pf["full_name"]); add(pf["full_name_comma"]) }
        } else if matches(lbl, #"\bfirst\s*name\b|\bgiven\s*name\b"#) {
            add(pf["first_name"]); add(pf["first_only"])
        } else if matches(lbl, #"\blast\s*name\b|\bsurname\b|\bfamily\s*name\b"#) {
            add(pf["last_name"])
        } else if matches(lbl, #"\bmiddle\s+initial\b|^\s*mi\s*$"#) {
            add(pf["middle_initial"]); add(pf["middle_name"])
        } else if matches(lbl, #"\bmiddle\s*name\b"#) {
            add(pf["middle_name"]); add(pf["middle_initial"])
        } else if matches(lbl, #"\b(date\s+of\s+birth|birth\s*date|birthdate|dob)\b"#) && !matches(lbl, #"\b(age\s+when|well-child|admission)\b"#) {
            if compactDate { add(pf["dob_yyyymmdd"]); add(pf["dob"]) }
            else { add(pf["dob"]); add(pf["dob_yyyymmdd"]) }
        } else if matches(lbl, #"\b(dod\s*id|edipi|dod\s+benefits\s+number|dbn)\b"#) {
            add(pf["dod_id"])
        } else if matches(lbl, #"\b(ssn\s*last\s*4|last\s*4\s*ssn|last\s*4|social\s+security|ssn)\b"#) {
            add(pf["ssn_last4"])
        } else if matches(lbl, #"\bcity\b.*\bstate\b.*\bzip\b|\bcity\s*\/\s*state"#) && !matches(lbl, #"\b(mailing\s+address|street)\b"#) {
            add(pf["city_state_zip"])
        } else if matches(lbl, #"\bstreet\b|\baddress\s*(?:line\s*)?1\b"#) && !matches(lbl, #"\bcity,\s*state,\s*zip\b"#) {
            add(pf["street"]); add(pf["address"] ?? pf["full_address"])
        } else if matches(lbl, #"\b(?:family\s+member\s+mailing|home|mailing|patient|current|residential)?\s*address\b|\bfamily_a\b"#) && !matches(lbl, #"\b(e-?mail|line\s*2|billing)\b"#) {
            add(pf["address"] ?? pf["full_address"]); add(pf["street"])
        } else if matches(lbl, #"^\s*city\s*$"#) {
            add(pf["city"])
        } else if matches(lbl, #"^\s*state\s*$"#) {
            add(pf["state"])
        } else if matches(lbl, #"\bzip(?:\s*code)?\b|\bpostal\s*code\b"#) && !matches(lbl, #"\b(city|include\s+zip)\b"#) {
            add(pf["zip"])
        } else if matches(lbl, #"\b(?:patient|family\s+home|home|cell|mobile|daytime|member|subscriber)(?:'s)?\s*(?:phone|tel|telephone|number|contact|#)\b"#) {
            add(pf["phone"] ?? pf["patient_phone"])
        } else if matches(lbl, #"\b(?:patient|family\s+home|member|subscriber)(?:'s)?\s*e-?mail\b"#) {
            add(pf["email"] ?? pf["patient_email"])
        } else if matches(lbl, #"\b(?:insurance\s*(?:id|number|no\.?|#)|member\s*(?:id|number|no\.?|#)|subscriber\s*(?:id|number|no\.?|#)|policy\s*(?:id|number|no\.?|#)|benefits?\s*(?:id|number|no\.?|#))\b"#) {
            add(pf["insurance_id"])
        } else if matches(lbl, #"\b(?:primary\s+)?insurance(?:\s+name|\s+carrier|\s+plan)?\b"#) && !matches(lbl, #"\b(id|number|no\.?|#|phone|address)\b"#) {
            add(pf["insurance_name"])
        } else if matches(lbl, #"\b(mrn|medical\s+record(?:\s+number|\s*#)?|fin)\b"#) {
            add(pf["mrn"])
        } else if matches(lbl, #"\b(sex|gender)\b"#) {
            add(pf["sex"])
        } else if matches(lbl, #"^\s*age\s*$"#) {
            add(pf["age"]); add(pf["age_raw"])
        }

        return Array(out.prefix(3))
    }

    public static func fieldSuggestions(
        for field: FormFieldModel,
        personaFields: [String: String],
        autocomplete: [AutocompleteItem],
        patient: PatientContext?
    ) -> [String] {
        guard field.isInteractiveInput,
              field.kind != "checkbox",
              field.kind != "radio",
              field.kind != "choice",
              field.kind != "signature" else {
            return []
        }

        let party = classifyParty(for: field)
        if party == .patient {
            return patientSuggestions(for: field, patient: patient)
        }
        if party == .sponsor || party == .other {
            return []
        }

        let rawLbl = fieldLabelText(for: field)
        guard !rawLbl.isEmpty, !matches(rawLbl, thirdPartyDenyRegex), !matches(rawLbl, otherThirdPartyDenyRegex) else {
            return []
        }

        var byKey: [String: String] = [:]
        for (k, v) in personaFields {
            let trimmed = v.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmed.isEmpty { byKey[k] = trimmed }
        }
        for item in autocomplete {
            let trimmed = item.value.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmed.isEmpty, byKey[item.key] == nil {
                byKey[item.key] = trimmed
            }
        }
        // Populate symmetric aliases between SignerPersona keys and autocomplete keys.
        if let fn = byKey["full_name"], byKey["name"] == nil { byKey["name"] = fn }
        if let nm = byKey["name"], byKey["full_name"] == nil { byKey["full_name"] = nm }
        if let dp = byKey["duty_phone"], byKey["phone"] == nil { byKey["phone"] = dp }
        if let ph = byKey["phone"], byKey["duty_phone"] == nil { byKey["duty_phone"] = ph }
        if let ti = byKey["title"], byKey["rank"] == nil { byKey["rank"] = ti }
        if let rk = byKey["rank"], byKey["title"] == nil { byKey["title"] = rk }
        if let cl = byKey["clinic"], byKey["facility"] == nil { byKey["facility"] = cl }
        if let un = byKey["unit"], byKey["department"] == nil { byKey["department"] = un }

        let lbl = rawLbl.lowercased()
        let compactDate = matches(rawLbl, #"\byyyymmdd\b|y\s+y\s+y\s+y\s+m\s+m\s+d\s+d"#)

        struct Rule {
            let pattern: String
            let deny: String?
            let keys: [String]
            let compactKeys: [String]?
        }
        let rules: [Rule] = [
            Rule(
                pattern: #"\bdate\b|dated"#,
                deny: #"\b(birth|dob|expir|well-child|treatment\s+completed|last\s+used|last\s+admission|hospitalization|sent\s+for\s+coding|edas|rfo|diagnosed)\b"#,
                keys: ["today", "today_us"],
                compactKeys: ["today_iso_compact", "today", "today_us"]
            ),
            Rule(pattern: #"\bnpi\b"#, deny: nil, keys: ["npi"], compactKeys: nil),
            Rule(pattern: #"\bdea\b"#, deny: nil, keys: ["dea"], compactKeys: nil),
            Rule(pattern: #"\b(?:state\s+)?licen[sc]e\b"#, deny: #"\b(driver|plate|vehicle)\b"#, keys: ["license"], compactKeys: nil),
            Rule(
                pattern: #"\bphone\b|\btel\b|telephone|contact\s*#|^provider\s+—.*\bcommercial\b"#,
                deny: #"\b(patient|family\s+home|home|cell|mobile|daytime|member|subscriber|emergency|insurance)\b|\(\d+\)\s*dsn\b|^\s*dsn\b"#,
                keys: ["duty_phone", "phone"],
                compactKeys: nil
            ),
            Rule(pattern: #"\bfax\b"#, deny: nil, keys: ["fax"], compactKeys: nil),
            Rule(pattern: #"\be-?mail\b"#, deny: #"\b(patient|family\s+home|member|subscriber)\b"#, keys: ["email"], compactKeys: nil),
            Rule(pattern: #"\btitle\b|\bposition\b|\brank\b|\brole\b"#, deny: #"\b(course|document|form|note)\b"#, keys: ["title", "rank"], compactKeys: nil),
            Rule(
                pattern: #"\b(name|physician|provider|practitioner|doctor|clinician|signer|supervisor|printed|oic|author)\b"#,
                deny: #"^provider\s+—(?!.*\bname\b)|\b(first|last|middle|family\s+member|patient|member|subscriber|beneficiary|child|student|insurance|carrier|plan|emergency|spouse|parent|mother|father|file|course|medication|address|phone|tel|telephone|fax|e-?mail|dsn|commercial|date|title|rank|npi|dea|license)\b"#,
                keys: ["full_name", "name"],
                compactKeys: nil
            ),
            Rule(
                pattern: #"\b(unit|clinic|department|facility|organization|hospital|mtf|command)\b|^provider\s+—.*\b(?:mailing\s+)?address\b|\bm_addres\b"#,
                deny: nil,
                keys: ["clinic", "unit", "facility", "department"],
                compactKeys: nil
            )
        ]

        var picked: [String] = []
        var seen = Set<String>()
        func add(_ val: String?) {
            guard let v = val?.trimmingCharacters(in: .whitespacesAndNewlines), !v.isEmpty else { return }
            if seen.insert(v).inserted { picked.append(v) }
        }

        for rule in rules {
            if matches(lbl, rule.pattern) && (rule.deny == nil || !matches(lbl, rule.deny!)) {
                let targetKeys = (compactDate && rule.compactKeys != nil) ? rule.compactKeys! : rule.keys
                for k in targetKeys { add(byKey[k]) }
            }
        }

        // Also match any custom persona/autocomplete key (e.g. specialty) whose words appear in the field label.
        for (key, value) in byKey.sorted(by: { $0.key < $1.key }) where !governedAutocompleteKeys.contains(key) {
            let words = key
                .components(separatedBy: CharacterSet(charactersIn: "_ -"))
                .map { $0.lowercased() }
                .filter { $0.count >= 3 }
            if !words.isEmpty && words.allSatisfy({ lbl.contains($0) }) {
                add(value)
            }
        }

        return Array(picked.prefix(3))
    }

    // MARK: - Signer Identity Autosave Extraction

    public static func extractSaveableSignerUpdates(
        fields: [FormFieldModel],
        values: [String: FormValue],
        patientApplied: [String: FormValue] = [:],
        patient: PatientContext? = nil
    ) -> [String: String] {
        var updates: [String: String] = [:]
        let patientValues: Set<String> = Set(
            (patient?.fields.values ?? [:].values)
                .map { $0.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() }
                .filter { !$0.isEmpty }
        )

        for field in fields {
            guard field.kind == "text",
                  !field.multiline,
                  field.isInteractiveInput,
                  patientApplied[field.name] == nil,
                  classifyParty(for: field) == .signer,
                  let key = signerAutosaveKey(for: field),
                  let currentVal = values[field.name],
                  case .text(let rawText) = currentVal else {
                continue
            }

            let trimmed = rawText.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !trimmed.isEmpty else { continue }
            if patientValues.contains(trimmed.lowercased()) { continue }
            if matches(trimmed, #"^(n\/a|na|none|unknown|null|-|--)$"#) { continue }

            let origTrimmed = field.value.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
            if !origTrimmed.isEmpty && origTrimmed == trimmed { continue }

            if key == "npi" {
                let digits = trimmed.filter(\.isNumber)
                let allowedChars = trimmed.allSatisfy { $0.isNumber || $0 == "-" || $0 == " " }
                guard allowedChars && digits.count == 10 else { continue }
                updates[key] = digits
            } else {
                updates[key] = trimmed
            }
        }

        return updates
    }

    private static func signerAutosaveKey(for field: FormFieldModel) -> String? {
        let lbl = fieldLabelText(for: field)
        guard !lbl.isEmpty,
              !matches(lbl, nonSignerQualifierRegex),
              !matches(lbl, thirdPartyDenyRegex),
              !matches(lbl, otherThirdPartyDenyRegex) else {
            return nil
        }

        if matches(lbl, #"\bnpi\b"#) { return "npi" }
        if matches(lbl, #"\bfax\b"#) && !matches(lbl, #"\bto\s+\d"#) { return "fax" }
        if matches(lbl, #"\bdea(?:\s*#|\s*number|\s*no\.?)?\b"#) { return "dea" }
        if matches(lbl, #"\b(?:state\s+)?licen[sc]e(?:\s*#|\s*number|\s*no\.?)?\b"#) && !matches(lbl, #"\b(driver|plate|vehicle)\b"#) {
            return "license"
        }
        if matches(lbl, #"\b(physician|provider|practitioner|doctor|clinician|prescriber|signer|supervisor|attending|examiner)[\s']*s?\s*(?:printed\s+|typed\s+or\s+printed\s+|full\s+)?name\b|^provider\s+—.*\bname\b"#) {
            return "full_name"
        }
        if matches(lbl, #"\b(physician|provider|practitioner|doctor|clinician|prescriber|signer|supervisor|duty|office)\s*(?:phone|tel|telephone|number|#)\b|^provider\s+—.*\b(?:commercial|phone|telephone)\b|^\s*(?:phone|tel|telephone)\s*:?\s*$"#) {
            return "duty_phone"
        }
        if matches(lbl, #"\b(title|position|rank|role|specialty)\b"#) && !matches(lbl, #"\b(course|document|form|note)\b"#) {
            return "title"
        }
        if matches(lbl, #"\b(unit|command|organization|battalion|brigade|squadron)\b"#) && !matches(lbl, #"\b(clinic|department|facility)\b"#) {
            return "unit"
        }
        if matches(lbl, #"\b(clinic|facility|department|mtf|practice|hospital)\b|^provider\s+—.*\b(?:mailing\s+)?address\b"#) {
            return "clinic"
        }
        if matches(lbl, #"\be-?mail\b"#) {
            return "email"
        }
        return nil
    }

    // MARK: - String & Regex Utilities

    private static func fieldLabelText(for field: FormFieldModel) -> String {
        var parts: [String] = []
        if let label = field.label?.trimmingCharacters(in: .whitespacesAndNewlines),
           !label.isEmpty,
           !matches(label, #"^Added(?:Text|Check)\d+$"#) {
            parts.append(label)
        }
        let name = field.name.trimmingCharacters(in: .whitespacesAndNewlines)
        if !name.isEmpty && !matches(name, #"^Added(?:Text|Check)\d+$"#) {
            if parts.first != name {
                parts.append(name)
            }
        }
        return parts.joined(separator: " ").trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private static func formatPhone(_ raw: String) -> String {
        var digits = raw.filter(\.isNumber)
        if digits.count == 11 && digits.hasPrefix("1") {
            digits.removeFirst()
        }
        guard digits.count == 10 else {
            return raw.trimmingCharacters(in: .whitespacesAndNewlines)
        }
        let a = digits.prefix(3)
        let b = digits.dropFirst(3).prefix(3)
        let c = digits.suffix(4)
        return "\(a)-\(b)-\(c)"
    }

    private static func collapseSpaces(_ raw: String) -> String {
        raw.components(separatedBy: .whitespacesAndNewlines)
            .filter { !$0.isEmpty }
            .joined(separator: " ")
    }

    private static func matches(_ text: String, _ regex: NSRegularExpression) -> Bool {
        let nsRange = NSRange(text.startIndex..<text.endIndex, in: text)
        return regex.firstMatch(in: text, options: [], range: nsRange) != nil
    }

    private static func matches(_ text: String, _ pattern: String) -> Bool {
        guard let regex = try? NSRegularExpression(pattern: pattern, options: [.caseInsensitive]) else {
            return false
        }
        return matches(text, regex)
    }

    private static func firstMatch(_ regex: NSRegularExpression, in text: String) -> NSTextCheckingResult? {
        let nsRange = NSRange(text.startIndex..<text.endIndex, in: text)
        return regex.firstMatch(in: text, options: [], range: nsRange)
    }

    private static func substring(_ text: String, range: NSRange) -> String {
        guard range.location != NSNotFound, let swiftRange = Range(range, in: text) else {
            return ""
        }
        return String(text[swiftRange])
    }
}
