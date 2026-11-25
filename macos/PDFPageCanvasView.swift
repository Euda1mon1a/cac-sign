import AppKit
import CoreGraphics
import SwiftUI

// MARK: - Strictly Read-Only CoreGraphics PDF Renderer (Never Mutates or Rewrites PDFs)

@MainActor
public final class CGPDFDocumentCache {
    public static let shared = CGPDFDocumentCache()

    private struct CacheEntry {
        let modTime: TimeInterval
        let fileSize: UInt64
        let document: CGPDFDocument
    }

    private var cache: [String: CacheEntry] = [:]

    public func document(for url: URL) -> CGPDFDocument? {
        let filePath = url.path
        let attrs = try? FileManager.default.attributesOfItem(atPath: filePath)
        let modDate = (attrs?[.modificationDate] as? Date)?.timeIntervalSince1970 ?? 0
        let size = (attrs?[.size] as? NSNumber)?.uint64Value ?? 0

        if let hit = cache[filePath], hit.modTime == modDate, hit.fileSize == size {
            return hit.document
        }
        guard let doc = CGPDFDocument(url as CFURL) else { return nil }
        if cache.count > 24 {
            cache.removeAll()
        }
        cache[filePath] = CacheEntry(modTime: modDate, fileSize: size, document: doc)
        return doc
    }

    public func pageCount(for url: URL) -> Int {
        document(for: url)?.numberOfPages ?? 0
    }
}

@MainActor
private final class FlattenedPageImageCache {
    static let shared = FlattenedPageImageCache()
    private var images: [String: NSImage] = [:]

    func cacheKey(for url: URL, pageIndex: Int) -> String {
        let attrs = try? FileManager.default.attributesOfItem(atPath: url.path)
        let modDate = (attrs?[.modificationDate] as? Date)?.timeIntervalSince1970 ?? 0
        let size = (attrs?[.size] as? NSNumber)?.uint64Value ?? 0
        return "\(url.path)|\(modDate)|\(size)|\(pageIndex)"
    }

    func cached(for key: String) -> NSImage? {
        images[key]
    }

    func store(_ image: NSImage, for key: String) {
        if images.count > 64 {
            images.removeAll()
        }
        images[key] = image
    }
}

/// Renders a PDF page with all `/Annots`, form `/AP` appearances, and signature stamps flattened
/// in memory via `cac_sign_desktop.py` (`pdf_readers.render_page`), matching the 3-reader WYSIWYS gate.
public struct CGPDFPageView: View {
    public let pdfURL: URL
    public let pageIndex: Int
    @State private var flattenedImage: NSImage?

    public init(pdfURL: URL, pageIndex: Int) {
        self.pdfURL = pdfURL
        self.pageIndex = pageIndex
    }

    public var body: some View {
        let key = FlattenedPageImageCache.shared.cacheKey(for: pdfURL, pageIndex: pageIndex)
        ZStack {
            CGPDFVectorBackdropRepresentable(pdfURL: pdfURL, pageIndex: pageIndex)
            if let img = flattenedImage ?? FlattenedPageImageCache.shared.cached(for: key) {
                Image(nsImage: img)
                    .resizable()
                    .interpolation(.high)
            }
        }
        .task(id: key) {
            if let hit = FlattenedPageImageCache.shared.cached(for: key) {
                flattenedImage = hit
                return
            }
            if let pngData = try? await DesktopBridge.shared.renderPagePNG(pdfPath: pdfURL.path, page: pageIndex, scale: 2.0),
               let decoded = NSImage(data: pngData) {
                FlattenedPageImageCache.shared.store(decoded, for: key)
                flattenedImage = decoded
            }
        }
    }
}

private struct CGPDFVectorBackdropRepresentable: NSViewRepresentable {
    let pdfURL: URL
    let pageIndex: Int

    func makeNSView(context: Context) -> CGPDFRenderNSView {
        let view = CGPDFRenderNSView()
        view.pdfURL = pdfURL
        view.pageIndex = pageIndex
        return view
    }

    func updateNSView(_ nsView: CGPDFRenderNSView, context: Context) {
        if nsView.pdfURL != pdfURL || nsView.pageIndex != pageIndex {
            nsView.pdfURL = pdfURL
            nsView.pageIndex = pageIndex
            nsView.needsDisplay = true
        }
    }
}

public final class CGPDFRenderNSView: NSView {
    public var pdfURL: URL?
    public var pageIndex: Int = 0

    public override var isFlipped: Bool { false }

    public override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        layerContentsRedrawPolicy = .onSetNeedsDisplay
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    public override func setFrameSize(_ newSize: NSSize) {
        super.setFrameSize(newSize)
        needsDisplay = true
    }

    public override func draw(_ dirtyRect: NSRect) {
        guard let ctx = NSGraphicsContext.current?.cgContext else { return }
        ctx.saveGState()
        ctx.setFillColor(NSColor.white.cgColor)
        ctx.fill(bounds)

        if let url = pdfURL,
           let doc = CGPDFDocumentCache.shared.document(for: url),
           let page = doc.page(at: pageIndex + 1) {
            ctx.interpolationQuality = .high
            ctx.setAllowsAntialiasing(true)
            ctx.setShouldAntialias(true)
            let transform = page.getDrawingTransform(.cropBox, rect: bounds, rotate: 0, preserveAspectRatio: true)
            ctx.concatenate(transform)
            ctx.drawPDFPage(page)
        }

        ctx.restoreGState()
    }
}

// MARK: - Interactive PDF Page Stack Canvas

public struct PDFPageCanvasView: View {
    @ObservedObject public var vm: WorkspaceViewModel
    public let session: DocumentSession

    public init(vm: WorkspaceViewModel, session: DocumentSession) {
        self.vm = vm
        self.session = session
    }

    public var body: some View {
        let pdfURL = URL(fileURLWithPath: session.inspected.pdf_path)
        ScrollView([.vertical, .horizontal]) {
            LazyVStack(spacing: 24) {
                ForEach(Array(session.inspected.pages.enumerated()), id: \.offset) { pageIndex, pageSizeModel in
                    VStack(alignment: .leading, spacing: 6) {
                        HStack {
                            Text("Page \(pageIndex + 1) of \(session.inspected.pages.count)")
                                .font(.caption.weight(.medium))
                                .foregroundStyle(.secondary)
                            Spacer()
                            if vm.activeTool != .select && !session.isBlocked && session.signedOutputURL == nil {
                                Label("Drag on page to place \(vm.activeTool.label)", systemImage: vm.activeTool.systemImage)
                                    .font(.caption.weight(.semibold))
                                    .foregroundStyle(.indigo)
                            }
                        }
                        .frame(width: pageSizeModel.w * vm.zoomScale)

                        PDFSinglePageInteractiveView(
                            vm: vm,
                            session: session,
                            pdfURL: pdfURL,
                            pageIndex: pageIndex,
                            scaledSize: CGSize(
                                width: pageSizeModel.w * vm.zoomScale,
                                height: pageSizeModel.h * vm.zoomScale
                            )
                        )
                    }
                }
            }
            .padding(28)
            .frame(maxWidth: .infinity)
        }
        .background(Color(nsColor: .underPageBackgroundColor))
    }
}

// MARK: - Single Page Interactive Layer (Widgets, Rubber-Band Creation,Placed Signature)

private struct PDFSinglePageInteractiveView: View {
    @ObservedObject var vm: WorkspaceViewModel
    let session: DocumentSession
    let pdfURL: URL
    let pageIndex: Int
    let scaledSize: CGSize

    @State private var dragStart: CGPoint?
    @State private var dragCurrent: CGPoint?

    var body: some View {
        ZStack(alignment: .topLeading) {
            CGPDFPageView(pdfURL: pdfURL, pageIndex: pageIndex)
                .frame(width: scaledSize.width, height: scaledSize.height)

            // Form & Added Field Widgets on this page
            ForEach(session.inspected.fields) { field in
                ForEach(Array(field.widgets.enumerated()), id: \.offset) { wIdx, widget in
                    if widget.page == pageIndex {
                        FieldWidgetOverlayView(
                            vm: vm,
                            session: session,
                            field: field,
                            widget: widget,
                            widgetIndex: wIdx,
                            pageIndex: pageIndex,
                            scaledSize: scaledSize
                        )
                    }
                }
            }

            // Placed / Suggested Signature Box (when no native signature field is selected)
            if session.selectedSignField == nil,
               let sigBox = session.placedSignature,
               sigBox.page == pageIndex {
                PlacedSignatureOverlayView(
                    vm: vm,
                    session: session,
                    sigBox: sigBox,
                    pageIndex: pageIndex,
                    scaledSize: scaledSize
                )
            }

            // Rubber-Band Creation Tool Layer
            if vm.activeTool != .select && !session.isBlocked && session.signedOutputURL == nil {
                Color.clear
                    .contentShape(Rectangle())
                    .frame(width: scaledSize.width, height: scaledSize.height)
                    .gesture(
                        DragGesture(minimumDistance: 2, coordinateSpace: .local)
                            .onChanged { val in
                                if dragStart == nil { dragStart = val.startLocation }
                                dragCurrent = val.location
                            }
                            .onEnded { val in
                                let start = dragStart ?? val.startLocation
                                let end = val.location
                                commitRubberBand(from: start, to: end)
                                dragStart = nil
                                dragCurrent = nil
                            }
                    )

                if let s = dragStart, let c = dragCurrent {
                    let rect = rubberBandRect(from: s, to: c, for: vm.activeTool)
                    RoundedRectangle(cornerRadius: 4)
                        .fill(Color.indigo.opacity(0.14))
                        .overlay(
                            RoundedRectangle(cornerRadius: 4)
                                .strokeBorder(Color.indigo, style: StrokeStyle(lineWidth: 1.5, dash: [5, 3]))
                        )
                        .overlay(alignment: .topLeading) {
                            Text(vm.activeTool.label)
                                .font(.caption2.weight(.semibold))
                                .foregroundStyle(.white)
                                .padding(.horizontal, 5)
                                .padding(.vertical, 2)
                                .background(Capsule().fill(Color.indigo))
                                .offset(y: -18)
                        }
                        .frame(width: rect.width, height: rect.height)
                        .offset(x: rect.minX, y: rect.minY)
                        .allowsHitTesting(false)
                }
            }
        }
        .frame(width: scaledSize.width, height: scaledSize.height)
        .background(Color.white)
        .clipShape(RoundedRectangle(cornerRadius: 3))
        .overlay(
            RoundedRectangle(cornerRadius: 3)
                .strokeBorder(Color.primary.opacity(0.14), lineWidth: 1)
        )
        .shadow(color: .black.opacity(0.15), radius: 6, y: 2)
    }

    private func rubberBandRect(from p1: CGPoint, to p2: CGPoint, for tool: CanvasTool) -> CGRect {
        let rawMinX = min(p1.x, p2.x)
        let rawMinY = min(p1.y, p2.y)
        let rawW = abs(p2.x - p1.x)
        let rawH = abs(p2.y - p1.y)

        let minW: CGFloat
        let minH: CGFloat
        switch tool {
        case .addCheck:
            minW = 18 * vm.zoomScale
            minH = 18 * vm.zoomScale
        case .drawSignature:
            minW = 160 * vm.zoomScale
            minH = 44 * vm.zoomScale
        default:
            minW = 120 * vm.zoomScale
            minH = 22 * vm.zoomScale
        }

        let w = max(rawW, minW)
        let h = max(rawH, minH)
        let clampedX = max(0, min(scaledSize.width - w, rawMinX))
        let clampedY = max(0, min(scaledSize.height - h, rawMinY))
        return CGRect(x: clampedX, y: clampedY, width: w, height: h)
    }

    private func commitRubberBand(from p1: CGPoint, to p2: CGPoint) {
        let rect = rubberBandRect(from: p1, to: p2, for: vm.activeTool)
        let normBox = DisplayBoxGeometry.normalizedBox(for: rect, in: scaledSize)
        switch vm.activeTool {
        case .addText:
            vm.addOverlayField(kind: "text", page: pageIndex, box: normBox)
        case .addCheck:
            vm.addOverlayField(kind: "checkbox", page: pageIndex, box: normBox)
        case .drawSignature:
            vm.setPlacedSignature(page: pageIndex, box: normBox)
        case .select:
            break
        }
    }
}

// MARK: - Field Widget Overlay & Placed Signature Views

private struct FieldWidgetOverlayView: View {
    @ObservedObject var vm: WorkspaceViewModel
    let session: DocumentSession
    let field: FormFieldModel
    let widget: WidgetPlacement
    let widgetIndex: Int
    let pageIndex: Int
    let scaledSize: CGSize

    @State private var isHovered: Bool = false
    @State private var moveOffset: CGSize = .zero
    @State private var resizeDelta: CGSize = .zero
    @FocusState private var isTextFocused: Bool

    private var isSelected: Bool { vm.selectedFieldName == field.name }
    private var isAdvisory: Bool { session.advisoryFields.contains(field.name) }
    private var isChanged: Bool { session.isValueChanged(for: field) }
    private var rendersOverlayValue: Bool { isChanged || field.added == true }
    private var currentVal: FormValue { session.currentValue(for: field) }
    private var isReadOnlyDoc: Bool { session.isBlocked || session.signedOutputURL != nil }

    var body: some View {
        let baseRect = DisplayBoxGeometry.viewRect(for: widget.box, in: scaledSize)
        let activeRect = CGRect(
            x: max(0, min(scaledSize.width - 14, baseRect.minX + moveOffset.width)),
            y: max(0, min(scaledSize.height - 14, baseRect.minY + moveOffset.height)),
            width: max(16, min(scaledSize.width - baseRect.minX, baseRect.width + resizeDelta.width)),
            height: max(14, min(scaledSize.height - baseRect.minY, baseRect.height + resizeDelta.height))
        )

        widgetControl(for: activeRect)
            .frame(width: activeRect.width, height: activeRect.height)
            .overlay(alignment: .topLeading) {
                if isAdvisory && widgetIndex == 0 {
                    Button {
                        vm.confirmAdvisoryField(field.name)
                    } label: {
                        HStack(spacing: 3) {
                            Image(systemName: "exclamationmark.triangle.fill")
                            Text("Advisory — Verify")
                        }
                        .font(.system(size: 9, weight: .bold))
                        .foregroundStyle(.white)
                        .padding(.horizontal, 5)
                        .padding(.vertical, 1.5)
                        .background(Capsule().fill(Color.orange))
                    }
                    .buttonStyle(.plain)
                    .offset(y: -15)
                    .help("Click to confirm advisory text or edit directly")
                }
            }
            .overlay(alignment: .topTrailing) {
                if field.added == true && !isReadOnlyDoc && (isHovered || isSelected) {
                    HStack(spacing: 3) {
                        Image(systemName: "arrow.up.and.down.and.arrow.left.and.right")
                            .font(.system(size: 9, weight: .bold))
                            .foregroundStyle(.white)
                            .padding(3.5)
                            .background(Circle().fill(Color.indigo))
                            .gesture(
                                DragGesture(minimumDistance: 1)
                                    .onChanged { g in moveOffset = g.translation }
                                    .onEnded { g in
                                        let moved = CGRect(
                                            x: baseRect.minX + g.translation.width,
                                            y: baseRect.minY + g.translation.height,
                                            width: baseRect.width,
                                            height: baseRect.height
                                        )
                                        moveOffset = .zero
                                        let norm = DisplayBoxGeometry.normalizedBox(for: moved, in: scaledSize)
                                        vm.updateAddedFieldBox(name: field.name, page: pageIndex, box: norm, recordUndo: true)
                                    }
                            )

                        Button {
                            vm.removeAddedField(name: field.name)
                        } label: {
                            Text("×")
                                .font(.system(size: 11, weight: .bold))
                                .foregroundStyle(.white)
                                .frame(width: 16, height: 16)
                                .background(Circle().fill(Color.red))
                        }
                        .buttonStyle(.plain)
                    }
                    .offset(x: 6, y: -14)
                }
            }
            .overlay(alignment: .bottomTrailing) {
                if field.added == true && !isReadOnlyDoc && (isHovered || isSelected) {
                    RoundedRectangle(cornerRadius: 2)
                        .fill(Color.indigo)
                        .frame(width: 9, height: 9)
                        .offset(x: 4, y: 4)
                        .gesture(
                            DragGesture(minimumDistance: 1)
                                .onChanged { g in resizeDelta = g.translation }
                                .onEnded { g in
                                    let resized = CGRect(
                                        x: baseRect.minX,
                                        y: baseRect.minY,
                                        width: max(18, baseRect.width + g.translation.width),
                                        height: max(16, baseRect.height + g.translation.height)
                                    )
                                    resizeDelta = .zero
                                    let norm = DisplayBoxGeometry.normalizedBox(for: resized, in: scaledSize)
                                    vm.updateAddedFieldBox(name: field.name, page: pageIndex, box: norm, recordUndo: true)
                                }
                        )
                }
            }
            .offset(x: activeRect.minX, y: activeRect.minY)
            .onHover { hovering in isHovered = hovering }
            .simultaneousGesture(
                TapGesture().onEnded {
                    vm.selectedFieldName = field.name
                }
            )
    }

    @ViewBuilder
    private func widgetControl(for rect: CGRect) -> some View {
        switch field.kind {
        case "signature":
            signatureWidgetView
        case "checkbox":
            checkboxWidgetView
        case "radio":
            radioWidgetView
        case "choice":
            choiceWidgetView(for: rect)
        default:
            textWidgetView(for: rect)
        }
    }

    @ViewBuilder
    private var signatureWidgetView: some View {
        if let sig = field.signature {
            HStack(spacing: 4) {
                Image(systemName: sig.ok ? "checkmark.seal.fill" : "exclamationmark.triangle.fill")
                    .font(.caption)
                Text(sig.ok ? "✓ Signed by \(sig.signer)" : "⚠ \(sig.signer)")
                    .font(.system(size: max(9, 10 * vm.zoomScale), weight: .semibold))
                    .lineLimit(1)
            }
            .foregroundStyle(sig.ok ? Color.green : Color.red)
            .padding(.horizontal, 6)
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
            .background((sig.ok ? Color.green : Color.red).opacity(0.13))
            .overlay(
                RoundedRectangle(cornerRadius: 3)
                    .strokeBorder(sig.ok ? Color.green : Color.red, lineWidth: 1.5)
            )
            .help(sig.detail.isEmpty ? "\(sig.signer) (\(sig.when))" : sig.detail)
        } else {
            let isTarget = (session.selectedSignField == field.name)
            Button {
                guard !isReadOnlyDoc else { return }
                vm.selectSignatureField(field.name)
            } label: {
                HStack(spacing: 4) {
                    Image(systemName: "signature")
                    Text(isTarget ? "Sign Here" : field.displayLabel)
                        .lineLimit(1)
                }
                .font(.system(size: max(9, 10.5 * vm.zoomScale), weight: .semibold))
                .foregroundStyle(isTarget ? Color.white : Color.indigo)
                .padding(.horizontal, 6)
                .padding(.vertical, 2)
                .background(
                    Capsule()
                        .fill(isTarget ? Color.indigo : Color.indigo.opacity(0.12))
                )
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(isTarget ? Color.indigo.opacity(0.14) : Color.indigo.opacity(0.04))
                .overlay(
                    RoundedRectangle(cornerRadius: 3)
                        .strokeBorder(
                            Color.indigo.opacity(isTarget ? 0.95 : 0.45),
                            style: StrokeStyle(lineWidth: isTarget ? 2 : 1, dash: [5, 3])
                        )
                )
            }
            .buttonStyle(.plain)
        }
    }

    private var checkboxWidgetView: some View {
        let checked = currentVal.boolValue
        let interactive = field.isInteractiveInput && !isReadOnlyDoc
        return Button {
            guard interactive else { return }
            vm.selectedFieldName = field.name
            vm.setFieldValue(field.name, value: .bool(!checked))
        } label: {
            ZStack {
                RoundedRectangle(cornerRadius: 2.5)
                    .fill(rendersOverlayValue ? Color.white.opacity(0.95) : Color.blue.opacity(0.05))
                RoundedRectangle(cornerRadius: 2.5)
                    .strokeBorder(borderColor, lineWidth: isSelected ? 2 : 1.2)
                if rendersOverlayValue && checked {
                    Image(systemName: "checkmark")
                        .font(.system(size: max(9, 11 * vm.zoomScale), weight: .bold))
                        .foregroundStyle(Color.black)
                } else if !field.isInteractiveInput {
                    Image(systemName: "lock.fill")
                        .font(.system(size: 8))
                        .foregroundStyle(.secondary)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .buttonStyle(.plain)
    }

    private var radioWidgetView: some View {
        let opt = widget.option ?? ""
        let checked = !opt.isEmpty && currentVal.stringValue == opt
        let interactive = field.isInteractiveInput && !isReadOnlyDoc
        return Button {
            guard interactive else { return }
            vm.selectedFieldName = field.name
            vm.setFieldValue(field.name, value: .text(opt))
        } label: {
            ZStack {
                Circle()
                    .fill(rendersOverlayValue ? Color.white.opacity(0.95) : Color.blue.opacity(0.05))
                Circle()
                    .strokeBorder(borderColor, lineWidth: isSelected ? 2 : 1.2)
                if rendersOverlayValue && checked {
                    Circle()
                        .fill(Color.black)
                        .padding(max(3, 4 * vm.zoomScale))
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .buttonStyle(.plain)
        .help("\(field.displayLabel): \(opt)")
    }

    private func choiceWidgetView(for rect: CGRect) -> some View {
        let choices: [(val: String, label: String)] = {
            if let co = field.choice_options, !co.isEmpty {
                return co.map { ($0.value, $0.label) }
            }
            return (field.options ?? []).map { ($0, $0) }
        }()
        let curText = currentVal.stringValue
        let interactive = field.isInteractiveInput && !isReadOnlyDoc

        return Menu {
            if interactive {
                Button("(Blank)") {
                    vm.setFieldValue(field.name, value: .text(""))
                }
                Divider()
                ForEach(choices, id: \.val) { item in
                    Button(item.label) {
                        vm.setFieldValue(field.name, value: .text(item.val))
                    }
                }
            }
        } label: {
            HStack(spacing: 3) {
                if rendersOverlayValue {
                    Text(curText.isEmpty ? "Select…" : curText)
                        .font(.system(size: max(9, min(13, rect.height * 0.62))))
                        .foregroundStyle(curText.isEmpty ? .secondary : Color.black)
                        .lineLimit(1)
                }
                Spacer(minLength: 2)
                Image(systemName: interactive ? "chevron.up.chevron.down" : "lock.fill")
                    .font(.system(size: 8))
                    .foregroundStyle(.secondary)
            }
            .padding(.horizontal, 4)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(fieldBackground)
            .overlay(RoundedRectangle(cornerRadius: 2.5).strokeBorder(borderColor, lineWidth: isSelected ? 1.8 : 1))
        }
        .menuStyle(.borderlessButton)
        .disabled(!interactive)
    }

    @ViewBuilder
    private func textWidgetView(for rect: CGRect) -> some View {
        let interactive = field.isInteractiveInput && !isReadOnlyDoc
        let fontSize = max(9, min(14 * vm.zoomScale, rect.height * 0.66))

        if interactive && (rendersOverlayValue || isTextFocused || isSelected) {
            TextField(
                "",
                text: Binding(
                    get: { currentVal.stringValue },
                    set: { newStr in vm.setFieldValue(field.name, value: .text(newStr)) }
                ),
                axis: field.multiline ? .vertical : .horizontal
            )
            .focused($isTextFocused)
            .textFieldStyle(.plain)
            .font(.system(size: fontSize))
            .foregroundStyle(Color.black)
            .padding(.horizontal, 3)
            .padding(.vertical, 1)
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: field.multiline ? .topLeading : .leading)
            .background(Color.white.opacity(0.96))
            .overlay(
                RoundedRectangle(cornerRadius: 2.5)
                    .strokeBorder(borderColor, lineWidth: (isSelected || isAdvisory) ? 1.8 : 1)
            )
        } else if interactive {
            Button {
                vm.selectedFieldName = field.name
                isTextFocused = true
            } label: {
                Color.clear
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .background(fieldBackground)
                    .overlay(
                        RoundedRectangle(cornerRadius: 2.5)
                            .strokeBorder(borderColor, lineWidth: 1)
                    )
            }
            .buttonStyle(.plain)
        } else {
            HStack(spacing: 3) {
                Spacer(minLength: 0)
                if field.locked || field.readonly || !field.editable {
                    Image(systemName: "lock.fill")
                        .font(.system(size: 8.5))
                        .foregroundStyle(.secondary)
                        .padding(2)
                }
            }
            .padding(.horizontal, 3)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(Color.gray.opacity(0.04))
            .overlay(
                RoundedRectangle(cornerRadius: 2.5)
                    .strokeBorder(Color.secondary.opacity(0.28), lineWidth: 0.8)
            )
        }
    }

    private var borderColor: Color {
        if isAdvisory { return .orange }
        if isSelected { return .accentColor }
        if isChanged { return .green }
        if currentVal.isBlank(forKind: field.kind) { return Color.blue.opacity(0.42) }
        return Color.secondary.opacity(0.32)
    }

    private var fieldBackground: Color {
        if isAdvisory { return Color.orange.opacity(0.18) }
        if isChanged || field.added == true { return Color.white.opacity(0.95) }
        if currentVal.isBlank(forKind: field.kind) { return Color.blue.opacity(0.07) }
        return Color.white.opacity(0.01)
    }
}

private struct PlacedSignatureOverlayView: View {
    @ObservedObject var vm: WorkspaceViewModel
    let session: DocumentSession
    let sigBox: SuggestedSignatureBox
    let pageIndex: Int
    let scaledSize: CGSize

    @State private var moveOffset: CGSize = .zero
    @State private var resizeDelta: CGSize = .zero

    private var isReadOnlyDoc: Bool { session.isBlocked || session.signedOutputURL != nil }

    var body: some View {
        let baseRect = DisplayBoxGeometry.viewRect(for: sigBox.box, in: scaledSize)
        let activeRect = CGRect(
            x: max(0, min(scaledSize.width - 40, baseRect.minX + moveOffset.width)),
            y: max(0, min(scaledSize.height - 24, baseRect.minY + moveOffset.height)),
            width: max(60, min(scaledSize.width - baseRect.minX, baseRect.width + resizeDelta.width)),
            height: max(24, min(scaledSize.height - baseRect.minY, baseRect.height + resizeDelta.height))
        )

        ZStack {
            RoundedRectangle(cornerRadius: 4)
                .fill(Color.indigo.opacity(0.14))
            RoundedRectangle(cornerRadius: 4)
                .strokeBorder(Color.indigo, style: StrokeStyle(lineWidth: 2, dash: [6, 3]))
            HStack(spacing: 5) {
                Image(systemName: "signature")
                Text(sigBox.label ?? "Sign Here")
                    .lineLimit(1)
            }
            .font(.system(size: max(10, 11 * vm.zoomScale), weight: .bold))
            .foregroundStyle(.white)
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .background(Capsule().fill(Color.indigo))
        }
        .frame(width: activeRect.width, height: activeRect.height)
        .gesture(
            DragGesture(minimumDistance: 2)
                .onChanged { g in
                    guard !isReadOnlyDoc else { return }
                    moveOffset = g.translation
                }
                .onEnded { g in
                    guard !isReadOnlyDoc else { return }
                    let moved = CGRect(
                        x: baseRect.minX + g.translation.width,
                        y: baseRect.minY + g.translation.height,
                        width: baseRect.width,
                        height: baseRect.height
                    )
                    moveOffset = .zero
                    let norm = DisplayBoxGeometry.normalizedBox(for: moved, in: scaledSize)
                    vm.setPlacedSignature(page: pageIndex, box: norm, recordUndo: true)
                }
        )
        .overlay(alignment: .topTrailing) {
            if !isReadOnlyDoc {
                Button {
                    vm.clearPlacedSignature()
                } label: {
                    Text("×")
                        .font(.system(size: 11, weight: .bold))
                        .foregroundStyle(.white)
                        .frame(width: 17, height: 17)
                        .background(Circle().fill(Color.red))
                }
                .buttonStyle(.plain)
                .offset(x: 6, y: -6)
            }
        }
        .overlay(alignment: .bottomTrailing) {
            if !isReadOnlyDoc {
                RoundedRectangle(cornerRadius: 2)
                    .fill(Color.indigo)
                    .frame(width: 10, height: 10)
                    .offset(x: 4, y: 4)
                    .gesture(
                        DragGesture(minimumDistance: 1)
                            .onChanged { g in resizeDelta = g.translation }
                            .onEnded { g in
                                let resized = CGRect(
                                    x: baseRect.minX,
                                    y: baseRect.minY,
                                    width: max(60, baseRect.width + g.translation.width),
                                    height: max(24, baseRect.height + g.translation.height)
                                )
                                resizeDelta = .zero
                                let norm = DisplayBoxGeometry.normalizedBox(for: resized, in: scaledSize)
                                vm.setPlacedSignature(page: pageIndex, box: norm, recordUndo: true)
                            }
                    )
            }
        }
        .offset(x: activeRect.minX, y: activeRect.minY)
    }
}
