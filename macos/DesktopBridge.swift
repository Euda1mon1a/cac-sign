import Darwin
import Foundation

public struct DesktopBridgeError: LocalizedError, Sendable {
    public let message: String
    public init(_ message: String) { self.message = message }
    public var errorDescription: String? { message }
}

/// Thread-safe persistent NDJSON subprocess transport running on a dedicated serial queue
/// so `DesktopBridge` actor methods never block the Swift cooperative thread pool.
private final class PersistentPythonWorker: @unchecked Sendable {
    private let ioQueue = DispatchQueue(label: "com.cacsign.desktopbridge.io", qos: .userInitiated)
    private let stateLock = NSLock()
    private let customPythonPath: String?
    private let customScriptPath: String?
    private let customAppSupportPath: String?

    private var process: Process?
    private var stdinHandle: FileHandle?
    private var stdoutHandle: FileHandle?
    private var stderrHandle: FileHandle?
    private var readBuffer = Data()
    private var stderrRing = Data()
    private var nextRequestID: Int = 1

    init(pythonPath: String? = nil, scriptPath: String? = nil, appSupportPath: String? = nil) {
        self.customPythonPath = pythonPath
        self.customScriptPath = scriptPath
        self.customAppSupportPath = appSupportPath
    }

    func shutdown() {
        stateLock.lock()
        let proc = process
        let stdin = stdinHandle
        process = nil
        stdinHandle = nil
        stdoutHandle = nil
        stderrHandle?.readabilityHandler = nil
        stderrHandle = nil
        stateLock.unlock()

        try? stdin?.close()
        if let proc, proc.isRunning {
            proc.terminate()
        }
    }

    func send(payload: [String: Any]) async throws -> Data {
        let requestBytes: Data = try {
            var body = payload
            stateLock.lock()
            let reqID = nextRequestID
            nextRequestID += 1
            stateLock.unlock()
            body["id"] = reqID
            guard JSONSerialization.isValidJSONObject(body) else {
                throw DesktopBridgeError("Invalid JSON RPC request payload.")
            }
            return try JSONSerialization.data(withJSONObject: body, options: [])
        }()

        return try await withCheckedThrowingContinuation { continuation in
            ioQueue.async { [self] in
                do {
                    let responseLine = try self.performRoundTripOnQueue(requestData: requestBytes)
                    continuation.resume(returning: responseLine)
                } catch {
                    continuation.resume(throwing: error)
                }
            }
        }
    }

    private func performRoundTripOnQueue(requestData: Data) throws -> Data {
        let (stdin, stdout) = try ensureRunningOnQueue()

        var framed = requestData
        framed.append(0x0A) // '\n'

        do {
            try stdin.write(contentsOf: framed)
        } catch {
            // Worker may have exited since the prior turn; respawn once and retry.
            tearDownOnQueue()
            let retryHandles = try ensureRunningOnQueue()
            try retryHandles.stdin.write(contentsOf: framed)
            return try readResponseLineOnQueue(from: retryHandles.stdout)
        }

        return try readResponseLineOnQueue(from: stdout)
    }

    private func readResponseLineOnQueue(from stdout: FileHandle) throws -> Data {
        while true {
            if let newlineIdx = readBuffer.firstIndex(of: 0x0A) {
                let line = readBuffer.subdata(in: 0..<newlineIdx)
                readBuffer.removeSubrange(0...newlineIdx)
                let trimmed = trimASCIIWhitespace(line)
                if !trimmed.isEmpty {
                    return trimmed
                }
                continue
            }

            // Note: Foundation's FileHandle.read(upToCount:) loops until N bytes or EOF,
            // which deadlocks persistent line-delimited pipes. Use POSIX Darwin.read() so
            // any flushed newline response returns immediately.
            var rawBuf = [UInt8](repeating: 0, count: 16384)
            let fd = stdout.fileDescriptor
            var bytesRead: Int = 0
            while true {
                bytesRead = rawBuf.withUnsafeMutableBytes { ptr in
                    guard let base = ptr.baseAddress else { return -1 }
                    return Darwin.read(fd, base, ptr.count)
                }
                if bytesRead < 0 && errno == EINTR {
                    continue
                }
                break
            }

            if bytesRead < 0 {
                let errCode = errno
                let detail = recentStderrSummary()
                tearDownOnQueue()
                throw DesktopBridgeError(
                    detail.isEmpty
                        ? "Failed reading from cac_sign_desktop.py (errno \(errCode))."
                        : "cac_sign_desktop.py exited unexpectedly: \(detail)"
                )
            }

            if bytesRead == 0 {
                let detail = recentStderrSummary()
                tearDownOnQueue()
                throw DesktopBridgeError(
                    detail.isEmpty
                        ? "cac_sign_desktop.py closed its output stream unexpectedly."
                        : "cac_sign_desktop.py terminated: \(detail)"
                )
            }
            readBuffer.append(contentsOf: rawBuf.prefix(bytesRead))
        }
    }

    private func ensureRunningOnQueue() throws -> (stdin: FileHandle, stdout: FileHandle) {
        stateLock.lock()
        if let proc = process, proc.isRunning, let sin = stdinHandle, let sout = stdoutHandle {
            stateLock.unlock()
            return (sin, sout)
        }
        stateLock.unlock()

        tearDownOnQueue()
        let (pythonURL, scriptURL) = try resolveWorkerExecutable()
        let cleanEnv = buildCleanEnvironment(scriptURL: scriptURL)

        let proc = Process()
        let inPipe = Pipe()
        let outPipe = Pipe()
        let errPipe = Pipe()
        _ = fcntl(inPipe.fileHandleForWriting.fileDescriptor, F_SETNOSIGPIPE, 1)

        proc.executableURL = pythonURL
        proc.arguments = ["-I", "-B", scriptURL.path, "--serve"]
        proc.environment = cleanEnv
        proc.currentDirectoryURL = scriptURL.deletingLastPathComponent()
        proc.standardInput = inPipe
        proc.standardOutput = outPipe
        proc.standardError = errPipe

        errPipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let self else { return }
            self.stateLock.lock()
            self.stderrRing.append(data)
            if self.stderrRing.count > 8192 {
                self.stderrRing.removeFirst(self.stderrRing.count - 8192)
            }
            self.stateLock.unlock()
        }

        do {
            try proc.run()
        } catch {
            errPipe.fileHandleForReading.readabilityHandler = nil
            throw DesktopBridgeError("Could not launch Python worker (\(pythonURL.path)): \(error.localizedDescription)")
        }

        stateLock.lock()
        self.process = proc
        self.stdinHandle = inPipe.fileHandleForWriting
        self.stdoutHandle = outPipe.fileHandleForReading
        self.stderrHandle = errPipe.fileHandleForReading
        self.readBuffer.removeAll(keepingCapacity: true)
        self.stderrRing.removeAll(keepingCapacity: true)
        stateLock.unlock()

        return (inPipe.fileHandleForWriting, outPipe.fileHandleForReading)
    }

    private func tearDownOnQueue() {
        stateLock.lock()
        let proc = process
        let sin = stdinHandle
        let serr = stderrHandle
        process = nil
        stdinHandle = nil
        stdoutHandle = nil
        stderrHandle = nil
        readBuffer.removeAll(keepingCapacity: true)
        stateLock.unlock()

        serr?.readabilityHandler = nil
        try? sin?.close()
        if let proc, proc.isRunning {
            proc.terminate()
        }
    }

    private func recentStderrSummary() -> String {
        stateLock.lock()
        let copy = stderrRing
        stateLock.unlock()
        guard !copy.isEmpty, let str = String(data: copy, encoding: .utf8) else { return "" }
        return str.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func trimASCIIWhitespace(_ data: Data) -> Data {
        var start = data.startIndex
        var end = data.endIndex
        while start < end && (data[start] == 0x20 || data[start] == 0x09 || data[start] == 0x0D || data[start] == 0x0A) {
            start += 1
        }
        while end > start && (data[end - 1] == 0x20 || data[end - 1] == 0x09 || data[end - 1] == 0x0D || data[end - 1] == 0x0A) {
            end -= 1
        }
        return data.subdata(in: start..<end)
    }

    private func buildCleanEnvironment(scriptURL: URL) -> [String: String] {
        let appDir = scriptURL.deletingLastPathComponent().path
        var env: [String: String] = [
            "HOME": FileManager.default.homeDirectoryForCurrentUser.path,
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": appDir
        ]
        let inheritedSupport = ProcessInfo.processInfo.environment["CAC_SIGN_APP_SUPPORT"]
        if let support = customAppSupportPath ?? inheritedSupport, !support.isEmpty {
            env["CAC_SIGN_APP_SUPPORT"] = support
        }
        return env
    }

    private func resolveWorkerExecutable() throws -> (python: URL, script: URL) {
        let fm = FileManager.default
        let parentEnv = ProcessInfo.processInfo.environment

        // 1. Explicit overrides or environment variables (for dev/tests).
        let explicitPy = nonEmpty(customPythonPath) ?? nonEmpty(parentEnv["CAC_SIGN_PYTHON"])
        let explicitScript = nonEmpty(customScriptPath) ?? nonEmpty(parentEnv["CAC_SIGN_DESKTOP_SCRIPT"])
        if let py = explicitPy, let sc = explicitScript,
           fm.isExecutableFile(atPath: py), fm.fileExists(atPath: sc) {
            return (URL(fileURLWithPath: py), URL(fileURLWithPath: sc))
        }

        // 2. Bundled .app resources: Resources/python/bin/python3(.12) + Resources/app/cac_sign_desktop.py
        if let resURL = Bundle.main.resourceURL {
            let bundledScriptCandidates = [
                resURL.appendingPathComponent("app/cac_sign_desktop.py"),
                resURL.appendingPathComponent("cac_sign_desktop.py")
            ]
            let bundledPyCandidates = [
                resURL.appendingPathComponent("python/bin/python3"),
                resURL.appendingPathComponent("python/bin/python3.12"),
                resURL.appendingPathComponent("python/bin/python")
            ]
            if explicitScript == nil, explicitPy == nil,
               let scURL = bundledScriptCandidates.first(where: { fm.fileExists(atPath: $0.path) }),
               let pyURL = bundledPyCandidates.first(where: { fm.isExecutableFile(atPath: $0.path) }) {
                return (pyURL, scURL)
            }
        }

        // 3. Repo root (Info.plist CACSignRepoRoot, current working directory, or ~/.cac-sign).
        let homeRepo = fm.homeDirectoryForCurrentUser.appendingPathComponent(".cac-sign").resolvingSymlinksInPath().path
        let plistRepo = Bundle.main.object(forInfoDictionaryKey: "CACSignRepoRoot") as? String
        let cwdRepo = fm.currentDirectoryPath
        let repoRoots = [plistRepo, cwdRepo, homeRepo].compactMap { nonEmpty($0) }

        let resolvedScript: URL? = {
            if let sc = explicitScript, fm.fileExists(atPath: sc) {
                return URL(fileURLWithPath: sc)
            }
            for root in repoRoots {
                let candidate = URL(fileURLWithPath: root).appendingPathComponent("cac_sign_desktop.py")
                if fm.fileExists(atPath: candidate.path) {
                    return candidate
                }
            }
            return nil
        }()

        guard let scriptURL = resolvedScript else {
            throw DesktopBridgeError("Could not locate cac_sign_desktop.py in bundle, CACSignRepoRoot, or ~/.cac-sign.")
        }

        let resolvedPy: URL? = {
            if let py = explicitPy, fm.isExecutableFile(atPath: py) {
                return URL(fileURLWithPath: py)
            }
            let scriptRoot = scriptURL.deletingLastPathComponent().path
            for root in [scriptRoot] + repoRoots {
                for rel in [".venv/bin/python", ".venv/bin/python3"] {
                    let candidate = URL(fileURLWithPath: root).appendingPathComponent(rel)
                    if fm.isExecutableFile(atPath: candidate.path) {
                        return candidate
                    }
                }
            }
            for sysPy in ["/opt/homebrew/bin/python3", "/usr/bin/python3"] where fm.isExecutableFile(atPath: sysPy) {
                return URL(fileURLWithPath: sysPy)
            }
            return nil
        }()

        guard let pythonURL = resolvedPy else {
            throw DesktopBridgeError("Could not locate Python interpreter (.venv/bin/python) for cac_sign_desktop.py.")
        }

        return (pythonURL, scriptURL)
    }

    private func nonEmpty(_ value: String?) -> String? {
        guard let trimmed = value?.trimmingCharacters(in: .whitespacesAndNewlines), !trimmed.isEmpty else {
            return nil
        }
        return trimmed
    }
}

public actor DesktopBridge {
    public static let shared = DesktopBridge()

    private let worker: PersistentPythonWorker
    private let decoder = JSONDecoder()
    private let encoder = JSONEncoder()

    public init() {
        self.worker = PersistentPythonWorker()
    }

    public init(pythonPath: String?, scriptPath: String?, appSupportPath: String? = nil) {
        self.worker = PersistentPythonWorker(
            pythonPath: pythonPath,
            scriptPath: scriptPath,
            appSupportPath: appSupportPath
        )
    }

    public func shutdown() {
        worker.shutdown()
    }

    public func inspect(paths: [String]) async throws -> [InspectedDocument] {
        let resp: InspectBatchResponse = try await request(op: "inspect", params: ["paths": paths])
        return resp.documents
    }

    public func fillCopy(
        pdfPath: String,
        values: [String: FormValue],
        additions: [FieldAddition]
    ) async throws -> FillCopyResult {
        try await request(
            op: "fill_copy",
            params: [
                "pdf_path": pdfPath,
                "values": encodeValues(values),
                "additions": additions.map(\.dictionaryObject)
            ]
        )
    }

    public func prepare(
        pdfPath: String,
        values: [String: FormValue],
        additions: [FieldAddition],
        mode: String,
        field: String?,
        page: Int?,
        box: [Double]?,
        opensc: Bool
    ) async throws -> PrepareResult {
        var params: [String: Any] = [
            "pdf_path": pdfPath,
            "values": encodeValues(values),
            "additions": additions.map(\.dictionaryObject),
            "mode": mode,
            "opensc": opensc,
            "field": field as Any? ?? NSNull(),
            "page": page as Any? ?? NSNull(),
            "box": box as Any? ?? NSNull()
        ]
        if field == nil { params["field"] = NSNull() }
        return try await request(op: "prepare", params: params)
    }

    public func commit(
        jobID: String,
        ack: Bool,
        reason: String?,
        opensc: Bool,
        pin: String?
    ) async throws -> CommitResult {
        try await request(
            op: "commit",
            params: [
                "job_id": jobID,
                "ack": ack,
                "reason": reason as Any? ?? NSNull(),
                "opensc": opensc,
                "pin": pin as Any? ?? NSNull(),
                "p12_path": NSNull(),
                "p12_pass": NSNull()
            ]
        )
    }

    public func discard(jobID: String? = nil) async {
        var params: [String: Any] = [:]
        if let jobID {
            params["job_id"] = jobID
        }
        _ = try? await sendRaw(op: "discard", params: params)
    }

    public func signQuick(
        paths: [String],
        dryRun: Bool,
        opensc: Bool,
        pin: String?
    ) async throws -> QuickSignResult {
        try await request(
            op: "sign_quick",
            params: [
                "paths": paths,
                "dry_run": dryRun,
                "opensc": opensc,
                "pin": pin as Any? ?? NSNull(),
                "p12_path": NSNull(),
                "p12_pass": NSNull()
            ]
        )
    }

    public func verify(path: String) async throws -> VerifyResult {
        try await request(op: "verify", params: ["path": path])
    }

    public func getPersonas() async throws -> PersonasState {
        try await request(op: "personas_get", params: [:])
    }

    public func savePersonas(activeID: String, personas: [SignerPersona]) async throws -> PersonasState {
        let personasJSON = try encodeToJSONObject(personas)
        return try await request(
            op: "personas_save",
            params: [
                "active_id": activeID,
                "personas": personasJSON
            ]
        )
    }

    public func autosaveIdentity(updates: [String: String]) async throws -> PersonasState {
        try await request(op: "identity_autosave", params: ["updates": updates])
    }

    public func listProfiles() async throws -> [FormProfileSpec] {
        let resp: ProfilesListResponse = try await request(op: "profiles_list", params: [:])
        return resp.profiles
    }

    public func saveProfile(_ profile: FormProfileSpec) async throws -> [FormProfileSpec] {
        let profileJSON = try encodeToJSONObject(profile)
        let resp: ProfilesListResponse = try await request(
            op: "profile_save",
            params: ["profile": profileJSON]
        )
        return resp.profiles
    }

    public func deleteProfile(id: String) async throws -> [FormProfileSpec] {
        let resp: ProfilesListResponse = try await request(
            op: "profile_delete",
            params: ["id": id]
        )
        return resp.profiles
    }

    public func listLedger(limit: Int = 100) async throws -> [LedgerRecord] {
        let resp: LedgerListResponse = try await request(
            op: "ledger_list",
            params: ["limit": limit]
        )
        return resp.entries
    }

    public func renderPagePNG(pdfPath: String, page: Int, scale: Double = 2.0) async throws -> Data {
        let resp: RenderPageResponse = try await request(
            op: "render_page",
            params: [
                "pdf_path": pdfPath,
                "page": page,
                "scale": scale
            ]
        )
        guard let bytes = Data(base64Encoded: resp.png_base64) else {
            throw DesktopBridgeError("Invalid base64 PNG returned by render_page.")
        }
        return bytes
    }

    public func diagnostics() async throws -> SystemDiagnostics {
        try await request(op: "diagnostics", params: [:])
    }

    // MARK: - Helpers

    private func encodeValues(_ values: [String: FormValue]) -> [String: Any] {
        var dict: [String: Any] = [:]
        for (k, v) in values {
            dict[k] = v.jsonObject
        }
        return dict
    }

    private func encodeToJSONObject<T: Encodable>(_ value: T) throws -> Any {
        let data = try encoder.encode(value)
        return try JSONSerialization.jsonObject(with: data, options: [])
    }

    private func sendRaw(op: String, params: [String: Any]) async throws -> Any {
        var payload = params
        payload["op"] = op
        let lineData = try await worker.send(payload: payload)

        guard let root = try JSONSerialization.jsonObject(with: lineData, options: []) as? [String: Any] else {
            throw DesktopBridgeError("Malformed JSON response from cac_sign_desktop.py.")
        }
        let ok = (root["ok"] as? Bool) ?? false
        if !ok {
            let errMsg = (root["error"] as? String)
                ?? (root["text"] as? String)
                ?? "Desktop worker operation '\(op)' failed."
            throw DesktopBridgeError(errMsg)
        }
        return root["data"] ?? [:]
    }

    private func request<T: Decodable>(op: String, params: [String: Any]) async throws -> T {
        let dataObj = try await sendRaw(op: op, params: params)
        let subData = try JSONSerialization.data(withJSONObject: dataObj, options: [])
        do {
            return try decoder.decode(T.self, from: subData)
        } catch {
            throw DesktopBridgeError("Failed decoding '\(op)' response: \(error.localizedDescription)")
        }
    }
}
