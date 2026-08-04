// 把栅格化后的品牌图标加工成符合 macOS 规范的应用图标画布。
//
// 解决两个问题：
// 1. QuickLook 栅格化 SVG 时会把圆角以外的区域补成不透明白色，直接用会看到白方块底；
// 2. macOS 图标约定画面内缩（1024 画布中实际占约 824），满幅图标会明显比同排应用大一圈。
//
// 做法：透明画布 + 与品牌圆角同比例的裁切路径，把源图缩放绘制进内缩矩形，
// 落在路径外的白角自然被裁掉。
//
// 用法：mkicon <输入png> <输出png>

import AppKit
import Foundation

let canvas: CGFloat = 1024
let inset: CGFloat = 100
let side = canvas - inset * 2                 // 824
let cornerRadius = side * (112.0 / 512.0)     // 与 bosun.svg 的 rx 保持同一比例

guard CommandLine.arguments.count == 3 else {
    FileHandle.standardError.write(Data("用法：mkicon <输入png> <输出png>\n".utf8))
    exit(2)
}
let inputURL = URL(fileURLWithPath: CommandLine.arguments[1])
let outputURL = URL(fileURLWithPath: CommandLine.arguments[2])

guard let source = NSImage(contentsOf: inputURL) else {
    FileHandle.standardError.write(Data("无法读取输入图片：\(inputURL.path)\n".utf8))
    exit(1)
}

guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil,
                                 pixelsWide: Int(canvas), pixelsHigh: Int(canvas),
                                 bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
                                 isPlanar: false, colorSpaceName: .deviceRGB,
                                 bytesPerRow: 0, bitsPerPixel: 0) else {
    FileHandle.standardError.write(Data("无法创建位图上下文\n".utf8))
    exit(1)
}
rep.size = NSSize(width: canvas, height: canvas)

NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
NSGraphicsContext.current?.imageInterpolation = .high

let target = NSRect(x: inset, y: inset, width: side, height: side)
NSBezierPath(roundedRect: target, xRadius: cornerRadius, yRadius: cornerRadius).setClip()
source.draw(in: target,
            from: NSRect(origin: .zero, size: source.size),
            operation: .sourceOver,
            fraction: 1.0)

NSGraphicsContext.restoreGraphicsState()

guard let png = rep.representation(using: .png, properties: [:]) else {
    FileHandle.standardError.write(Data("PNG 编码失败\n".utf8))
    exit(1)
}
try png.write(to: outputURL)
