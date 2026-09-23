"""把桌宠所有表情渲染成一张对比图，肉眼验绘制（AppKit 控件必须渲染成 PNG 实测）。

    source .venv-jev-jarvis/bin/activate
    python3 probe/pet_preview.py [输出.png]

离线、不读屏、不调模型。只建位图上下文画一遍 _PetView.drawRect_。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import AppKit  # noqa: E402
from AppKit import (  # noqa: E402
    NSApplication,
    NSBitmapImageRep,
    NSCalibratedRGBColorSpace,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSGraphicsContext,
    NSAttributedString,
    NSMakeRect,
)

from hud import PET_H, PET_W, _PetView  # noqa: E402

# (label, mood, tier, low_conf)
CELLS = [
    ("idle / 待机",              "idle",     0, False),
    ("alert / 新消息",           "alert",    0, False),
    ("think / 分析中",           "think",    0, False),
    ("think / 低把握?",          "think",    0, True),
    ("happy / 夸奖",             "happy",    0, False),
    ("chill / 闲聊",             "chill",    0, False),
    ("ready / 派活·问进度·约会",  "ready",    0, False),
    ("stressed / 催进度",        "stressed", 0, False),
    ("guard / 批评  安全≤3",     "guard",    0, False),
    ("guard / 批评  留神≤6",     "guard",    1, False),
    ("guard / 批评  危险>6",     "guard",    2, False),
    ("error / 出错",             "error",    2, False),
    ("happy + 危险>6",           "happy",    2, False),
    ("ready + 留神≤6",           "ready",    1, False),
    ("chill + 低把握?",          "chill",    0, True),
    ("alert + 危险>6",           "alert",    2, False),
]

COLS = 4
SCALE = 2.0


def _png_type():
    for name in ("NSBitmapImageFileTypePNG", "NSPNGFileType"):
        t = getattr(AppKit, name, None)
        if t is not None:
            return t
    raise RuntimeError("找不到 PNG 常量")


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/pet_preview.png")
    S = SCALE
    CW = int((PET_W + 24) * S)          # 单元格（像素）
    CH = int((PET_H + 30) * S)
    rows = (len(CELLS) + COLS - 1) // COLS
    W, H = COLS * CW, rows * CH

    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(  # noqa: E501
        None, W, H, 8, 4, True, False, NSCalibratedRGBColorSpace, 0, 0)

    NSApplication.sharedApplication()
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(
        NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep))

    # 深底：白脸和透明区才看得出来
    AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(
        0.16, 0.17, 0.20, 1.0).set()
    AppKit.NSBezierPath.fillRect_(NSMakeRect(0, 0, W, H))

    for i, (label, mood, tier, low_conf) in enumerate(CELLS):
        col, row = i % COLS, i // COLS
        cb = (rows - 1 - row) * CH                       # 该行底边（位图 y 向上）
        px = col * CW + int(12 * S)
        py = cb + int(32 * S)

        # 每个表情按真实 96pt 渲染，再整块放大 2x —— 线宽比例与运行时一致
        one = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(  # noqa: E501
            None, PET_W, PET_H, 8, 4, True, False,
            NSCalibratedRGBColorSpace, 0, 0)
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.setCurrentContext_(
            NSGraphicsContext.graphicsContextWithBitmapImageRep_(one))
        view = _PetView.alloc().initWithFrame_(NSMakeRect(0, 0, PET_W, PET_H))
        view.mood, view.tier, view.low_conf, view.blink, view.breath = \
            mood, tier, low_conf, False, 1
        view.drawRect_(view.bounds())
        NSGraphicsContext.restoreGraphicsState()
        NSGraphicsContext.setCurrentContext_(
            NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep))

        img = AppKit.NSImage.alloc().initWithSize_(NSMakeRect(0, 0, PET_W, PET_H).size)
        img.addRepresentation_(one)
        img.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
            NSMakeRect(px, py, PET_W * S, PET_H * S),
            NSMakeRect(0, 0, PET_W, PET_H),
            AppKit.NSCompositingOperationSourceOver, 1.0, False, None)

        ch = NSAttributedString.alloc().initWithString_attributes_(
            label,
            {NSFontAttributeName: AppKit.NSFont.systemFontOfSize_(11),
             NSForegroundColorAttributeName: AppKit.NSColor.whiteColor()})
        ch.drawAtPoint_((px, cb + int(7 * S)))

    NSGraphicsContext.restoreGraphicsState()

    data = rep.representationUsingType_properties_(_png_type(), {})
    if not data.writeToFile_atomically_(str(out), True):
        print("写文件失败", file=sys.stderr)
        return 1
    print(f"OK {out}  {W}x{H}  {len(CELLS)} 个表情")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
