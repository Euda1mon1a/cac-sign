import AppKit
import SwiftUI

public struct PostSignActionBar: View {
    public let session: DocumentSession
    @State private var showQuickLookSheet: Bool = false
    @State private var showNoMailAlert: Bool = false

    public init(session: DocumentSession) {
        self.session = session
    }

    public var body: some View {
        if let signedURL = session.signedOutputURL {
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 12) {
                    Image(systemName: "checkmark.seal.fill")
                        .font(.title2)
                        .foregroundStyle(.white)
                        .padding(6)
                        .background(Circle().fill(Color.green.gradient))

                    VStack(alignment: .leading, spacing: 2) {
                        HStack(spacing: 6) {
                            Text("Signed & Verified")
                                .font(.headline)
                                .foregroundStyle(.primary)
                            if let firstSig = session.signedSummaries.first {
                                Text("• \(firstSig.signer)")
                                    .font(.subheadline)
                                    .foregroundStyle(.secondary)
                            }
                        }
                        Text(signedURL.lastPathComponent)
                            .font(.caption.monospaced())
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                    }

                    Spacer(minLength: 8)

                    draggablePill(for: signedURL)

                    Button {
                        showQuickLookSheet = true
                    } label: {
                        Label("Quick Look", systemImage: "eye")
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.small)

                    Button {
                        NSWorkspace.shared.activateFileViewerSelecting([signedURL])
                    } label: {
                        Label("Reveal in Finder", systemImage: "folder")
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.small)

                    Button {
                        draftEmail(with: signedURL)
                    } label: {
                        Label("Draft Email…", systemImage: "envelope")
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                }

                if let nextStep = session.inspected.next_step, !nextStep.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    HStack(spacing: 6) {
                        Image(systemName: "arrow.turn.down.right")
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(.green)
                        Text("Next step: \(nextStep)")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    .padding(.leading, 44)
                }
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .background(
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .fill(Color.green.opacity(0.12))
                    .overlay(
                        RoundedRectangle(cornerRadius: 10, style: .continuous)
                            .strokeBorder(Color.green.opacity(0.45), lineWidth: 1)
                    )
            )
            .padding(.horizontal, 12)
            .padding(.top, 8)
            .sheet(isPresented: $showQuickLookSheet) {
                SignedPDFQuickLookSheet(url: signedURL, pageModels: session.inspected.pages)
            }
            .alert("No Mail Client Configured", isPresented: $showNoMailAlert) {
                Button("Reveal in Finder") {
                    NSWorkspace.shared.activateFileViewerSelecting([signedURL])
                }
                Button("OK", role: .cancel) {}
            } message: {
                Text("macOS could not open an email draft automatically. Reveal \(signedURL.lastPathComponent) in Finder to attach it manually.")
            }
        }
    }

    private func draggablePill(for url: URL) -> some View {
        HStack(spacing: 6) {
            Image(systemName: "doc.badge.arrow.up.fill")
                .foregroundStyle(.white)
            VStack(alignment: .leading, spacing: 0) {
                Text("Drag Signed PDF to Browser / EHR")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.white)
                Text(url.lastPathComponent)
                    .font(.caption2)
                    .foregroundStyle(.white.opacity(0.88))
                    .lineLimit(1)
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .background(
            Capsule(style: .continuous)
                .fill(Color.green.gradient)
                .shadow(color: .black.opacity(0.12), radius: 2, y: 1)
        )
        .onDrag {
            NSItemProvider(contentsOf: url) ?? NSItemProvider()
        }
        .help("Drag and drop \(url.lastPathComponent) directly into your EHR, web upload portals, or email")
    }

    private func draftEmail(with url: URL) {
        guard let service = NSSharingService(named: .composeEmail),
              service.canPerform(withItems: [url]) else {
            showNoMailAlert = true
            return
        }
        let stem = url.deletingPathExtension().lastPathComponent
        service.subject = "Signed Form: \(stem)"
        let messageBody = "Attached is the CAC-signed PDF (\(url.lastPathComponent)). Please review before sending."
        service.perform(withItems: [messageBody, url])
    }
}

private struct SignedPDFQuickLookSheet: View {
    let url: URL
    let pageModels: [PageSizeModel]
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Label(url.lastPathComponent, systemImage: "doc.text.magnifyingglass")
                    .font(.headline)
                Spacer()
                Button("Open in Preview") {
                    NSWorkspace.shared.open(url)
                }
                .buttonStyle(.bordered)
                Button("Done") {
                    dismiss()
                }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.cancelAction)
            }
            .padding(12)
            .background(.ultraThinMaterial)

            Divider()

            ScrollView {
                LazyVStack(spacing: 16) {
                    let count = max(1, CGPDFDocumentCache.shared.pageCount(for: url))
                    ForEach(0..<count, id: \.self) { idx in
                        let sizeModel = pageModels.indices.contains(idx) ? pageModels[idx] : PageSizeModel(w: 612, h: 792)
                        CGPDFPageView(pdfURL: url, pageIndex: idx)
                            .frame(width: sizeModel.w * 0.9, height: sizeModel.h * 0.9)
                            .background(Color.white)
                            .clipShape(RoundedRectangle(cornerRadius: 4))
                            .shadow(color: .black.opacity(0.14), radius: 5, y: 2)
                    }
                }
                .padding(20)
            }
            .background(Color(nsColor: .underPageBackgroundColor))
        }
        .frame(minWidth: 700, minHeight: 620)
    }
}
