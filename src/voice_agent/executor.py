"""検証済みのアクションを実行する（dry-run なら何も操作せず、実行内容を返すだけ）。

安全ルール（確認・パスワード拒否・前面アプリ照合など）は safety.py でこの手前に掛ける（フェーズ6）。
"""
import os
import threading
import time
from dataclasses import dataclass

import pyautogui
import pyperclip

from . import actions, winapi

pyautogui.FAILSAFE = True   # マウスを画面の隅へ動かすと FailSafeException で止まる
pyautogui.PAUSE = 0.05

WHEEL_DELTA = 120           # Windows のホイール1目盛り


@dataclass
class ExecResult:
    label: str                         # 日本語ラベル（ログ・読み上げ用）
    point: tuple[int, int] | None      # 実画面の座標（クリック・スクロール位置）
    executed: bool                     # 実際に操作したか（dry-run なら False）
    detail: str = ""                   # 補足（どのウィンドウを前面にしたか等）


class Executor:
    def __init__(self, screen_size: tuple[int, int], apps: list[actions.App], dry_run: bool = True,
                 stop_event: threading.Event | None = None):
        self.screen_size = screen_size
        self.apps = apps
        self.dry_run = dry_run
        self.stop_event = stop_event or threading.Event()

    def execute(self, a: dict, image_size: tuple[int, int]) -> ExecResult:
        """a は normalize + validate 済みであること。"""
        t = a["action"]
        point = None
        if t in actions.CLICKS or (t == "scroll" and a.get("x") is not None):
            point = actions.to_screen_point(a["x"], a["y"], image_size, self.screen_size)
        label = actions.describe(a, point)

        if t in ("done", "fail"):
            return ExecResult(label, None, False)
        if t == "open_app":
            return self._open_app(a, label)
        if self.dry_run:
            return ExecResult(label, point, False, self._dry_detail(a))
        if self.stop_event.is_set():
            return ExecResult(label, point, False, "停止要求のため実行せず")

        if t == "click":
            pyautogui.moveTo(*point, duration=0.25, tween=pyautogui.easeOutQuad)
            pyautogui.click()
        elif t == "double_click":
            pyautogui.moveTo(*point, duration=0.25, tween=pyautogui.easeOutQuad)
            pyautogui.doubleClick(interval=0.08)
        elif t == "type":
            paste_text(a["text"])
        elif t == "key":
            pyautogui.hotkey(*actions.parse_keys(a["text"]), interval=0.05)
        elif t == "scroll":
            if point:
                pyautogui.moveTo(*point, duration=0.15)
            pyautogui.scroll(a["dy"] * WHEEL_DELTA)
        return ExecResult(label, point, True)

    def _dry_detail(self, a: dict) -> str:
        t = a["action"]
        if t == "type":
            return f"クリップボード経由で {len(a['text'])} 文字を貼り付け（Ctrl+V）"
        if t == "key":
            return f"pyautogui.hotkey{tuple(actions.parse_keys(a['text']))}"
        if t == "scroll":
            return f"ホイール {a['dy'] * WHEEL_DELTA}"
        return ""

    def _open_app(self, a: dict, label: str) -> ExecResult:
        app = actions.resolve_app(a["text"], self.apps)
        if app is None:  # validate 済みなら起きない
            return ExecResult(label, None, False, "許可されていないアプリ")
        existing = winapi.find_window(app.process)
        if self.dry_run:
            if existing:
                return ExecResult(label, None, False, f"既存のウィンドウを前面にする: {existing.describe()}")
            return ExecResult(label, None, False, f"起動する: {app.launch}")
        if self.stop_event.is_set():
            return ExecResult(label, None, False, "停止要求のため実行せず")
        if existing:
            ok = winapi.activate(existing.hwnd)
            return ExecResult(label, None, True, f"前面化{'成功' if ok else '失敗'}: {existing.describe()}")
        os.startfile(app.launch)  # App Paths で解決される（msedge.exe など）
        # ウィンドウが出るまで最大5秒待つ（画面の落ち着き待ちはループ側で行う）
        end = time.time() + 5
        while time.time() < end and not self.stop_event.is_set():
            w = winapi.find_window(app.process)
            if w:
                winapi.activate(w.hwnd)
                return ExecResult(label, None, True, f"起動: {w.describe()}")
            time.sleep(0.25)
        return ExecResult(label, None, True, f"起動したがウィンドウが見つからない: {app.launch}")


def paste_text(text: str) -> None:
    """日本語を含む文字列をクリップボード経由で入力し、元のクリップボードを戻す。"""
    try:
        old = pyperclip.paste()
    except Exception:
        old = None
    pyperclip.copy(text)
    time.sleep(0.05)
    pyautogui.hotkey("ctrl", "v", interval=0.05)
    time.sleep(0.3)  # 貼り付けが終わる前に戻すと、元の内容が貼られてしまう
    if old is not None:
        try:
            pyperclip.copy(old)
        except Exception:
            pass
