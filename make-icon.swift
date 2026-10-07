// Generates CTally.icns from the same hexagon geometry CTally draws.
// Usage: swiftc -O make-icon.swift -o make-icon && ./make-icon <output.icns>

import Cocoa

func hexagonPoints(_ c: CGPoint, _ r: CGFloat) -> [CGPoint] {
    (0..<6).map { i in
        let a = CGFloat.pi / 180 * (30 + 60 * CGFloat(i))
        return CGPoint(x: c.x + r * cos(a), y: c.y + r * sin(a))
    }
}

func hexagon(_ c: CGPoint, _ r: CGFloat) -> NSBezierPath {
    let pts = hexagonPoints(c, r)
    let path = NSBezierPath()
    path.move(to: pts[0])
    for p in pts.dropFirst() { path.line(to: p) }
    path.close()
    return path
}

/// Exact pixel dimensions matter for an iconset, so draw into a sized rep directly.
func render(_ pixels: Int) -> Data {
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels,
                               bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
                               isPlanar: false, colorSpaceName: .deviceRGB,
                               bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)

    let size = CGFloat(pixels)
    let c = CGPoint(x: size / 2, y: size / 2)
    let r = size * 0.44
    let s = r / 44
    let accent = NSColor(srgbRed: 0.29, green: 0.80, blue: 0.95, alpha: 1)

    NSColor(srgbRed: 0.09, green: 0.10, blue: 0.13, alpha: 1).setFill()
    hexagon(c, r).fill()
    NSColor(white: 1, alpha: 0.12).setStroke()
    let edge = hexagon(c, r); edge.lineWidth = max(1, 1 * s); edge.stroke()

    let pts = hexagonPoints(c, r * 0.82)
    let ring = NSBezierPath()
    ring.lineWidth = 2.5 * s
    ring.lineCapStyle = .round
    for i in 0..<6 {
        let a = pts[i], b = pts[(i + 1) % 6]
        func at(_ t: CGFloat) -> CGPoint { CGPoint(x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t) }
        ring.move(to: at(0.17))
        ring.line(to: at(0.83))
    }
    accent.withAlphaComponent(0.9).setStroke()
    ring.stroke()

    let play = NSBezierPath()
    play.move(to: CGPoint(x: c.x - 9 * s, y: c.y + 13 * s))
    play.line(to: CGPoint(x: c.x + 14 * s, y: c.y))
    play.line(to: CGPoint(x: c.x - 9 * s, y: c.y - 13 * s))
    play.close()
    play.lineJoinStyle = .round
    play.lineWidth = 4 * s
    accent.setFill(); accent.setStroke()
    play.fill(); play.stroke()

    NSGraphicsContext.restoreGraphicsState()
    return rep.representation(using: .png, properties: [:])!
}

let out = CommandLine.arguments[1]
let work = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("CTally.iconset")
try? FileManager.default.removeItem(at: work)
try! FileManager.default.createDirectory(at: work, withIntermediateDirectories: true)

for (points, scale) in [(16, 1), (16, 2), (32, 1), (32, 2), (128, 1), (128, 2),
                        (256, 1), (256, 2), (512, 1), (512, 2)] {
    let name = scale == 1 ? "icon_\(points)x\(points).png" : "icon_\(points)x\(points)@2x.png"
    try! render(points * scale).write(to: work.appendingPathComponent(name))
}

let task = Process()
task.executableURL = URL(fileURLWithPath: "/usr/bin/iconutil")
task.arguments = ["-c", "icns", work.path, "-o", out]
try! task.run()
task.waitUntilExit()
try? FileManager.default.removeItem(at: work)
exit(task.terminationStatus)
