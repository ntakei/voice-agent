"""画面上の状態表示（macOS の Spotlight のような、画面の上のほうに浮かぶ角丸のバー）。

待機中・録音中（F9）・認識中・考え中・操作中・確認待ち・完了・失敗・停止を、端末を見なくても分かるように出す。

- 常に最前面。クリックは下のウィンドウに素通り（WS_EX_TRANSPARENT）、フォーカスも取らない（WS_EX_NOACTIVATE）。
  エージェントのクリックやキー入力、前面アプリの判定を邪魔しない。
- スクリーンショットには写らない（SetWindowDisplayAffinity: WDA_EXCLUDEFROMCAPTURE）。モデルには見えない。
- 描画は PIL で行い、UpdateLayeredWindow で半透明・角丸のまま表示する。
- 専用スレッドで動く。show() はどのスレッドから呼んでもよい。
"""
import ctypes
import math
import threading
import time
from ctypes import wintypes

from PIL import Image, ImageDraw, ImageFont

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

WS_POPUP = 0x80000000
WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_TOPMOST = 0x80000, 0x20, 0x8
WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x80, 0x08000000
SW_SHOWNOACTIVATE, SW_HIDE = 4, 0
ULW_ALPHA, AC_SRC_OVER, AC_SRC_ALPHA = 2, 0, 1
WDA_EXCLUDEFROMCAPTURE = 0x11
PM_REMOVE = 1
HWND_TOPMOST = -1
SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE = 2, 1, 0x10

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
# 64bit のハンドルを int として渡すと桁あふれするので、引数と戻り値の型を宣言しておく
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
                                   wintypes.HANDLE, wintypes.DWORD]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
user32.UpdateLayeredWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.c_void_p, ctypes.c_void_p, wintypes.HDC,
                                       ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                wintypes.UINT]
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.PeekMessageW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
user32.TranslateMessage.argtypes = [ctypes.c_void_p]
user32.DispatchMessageW.argtypes = [ctypes.c_void_p]
user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


# 状態ごとの色（アイコンの丸）と記号
STATES = {
    "ready":        ((142, 142, 147), "mic"),
    "listening":    ((255, 69, 58), "rec"),
    "transcribing": ((10, 132, 255), "spin"),
    "correcting":   ((10, 132, 255), "spin"),
    "start":        ((10, 132, 255), "spin"),
    "looking":      ((10, 132, 255), "spin"),
    "thinking":     ((191, 90, 242), "spin"),
    "acting":       ((48, 209, 88), "▶"),
    "confirm":      ((255, 159, 10), "!"),
    "blocked":      ((255, 159, 10), "!"),
    "ignored":      ((255, 159, 10), "?"),
    "done":         ((48, 209, 88), "✓"),
    "warn":         ((255, 159, 10), "✓"),
    "fail":         ((255, 69, 58), "✕"),
    "stopped":      ((142, 142, 147), "■"),
}
ANIMATED = {"listening", "transcribing", "correcting", "start", "looking", "thinking", "acting"}
# この時間がたったら待機中の表示に戻す
SETTLE_SECS = {"done": 6.0, "warn": 8.0, "fail": 8.0, "stopped": 5.0, "ignored": 5.0}


def _font(names: list[str], size: int):
    for n in names:
        try:
            return ImageFont.truetype(n, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _fit(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> str:
    """幅に収まらなければ末尾を … にする。"""
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text + "…"


def premultiplied_bgra(img: Image.Image) -> bytes:
    """RGBA 画像 → 乗算済みアルファの BGRA（UpdateLayeredWindow の決まり）。"""
    import numpy as np
    w, h = img.size
    arr = np.frombuffer(img.convert("RGBA").tobytes(), dtype=np.uint8).reshape(h, w, 4).astype(np.uint16)
    alpha = arr[:, :, 3:4]
    bgr = (arr[:, :, [2, 1, 0]] * alpha // 255).astype(np.uint8)
    return np.concatenate([bgr, alpha.astype(np.uint8)], axis=2).tobytes()


class Renderer:
    """状態を角丸のバーの画像（RGBA）にする。ウィンドウとは独立（テストしやすいように）。"""

    def __init__(self, width: int = 760, height: int = 76, scale: float = 1.0, hints: list[str] | None = None):
        self.s = scale
        self.w, self.h = int(width * scale), int(height * scale)
        self.f_main = _font(["YuGothB.ttc", "meiryob.ttc", "meiryo.ttc", "msgothic.ttc"], int(23 * scale))
        self.f_sub = _font(["YuGothM.ttc", "meiryo.ttc", "msgothic.ttc"], int(14 * scale))
        self.f_sym = _font(["seguisym.ttf", "segoeui.ttf", "arial.ttf"], int(22 * scale))
        self.f_chip = _font(["YuGothM.ttc", "meiryo.ttc", "segoeui.ttf"], int(12 * scale))
        self.hints = hints or []

    def render(self, state: str, main: str, sub: str = "", t: float = 0.0, levels: list[float] | None = None):
        s = self.s
        img = Image.new("RGBA", (self.w, self.h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        color, sym = STATES.get(state, STATES["ready"])
        r = int(20 * s)
        d.rounded_rectangle([0, 0, self.w - 1, self.h - 1], radius=r, fill=(30, 30, 34, 235),
                            outline=(255, 255, 255, 46), width=max(1, int(s)))
        # 左のアイコン
        cx, cy, rad = int(40 * s), self.h // 2, int(21 * s)
        if sym == "rec":                                  # 録音中: 脈打つ赤い丸
            pulse = 0.82 + 0.18 * math.sin(t * 6.0)
            rr = int(rad * pulse)
            d.ellipse([cx - rad - 5 * s, cy - rad - 5 * s, cx + rad + 5 * s, cy + rad + 5 * s],
                      fill=(*color, int(60 + 40 * math.sin(t * 6.0))))
            d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=(*color, 255))
            self._mic(d, cx, cy, s, (255, 255, 255, 255))
        elif sym == "mic":
            d.ellipse([cx - rad, cy - rad, cx + rad, cy + rad], fill=(*color, 70))
            self._mic(d, cx, cy, s, (235, 235, 240, 255))
        elif sym == "spin":                               # 処理中: 回る弧
            d.ellipse([cx - rad, cy - rad, cx + rad, cy + rad], outline=(*color, 70), width=int(4 * s))
            a0 = (t * 300) % 360
            d.arc([cx - rad, cy - rad, cx + rad, cy + rad], a0, a0 + 100, fill=(*color, 255), width=int(4 * s))
        else:
            d.ellipse([cx - rad, cy - rad, cx + rad, cy + rad], fill=(*color, 255))
            tw = d.textlength(sym, font=self.f_sym)
            d.text((cx - tw / 2, cy - self.f_sym.size * 0.62), sym, font=self.f_sym, fill=(255, 255, 255, 255))
        # 右のキーの案内
        x_right = self.w - int(16 * s)
        for hint in reversed(self.hints):
            tw = d.textlength(hint, font=self.f_chip)
            x0 = x_right - tw - 16 * s
            d.rounded_rectangle([x0, cy - 12 * s, x_right, cy + 12 * s], radius=int(8 * s), fill=(255, 255, 255, 28))
            d.text((x0 + 8 * s, cy - self.f_chip.size * 0.62), hint, font=self.f_chip, fill=(200, 200, 205, 255))
            x_right = x0 - 8 * s
        # 文字
        x = int(76 * s)
        width = int(x_right - x - 12 * s)
        if state == "listening" and levels:              # 録音中: 声の大きさのバー
            bw, gap = int(5 * s), int(3 * s)
            n = max(1, min(len(levels), width // (bw + gap)))
            for i, lv in enumerate(levels[-n:]):
                hh = max(int(3 * s), int(min(1.0, lv * 12) * 26 * s))
                bx = x + i * (bw + gap)
                d.rounded_rectangle([bx, cy + 18 * s - hh, bx + bw, cy + 18 * s], radius=int(2 * s),
                                    fill=(*color, 230))
            d.text((x, cy - 30 * s), _fit(d, main, self.f_sub, width), font=self.f_sub, fill=(235, 235, 240, 255))
            return img
        if sub:
            d.text((x, cy - 26 * s), _fit(d, main, self.f_main, width), font=self.f_main, fill=(245, 245, 247, 255))
            d.text((x, cy + 6 * s), _fit(d, sub, self.f_sub, width), font=self.f_sub, fill=(160, 160, 168, 255))
        else:
            d.text((x, cy - 15 * s), _fit(d, main, self.f_main, width), font=self.f_main, fill=(245, 245, 247, 255))
        return img

    @staticmethod
    def _mic(d, cx, cy, s, fill):
        w, h = 7 * s, 11 * s
        d.rounded_rectangle([cx - w, cy - h - 2 * s, cx + w, cy + 3 * s], radius=int(w), fill=fill)
        d.arc([cx - 11 * s, cy - 10 * s, cx + 11 * s, cy + 9 * s], 20, 160, fill=fill, width=max(1, int(2 * s)))
        d.line([cx, cy + 9 * s, cx, cy + 14 * s], fill=fill, width=max(1, int(2 * s)))


class Overlay:
    def __init__(self, cfg: dict | None = None, hints: list[str] | None = None, level_fn=None):
        oc = ((cfg or {}).get("ui") or {}).get("overlay") or {}
        self.position = str(oc.get("position", "top"))
        self.y_ratio = float(oc.get("y", 0.10))
        self.width = int(oc.get("width", 760))
        self.exclude_from_capture = bool(oc.get("exclude_from_capture", True))
        self.hints = hints or []
        self.level_fn = level_fn                     # 録音中の声の大きさ（0〜1）を返す関数
        self._lock = threading.Lock()
        self._state = ("ready", "", "", time.time())
        self._dirty = True
        self._running = False
        self._thread = None
        self.hwnd = None
        self.captured_excluded = False
        self._levels: list[float] = []

    # ---- 呼び出し側（どのスレッドからでも）----

    def show(self, state: str, main: str, sub: str = "") -> None:
        with self._lock:
            self._state = (state, main, sub, time.time())
            self._dirty = True
            if state == "listening":
                self._levels = []

    def start(self) -> "Overlay":
        if self._thread is None:
            ready = threading.Event()
            self._running = True
            self._thread = threading.Thread(target=self._run, args=(ready,), name="overlay", daemon=True)
            self._thread.start()
            ready.wait(3)
        return self

    def close(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(2)
            self._thread = None

    # ---- 表示スレッド ----

    def _run(self, ready: threading.Event) -> None:
        try:
            scale = user32.GetDpiForSystem() / 96.0
        except Exception:  # noqa: BLE001
            scale = 1.0
        self.renderer = Renderer(self.width, 76, scale, self.hints)
        self._wndproc = WNDPROC(lambda h, m, w, l: user32.DefWindowProcW(h, m, w, l))
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSEXW(cbSize=ctypes.sizeof(WNDCLASSEXW), lpfnWndProc=self._wndproc, hInstance=hinst,
                         lpszClassName="VoiceAgentOverlay")
        user32.RegisterClassExW(ctypes.byref(wc))
        sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        w, h = self.renderer.w, self.renderer.h
        x = (sw - w) // 2
        y = int(sh * self.y_ratio) if self.position == "top" else int(sh - h - 72 * scale)
        ex = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        self.hwnd = user32.CreateWindowExW(ex, "VoiceAgentOverlay", "", WS_POPUP, x, y, w, h, None, None, hinst, None)
        self._pos = (x, y)
        if self.exclude_from_capture:
            self.captured_excluded = bool(user32.SetWindowDisplayAffinity(self.hwnd, WDA_EXCLUDEFROMCAPTURE))
        user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
        ready.set()
        msg = wintypes.MSG()
        last_top = 0.0
        while self._running:
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            with self._lock:
                state, main, sub, since = self._state
                dirty, self._dirty = self._dirty, False
            if state in SETTLE_SECS and time.time() - since > SETTLE_SECS[state]:
                # 完了・失敗などの表示は、しばらくしたら待機中に戻す
                self.show("ready", self.idle_main, "待機中")
                continue
            if state == "listening" and self.level_fn:
                try:
                    self._levels.append(float(self.level_fn()))
                except Exception:  # noqa: BLE001
                    pass
                self._levels = self._levels[-80:]
            if dirty or state in ANIMATED:
                self._paint(self.renderer.render(state, main, sub, time.time(), self._levels))
            if time.time() - last_top > 1.0:       # 他の最前面ウィンドウの下に隠れないように
                user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
                last_top = time.time()
            time.sleep(1 / 20)
        user32.DestroyWindow(self.hwnd)
        user32.UnregisterClassW("VoiceAgentOverlay", hinst)

    idle_main = "F9 を押しながら話してください"

    def _paint(self, img: Image.Image) -> None:
        w, h = img.size
        data = premultiplied_bgra(img)
        bmi = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=w, biHeight=-h, biPlanes=1,
                               biBitCount=32, biCompression=0)
        screen_dc = user32.GetDC(None)
        mem_dc = gdi32.CreateCompatibleDC(screen_dc)
        bits = ctypes.c_void_p()
        hbmp = gdi32.CreateDIBSection(mem_dc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        ctypes.memmove(bits, data, len(data))
        old = gdi32.SelectObject(mem_dc, hbmp)
        size = wintypes.SIZE(w, h)
        src = wintypes.POINT(0, 0)
        dst = wintypes.POINT(*self._pos)
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        user32.UpdateLayeredWindow(self.hwnd, screen_dc, ctypes.byref(dst), ctypes.byref(size), mem_dc,
                                   ctypes.byref(src), 0, ctypes.byref(blend), ULW_ALPHA)
        gdi32.SelectObject(mem_dc, old)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(None, screen_dc)


class NullOverlay:
    """表示しない（ui.overlay.enabled: false のとき）。"""

    def show(self, state: str, main: str, sub: str = "") -> None:
        pass

    def start(self):
        return self

    def close(self) -> None:
        pass
