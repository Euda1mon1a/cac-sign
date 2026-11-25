import AppKit
import Foundation

/// Cocoa NSServices receiver bridging Finder "Services" actions into CACSign.app.
@MainActor
public final class ServicesProvider: NSObject {
    public static let shared = ServicesProvider()
    public var onReviewFiles: (([URL]) -> Void)?
    public var onQuickSignFiles: (([URL]) -> Void)?

    public override init() {
        super.init()
    }

    @objc(reviewWithCACSign:userData:error:)
    public func reviewWithCACSign(
        _ pboard: NSPasteboard,
        userData: String?,
        error: AutoreleasingUnsafeMutablePointer<NSString?>?
    ) {
        handleServicePasteboard(pboard, error: error, callback: onReviewFiles)
    }

    @objc(quickSignWithCAC:userData:error:)
    public func quickSignWithCAC(
        _ pboard: NSPasteboard,
        userData: String?,
        error: AutoreleasingUnsafeMutablePointer<NSString?>?
    ) {
        handleServicePasteboard(pboard, error: error, callback: onQuickSignFiles)
    }

    private func handleServicePasteboard(
        _ pboard: NSPasteboard,
        error: AutoreleasingUnsafeMutablePointer<NSString?>?,
        callback: (([URL]) -> Void)?
    ) {
        let urls = extractFileURLs(from: pboard)
        do {
            let supported = try FileIntake.collectSupportedFiles(from: urls)
            callback?(supported)
        } catch let intakeErr {
            error?.pointee = (intakeErr.localizedDescription as NSString)
        }
    }

    private func extractFileURLs(from pboard: NSPasteboard) -> [URL] {
        if let nsURLs = pboard.readObjects(
            forClasses: [NSURL.self],
            options: [.urlReadingFileURLsOnly: true]
        ) as? [URL], !nsURLs.isEmpty {
            return nsURLs
        }

        let legacyFilenamesType = NSPasteboard.PasteboardType("NSFilenamesPboardType")
        if let paths = pboard.propertyList(forType: legacyFilenamesType) as? [String], !paths.isEmpty {
            return paths.map { URL(fileURLWithPath: $0) }
        }

        return []
    }
}
