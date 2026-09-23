"""Floating HUD: a non-activating panel beside WeChat showing intent, risk and actions.

Simplified version: only shows judgment results (intent, risk, actions).
No candidate reply generation, no tone selection, no fill/copy buttons.
All processing is local - no data sent to external APIs.
"""

from __future__ import annotations

import math
import objc
import os
import threading
import time
from pathlib import Path

import AppKit
import sys

from AppKit import (
    NSAppearance,
    NSAttributedString,
    NSBackingStoreBuffered,
    NSBackgroundColorAttributeName,
    NSBezierPath,
    NSBezelStyleRounded,
    NSButton,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSPanel,
    NSScreen,
    NSTextField,
    NSView,
    NSWindowMiniaturizeButton,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskNonactivatingPanel,
    NSWindowStyleMaskTitled,
    NSWindowZoomButton,
    NSWindowCloseButton,
)
from Foundation import NSMakeRect, NSMakeSize, NSObject, NSTimer

sys.path.insert(0, str(Path(__file__).parent))
import userconfig  # noqa: E402

userconfig.load()   # ~/.config/jev-jarvis/env -> os.environ (Finder apps inherit none)

from perception import (  # noqa: E402
    read_conversation, screen_capture_ok, request_screen_capture, warm_ocr)
from judge import make_judge  # noqa: E402

# Panel dimensions - simplified for judgment only
PANEL_W = 320
PANEL_H = 280   # reduced height, no candidate section
COLLAPSED_H = 80

# Poll timing
FAST_TICK = 0.25
BURST_TICK = 0.45
BURST_READS = 3
SLOW_TICK = 1.0
SETTLE_S = 1.2
EARLY_SETTLE_S = 0.70
STABLE_READS = 3
MIN_GAP_S = 2.0
CONTEXT_TURNS = 4
JUDGE_TURNS = 2

# 桌宠：矢量小人，点击穿透、纯展示，不参与任何管线
PET_W = PET_H = 96          # 固定尺寸（同「微信窗口不改」的原则，不自适应）
PET_GAP = 10                # 与 HUD 面板的间距
PET_TICK = 0.1              # 眨眼/呼吸的驱动频率

# 意图 → 表情。风险不另起映射，直接复用 applyJudgment_ 已有的分档：
# ≤3 安全 / ≤6 留神 / >6 危险（同 721-723 行的绿/琥珀/红）
_MOOD_BY_INTENT = {
    "夸奖": "happy",
    "闲聊": "chill",
    "派活": "ready", "问进度": "ready", "约会议": "ready",
    "催进度": "stressed",
    "批评": "guard", "要解释": "guard",
}


# ---------------------------------------------------------------- palette
def _rgb(hex_code: int, alpha: float = 1.0) -> NSColor:
    return NSColor.colorWithCalibratedRed_green_blue_alpha_(
        ((hex_code >> 16) & 0xFF) / 255.0,
        ((hex_code >> 8) & 0xFF) / 255.0,
        (hex_code & 0xFF) / 255.0,
        alpha,
    )


PALETTE = {
    "bg": _rgb(0xF7F7F7),
    "text": _rgb(0x191919),
    "muted": _rgb(0x888888),
    "green": _rgb(0x07C160),
    "amber": _rgb(0xFA9D3B),
    "red": _rgb(0xFA5151),
    "field": _rgb(0xF2F2F2),
    "edge": _rgb(0xE3E3E3),
}


LOG_PATH = Path.home() / "Library" / "Logs" / "jev-jarvis.log"


def _log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        if os.fstat(sys.stdout.fileno()).st_ino == LOG_PATH.stat().st_ino:
            return
    except Exception:
        pass
    try:
        with open(LOG_PATH, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


class _BoxesView(NSView):
    def drawRect_(self, rect):
        for r, color, lw, chip in getattr(self, "boxes", None) or []:
            color.set()
            NSBezierPath.setDefaultLineWidth_(lw)
            NSBezierPath.strokeRect_(r)
            chip.drawAtPoint_((r.origin.x, r.origin.y + r.size.height + 2))


# ---------------------------------------------------------------- 桌宠
_PET_FACE = _rgb(0xFDFDFD)
_PET_INK = _rgb(0x2B2B2B)
_PET_BLUSH = _rgb(0xFF9DB0, 0.55)
_PET_SHADOW = _rgb(0x000000, 0.10)
_PET_SWEAT = _rgb(0x6FB3FF)


def _pet_shape(cx, cy, w, k):
    """两端钉在 (cx±w, cy)，中点朝 +y 偏 k。k>0 向上凸，k<0 向下凹。

    眼睛和嘴共用这一条：happy 眼 k>0 是 `^`，笑嘴 k<0 是 `‿`，
    guard 嘴 k>0 是 `⌢`——正负号的含义在视图坐标（y 向上）下统一。
    """
    p = NSBezierPath.bezierPath()
    p.moveToPoint_((cx - w, cy))
    p.curveToPoint_controlPoint1_controlPoint2_(
        (cx + w, cy),
        (cx - w * 0.55, cy + k * 1.9),
        (cx + w * 0.55, cy + k * 1.9))
    return p


def _pet_oval(cx, cy, rx, ry):
    return NSBezierPath.bezierPathWithOvalInRect_(
        NSMakeRect(cx - rx, cy - ry, rx * 2, ry * 2))


class _PetView(NSView):
    """矢量画的圆脸小人。状态全靠类属性兜底（同 _BoxesView 的 getattr 习惯）。"""

    mood = "idle"
    tier = 0
    low_conf = False
    blink = False
    breath = 0

    def drawRect_(self, rect):
        b = self.bounds()
        W, H = b.size.width, b.size.height
        mood = getattr(self, "mood", "idle")
        tier = max(0, min(2, getattr(self, "tier", 0)))
        low_conf = getattr(self, "low_conf", False)
        blink = bool(getattr(self, "blink", False)) and mood != "happy"
        breath = getattr(self, "breath", 0)
        accent = (PALETTE["green"], PALETTE["amber"], PALETTE["red"])[tier]

        cx = W / 2.0
        cy = H / 2.0 - 4.0
        r = W * 0.36 * (1.0 - 0.025 * breath)   # 呼吸 = 半径 ±2.5%

        # 地面阴影
        sh = _pet_oval(cx, cy - r - 6, r * 0.78, 5)
        _PET_SHADOW.set()
        sh.fill()

        # 脸 + 风险描边
        face = _pet_oval(cx, cy, r, r)
        _PET_FACE.set()
        face.fill()
        accent.set()
        face.setLineWidth_(3.2)
        face.stroke()

        ex = r * 0.36
        ey = cy + r * 0.14 + (r * 0.10 if mood == "think" else 0.0)
        my = cy - r * 0.34
        mw = r * 0.30
        _PET_INK.set()

        if blink:
            for sx in (-1, 1):
                p = _pet_shape(cx + sx * ex, ey, r * 0.17, -1.5)
                p.setLineWidth_(3.0)
                p.stroke()
        elif mood == "happy":
            for sx in (-1, 1):
                p = _pet_shape(cx + sx * ex, ey, r * 0.17, 4.5)
                p.setLineWidth_(3.0)
                p.stroke()
            _PET_BLUSH.set()
            _pet_oval(cx - ex - r * 0.08, ey - r * 0.36, r * 0.15, r * 0.09).fill()
            _pet_oval(cx + ex + r * 0.08, ey - r * 0.36, r * 0.15, r * 0.09).fill()
            _PET_INK.set()
        elif mood == "stressed":
            for sx in (-1, 1):
                p = _pet_shape(cx + sx * ex, ey, r * 0.17, -4.5)
                p.setLineWidth_(3.0)
                p.stroke()
            _PET_SWEAT.set()
            _pet_oval(cx + r * 0.86, cy + r * 0.34, r * 0.11, r * 0.15).fill()
            _PET_INK.set()
        elif mood == "guard":
            for sx in (-1, 1):                      # 眯眼
                _pet_oval(cx + sx * ex, ey, r * 0.19, r * 0.085).fill()
        elif mood == "alert":
            for sx in (-1, 1):                      # 瞪大 + 高光
                _pet_oval(cx + sx * ex, ey, r * 0.15, r * 0.17).fill()
                _PET_FACE.set()
                _pet_oval(cx + sx * ex + r * 0.05, ey + r * 0.05,
                          r * 0.05, r * 0.05).fill()
                _PET_INK.set()
        elif mood == "chill":
            for sx in (-1, 1):
                p = _pet_shape(cx + sx * ex, ey, r * 0.17, 1.5)
                p.setLineWidth_(3.0)
                p.stroke()
        else:                                        # idle / ready / think / error
            for sx in (-1, 1):
                _pet_oval(cx + sx * ex, ey, r * 0.11, r * 0.13).fill()

        # 嘴
        if mood in ("alert", "error"):
            mouth = _pet_oval(cx, my, r * 0.13, r * 0.16)
        else:
            # think/ready 是平直的「嗯…」，别用 k>0——那会凸成哭脸（实测过）
            k = {"happy": -6.5, "chill": -3.5, "guard": 5.0,
                 "stressed": 3.0, "think": 0.0, "ready": 0.0}.get(mood, -3.0)
            wscale = {"happy": 1.15, "think": 0.72, "ready": 0.95}.get(mood, 0.95)
            mouth = _pet_shape(cx, my, mw * wscale, k)
        mouth.setLineWidth_(3.2 if mood != "happy" else 3.6)
        mouth.stroke()

        # 角标：状态自带的，或低把握时的问号
        badge, bcolor = {"alert": ("!", PALETTE["amber"]),
                         "error": ("!", PALETTE["red"]),
                         "think": ("?", PALETTE["muted"])}.get(mood, (None, None))
        if badge is None and low_conf:
            badge, bcolor = ("?", PALETTE["muted"])
        if badge:
            ch = NSAttributedString.alloc().initWithString_attributes_(
                badge, {NSFontAttributeName: NSFont.boldSystemFontOfSize_(17),
                        NSForegroundColorAttributeName: bcolor})
            ch.drawAtPoint_((cx + r * 0.70, cy + r * 0.52))


class HudController(NSObject):
    def init(self):
        self = objc.super(HudController, self).init()
        if self is None:
            return None
        self.last_seen = None
        self.last_change_ts = 0.0
        self.last_analyze_ts = 0.0
        self.analyzed_text = None
        self._judged_once = False
        self._read_once = False
        self._last_skip_reason = None
        self.judge = make_judge()
        self._fixed: list = []
        self._title_h = 28
        self._last_intent = ""
        self._busy = False
        self._next_read_ts = 0.0
        self._fingerprint = None
        self._last_full = None
        self._last_nothem_sig = ()
        self._analyzing = False
        self._model_lock = threading.Lock()
        self._prejudge_req = None
        self._prejudge_result = None
        self._prejudging = False
        self._prejudge_event = threading.Event()
        threading.Thread(target=self._prejudge_loop, daemon=True).start()
        self._burst_left = BURST_READS
        self._stable_n = 0
        self._collapsed = False
        self._expanded_h = None
        self._paused = False
        self._show_boxes = userconfig.get("JEV_BOXES").strip().lower() in (
            "1", "true", "yes", "on")
        # 桌宠默认开（JEV_PET=0 关）——同 JEV_BOXES 的开关写法
        self._show_pet = userconfig.get("JEV_PET").strip().lower() not in (
            "0", "false", "no", "off")
        self._pet_tier = 0
        self._last_risk = 0.0
        self._chat_title = ""
        self._asked_permission = False
        self._win_wid = None
        self._last_origin = None
        self._pending_origin = None
        self._build_panel()
        self._build_overlay()
        self._build_pet()
        self._expanded_h = self.panel.frame().size.height
        return self

    @objc.python_method
    def _build_panel(self):
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                 | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskNonactivatingPanel)
        self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, PANEL_W, PANEL_H), style, NSBackingStoreBuffered, False)
        self.panel.setLevel_(AppKit.NSFloatingWindowLevel)
        self.panel.setOpaque_(False)
        self.panel.setAlphaValue_(1.0)
        self.panel.setAppearance_(NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameAqua))
        self.panel.setBackgroundColor_(PALETTE["bg"])
        self.panel.setTitle_("jev-jarvis · 本地判断")
        self.panel.setHidesOnDeactivate_(False)
        self.panel.setBecomesKeyOnlyIfNeeded_(True)

        view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, PANEL_W, PANEL_H))
        view.setWantsLayer_(True)
        view.layer().setBackgroundColor_(PALETTE["bg"].CGColor())
        self.rows: dict[str, NSTextField] = {}

        dy = 30
        for key, size, color, bold, height in (
            ("chat", 12, PALETTE["green"], True, 18),
            ("status", 10, PALETTE["muted"], False, 14),
            ("message", 14, PALETTE["text"], False, 45),
            ("sender", 10, PALETTE["muted"], False, 14),
            ("intent", 22, PALETTE["text"], True, 30),
            ("confidence", 12, PALETTE["muted"], False, 18),
            ("risk", 16, PALETTE["green"], True, 24),
            ("actions", 13, PALETTE["text"], False, 20),
        ):
            tf = self._make_label(14, 0, PANEL_W - 28, height,
                                  size=size, color=color, bold=bold)
            if key == "message":
                tf.cell().setWraps_(True)
            view.addSubview_(tf)
            self.rows[key] = tf
            self._fixed.append((tf, 14, dy, PANEL_W - 28, height))
            dy += height + 8

        self.panel.setContentView_(view)
        self._title_h = self.panel.frame().size.height - PANEL_H
        self._relayout()
        self.rows["status"].setStringValue_("等待微信消息…")
        self._wire_window_controls()
        self._install_status_item()

    @objc.python_method
    def _build_overlay(self):
        self._ov_panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 200, 200), NSWindowStyleMaskBorderless,
            NSBackingStoreBuffered, False)
        self._ov_panel.setLevel_(AppKit.NSFloatingWindowLevel)
        self._ov_panel.setOpaque_(False)
        self._ov_panel.setHasShadow_(False)
        self._ov_panel.setIgnoresMouseEvents_(True)
        self._ov_panel.setHidesOnDeactivate_(False)
        self._ov_panel.setBackgroundColor_(NSColor.clearColor())
        view = _BoxesView.alloc().init()
        view.boxes = []
        self._ov_panel.setContentView_(view)

    @objc.python_method
    def _build_pet(self):
        """桌宠窗口：照抄 _build_overlay 的窗口属性，外加「所有桌面都跟」。"""
        self._pet_panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, PET_W, PET_H), NSWindowStyleMaskBorderless,
            NSBackingStoreBuffered, False)
        self._pet_panel.setLevel_(AppKit.NSFloatingWindowLevel)
        self._pet_panel.setOpaque_(False)
        self._pet_panel.setHasShadow_(False)
        self._pet_panel.setIgnoresMouseEvents_(True)   # 点击穿透，绝不挡鼠标
        self._pet_panel.setHidesOnDeactivate_(False)
        self._pet_panel.setBackgroundColor_(NSColor.clearColor())
        self._pet_panel.setCollectionBehavior_(
            AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
            | AppKit.NSWindowCollectionBehaviorStationary
            | AppKit.NSWindowCollectionBehaviorIgnoresCycle)
        view = _PetView.alloc().initWithFrame_(NSMakeRect(0, 0, PET_W, PET_H))
        view.setWantsLayer_(True)
        self._pet_view = view
        self._pet_panel.setContentView_(view)
        self._place_pet()
        # 眨眼/呼吸：只在状态真变了才 setNeedsDisplay（Intel 上每帧重绘没必要）
        self._pet_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            PET_TICK, self, "petTick:", None, True)

    def petTick_(self, _timer):
        v = getattr(self, "_pet_view", None)
        if v is None:
            return
        now = time.monotonic()
        blink = (now % 2.6) < 0.13                                  # 每 2.6s 眨一次
        breath = int((math.sin(now * 2 * math.pi / 2.4) * 0.5 + 0.5) * 3)
        if v.blink != blink or v.breath != breath:
            v.blink, v.breath = blink, breath
            v.setNeedsDisplay_(True)

    @objc.python_method
    def _place_pet(self):
        """贴着 HUD 面板摆：优先右侧，放不下翻左侧，再不行压到面板上方，全程夹紧在屏内。"""
        panel = getattr(self, "_pet_panel", None)
        if panel is None or not hasattr(self, "panel"):
            return
        screens = list(NSScreen.screens())
        if not screens:
            return
        pf = self.panel.frame()
        host = next((s for s in screens
                     if s.frame().origin.x <= pf.origin.x
                     < s.frame().origin.x + s.frame().size.width
                     and s.frame().origin.y <= pf.origin.y
                     < s.frame().origin.y + s.frame().size.height), screens[0])
        sf = host.frame()
        x = pf.origin.x + pf.size.width + PET_GAP
        if x + PET_W > sf.origin.x + sf.size.width - 8:
            x = pf.origin.x - PET_W - PET_GAP
        if x < sf.origin.x + 8:
            x = pf.origin.x + (pf.size.width - PET_W) / 2.0
        y = pf.origin.y + pf.size.height + PET_GAP
        if y + PET_H > sf.origin.y + sf.size.height - 8:
            y = pf.origin.y + (pf.size.height - PET_H) / 2.0
        panel.setFrameOrigin_((round(x), round(y)))

    @objc.python_method
    def _pet_set(self, mood: str, tier: int = 0, low_conf: bool = False):
        if not getattr(self, "_show_pet", False) or not hasattr(self, "_pet_view"):
            return
        v = self._pet_view
        changed = (v.mood, v.tier, v.low_conf) != (mood, tier, low_conf)
        v.mood, v.tier, v.low_conf = mood, tier, low_conf
        if changed:
            v.setNeedsDisplay_(True)
        if not self._pet_panel.isVisible():
            self._pet_panel.orderFrontRegardless()

    @objc.python_method
    def _relayout(self):
        dy = 30
        for key, _, _, _, height in (
            ("chat", 12, PALETTE["green"], True, 18),
            ("status", 10, PALETTE["muted"], False, 14),
            ("message", 14, PALETTE["text"], False, 45),
            ("sender", 10, PALETTE["muted"], False, 14),
            ("intent", 22, PALETTE["text"], True, 30),
            ("confidence", 12, PALETTE["muted"], False, 18),
            ("risk", 16, PALETTE["green"], True, 24),
            ("actions", 13, PALETTE["text"], False, 20),
        ):
            dy += height + 8

        content_h = dy + 18
        view = self.panel.contentView()
        view.setFrameSize_(NSMakeSize(PANEL_W, content_h))
        for ctrl, x, top, w, h in self._fixed:
            ctrl.setFrame_(NSMakeRect(x, content_h - top - h, w, h))

        f = self.panel.frame()
        top = f.origin.y + f.size.height
        frame_h = content_h + self._title_h
        self.panel.setFrame_display_(
            NSMakeRect(f.origin.x, top - frame_h, PANEL_W, frame_h), True)
        self._expanded_h = frame_h

    @objc.python_method
    def _wire_window_controls(self):
        close = self.panel.standardWindowButton_(NSWindowCloseButton)
        mini = self.panel.standardWindowButton_(NSWindowMiniaturizeButton)
        zoom = self.panel.standardWindowButton_(NSWindowZoomButton)
        if close:
            close.setTarget_(self)
            close.setAction_("quitApp:")
            close.setToolTip_("退出 jev-jarvis")
        if mini:
            mini.setTarget_(self)
            mini.setAction_("collapsePanel:")
            mini.setToolTip_("收起 / 展开面板")
        if zoom:
            zoom.setHidden_(True)

    @objc.python_method
    def _install_status_item(self):
        bar = AppKit.NSStatusBar.systemStatusBar()
        self.status_item = bar.statusItemWithLength_(AppKit.NSVariableStatusItemLength)
        self.status_item.button().setTitle_("J")
        self.status_item.button().setToolTip_("jev-jarvis · 本地判断（无外传）")

        menu = AppKit.NSMenu.alloc().init()
        for title, action, key in (
            ("显示 / 收起面板", "collapsePanel:", ""),
            ("暂停读屏", "togglePause:", ""),
            ("YOLO 检测框", "toggleBoxes:", ""),
            ("桌宠", "togglePet:", ""),
            ("立即重新分析", "reanalyze:", ""),
        ):
            menu.addItemWithTitle_action_keyEquivalent_(title, action, key)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        menu.addItemWithTitle_action_keyEquivalent_("退出 jev-jarvis", "quitApp:", "q")
        for item in menu.itemArray():
            item.setTarget_(self)
        self.pause_item = menu.itemArray()[1]
        self.boxes_item = menu.itemArray()[2]
        self.boxes_item.setState_(
            AppKit.NSOnState if self._show_boxes else AppKit.NSOffState)
        self.pet_item = menu.itemArray()[3]
        self.pet_item.setState_(
            AppKit.NSOnState if self._show_pet else AppKit.NSOffState)
        self.status_item.setMenu_(menu)

    @objc.python_method
    def _make_label(self, x, y, w, h, size=13, color=None, bold=False):
        tf = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
        tf.setStringValue_("")
        tf.setBezeled_(False)
        tf.setDrawsBackground_(False)
        tf.setEditable_(False)
        tf.setSelectable_(True)
        tf.setTextColor_(PALETTE["text"] if color is None else color)
        tf.setFont_(NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size))
        return tf

    @objc.python_method
    def _show(self):
        if not self.panel.isVisible():
            self.panel.orderFrontRegardless()
        if getattr(self, "_show_pet", False) and hasattr(self, "_pet_panel") \
                and not self._pet_panel.isVisible():
            self._place_pet()
            self._pet_panel.orderFrontRegardless()

    @objc.python_method
    def _context_line(self, sender, prev: str) -> str:
        parts = []
        if sender:
            parts.append(f"来自 {sender}")
        if prev:
            parts.append(f"上文：{prev[:26]}")
        return " · ".join(parts)

    @objc.python_method
    def _render(self, key: str, text: str, color: NSColor | None = None):
        tf = self.rows[key]
        tf.setStringValue_(text)
        if color is not None:
            tf.setTextColor_(color)

    @objc.python_method
    def _display_height(self) -> float:
        for scr in NSScreen.screens():
            f = scr.frame()
            if f.origin.x == 0 and f.origin.y == 0:
                return f.size.height
        return NSScreen.mainScreen().frame().size.height

    @objc.python_method
    def _position_near(self, win: dict | None):
        flip = self._display_height()
        panel_h = self.panel.frame().size.height or PANEL_H
        panel_w = self.panel.frame().size.width or PANEL_W
        screens = list(NSScreen.screens())
        primary = next((s for s in screens
                        if s.frame().origin.x == 0 and s.frame().origin.y == 0), screens[0])

        if win:
            wx, wy = win["x"], win["y"]
            ww, wh = win["w"], win["h"]
            cx_win = wx + ww / 2.0
            cyan = flip - (wy + wh / 2.0)
            host = next((s for s in screens
                         if s.frame().origin.x <= cx_win <= s.frame().origin.x + s.frame().size.width
                         and s.frame().origin.y <= cyan <= s.frame().origin.y + s.frame().size.height),
                        primary)
            sf = host.frame()
            x = wx + ww + 8
            if x + panel_w > sf.origin.x + sf.size.width:
                x = wx - panel_w - 8
            if x < sf.origin.x:
                x = sf.origin.x + sf.size.width - panel_w - 12
            y = flip - wy - panel_h
            y = max(sf.origin.y + 40, min(y, sf.origin.y + sf.size.height - panel_h - 40))
        else:
            sf = primary.frame()
            x = sf.size.width - panel_w - 12
            y = sf.size.height - panel_h - 60

        target = (round(x), round(y))
        last = self._last_origin
        if last is None:
            self._last_origin = target
            self._pending_origin = target
            self.panel.setFrameOrigin_(target)
            return
        if abs(target[0] - last[0]) <= 2 and abs(target[1] - last[1]) <= 2:
            return
        if target != self._pending_origin:
            self._pending_origin = target
            return
        self._last_origin = target
        self.panel.setFrameOrigin_(target)

    # ------------------------------------------------------------ actions
    def collapsePanel_(self, sender):
        self._set_collapsed(not self._collapsed)

    def togglePause_(self, sender):
        self._paused = not self._paused
        self.pause_item.setTitle_("继续读屏" if self._paused else "暂停读屏")
        if self._paused:
            self._prejudge_req = None
            self._prejudge_result = None
            if self._ov_panel.isVisible():
                self._ov_panel.orderOut_(None)
            self._render("status", "已暂停 · 不再读屏", PALETTE["amber"])
            self._render("message", "", PALETTE["text"])
            self._render("sender", "", PALETTE["muted"])
            self._render("intent", "—", PALETTE["muted"])
            self._render("confidence", "", PALETTE["muted"])
            self._render("risk", "", PALETTE["muted"])
            self._render("actions", "", PALETTE["text"])
        else:
            self._prejudge_result = None
            self.last_seen = None
            self.analyzed_text = None
            self._render("status", "已恢复 · 读屏中", PALETTE["muted"])

    def reanalyze_(self, sender):
        self._prejudge_req = None
        self._prejudge_result = None
        self.last_seen = None
        self.analyzed_text = None
        self._render("status", "重新分析中…", PALETTE["muted"])

    def quitApp_(self, sender):
        AppKit.NSApplication.sharedApplication().terminate_(None)

    @objc.python_method
    def _set_collapsed(self, collapsed: bool):
        self._collapsed = collapsed
        controlled = ["message", "sender", "intent", "confidence", "risk", "actions"]
        for key in controlled:
            self.rows[key].setHidden_(collapsed)
        if not collapsed:
            self._relayout()
            self._last_origin = None
            return

        rect = self.panel.frame()
        new_h = COLLAPSED_H if collapsed else (self._expanded_h or PANEL_H)
        self.panel.setFrame_display_(
            NSMakeRect(rect.origin.x, rect.origin.y + (rect.size.height - new_h),
                       rect.size.width, new_h), True)
        self._last_origin = None

    # --------------------------------------------------------------- loop
    def tick_(self, timer):
        if self._paused or self._busy or time.time() < self._next_read_ts:
            return
        self._busy = True
        threading.Thread(target=self._work, daemon=True).start()

    @objc.python_method
    def _work(self):
        try:
            self._work_inner()
        finally:
            self._busy = False

    @objc.python_method
    def _work_inner(self):
        if not screen_capture_ok():
            if not self._asked_permission:
                self._asked_permission = True
                request_screen_capture()
            self._push("applyError:", "需要屏幕录制权限 · 系统设置 › 隐私与安全性")
            self._next_read_ts = time.time() + SLOW_TICK
            return
        try:
            # 不能用 signal.alarm 兜超时：本函数跑在工作线程，signal.signal 只在
            # 主线程可用；而且 SIGALRM 会打断 AppKit 主循环。读屏本身有子进程
            # fallback，卡住的情况交给 _busy 门控 + 下一跳即可。
            res = read_conversation(previous_wid=self._win_wid,
                                    prev_fingerprint=self._fingerprint)
        except Exception as e:
            _log(f"读屏异常 {type(e).__name__}: {str(e)[:60]}")
            self._push("applyError:", f"读取失败: {type(e).__name__}: {str(e)[:40]}")
            self._next_read_ts = time.time() + SLOW_TICK
            return
        if not res["ok"]:
            _log(f"读屏失败: {res.get('error')}")
            self._push("applyHidden:", res["error"])
            self._next_read_ts = time.time() + SLOW_TICK
            return

        self._fingerprint = res.get("fingerprint")
        if res["unchanged"]:
            self._stable_n += 1
            self._burst_left = BURST_READS
            self._next_read_ts = time.time() + FAST_TICK
        elif self._burst_left > 0:
            self._burst_left -= 1
            self._stable_n = 0
            self._next_read_ts = time.time() + BURST_TICK
        else:
            self._stable_n = 0
            self._next_read_ts = time.time() + SLOW_TICK

        self._win_wid = res["window"]["wid"]
        self._push("applyPosition:", res["window"])
        if res["unchanged"] and self._last_full is not None:
            res = self._last_full
        else:
            self._last_full = res
            title = res.get("chat_title") or ""
            if title != self._chat_title:
                # 换会话必须作废上一个会话的状态：同一句话在新会话里不能被
                # 当成「已经预判过/已经分析过」，否则面板会一直挂着上一个
                # 会话的结论，新会话的真实语境永远判不到。
                self._chat_title = title
                self.last_seen = None
                self.analyzed_text = None
                self._prejudge_result = None
            self._push("applyChat:", title)

        msgs = res["messages"]
        thems = [m for m in msgs if m.side == "them"]
        newest = thems[-1] if thems else None
        prev_text = thems[-2].text if len(thems) > 1 else ""

        # 检测框必须在「没有对方消息」的早退之前推送，否则面板等对方消息时
        # 菜单开了 YOLO 也永远刷不出来（纯视觉层，不依赖判断管线）。
        if self._show_boxes:
            self._push("applyBoxes:", (res["window"], msgs,
                                       newest.text if newest else None))

        if newest is None:
            # 没有可确认的对方消息，就把上一条的追踪状态一并作废：否则判断期间
            # 消息消失时，迟到的预判结果（其守卫 `text == self.last_seen` 仍成立）
            # 会把结论存回来；屏幕空着时留着「已分析」标记，等它再出现也不重判。
            # _prejudge_req 不用清——它同样被 last_seen 守着，下一跳必然被覆盖。
            self.last_seen = None
            self.analyzed_text = None
            self._prejudge_result = None
            # 只在「可见消息变了」时打一行，避免每跳刷屏；否则窗口里全是我方
            # 消息时会完全静默，看起来像读屏坏了。
            sig = tuple(m.text for m in msgs)
            if sig != self._last_nothem_sig:
                self._last_nothem_sig = sig
                _log(f"读屏 {len(msgs)} 条 · 暂无已确认的对方消息")
            self._push("applyWaiting:", None)
            return
        now = time.time()

        if newest.text != self.last_seen:
            self.last_seen = newest.text
            self.last_change_ts = now
            t = res.get("timing_ms") or {}
            first_read = not self._read_once
            self._read_once = True
            note = "（首次，含 Vision 加载）" if first_read and t.get("ocr", 0) > 400 else ""
            slow_cap = " · 抓屏走了子进程" if t.get("capture_path") == "subprocess" else ""
            _log(f"读屏 抓取 {t.get('capture', 0):.0f}ms + OCR {t.get('ocr', 0):.0f}ms"
                 f" = {t.get('total', 0):.0f}ms · {len(msgs)} 条（对方 {len(thems)} 条）"
                 f"{note}{slow_cap}")
            self._prejudge_req = (newest.text, self._context_text(msgs, newest, JUDGE_TURNS),
                                  newest.sender, prev_text)
            self._prejudge_result = None
            self._prejudge_event.set()
            self._push("applyIncoming:", (newest.text, newest.sender, prev_text))

        elapsed = now - self.last_change_ts
        settled = elapsed >= SETTLE_S or (elapsed >= EARLY_SETTLE_S
                                          and self._stable_n >= STABLE_READS)
        cooled = (now - self.last_analyze_ts) >= MIN_GAP_S
        pr = self._prejudge_result
        pre_hit = pr is not None and pr[0] == newest.text
        if (newest.text != self.analyzed_text and settled and not self._analyzing
                and not self._prejudging and (pre_hit or cooled)):
            self.last_analyze_ts = now
            self.analyzed_text = newest.text
            self._prejudge_result = None
            self._analyzing = True
            if pre_hit:
                _log(f"停稳 · 用预判结论上屏 · {now - self.last_change_ts:.1f}s")
                self._push("applyJudgment:", (pr[1], pr[2], pr[3]))
                self._analyzing = False
            else:
                _log(f"开始分析 · {now - self.last_change_ts:.1f}s")
                self._push("applyPending:", (newest.text, newest.sender, prev_text))
                threading.Thread(target=self._run_analysis,
                                 args=(newest, msgs, prev_text), daemon=True).start()
        elif newest.text != self.analyzed_text:
            why = ("消息还在变" if not settled else
                   "上一条还在分析" if self._analyzing else
                   "预判还在跑" if self._prejudging else
                   f"距上次分析不足 {MIN_GAP_S}s")
            if self._last_skip_reason != why:
                self._last_skip_reason = why
                _log(f"暂不分析（{why}）")
        else:
            self._last_skip_reason = None

    @objc.python_method
    def _run_analysis(self, newest, msgs, prev_text: str):
        try:
            if not self._reply_current():
                return
            self._analyze(newest, msgs, prev_text)
        except Exception as e:
            _log(f"分析失败 {type(e).__name__}: {str(e)[:60]}")
            self._push("applyError:", f"分析失败: {type(e).__name__}: {str(e)[:40]}")
        finally:
            self._analyzing = False

    @objc.python_method
    def _prejudge_loop(self):
        while True:
            try:
                self._prejudge_event.wait()
                self._prejudge_event.clear()
                req = self._prejudge_req
                self._prejudge_req = None
                if req is None:
                    continue
                text, context, sender, prev = req
                if self._paused or text != self.last_seen:
                    continue
                self._prejudging = True
                try:
                    t0 = time.perf_counter()
                    with self._model_lock:
                        verdict = self.judge.judge(text, context=context)
                    ms = (time.perf_counter() - t0) * 1000
                    first = not self._judged_once
                    self._judged_once = True
                    note = "（首次，含本地模型加载）" if first else ""
                    _log(f"预判 {ms:.0f}ms → {verdict.get('intent', '?')}"
                         f" 把握 {verdict.get('confidence', 0):.0%}"
                         f" 风险 {verdict.get('risk', '?')}{note}（待停稳上屏）")
                except Exception as e:
                    _log(f"预判失败 {type(e).__name__}: {str(e)[:60]}")
                    verdict = None
                finally:
                    self._prejudging = False
                if verdict is not None and not self._paused and text == self.last_seen:
                    self._prejudge_result = (text, verdict, sender, prev)
            except Exception:
                pass

    @objc.python_method
    def _analyze(self, newest, msgs, prev_text: str = ""):
        t0 = time.perf_counter()
        verdict = None
        t_judge = time.perf_counter()
        try:
            with self._model_lock:
                verdict = self.judge.judge(
                    newest.text, context=self._context_text(msgs, newest, JUDGE_TURNS))
            ms = (time.perf_counter() - t_judge) * 1000
            first = not self._judged_once
            self._judged_once = True
            note = "（首次，含本地模型加载）" if first else ""
            _log(f"判断 {ms:.0f}ms → {verdict.get('intent', '?')}"
                 f" 把握 {verdict.get('confidence', 0):.0%}"
                 f" 风险 {verdict.get('risk', '?')}{note}")
            self._push("applyJudgment:", (verdict, newest.sender, prev_text))
        except Exception as e:
            _log(f"判断失败 {type(e).__name__}: {str(e)[:60]}")
            self._push("applyError:", f"判断失败: {type(e).__name__}: {str(e)[:40]}")

        _log(f"分析完成 · 端到端 {(time.perf_counter() - t0) * 1000:.0f}ms")

    @objc.python_method
    def _context_text(self, msgs, newest, turns: int = CONTEXT_TURNS) -> str | None:
        prior = [m for m in msgs if m is not newest][-turns:]
        if not prior:
            return None
        return "\n".join(
            f"{m.sender or {'me': '我', 'them': '对方'}.get(m.side, '方向未确认')}: {m.text}"
            for m in prior)

    @objc.python_method
    def _push(self, selector: str, payload=None):
        if selector in {"applyIncoming:", "applyPending:", "applyJudgment:",
                        "applyWaiting:", "applyError:"}:
            self.performSelectorOnMainThread_withObject_waitUntilDone_(selector, payload, False)
            return
        self.performSelectorOnMainThread_withObject_waitUntilDone_(selector, payload, False)

    @objc.python_method
    def _reply_current(self):
        return not self._paused

    def applyWaiting_(self, _payload):
        self._show()
        self._last_intent = ""
        self._last_risk = 0.0
        self._pet_tier = 0
        for key in ("message", "sender", "intent", "confidence", "risk", "actions"):
            self._render(key, "", PALETTE["muted"])
        self._render("status", "等待可确认的对方消息…", PALETTE["muted"])
        self._pet_set("idle", 0)

    def applyChat_(self, title):
        self._chat_title = title
        self._render("chat", title, PALETTE["green"])

    def applyIncoming_(self, payload):
        text, sender, prev = payload
        self._show()
        self._render("status", "有新消息 · 等消息停稳…", PALETTE["muted"])
        self._render("message", text, PALETTE["muted"])
        self._render("sender", self._context_line(sender, prev), PALETTE["muted"])
        self._pet_set("alert", self._pet_tier)

    def applyPending_(self, payload):
        text, sender, prev = payload
        self._show()
        self._render("status", "分析中…", PALETTE["muted"])
        self._render("message", text, PALETTE["text"])
        self._render("sender", self._context_line(sender, prev), PALETTE["muted"])
        self._pet_set("think", self._pet_tier)

    def applyJudgment_(self, payload):
        v, sender, prev = payload
        self._show()
        self._last_intent = v.get("intent", "")
        self._last_risk = v.get("risk", 0)
        backend = v.get("backend", "")
        if "laya" in backend.lower():
            self._render("status", f"本地 Laya · {v.get('elapsed_ms', 0):.0f}ms", PALETTE["green"])
        elif "jev" in backend.lower():
            self._render("status", f"TypeSafe Jev", PALETTE["muted"])
        else:
            self._render("status", "分析完成", PALETTE["muted"])
        self._render("message", v["message"], PALETTE["text"])
        self._render("sender", self._context_line(sender, prev), PALETTE["muted"])
        self._render("intent", v["intent"], PALETTE["text"])
        self._render("confidence", f"意图识别率 {v['confidence']:.0%}", PALETTE["muted"])
        risk = int(round(float(v.get("risk", 0))))
        label = "安全" if risk <= 3 else ("留神" if risk <= 6 else "危险")
        color = PALETTE["green"] if risk <= 3 else (
            PALETTE["amber"] if risk <= 6 else PALETTE["red"])
        self._render("risk", f"● {label}  {risk}/9", color)
        self._render("actions", " · ".join(v.get("actions", [])), PALETTE["text"])
        # 桌宠：意图给表情，风险给描边色，把握低<0.5 加问号角标
        tier = 0 if risk <= 3 else (1 if risk <= 6 else 2)
        self._pet_tier = tier
        self._pet_set(_MOOD_BY_INTENT.get(v["intent"], "ready"), tier,
                      low_conf=float(v.get("confidence", 1.0)) < 0.5)

    def applyError_(self, text):
        self._show()
        self._render("status", text, PALETTE["red"])
        self._pet_set("error", 2)

    def applyHidden_(self, reason):
        self._render("status", reason, PALETTE["muted"])
        if self.panel.isVisible():
            self.panel.orderOut_(None)
        if self._ov_panel.isVisible():
            self._ov_panel.orderOut_(None)
        if hasattr(self, "_pet_panel") and self._pet_panel.isVisible():
            self._pet_panel.orderOut_(None)

    def applyPosition_(self, win):
        self._position_near(win)
        self._place_pet()

    def applyBoxes_(self, payload):
        if not self._show_boxes:
            return
        win, msgs, newest_text = payload
        W, H = win["w"], win["h"]
        flip = self._display_height()
        self._ov_panel.setFrame_display_(
            NSMakeRect(win["x"], flip - win["y"] - H, W, H), False)
        font = (NSFont.fontWithName_size_("Menlo-Bold", 10)
                or NSFont.boldSystemFontOfSize_(10))
        judged = (newest_text is not None and newest_text == self.analyzed_text
                  and bool(self._last_intent))
        risk = int(round(float(self._last_risk)))
        boxes = []
        for m in msgs:
            if m.w <= 0:
                continue
            who = m.sender or {"them": "对方", "me": "我"}.get(m.side, "方向未确认")
            label = f"{who} {m.conf:.2f}"
            if judged and m.side == "them" and m.text == newest_text:
                color = (PALETTE["green"] if risk <= 3 else
                         PALETTE["amber"] if risk <= 6 else PALETTE["red"])
                lw = 2.5
                label += f" · {self._last_intent} 风险{risk}/9"
            else:
                color = _rgb(0x576B95) if m.side == "me" else PALETTE["green"]
                lw = 1.5
            chip = NSAttributedString.alloc().initWithString_attributes_(
                label,
                {NSFontAttributeName: font,
                 NSForegroundColorAttributeName: NSColor.whiteColor(),
                 NSBackgroundColorAttributeName: color.colorWithAlphaComponent_(0.85)})
            y = H - (m.y + m.h) * H
            boxes.append((NSMakeRect(m.x * W, y, m.w * W, m.h * H), color, lw, chip))
        view = self._ov_panel.contentView()
        view.boxes = boxes
        view.setNeedsDisplay_(True)
        if not self._ov_panel.isVisible():
            self._ov_panel.orderFrontRegardless()

    def toggleBoxes_(self, sender):
        self._show_boxes = not self._show_boxes
        self.boxes_item.setState_(
            AppKit.NSOnState if self._show_boxes else AppKit.NSOffState)
        if not self._show_boxes:
            if self._ov_panel.isVisible():
                self._ov_panel.orderOut_(None)
            return
        _log("YOLO 检测框 开")
        # 立刻用最近一次读屏的结果上屏，不等下一跳（否则点了没反应）。
        cached = self._last_full
        if cached and cached.get("ok") and not cached.get("unchanged"):
            msgs = cached["messages"]
            thems = [m for m in msgs if m.side == "them"]
            newest = thems[-1].text if thems else None
            self.applyBoxes_((cached["window"], msgs, newest))
            _log(f"YOLO 立即上屏 · {len(msgs)} 框（缓存）")
        else:
            _log("YOLO 等待下一跳读屏（暂无缓存）")

    def togglePet_(self, sender):
        self._show_pet = not self._show_pet
        self.pet_item.setState_(
            AppKit.NSOnState if self._show_pet else AppKit.NSOffState)
        if not self._show_pet:
            if self._pet_panel.isVisible():
                self._pet_panel.orderOut_(None)
            _log("桌宠 关")
            return
        _log("桌宠 开")
        v = self._pet_view
        self._place_pet()
        self._pet_set(v.mood, v.tier, v.low_conf)

    @objc.python_method
    def _warm(self):
        t0 = time.perf_counter()
        ocr_ms = warm_ocr()
        if ocr_ms >= 0:
            self._read_once = True
            _log(f"预热 OCR 就绪 · {ocr_ms:.0f}ms")
        else:
            _log("预热 OCR 失败 · 首次读屏会稍慢")

        try:
            self.judge.warm()
        except Exception as e:
            _log(f"预热判断模型失败 {type(e).__name__}: {str(e)[:60]}")
        else:
            self._judged_once = True
            _log(f"预热 判断模型就绪 · 总耗时 {(time.perf_counter() - t0) * 1000:.0f}ms")


def main() -> None:
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    controller = HudController.alloc().init()
    _log(f"启动 · 判断层 本地 Laya · 生成层 关闭 · 纯本地运行，无数据外传")
    controller._show()
    threading.Thread(target=controller._warm, daemon=True).start()
    timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        FAST_TICK, controller, "tick:", None, True)
    AppKit.NSRunLoop.currentRunLoop().addTimer_forMode_(timer, AppKit.NSDefaultRunLoopMode)
    app.run()


if __name__ == "__main__":
    main()
