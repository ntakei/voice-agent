"""前面ウィンドウ・プロセス名の取得とウィンドウの前面化（pywin32）。"""
import ctypes
import os
import time
from dataclasses import dataclass

import win32api
import win32con
import win32gui
import win32process

# デスクトップやタスクバーなど、アプリのウィンドウとして扱わないクラス
_SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    exe: str          # 実行ファイル名（例: notepad.exe）。取得できなければ ""
    cls: str = ""

    def describe(self) -> str:
        return f"{self.exe or '?'} 「{self.title[:40]}」"


def process_name(pid: int) -> str:
    try:
        h = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    except Exception:
        return ""
    try:
        return os.path.basename(win32process.GetModuleFileNameEx(h, 0))
    except Exception:
        return ""
    finally:
        win32api.CloseHandle(h)


def window_info(hwnd: int) -> WindowInfo:
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    exe = process_name(pid)
    # UWP アプリ（電卓など）は ApplicationFrameHost.exe の中にいるので、子ウィンドウの本体を探す
    if exe.lower() == "applicationframehost.exe":
        found = []

        def cb(child, _):
            _, cpid = win32process.GetWindowThreadProcessId(child)
            if cpid != pid:
                found.append(process_name(cpid))
        try:
            win32gui.EnumChildWindows(hwnd, cb, None)
        except Exception:
            pass
        exe = next((n for n in found if n), exe)
    return WindowInfo(hwnd, win32gui.GetWindowText(hwnd), exe, win32gui.GetClassName(hwnd))


def foreground() -> WindowInfo | None:
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return None
    return window_info(hwnd)


def window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """ウィンドウの矩形（実画面のピクセル。left, top, right, bottom）。最小化中や取得できなければ None。"""
    try:
        if not hwnd or win32gui.IsIconic(hwnd):
            return None
        l, t, r, b = win32gui.GetWindowRect(hwnd)
    except Exception:
        return None
    return (l, t, r, b) if r > l and b > t else None


def is_cloaked(hwnd: int) -> bool:
    """見えない UWP ウィンドウ（別の仮想デスクトップや待機中のもの）なら True。"""
    val = ctypes.c_int(0)
    try:
        ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(val), ctypes.sizeof(val))  # DWMWA_CLOAKED
    except Exception:
        return False
    return val.value != 0


def app_windows() -> list[WindowInfo]:
    """表示中のトップレベルのアプリウィンドウ（Z順、手前から）。"""
    out = []

    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd) or not win32gui.GetWindowText(hwnd) or is_cloaked(hwnd):
            return True
        if win32gui.GetClassName(hwnd) in _SHELL_CLASSES:
            return True
        if win32gui.GetWindow(hwnd, win32con.GW_OWNER):
            return True  # ダイアログなど所有されたウィンドウは除く
        out.append(window_info(hwnd))
        return True
    win32gui.EnumWindows(cb, None)
    return out


def find_window(process_names: list[str]) -> WindowInfo | None:
    want = {p.lower() for p in process_names}
    for w in app_windows():
        if w.exe.lower() in want:
            return w
    return None


# 割り当てのない仮想キー。前面化の制限を解くための「入力」として送っても、どのアプリにも意味を持たない。
# ※ Alt（VK_MENU）は使わない: メモ帳などがキーヒント待ちになり、その後のキー入力が吸い込まれる（フェーズ6で確認）。
#   Shift も使わない: 日本語 IME の英数切り替えになることがある。
_VK_UNASSIGNED = 0x07


def _wait_foreground(hwnd: int, secs: float) -> bool:
    """切り替えは非同期なので、少し待ってから確かめる。"""
    end = time.time() + secs
    while time.time() < end:
        if win32gui.GetForegroundWindow() == hwnd:
            return True
        time.sleep(0.05)
    return win32gui.GetForegroundWindow() == hwnd


def _set_foreground(hwnd: int) -> None:
    """今の前面スレッドの入力に一時的につないでから前面化する（AttachThreadInput）。"""
    user32 = ctypes.windll.user32
    cur = win32api.GetCurrentThreadId()
    fg = win32gui.GetForegroundWindow()
    fg_tid = win32process.GetWindowThreadProcessId(fg)[0] if fg else 0
    attached = bool(fg_tid and fg_tid != cur and user32.AttachThreadInput(cur, fg_tid, True))
    try:
        win32gui.BringWindowToTop(hwnd)
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass
    finally:
        if attached:
            user32.AttachThreadInput(cur, fg_tid, False)


def activate(hwnd: int) -> bool:
    """ウィンドウを前面にする（Alt キーは送らない）。"""
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        _set_foreground(hwnd)
        if _wait_foreground(hwnd, 0.5):
            return True
        # まだ前面にならなければ、意味のないキーを1回送って前面化の制限を解き、もう一度試す
        win32api.keybd_event(_VK_UNASSIGNED, 0, 0, 0)
        win32api.keybd_event(_VK_UNASSIGNED, 0, win32con.KEYEVENTF_KEYUP, 0)
        _set_foreground(hwnd)
        return _wait_foreground(hwnd, 1.0)
    except Exception:
        return False
