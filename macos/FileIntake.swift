import Foundation

/// Unified local file intake for Finder drops, Dock drops, Open panels, and macOS Services.
public enum FileIntake {
    public struct IntakeError: LocalizedError {
        public let message: String
        public init(_ message: String) { self.message = message }
        public var errorDescription: String? { message }
    }

    public static let supportedExtensions: Set<String> = ["pdf", "docx"]

    public static func fileURL(from item: Any?) -> URL? {
        let url: URL?
        if let value = item as? URL {
            url = value
        } else if let nsURL = item as? NSURL {
            url = nsURL as URL
        } else if let data = item as? Data {
            url = URL(dataRepresentation: data, relativeTo: nil)
        } else if let text = item as? String {
            if text.hasPrefix("/") {
                url = URL(fileURLWithPath: text)
            } else {
                url = URL(string: text)
            }
        } else {
            url = nil
        }
        return url?.isFileURL == true ? url : nil
    }

    public static func collectSupportedFiles(from urls: [URL]) throws -> [URL] {
        guard !urls.isEmpty else {
            throw IntakeError("Drop or choose a PDF or DOCX document, or a folder containing forms.")
        }

        let fm = FileManager.default
        var collected: [URL] = []
        var sawDirectory = false

        for rawURL in urls {
            guard rawURL.isFileURL else {
                throw IntakeError("Drop local PDF or DOCX files from Finder.")
            }
            let url = rawURL.standardizedFileURL
            var isDir: ObjCBool = false
            guard fm.fileExists(atPath: url.path, isDirectory: &isDir) else {
                throw IntakeError("The selected item is unavailable: \(url.lastPathComponent)")
            }

            if isDir.boolValue {
                sawDirectory = true
                let dirFiles = try enumerateSupportedFiles(in: url, fileManager: fm)
                collected.append(contentsOf: dirFiles)
            } else {
                let ext = url.pathExtension.lowercased()
                guard supportedExtensions.contains(ext) else {
                    throw IntakeError("Unsupported file type: \(url.lastPathComponent). Choose a PDF or DOCX document.")
                }
                collected.append(url)
            }
        }

        var seenPaths = Set<String>()
        var unique: [URL] = []
        for url in collected {
            let canonical = url.resolvingSymlinksInPath().standardizedFileURL.path
            if seenPaths.insert(canonical).inserted {
                unique.append(url)
            }
        }

        if unique.isEmpty {
            if sawDirectory {
                throw IntakeError("No PDF or DOCX documents were found in the selected folder.")
            }
            throw IntakeError("No supported PDF or DOCX documents were provided.")
        }

        return unique
    }

    private static func enumerateSupportedFiles(in directory: URL, fileManager: FileManager) throws -> [URL] {
        guard let enumerator = fileManager.enumerator(
            at: directory,
            includingPropertiesForKeys: [.isRegularFileKey, .isDirectoryKey],
            options: [.skipsHiddenFiles, .skipsPackageDescendants]
        ) else {
            throw IntakeError("Unable to read folder: \(directory.lastPathComponent)")
        }

        var matches: [URL] = []
        for case let candidate as URL in enumerator {
            let normalized = candidate.standardizedFileURL
            let ext = normalized.pathExtension.lowercased()
            guard supportedExtensions.contains(ext) else { continue }
            var childIsDir: ObjCBool = false
            if fileManager.fileExists(atPath: normalized.path, isDirectory: &childIsDir), !childIsDir.boolValue {
                matches.append(normalized)
            }
        }

        matches.sort { lhs, rhs in
            lhs.path.localizedStandardCompare(rhs.path) == .orderedAscending
        }
        return matches
    }
}
