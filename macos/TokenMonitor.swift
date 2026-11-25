import Combine
import CryptoTokenKit
import Foundation
import Security

// MARK: - Minimal RFC 5280 DER X.509 Inspector

enum DERX509Inspector {
    struct ParsedCertificate {
        let serialHex: String
        let subjectCommonName: String?
        let issuerCommonName: String?
        let notAfter: Date?
        let rank: Int // 2 = Non-Repudiation, 1 = Digital Signature, 0 = Other
        let keyUsageDescription: String
    }

    private struct TLV {
        let tag: UInt8
        let value: Data
        let nextOffset: Int
    }

    static func parse(_ der: Data) -> ParsedCertificate? {
        guard let certSeq = readTLV(in: der, at: 0), certSeq.tag == 0x30,
              let tbs = readTLV(in: certSeq.value, at: 0), tbs.tag == 0x30 else {
            return nil
        }

        let tbsBytes = tbs.value
        var cursor = 0

        // Optional [0] EXPLICIT Version
        if let first = readTLV(in: tbsBytes, at: cursor), first.tag == 0xA0 {
            cursor = first.nextOffset
        }
        // serialNumber INTEGER
        guard let serialTLV = readTLV(in: tbsBytes, at: cursor), serialTLV.tag == 0x02 else {
            return nil
        }
        let serialHex = serialTLV.value.map { String(format: "%02x", $0) }.joined()
        cursor = serialTLV.nextOffset

        // signature AlgorithmIdentifier
        guard let sigAlg = readTLV(in: tbsBytes, at: cursor), sigAlg.tag == 0x30 else {
            return nil
        }
        cursor = sigAlg.nextOffset

        // issuer Name
        guard let issuerTLV = readTLV(in: tbsBytes, at: cursor), issuerTLV.tag == 0x30 else {
            return nil
        }
        let issuerCN = extractCommonName(fromNameSequence: issuerTLV.value)
        cursor = issuerTLV.nextOffset

        // validity SEQUENCE { notBefore, notAfter }
        guard let validityTLV = readTLV(in: tbsBytes, at: cursor), validityTLV.tag == 0x30 else {
            return nil
        }
        let notAfter = extractNotAfter(fromValiditySequence: validityTLV.value)
        cursor = validityTLV.nextOffset

        // subject Name
        guard let subjectTLV = readTLV(in: tbsBytes, at: cursor), subjectTLV.tag == 0x30 else {
            return nil
        }
        let subjectCN = extractCommonName(fromNameSequence: subjectTLV.value)
        cursor = subjectTLV.nextOffset

        // subjectPublicKeyInfo SEQUENCE
        guard let spkiTLV = readTLV(in: tbsBytes, at: cursor), spkiTLV.tag == 0x30 else {
            return nil
        }
        cursor = spkiTLV.nextOffset

        var rank = 0
        var usageDesc = "Unspecified"

        // Optional [1] issuerUniqueID, [2] subjectUniqueID, [3] Extensions (0xA3)
        while cursor < tbsBytes.count, let extra = readTLV(in: tbsBytes, at: cursor) {
            if extra.tag == 0xA3, let extSeq = readTLV(in: extra.value, at: 0), extSeq.tag == 0x30 {
                if let parsedUsage = extractKeyUsage(fromExtensionsSequence: extSeq.value) {
                    rank = parsedUsage.rank
                    usageDesc = parsedUsage.description
                }
            }
            cursor = extra.nextOffset
        }

        return ParsedCertificate(
            serialHex: serialHex,
            subjectCommonName: subjectCN,
            issuerCommonName: issuerCN,
            notAfter: notAfter,
            rank: rank,
            keyUsageDescription: usageDesc
        )
    }

    private static func readTLV(in data: Data, at offset: Int) -> TLV? {
        guard offset >= 0, offset + 2 <= data.count else { return nil }
        let base = data.startIndex
        let tag = data[base + offset]
        let lenByte = data[base + offset + 1]
        var length = 0
        var headerLen = 2

        if (lenByte & 0x80) == 0 {
            length = Int(lenByte)
        } else {
            let countBytes = Int(lenByte & 0x7F)
            guard countBytes > 0, countBytes <= 4, offset + 2 + countBytes <= data.count else { return nil }
            for i in 0..<countBytes {
                length = (length << 8) | Int(data[base + offset + 2 + i])
            }
            headerLen += countBytes
        }

        let valueStart = offset + headerLen
        let valueEnd = valueStart + length
        guard valueEnd <= data.count else { return nil }
        let sub = data.subdata(in: (base + valueStart)..<(base + valueEnd))
        return TLV(tag: tag, value: sub, nextOffset: valueEnd)
    }

    private static func extractCommonName(fromNameSequence nameData: Data) -> String? {
        // OID 2.5.4.3 (id-at-commonName) = [0x55, 0x04, 0x03]
        let cnOID = Data([0x55, 0x04, 0x03])
        var rdnOffset = 0
        while rdnOffset < nameData.count, let rdnSet = readTLV(in: nameData, at: rdnOffset) {
            rdnOffset = rdnSet.nextOffset
            guard rdnSet.tag == 0x31 else { continue }
            var atvOffset = 0
            while atvOffset < rdnSet.value.count, let atvSeq = readTLV(in: rdnSet.value, at: atvOffset) {
                atvOffset = atvSeq.nextOffset
                guard atvSeq.tag == 0x30,
                      let oidTLV = readTLV(in: atvSeq.value, at: 0), oidTLV.tag == 0x06,
                      oidTLV.value == cnOID,
                      let valTLV = readTLV(in: atvSeq.value, at: oidTLV.nextOffset) else {
                    continue
                }
                if let str = String(data: valTLV.value, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines),
                   !str.isEmpty {
                    return str
                }
                if let latin = String(data: valTLV.value, encoding: .isoLatin1)?.trimmingCharacters(in: .whitespacesAndNewlines),
                   !latin.isEmpty {
                    return latin
                }
            }
        }
        return nil
    }

    private static func extractNotAfter(fromValiditySequence valData: Data) -> Date? {
        guard let notBefore = readTLV(in: valData, at: 0),
              let notAfter = readTLV(in: valData, at: notBefore.nextOffset) else {
            return nil
        }
        guard let raw = String(data: notAfter.value, encoding: .ascii) else { return nil }
        return parseASN1Time(tag: notAfter.tag, string: raw)
    }

    private static func parseASN1Time(tag: UInt8, string: String) -> Date? {
        let fmt = DateFormatter()
        fmt.locale = Locale(identifier: "en_US_POSIX")
        fmt.timeZone = TimeZone(secondsFromGMT: 0)
        if tag == 0x17 { // UTCTime: YYMMDDHHMMSSZ
            guard string.count >= 12 else { return nil }
            let yy = Int(string.prefix(2)) ?? 0
            let fullYear = yy >= 50 ? (1900 + yy) : (2000 + yy)
            let rest = String(string.dropFirst(2).prefix(10))
            fmt.dateFormat = "yyyyMMddHHmmss"
            return fmt.date(from: String(format: "%04d%@", fullYear, rest))
        } else if tag == 0x18 { // GeneralizedTime: YYYYMMDDHHMMSSZ
            let core = String(string.prefix(14))
            fmt.dateFormat = "yyyyMMddHHmmss"
            return fmt.date(from: core)
        }
        return nil
    }

    private static func extractKeyUsage(fromExtensionsSequence extSeqData: Data) -> (rank: Int, description: String)? {
        // OID 2.5.29.15 (id-ce-keyUsage) = [0x55, 0x1D, 0x0F]
        let keyUsageOID = Data([0x55, 0x1D, 0x0F])
        var offset = 0
        while offset < extSeqData.count, let extTLV = readTLV(in: extSeqData, at: offset) {
            offset = extTLV.nextOffset
            guard extTLV.tag == 0x30,
                  let oidTLV = readTLV(in: extTLV.value, at: 0), oidTLV.tag == 0x06 else {
                continue
            }
            guard oidTLV.value == keyUsageOID else { continue }

            var innerOffset = oidTLV.nextOffset
            guard let nextTLV = readTLV(in: extTLV.value, at: innerOffset) else { continue }
            let octetTLV: TLV
            if nextTLV.tag == 0x01 { // critical BOOLEAN
                innerOffset = nextTLV.nextOffset
                guard let oct = readTLV(in: extTLV.value, at: innerOffset), oct.tag == 0x04 else { continue }
                octetTLV = oct
            } else if nextTLV.tag == 0x04 {
                octetTLV = nextTLV
            } else {
                continue
            }

            guard let bitTLV = readTLV(in: octetTLV.value, at: 0),
                  bitTLV.tag == 0x03,
                  bitTLV.value.count >= 2 else {
                continue
            }
            let b0 = bitTLV.value[bitTLV.value.startIndex + 1]
            let digitalSig = (b0 & 0x80) != 0
            let nonRepudiation = (b0 & 0x40) != 0
            let keyEncipherment = (b0 & 0x20) != 0
            let keyAgreement = (b0 & 0x08) != 0

            var usages: [String] = []
            if nonRepudiation { usages.append("Non-Repudiation") }
            if digitalSig { usages.append("Digital Signature") }
            if keyEncipherment { usages.append("Key Encipherment") }
            if keyAgreement { usages.append("Key Agreement") }

            let rank = nonRepudiation ? 2 : (digitalSig ? 1 : 0)
            let desc = usages.isEmpty ? "Other" : usages.joined(separator: ", ")
            return (rank, desc)
        }
        return nil
    }
}

// MARK: - TokenMonitor

@MainActor
public final class TokenMonitor: ObservableObject {
    public static let shared = TokenMonitor()

    public struct RawCertificateRecord: Sendable {
        public let der: Data
        public let subjectSummary: String?
        public let label: String?

        public init(der: Data, subjectSummary: String? = nil, label: String? = nil) {
            self.der = der
            self.subjectSummary = subjectSummary
            self.label = label
        }
    }

    @Published public private(set) var status: CACCardStatus = .initial
    private var watcher: TKTokenWatcher?

    public init() {
        let tokenWatcher = TKTokenWatcher()
        self.watcher = tokenWatcher

        for tokenID in tokenWatcher.tokenIDs {
            registerRemovalHandler(on: tokenWatcher, tokenID: tokenID)
        }

        tokenWatcher.setInsertionHandler { [weak self] tokenID in
            Task { @MainActor [weak self] in
                guard let self, let activeWatcher = self.watcher else { return }
                self.registerRemovalHandler(on: activeWatcher, tokenID: tokenID)
                self.refreshNow()
            }
        }

        refreshNow()
    }

    private func registerRemovalHandler(on tokenWatcher: TKTokenWatcher, tokenID: String) {
        tokenWatcher.addRemovalHandler({ [weak self] _ in
            Task { @MainActor [weak self] in
                self?.refreshNow()
            }
        }, forTokenID: tokenID)
    }

    public func refreshNow() {
        let records = Self.queryTokenIdentities()
        let dodTrusted = Self.hasDoDRootCertificatesInKeychain()
        let fmAvailable = AdvisoryFillController.isFoundationModelsAvailable
        self.status = Self.evaluateStatus(
            records: records,
            dodRootsTrusted: dodTrusted,
            foundationModelsAvailable: fmAvailable,
            now: Date()
        )
    }

    public static func evaluateStatus(
        records: [RawCertificateRecord],
        dodRootsTrusted: Bool,
        foundationModelsAvailable: Bool,
        now: Date = Date()
    ) -> CACCardStatus {
        guard !records.isEmpty else {
            return CACCardStatus(
                readiness: .noCard,
                preferredCert: nil,
                certificates: [],
                dodRootsTrusted: dodRootsTrusted,
                foundationModelsAvailable: foundationModelsAvailable
            )
        }

        let dateFmt = DateFormatter()
        dateFmt.locale = Locale(identifier: "en_US_POSIX")
        dateFmt.dateFormat = "dd MMM yyyy"

        struct Candidate {
            let index: Int
            let rank: Int
            let id: String
            let commonName: String
            let issuer: String
            let keyUsage: String
            let expires: Date?
            let expiresDisplay: String
            let daysRemaining: Int?
        }

        var candidates: [Candidate] = []
        for (idx, rec) in records.enumerated() {
            let parsed = DERX509Inspector.parse(rec.der)
            let secSummary: String? = rec.subjectSummary ?? {
                guard let cert = SecCertificateCreateWithData(nil, rec.der as CFData) else { return nil }
                return SecCertificateCopySubjectSummary(cert) as String?
            }()

            let cn = parsed?.subjectCommonName ?? secSummary ?? rec.label ?? "Smart-Card Certificate \(idx + 1)"
            let issuer = parsed?.issuerCommonName ?? "DoD / PIV CA"
            let rank = parsed?.rank ?? {
                let hint = (rec.label ?? "").lowercased()
                if hint.contains("digital signature") || hint.contains("signing") { return 2 }
                if hint.contains("piv auth") || hint.contains("authentication") { return 1 }
                return 0
            }()
            let usage = parsed?.keyUsageDescription ?? (rank == 2 ? "Non-Repudiation" : (rank == 1 ? "Digital Signature" : "Other"))
            let expDate = parsed?.notAfter
            let expDisplay = expDate.map { dateFmt.string(from: $0) } ?? "Unknown"
            let daysLeft: Int? = expDate.map { exp in
                Int(floor(exp.timeIntervalSince(now) / 86_400.0))
            }
            let certID = parsed.map { "\($0.serialHex)-\(idx)" } ?? "cert-\(idx)"

            candidates.append(
                Candidate(
                    index: idx,
                    rank: rank,
                    id: certID,
                    commonName: cn,
                    issuer: issuer,
                    keyUsage: usage,
                    expires: expDate,
                    expiresDisplay: expDisplay,
                    daysRemaining: daysLeft
                )
            )
        }

        // Rank Non-Repudiation (2) ahead of Digital Signature (1), preserving card order within rank.
        let bestSigning = candidates
            .filter { $0.rank > 0 }
            .sorted { lhs, rhs in
                if lhs.rank != rhs.rank { return lhs.rank > rhs.rank }
                return lhs.index < rhs.index
            }
            .first

        let orderedCandidates = candidates.sorted { lhs, rhs in
            if lhs.rank != rhs.rank { return lhs.rank > rhs.rank }
            return lhs.index < rhs.index
        }

        let infos: [CACCertificateInfo] = orderedCandidates.map { cand in
            CACCertificateInfo(
                id: cand.id,
                commonName: cand.commonName,
                issuer: cand.issuer,
                keyUsage: cand.keyUsage,
                expires: cand.expires,
                expiresDisplay: cand.expiresDisplay,
                daysRemaining: cand.daysRemaining,
                isPreferredSigningCert: bestSigning?.id == cand.id
            )
        }
        let preferred = infos.first(where: \.isPreferredSigningCert)

        let readiness: CACCardStatus.Readiness
        if let preferred {
            if let days = preferred.daysRemaining, days < 0 {
                readiness = .error("Signing certificate expired on \(preferred.expiresDisplay).")
            } else if !dodRootsTrusted {
                readiness = .untrustedRoots
            } else if let days = preferred.daysRemaining, days <= 30 {
                readiness = .expiringSoon(days: days)
            } else {
                readiness = .ready
            }
        } else {
            readiness = .error("No signing certificate found on the inserted smart card.")
        }

        return CACCardStatus(
            readiness: readiness,
            preferredCert: preferred,
            certificates: infos,
            dodRootsTrusted: dodRootsTrusted,
            foundationModelsAvailable: foundationModelsAvailable
        )
    }

    private static func queryTokenIdentities() -> [RawCertificateRecord] {
        let query: [String: Any] = [
            kSecClass as String: kSecClassIdentity,
            kSecAttrAccessGroup as String: kSecAttrAccessGroupToken,
            kSecMatchLimit as String: kSecMatchLimitAll,
            kSecReturnRef as String: true,
            kSecReturnAttributes as String: true
        ]
        var rawResult: CFTypeRef?
        let code = SecItemCopyMatching(query as CFDictionary, &rawResult)
        guard code == errSecSuccess, let items = rawResult as? [[String: Any]] else {
            return []
        }

        var out: [RawCertificateRecord] = []
        for item in items {
            guard let rawRef = item[kSecValueRef as String],
                  CFGetTypeID(rawRef as CFTypeRef) == SecIdentityGetTypeID() else {
                continue
            }
            let identity = rawRef as! SecIdentity
            var certRef: SecCertificate?
            var keyRef: SecKey?
            guard SecIdentityCopyCertificate(identity, &certRef) == errSecSuccess,
                  let cert = certRef,
                  SecIdentityCopyPrivateKey(identity, &keyRef) == errSecSuccess,
                  keyRef != nil else {
                continue
            }
            let der = SecCertificateCopyData(cert) as Data
            let summary = SecCertificateCopySubjectSummary(cert) as String?
            let label = item[kSecAttrLabel as String] as? String
            out.append(RawCertificateRecord(der: der, subjectSummary: summary, label: label))
        }
        return out
    }

    private static func hasDoDRootCertificatesInKeychain() -> Bool {
        let query: [String: Any] = [
            kSecClass as String: kSecClassCertificate,
            kSecMatchLimit as String: kSecMatchLimitAll,
            kSecReturnRef as String: true
        ]
        var rawResult: CFTypeRef?
        let code = SecItemCopyMatching(query as CFDictionary, &rawResult)
        guard code == errSecSuccess, let certs = rawResult as? [SecCertificate] else {
            return false
        }
        for cert in certs {
            if let summary = SecCertificateCopySubjectSummary(cert) as String?,
               summary.range(of: "dod root ca", options: .caseInsensitive) != nil {
                return true
            }
        }
        return false
    }
}
