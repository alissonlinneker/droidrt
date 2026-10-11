// compose: rasterizes an icon spec produced by iconkit.py into a macOS style PNG.
//
// Usage: compose <spec.json> <out.png>
//
// The spec describes Android drawables already resolved by iconkit.py:
//   color, bitmap, vector, layers (layer-list / inset) and shape nodes.
// Vector paths arrive normalized as absolute M, L, C, Q and Z operations
// (arcs, smooth curves, H and V are expanded on the Python side).
//
// Adaptive icons are rendered on the 108dp canvas, with the visible 72dp
// center mapped onto the 824px body of the 1024px macOS icon grid (100px
// margin), clipped by a superellipse and given a soft drop shadow.
//
// The spec is derived from untrusted APKs, so the compositor enforces its own
// limits even though iconkit.py already checks them: the spec file size, the
// number of rendered nodes, the total number of path commands and the pixel
// dimensions of every bitmap (read from the image header before decoding).

import CoreGraphics
import Foundation
import ImageIO
import UniformTypeIdentifiers

typealias JSON = [String: Any]

/// Largest magnitude accepted for any number in the spec; JSON allows 1e308,
/// which would overflow to infinity as soon as it is scaled.
let maxMagnitude: CGFloat = 1e7

/// A finite number from the spec, clamped to +-maxMagnitude; `d` otherwise.
func num(_ v: Any?, _ d: CGFloat = 0) -> CGFloat {
    guard let n = v as? NSNumber else { return d }
    let x = CGFloat(truncating: n)
    guard x.isFinite else { return d }
    return min(max(x, -maxMagnitude), maxMagnitude)
}

/// Int(x) traps on NaN, infinity and values outside Int; this clamps instead.
func clampedInt(_ x: CGFloat, _ lo: Int, _ hi: Int) -> Int {
    guard x.isFinite else { return lo }
    if x <= CGFloat(lo) { return lo }
    if x >= CGFloat(hi) { return hi }
    return Int(x)
}

func clamp(_ x: CGFloat, _ lo: CGFloat, _ hi: CGFloat) -> CGFloat {
    return min(max(x, lo), hi)
}

func finite(_ r: CGRect) -> Bool {
    return r.origin.x.isFinite && r.origin.y.isFinite && r.size.width.isFinite && r.size.height.isFinite
}

func rgba(_ v: Any?) -> CGColor? {
    guard let a = v as? [Any], a.count == 4 else { return nil }
    func c(_ v: Any) -> CGFloat { clamp(num(v), 0, 255) / 255 }
    return CGColor(srgbRed: c(a[0]), green: c(a[1]), blue: c(a[2]), alpha: c(a[3]))
}

let srgb = CGColorSpace(name: CGColorSpace.sRGB)!

// MARK: - Limits

let maxSpecBytes = 16 * 1024 * 1024
let maxBitmapSide = 4096
let maxNodes = 20_000
let maxPathOps = 200_000
let maxVectorDepth = 128
let allowedImageTypes: Set<String> = ["public.png", "org.webmproject.webp", "public.jpeg", "com.compuserve.gif"]

var nodeCount = 0
var pathOpCount = 0

/// Charges one rendered node against the global budget; false once it is spent.
func takeNode() -> Bool {
    nodeCount += 1
    return nodeCount <= maxNodes
}

// MARK: - Paths

func buildPath(_ ops: Any?) -> CGPath {
    let p = CGMutablePath()
    guard let list = ops as? [[Any]] else { return p }
    pathOpCount += list.count
    guard pathOpCount <= maxPathOps else { return p }
    var open = false
    for op in list {
        guard let c = op.first as? String else { continue }
        let v = op.dropFirst().map { num($0) }
        switch c {
        case "M" where v.count >= 2:
            p.move(to: CGPoint(x: v[0], y: v[1])); open = true
        case "L" where v.count >= 2:
            if !open { p.move(to: .zero); open = true }
            p.addLine(to: CGPoint(x: v[0], y: v[1]))
        case "C" where v.count >= 6:
            if !open { p.move(to: .zero); open = true }
            p.addCurve(to: CGPoint(x: v[4], y: v[5]), control1: CGPoint(x: v[0], y: v[1]),
                       control2: CGPoint(x: v[2], y: v[3]))
        case "Q" where v.count >= 4:
            if !open { p.move(to: .zero); open = true }
            p.addQuadCurve(to: CGPoint(x: v[2], y: v[3]), control: CGPoint(x: v[0], y: v[1]))
        case "Z":
            if open { p.closeSubpath() }
        default:
            break
        }
    }
    return p
}

func superellipse(_ r: CGRect, n: CGFloat = 5) -> CGPath {
    let p = CGMutablePath()
    let a = r.width / 2, b = r.height / 2, cx = r.midX, cy = r.midY
    let steps = 720
    for i in 0..<steps {
        let t = CGFloat(i) / CGFloat(steps) * 2 * .pi
        let c = cos(t), s = sin(t)
        let x = cx + a * (c < 0 ? -1 : 1) * pow(abs(c), 2 / n)
        let y = cy + b * (s < 0 ? -1 : 1) * pow(abs(s), 2 / n)
        if i == 0 { p.move(to: CGPoint(x: x, y: y)) } else { p.addLine(to: CGPoint(x: x, y: y)) }
    }
    p.closeSubpath()
    return p
}

// MARK: - Paint

/// Fills the current clip with a paint. `unit` maps 0..1 gradient coords onto a rect.
func fillClip(_ ctx: CGContext, paint: JSON, alpha: CGFloat, unit: CGRect?) {
    if let c = rgba(paint["color"]) {
        ctx.setFillColor(c.copy(alpha: c.alpha * alpha) ?? c)
        ctx.fill(ctx.boundingBoxOfClipPath)
        return
    }
    guard let g = paint["gradient"] as? JSON, let stops = g["stops"] as? [[Any]], !stops.isEmpty else { return }
    var colors: [CGColor] = []
    var locs: [CGFloat] = []
    for s in stops where s.count == 2 {
        if let c = rgba(s[1]) { colors.append(c.copy(alpha: c.alpha * alpha) ?? c); locs.append(clamp(num(s[0]), 0, 1)) }
    }
    guard !colors.isEmpty else { return }
    if colors.count == 1 { colors.append(colors[0]); locs.append(1) }
    guard let grad = CGGradient(colorsSpace: srgb, colors: colors as CFArray, locations: locs) else { return }
    func pt(_ x: CGFloat, _ y: CGFloat) -> CGPoint {
        guard let u = unit else { return CGPoint(x: x, y: y) }
        return CGPoint(x: u.minX + x * u.width, y: u.minY + y * u.height)
    }
    let opts: CGGradientDrawingOptions = [.drawsBeforeStartLocation, .drawsAfterEndLocation]
    switch g["type"] as? String ?? "linear" {
    case "radial":
        let c = pt(num(g["cx"]), num(g["cy"]))
        var r = num(g["r"])
        if let u = unit { r *= min(u.width, u.height) }
        ctx.drawRadialGradient(grad, startCenter: c, startRadius: 0, endCenter: c, endRadius: max(r, 0.001), options: opts)
    case "sweep":
        ctx.setFillColor(colors[0])
        ctx.fill(ctx.boundingBoxOfClipPath)
    default:
        ctx.drawLinearGradient(grad, start: pt(num(g["x1"]), num(g["y1"])), end: pt(num(g["x2"]), num(g["y2"])), options: opts)
    }
}

// MARK: - Vector drawables

func renderVectorChildren(_ ctx: CGContext, _ children: Any?, depth: Int = 0) {
    guard let list = children as? [JSON], depth < maxVectorDepth else { return }
    for ch in list {
        guard takeNode() else { return }
        switch ch["kind"] as? String {
        case "group":
            ctx.saveGState()
            let px = num(ch["pivotX"]), py = num(ch["pivotY"])
            ctx.translateBy(x: num(ch["translateX"]) + px, y: num(ch["translateY"]) + py)
            ctx.rotate(by: clamp(num(ch["rotation"]), -3600, 3600) * .pi / 180)
            ctx.scaleBy(x: num(ch["scaleX"], 1), y: num(ch["scaleY"], 1))
            ctx.translateBy(x: -px, y: -py)
            // Nested scales multiply; once the transform stops being finite, skip the group.
            let m = ctx.ctm
            if [m.a, m.b, m.c, m.d, m.tx, m.ty].allSatisfy({ $0.isFinite }) {
                renderVectorChildren(ctx, ch["children"], depth: depth + 1)
            }
            ctx.restoreGState()
        case "clip":
            ctx.addPath(buildPath(ch["d"]))
            ctx.clip()
        case "path":
            let path = buildPath(ch["d"])
            if path.isEmpty { continue }
            if let fill = ch["fill"] as? JSON {
                ctx.saveGState()
                ctx.addPath(path)
                ctx.clip(using: (ch["evenOdd"] as? Bool ?? false) ? .evenOdd : .winding)
                fillClip(ctx, paint: fill, alpha: num(ch["fillAlpha"], 1), unit: nil)
                ctx.restoreGState()
            }
            let sw = num(ch["strokeWidth"])
            if let stroke = ch["stroke"] as? JSON, sw > 0 {
                let cap: CGLineCap = [.butt, .round, .square][clampedInt(num(ch["cap"]), 0, 2)]
                let join: CGLineJoin = [.miter, .round, .bevel][clampedInt(num(ch["join"]), 0, 2)]
                let outline = path.copy(strokingWithWidth: min(sw, 1e4), lineCap: cap, lineJoin: join,
                                        miterLimit: clamp(num(ch["miter"], 4), 1, 1000))
                ctx.saveGState()
                ctx.addPath(outline)
                ctx.clip()
                fillClip(ctx, paint: stroke, alpha: num(ch["strokeAlpha"], 1), unit: nil)
                ctx.restoreGState()
            }
        default:
            break
        }
    }
}

func renderVector(_ ctx: CGContext, _ node: JSON, in rect: CGRect) {
    // A tiny viewport would scale every coordinate up to infinity.
    let vw = num(node["vw"], 24), vh = num(node["vh"], 24)
    guard vw >= 0.001, vh >= 0.001 else { return }
    ctx.saveGState()
    ctx.clip(to: rect)
    ctx.setAlpha(clamp(num(node["alpha"], 1), 0, 1))
    ctx.beginTransparencyLayer(auxiliaryInfo: nil)
    ctx.saveGState()
    ctx.translateBy(x: rect.minX, y: rect.minY)
    ctx.scaleBy(x: rect.width / vw, y: rect.height / vh)
    renderVectorChildren(ctx, node["children"])
    ctx.restoreGState()
    if let tint = rgba(node["tint"]) {
        ctx.setBlendMode(.sourceIn)
        ctx.setFillColor(tint)
        ctx.fill(rect)
    }
    ctx.endTransparencyLayer()
    ctx.restoreGState()
}

// MARK: - Generic drawables

var imageCache: [String: CGImage?] = [:]

/// Decodes a bitmap only after its header proves it is a known format no larger
/// than maxBitmapSide on either side; anything else is skipped (returns nil).
/// Each file is decoded once, however many nodes reference it.
func loadImage(_ path: String) -> CGImage? {
    if let cached = imageCache[path] { return cached }
    let img = decodeImage(path)
    imageCache[path] = img
    return img
}

func decodeImage(_ path: String) -> CGImage? {
    let noCache = [kCGImageSourceShouldCache: false] as CFDictionary
    guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, noCache),
          let type = CGImageSourceGetType(src) as String?, allowedImageTypes.contains(type),
          CGImageSourceGetCount(src) > 0,
          let props = CGImageSourceCopyPropertiesAtIndex(src, 0, noCache) as? [CFString: Any],
          let w = (props[kCGImagePropertyPixelWidth] as? NSNumber)?.intValue,
          let h = (props[kCGImagePropertyPixelHeight] as? NSNumber)?.intValue,
          w > 0, h > 0, w <= maxBitmapSide, h <= maxBitmapSide else {
        FileHandle.standardError.write("compose: skipping bitmap with unknown format or size\n".data(using: .utf8)!)
        return nil
    }
    return CGImageSourceCreateImageAtIndex(src, 0, noCache)
}

func drawImage(_ ctx: CGContext, _ img: CGImage, in rect: CGRect) {
    // The context is flipped to a top-left origin; flip back locally for images.
    ctx.saveGState()
    ctx.translateBy(x: rect.minX, y: rect.maxY)
    ctx.scaleBy(x: 1, y: -1)
    ctx.interpolationQuality = .high
    ctx.draw(img, in: CGRect(x: 0, y: 0, width: rect.width, height: rect.height))
    ctx.restoreGState()
}

func render(_ ctx: CGContext, _ node: Any?, in rect: CGRect, depth: Int = 0) {
    // CGRect.width is the standardized (absolute) width, so check the raw size too.
    guard let n = node as? JSON, depth < 32, finite(rect), rect.size.width > 0, rect.size.height > 0,
          takeNode() else { return }
    switch n["type"] as? String {
    case "color":
        if let c = rgba(n["color"]) { ctx.setFillColor(c); ctx.fill(rect) }
    case "bitmap":
        if let p = n["path"] as? String, let img = loadImage(p) { drawImage(ctx, img, in: rect) }
    case "vector":
        renderVector(ctx, n, in: rect)
    case "layers":
        for item in n["items"] as? [JSON] ?? [] {
            let ins = (item["inset"] as? [Any] ?? []).prefix(4).map { clamp(num($0), -2, 2) }
            let l = ins.count > 0 ? ins[0] : 0, t = ins.count > 1 ? ins[1] : 0
            let r = ins.count > 2 ? ins[2] : 0, b = ins.count > 3 ? ins[3] : 0
            let sub = CGRect(x: rect.minX + l * rect.width, y: rect.minY + t * rect.height,
                             width: rect.width * (1 - l - r), height: rect.height * (1 - t - b))
            render(ctx, item["drawable"], in: sub, depth: depth + 1)
        }
    case "shape":
        let path: CGPath
        if n["shape"] as? String == "oval" {
            path = CGPath(ellipseIn: rect, transform: nil)
        } else {
            let rad = clamp(num(n["radius"]) * min(rect.width, rect.height), 0, min(rect.width, rect.height) / 2)
            path = CGPath(roundedRect: rect, cornerWidth: rad, cornerHeight: rad, transform: nil)
        }
        if let fill = n["fill"] as? JSON {
            ctx.saveGState(); ctx.addPath(path); ctx.clip()
            fillClip(ctx, paint: fill, alpha: 1, unit: rect)
            ctx.restoreGState()
        }
        let sw = clamp(num(n["strokeWidth"]), 0, 2) * min(rect.width, rect.height)
        if let stroke = n["stroke"] as? JSON, sw > 0 {
            ctx.saveGState()
            ctx.addPath(path.copy(strokingWithWidth: sw, lineCap: .butt, lineJoin: .miter, miterLimit: 4))
            ctx.clip()
            fillClip(ctx, paint: stroke, alpha: 1, unit: rect)
            ctx.restoreGState()
        }
    default:
        break
    }
}

/// True when every corner pixel of a legacy bitmap icon is opaque (a full square icon).
func cornersOpaque(_ node: Any?) -> Bool {
    guard let n = node as? JSON, n["type"] as? String == "bitmap", let p = n["path"] as? String,
          let img = loadImage(p) else { return (node as? JSON)?["type"] as? String == "color" }
    let w = 32, h = 32
    guard let c = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: w * 4, space: srgb,
                            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return false }
    c.draw(img, in: CGRect(x: 0, y: 0, width: w, height: h))
    guard let data = c.data?.assumingMemoryBound(to: UInt8.self) else { return false }
    for (x, y) in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)] where data[(y * w + x) * 4 + 3] < 250 {
        return false
    }
    return true
}

// MARK: - Main

let args = CommandLine.arguments
guard args.count == 3 else {
    FileHandle.standardError.write("usage: compose <spec.json> <out.png>\n".data(using: .utf8)!)
    exit(2)
}
let specAttrs = try? FileManager.default.attributesOfItem(atPath: args[1])
let specSize = (specAttrs?[.size] as? NSNumber)?.intValue ?? -1
guard specSize >= 0, specSize <= maxSpecBytes else {
    FileHandle.standardError.write("compose: spec missing or too large\n".data(using: .utf8)!)
    exit(1)
}
guard let data = FileManager.default.contents(atPath: args[1]), data.count <= maxSpecBytes,
      let spec = (try? JSONSerialization.jsonObject(with: data)) as? JSON else {
    FileHandle.standardError.write("compose: invalid spec\n".data(using: .utf8)!)
    exit(1)
}

let size = clampedInt(num(spec["size"], 1024), 16, 4096)
let S = CGFloat(size)
guard let ctx = CGContext(data: nil, width: size, height: size, bitsPerComponent: 8, bytesPerRow: 0, space: srgb,
                          bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { exit(1) }
ctx.translateBy(x: 0, y: S)
ctx.scaleBy(x: 1, y: -1)

// macOS icon grid: 824/1024 body with a 100/1024 margin.
let body = CGRect(x: S * 100 / 1024, y: S * 100 / 1024, width: S * 824 / 1024, height: S * 824 / 1024)
let mask = superellipse(body)

ctx.saveGState()
ctx.setShadow(offset: CGSize(width: 0, height: -S * 10 / 1024), blur: S * 28 / 1024,
              color: CGColor(srgbRed: 0, green: 0, blue: 0, alpha: 0.30))
ctx.beginTransparencyLayer(auxiliaryInfo: nil)
if spec["kind"] as? String == "adaptive" {
    // 72dp visible area maps to the body; the full 108dp layer overflows and is clipped.
    let scale = body.width / 72
    let layer = CGRect(x: body.midX - 54 * scale, y: body.midY - 54 * scale, width: 108 * scale, height: 108 * scale)
    ctx.addPath(mask)
    ctx.clip()
    render(ctx, spec["background"], in: layer)
    render(ctx, spec["foreground"], in: layer)
} else {
    let layer = spec["layer"]
    if cornersOpaque(layer) {
        ctx.addPath(mask)
        ctx.clip()
    }
    render(ctx, layer, in: body)
}
ctx.endTransparencyLayer()
ctx.restoreGState()

guard let image = ctx.makeImage(),
      let dest = CGImageDestinationCreateWithURL(URL(fileURLWithPath: args[2]) as CFURL,
                                                 UTType.png.identifier as CFString, 1, nil) else { exit(1) }
CGImageDestinationAddImage(dest, image, nil)
exit(CGImageDestinationFinalize(dest) ? 0 : 1)
