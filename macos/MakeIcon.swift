import AppKit
import Foundation

// Standalone icon generator script (not compiled into the main CACSign.app binary).
// Usage: swift macos/MakeIcon.swift /path/to/out.png

guard CommandLine.arguments.count >= 2 else {
    fputs("Usage: MakeIcon.swift <output-png-path>\n", stderr)
    exit(2)
}

let outputURL = URL(fileURLWithPath: CommandLine.arguments[1])
let parentDir = outputURL.deletingLastPathComponent()
try FileManager.default.createDirectory(at: parentDir, withIntermediateDirectories: true)

let canvasPx = 1024
let size = CGFloat(canvasPx)
guard let bitmap = NSBitmapImageRep(
    bitmapDataPlanes: nil,
    pixelsWide: canvasPx,
    pixelsHigh: canvasPx,
    bitsPerSample: 8,
    samplesPerPixel: 4,
    hasAlpha: true,
    isPlanar: false,
    colorSpaceName: .deviceRGB,
    bytesPerRow: 0,
    bitsPerPixel: 0
), let graphicsContext = NSGraphicsContext(bitmapImageRep: bitmap) else {
    fputs("Failed to allocate 1024x1024 bitmap context.\n", stderr)
    exit(1)
}

NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = graphicsContext
let cg = graphicsContext.cgContext
cg.setAllowsAntialiasing(true)
cg.setShouldAntialias(true)

// Clear transparent outer frame.
NSColor.clear.setFill()
NSRect(x: 0, y: 0, width: size, height: size).fill()

// 1. macOS squircle base in deep maritime slate gradient.
let squircleRect = NSRect(x: 92, y: 92, width: 840, height: 840)
let squircleRadius: CGFloat = 190
let squirclePath = NSBezierPath(roundedRect: squircleRect, xRadius: squircleRadius, yRadius: squircleRadius)

NSGraphicsContext.saveGraphicsState()
let outerShadow = NSShadow()
outerShadow.shadowColor = NSColor(calibratedWhite: 0.0, alpha: 0.36)
outerShadow.shadowBlurRadius = 34
outerShadow.shadowOffset = NSSize(width: 0, height: -14)
outerShadow.set()
NSColor(calibratedRed: 0.06, green: 0.13, blue: 0.20, alpha: 1.0).setFill()
squirclePath.fill()
NSGraphicsContext.restoreGraphicsState()

NSGraphicsContext.saveGraphicsState()
squirclePath.addClip()
let slateGradient = NSGradient(colors: [
    NSColor(calibratedRed: 0.13, green: 0.27, blue: 0.38, alpha: 1.0),
    NSColor(calibratedRed: 0.08, green: 0.17, blue: 0.26, alpha: 1.0),
    NSColor(calibratedRed: 0.05, green: 0.11, blue: 0.18, alpha: 1.0)
])!
slateGradient.draw(in: squircleRect, angle: -65)

// Subtle upper-left maritime glow.
let glowGradient = NSGradient(colors: [
    NSColor(calibratedRed: 0.25, green: 0.50, blue: 0.66, alpha: 0.24),
    NSColor(calibratedRed: 0.10, green: 0.22, blue: 0.32, alpha: 0.0)
])!
glowGradient.draw(
    fromCenter: NSPoint(x: 270, y: 780),
    radius: 20,
    toCenter: NSPoint(x: 340, y: 700),
    radius: 520,
    options: []
)
NSGraphicsContext.restoreGraphicsState()

// Crisp inner rim highlight.
NSColor(calibratedWhite: 1.0, alpha: 0.14).setStroke()
squirclePath.lineWidth = 4
squirclePath.stroke()

// 2. Clean ivory document sheet with folded top-right corner.
let docX: CGFloat = 292
let docY: CGFloat = 204
let docW: CGFloat = 468
let docH: CGFloat = 604
let fold: CGFloat = 96
let cornerR: CGFloat = 34

let docPath = NSBezierPath()
docPath.move(to: NSPoint(x: docX + cornerR, y: docY))
docPath.line(to: NSPoint(x: docX + docW - cornerR, y: docY))
docPath.appendArc(withCenter: NSPoint(x: docX + docW - cornerR, y: docY + cornerR), radius: cornerR, startAngle: 270, endAngle: 360)
docPath.line(to: NSPoint(x: docX + docW, y: docY + docH - fold))
docPath.line(to: NSPoint(x: docX + docW - fold, y: docY + docH))
docPath.line(to: NSPoint(x: docX + cornerR, y: docY + docH))
docPath.appendArc(withCenter: NSPoint(x: docX + cornerR, y: docY + docH - cornerR), radius: cornerR, startAngle: 90, endAngle: 180)
docPath.line(to: NSPoint(x: docX, y: docY + cornerR))
docPath.appendArc(withCenter: NSPoint(x: docX + cornerR, y: docY + cornerR), radius: cornerR, startAngle: 180, endAngle: 270)
docPath.close()

NSGraphicsContext.saveGraphicsState()
let docShadow = NSShadow()
docShadow.shadowColor = NSColor(calibratedRed: 0.02, green: 0.05, blue: 0.09, alpha: 0.45)
docShadow.shadowBlurRadius = 28
docShadow.shadowOffset = NSSize(width: 0, height: -12)
docShadow.set()
NSColor(calibratedRed: 0.98, green: 0.97, blue: 0.93, alpha: 1.0).setFill()
docPath.fill()
NSGraphicsContext.restoreGraphicsState()

NSGraphicsContext.saveGraphicsState()
docPath.addClip()
let ivoryGrad = NSGradient(colors: [
    NSColor(calibratedRed: 0.99, green: 0.98, blue: 0.95, alpha: 1.0),
    NSColor(calibratedRed: 0.94, green: 0.92, blue: 0.87, alpha: 1.0)
])!
ivoryGrad.draw(in: NSRect(x: docX, y: docY, width: docW, height: docH), angle: -90)
NSGraphicsContext.restoreGraphicsState()

// Folded dog-ear flap.
let flap = NSBezierPath()
flap.move(to: NSPoint(x: docX + docW - fold, y: docY + docH))
flap.line(to: NSPoint(x: docX + docW, y: docY + docH - fold))
flap.line(to: NSPoint(x: docX + docW - fold + 18, y: docY + docH - fold))
flap.appendArc(
    withCenter: NSPoint(x: docX + docW - fold + 18, y: docY + docH - fold + 18),
    radius: 18,
    startAngle: 270,
    endAngle: 180,
    clockwise: true
)
flap.close()
NSColor(calibratedRed: 0.86, green: 0.84, blue: 0.78, alpha: 1.0).setFill()
flap.fill()

// Subtle form lines and signature baseline on the document.
NSColor(calibratedRed: 0.62, green: 0.68, blue: 0.73, alpha: 0.55).setFill()
for (rowIdx, widthFrac) in [0.56, 0.72, 0.66, 0.48].enumerated() {
    let lineY = docY + docH - 168 - CGFloat(rowIdx) * 56
    let lineRect = NSRect(x: docX + 64, y: lineY, width: (docW - 128) * CGFloat(widthFrac), height: 14)
    NSBezierPath(roundedRect: lineRect, xRadius: 7, yRadius: 7).fill()
}
NSColor(calibratedRed: 0.24, green: 0.35, blue: 0.44, alpha: 0.75).setFill()
NSBezierPath(roundedRect: NSRect(x: docX + 64, y: docY + 112, width: 224, height: 10), xRadius: 5, yRadius: 5).fill()

// 3. Gold PIV smart-card contact pad motif (ISO 7816 metallic chip badge on left).
let chipRect = NSRect(x: 190, y: 454, width: 174, height: 138)
let chipPath = NSBezierPath(roundedRect: chipRect, xRadius: 28, yRadius: 28)

NSGraphicsContext.saveGraphicsState()
let chipShadow = NSShadow()
chipShadow.shadowColor = NSColor(calibratedRed: 0.03, green: 0.07, blue: 0.12, alpha: 0.50)
chipShadow.shadowBlurRadius = 18
chipShadow.shadowOffset = NSSize(width: 0, height: -8)
chipShadow.set()
NSColor(calibratedRed: 0.85, green: 0.67, blue: 0.27, alpha: 1.0).setFill()
chipPath.fill()
NSGraphicsContext.restoreGraphicsState()

NSGraphicsContext.saveGraphicsState()
chipPath.addClip()
let goldGrad = NSGradient(colors: [
    NSColor(calibratedRed: 0.96, green: 0.83, blue: 0.46, alpha: 1.0),
    NSColor(calibratedRed: 0.84, green: 0.64, blue: 0.24, alpha: 1.0),
    NSColor(calibratedRed: 0.72, green: 0.51, blue: 0.16, alpha: 1.0)
])!
goldGrad.draw(in: chipRect, angle: -45)

// PIV chip contact grooves.
let grooveColor = NSColor(calibratedRed: 0.44, green: 0.30, blue: 0.08, alpha: 0.72)
grooveColor.setStroke()
let centerWell = NSBezierPath(
    roundedRect: NSRect(x: chipRect.minX + 52, y: chipRect.minY + 30, width: 70, height: 78),
    xRadius: 12,
    yRadius: 12
)
centerWell.lineWidth = 4.5
centerWell.stroke()

let grooves = NSBezierPath()
for offset in [CGFloat(42), CGFloat(96)] {
    grooves.move(to: NSPoint(x: chipRect.minX + 10, y: chipRect.minY + offset))
    grooves.line(to: NSPoint(x: chipRect.minX + 52, y: chipRect.minY + offset))
    grooves.move(to: NSPoint(x: chipRect.maxX - 52, y: chipRect.minY + offset))
    grooves.line(to: NSPoint(x: chipRect.maxX - 10, y: chipRect.minY + offset))
}
grooves.move(to: NSPoint(x: chipRect.midX, y: chipRect.minY + 8))
grooves.line(to: NSPoint(x: chipRect.midX, y: chipRect.minY + 30))
grooves.move(to: NSPoint(x: chipRect.midX, y: chipRect.maxY - 30))
grooves.line(to: NSPoint(x: chipRect.midX, y: chipRect.maxY - 8))
grooves.lineWidth = 4.5
grooves.stroke()
NSGraphicsContext.restoreGraphicsState()

NSColor(calibratedRed: 1.0, green: 0.94, blue: 0.72, alpha: 0.55).setStroke()
chipPath.lineWidth = 3
chipPath.stroke()

// 4. Emerald cryptographic signature seal overlapping lower-right of sheet.
let sealCenter = NSPoint(x: 696, y: 266)
let sealRadius: CGFloat = 108
let sealRect = NSRect(
    x: sealCenter.x - sealRadius,
    y: sealCenter.y - sealRadius,
    width: sealRadius * 2,
    height: sealRadius * 2
)
let sealPath = NSBezierPath(ovalIn: sealRect)

NSGraphicsContext.saveGraphicsState()
let sealShadow = NSShadow()
sealShadow.shadowColor = NSColor(calibratedRed: 0.02, green: 0.14, blue: 0.10, alpha: 0.48)
sealShadow.shadowBlurRadius = 22
sealShadow.shadowOffset = NSSize(width: 0, height: -9)
sealShadow.set()
NSColor(calibratedRed: 0.06, green: 0.56, blue: 0.40, alpha: 1.0).setFill()
sealPath.fill()
NSGraphicsContext.restoreGraphicsState()

NSGraphicsContext.saveGraphicsState()
sealPath.addClip()
let emeraldGrad = NSGradient(colors: [
    NSColor(calibratedRed: 0.12, green: 0.74, blue: 0.54, alpha: 1.0),
    NSColor(calibratedRed: 0.05, green: 0.50, blue: 0.36, alpha: 1.0)
])!
emeraldGrad.draw(in: sealRect, angle: -55)
NSGraphicsContext.restoreGraphicsState()

// Inner seal ring & crisp white verification checkmark.
let innerRing = NSBezierPath(ovalIn: sealRect.insetBy(dx: 14, dy: 14))
NSColor(calibratedWhite: 1.0, alpha: 0.32).setStroke()
innerRing.lineWidth = 3.5
innerRing.stroke()

let check = NSBezierPath()
check.move(to: NSPoint(x: sealCenter.x - 46, y: sealCenter.y - 4))
check.line(to: NSPoint(x: sealCenter.x - 12, y: sealCenter.y - 38))
check.line(to: NSPoint(x: sealCenter.x + 52, y: sealCenter.y + 34))
check.lineCapStyle = .round
check.lineJoinStyle = .round
check.lineWidth = 22
NSColor(calibratedRed: 0.98, green: 0.99, blue: 0.98, alpha: 1.0).setStroke()
check.stroke()

NSGraphicsContext.restoreGraphicsState()

guard let pngData = bitmap.representation(using: .png, properties: [:]) else {
    fputs("Failed to encode PNG bytes.\n", stderr)
    exit(1)
}
try pngData.write(to: outputURL, options: .atomic)
