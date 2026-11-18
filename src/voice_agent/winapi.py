"""前面ウィンドウ・プロセス名の取得とウィンドウの前面化（pywin32）。"""
import ctypes
import os
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


def activate(hwnd: int) -> bool:
    """ウィンドウを前面にする。Windows は他プロセスからの前面化を制限するので Alt を押して解除する。"""
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
        win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
        win32gui.SetForegroundWindow(hwnd)
        return win32gui.GetForegroundWindow() == hwnd
    except Exception:
        return False
