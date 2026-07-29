#!/usr/bin/env python3
"""
生成 App 图标（.icns）。

用 AppKit 绘制，不引入额外依赖——pyobjc 已经随 rumps 一起装好了。
"""
import subprocess
import sys
from pathlib import Path

try:
    from AppKit import (
        NSBitmapImageRep, NSColor, NSFont, NSGraphicsContext, NSImage,
        NSMakeRect, NSMutableParagraphStyle, NSPNGFileType, NSString,
    )
    from Foundation import NSMakeSize
except ImportError:
    print("需要 pyobjc（随 rumps 安装）: pip install rumps")
    sys.exit(1)

ACCENT = (0.91, 0.22, 0.31)      # 小红书风格的红
SIZES = [16, 32, 64, 128, 256, 512, 1024]


def draw(size: int) -> bytes:
    image = NSImage.alloc().initWithSize_(NSMakeSize(size, size))
    image.lockFocus()

    ctx = NSGraphicsContext.currentContext()
    ctx.setShouldAntialias_(True)

    # 圆角底板
    from AppKit import NSBezierPath
    radius = size * 0.225
    rect = NSMakeRect(0, 0, size, size)
    path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, radius, radius)
    NSColor.colorWithSRGBRed_green_blue_alpha_(*ACCENT, 1.0).set()
    path.fill()

    # 中间的「书」字
    style = NSMutableParagraphStyle.alloc().init()
    style.setAlignment_(1)   # center
    font_size = size * 0.58
    attrs = {
        "NSFont": NSFont.boldSystemFontOfSize_(font_size),
        "NSColor": NSColor.whiteColor(),
        "NSParagraphStyle": style,
    }
    text = NSString.stringWithString_("书")
    text_size = text.sizeWithAttributes_(attrs)
    text.drawInRect_withAttributes_(
        NSMakeRect(0, (size - text_size.height) / 2, size, text_size.height), attrs
    )

    rep = NSBitmapImageRep.alloc().initWithFocusedViewRect_(rect)
    image.unlockFocus()
    return rep.representationUsingType_properties_(NSPNGFileType, None)


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("icon.icns")
    iconset = out.parent / "icon.iconset"
    iconset.mkdir(parents=True, exist_ok=True)

    for s in SIZES:
        data = draw(s)
        (iconset / f"icon_{s}x{s}.png").write_bytes(bytes(data))
        if s <= 512:
            data2x = draw(s * 2)
            (iconset / f"icon_{s}x{s}@2x.png").write_bytes(bytes(data2x))

    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out)], check=True)
    subprocess.run(["rm", "-rf", str(iconset)], check=False)
    print(f"图标已生成: {out}")


if __name__ == "__main__":
    main()
