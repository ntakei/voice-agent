"""検証済みのアクションを実行する（dry-run なら何も操作せず、実行内容を返すだけ）。

安全ルール（確認・パスワード拒否・前面アプリ照合など）は safety.py でこの手前に掛ける（フェーズ6）。
"""
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass

import pyautogui
import pyperclip

from . import actions, uia, winapi

pyautogui.FAILSAFE = True   # マウスを画面の隅へ動かすと FailSafeException で止まる
pyautogui.PAUSE = 0.05

WHEEL_DELTA = 120           # Windows のホイール1目盛り
LAUNCH_WAIT = 5.0           # 起動したアプリのウィンドウが出るまで待つ最大秒数


@dataclass
class ExecResult:
    label: str                         # 日本語ラベル（ログ・読み上げ用）
    point: tuple[int, int] | None      # 実画面の座標（クリック・スクロール位置）
    executed: bool                     # 実際に操作したか（dry-run なら False）
    detail: str = ""                   # 補足（どのウィンドウを前面にしたか等）


class Executor:
    def __init__(self, screen_size: tuple[int, int], apps: list[actions.App], dry_run: bool = True,
                 stop_event: threading.Event | None = None, maximize: bool = True):
        self.screen_size = screen_size
        self.maximize = maximize   # open_app で前面にしたウィンドウを最大化する
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
        if t == "wait":
            # 読み込みや回答の生成を待つ。停止要求が来たらすぐ戻る
            if self.dry_run:
                return ExecResult(label, None, False, f"{a.get('seconds', 2):.0f} 秒待つ")
            self.stop_event.wait(float(a.get("seconds", 2)))
            return ExecResult(label, None, True)
        if t == "open_app":
            return self._open_app(a, label)
        if self.dry_run:
            return ExecResult(label, point, False, self._dry_detail(a))
        if self.stop_event.is_set():
            return ExecResult(label, point, False, "停止要求のため実行せず")

        if t == "click":
            pyautogui.moveTo(*point, duration=0.25, tween=pyautogui.easeOutQuad)
            detail = click_and_watch_focus()
            if detail:
                return ExecResult(label, point, True, detail)
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

    def press_link(self, a: dict, image_size: tuple[int, int]) -> ExecResult | None:
        """リンクを UI Automation で直接押す（マウスはその位置へ動かして見せるだけ）。押せなければ None。

        フェーズ7: Edge ではページ内のリンクをマウスでクリックしても開かなかった（Chrome では開いた）。
        """
        point = actions.to_screen_point(a["x"], a["y"], image_size, self.screen_size)
        label = actions.describe(a, point)
        if self.dry_run:
            return ExecResult(label, point, False, "リンクを UI Automation で直接押す")
        if self.stop_event.is_set():
            return ExecResult(label, point, False, "停止要求のため実行せず")
        pyautogui.moveTo(*point, duration=0.25, tween=pyautogui.easeOutQuad)
        what = uia.invoke_at(*point)
        if not what:
            return None
        return ExecResult(label, point, True, f"{what} を UI Automation で直接押した")

    press_element = press_link   # 番号で選んだボタン・リンクも同じ方法で押す

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
            mx = "（最大化）" if self.maximize else ""
            if existing:
                return ExecResult(label, None, False, f"既存のウィンドウを前面にする{mx}: {existing.describe()}")
            return ExecResult(label, None, False, f"起動する{mx}: {app.launch}")
        if self.stop_event.is_set():
            return ExecResult(label, None, False, "停止要求のため実行せず")
        if existing:
            ok = winapi.activate(existing.hwnd)
            mx = self._maximize(existing.hwnd)
            return ExecResult(label, None, True, f"前面化{'成功' if ok else '失敗'}{mx}: {existing.describe()}")
        try:
            how = launch(app.launch)
        except OSError as e:
            return ExecResult(label, None, False, f"起動に失敗: {app.launch}（{type(e).__name__}: {e}）")
        # ウィンドウが出るまで最大 LAUNCH_WAIT 秒待つ（画面の落ち着き待ちはループ側で行う）
        end = time.time() + LAUNCH_WAIT
        while time.time() < end and not self.stop_event.is_set():
            w = winapi.find_window(app.process)
            if w:
                winapi.activate(w.hwnd)
                mx = self._maximize(w.hwnd)
                return ExecResult(label, None, True, f"起動（{how}）{mx}: {w.describe()}")
            time.sleep(0.25)
        return ExecResult(label, None, True, f"起動したがウィンドウが見つからない: {app.launch}（{how}）")


    def _maximize(self, hwnd: int) -> str:
        if not self.maximize:
            return ""
        return "・最大化" if winapi.maximize(hwnd) else "・最大化できず"


def click_and_watch_focus() -> str:
    """押す・離すを分けてクリックし、その間に前面ウィンドウが変わったら記録する（原因調べ用）。

    フェーズ7: Edge の中をクリックすると、後ろのウィンドウ（Claude・デスクトップ）が前面に来て、
    リンクのクリックが効かないことがあった。押した時点で変わるのか、離した後に変わるのかを残す。
    """
    before = winapi.foreground()
    pyautogui.mouseDown()
    after_down = winapi.foreground()
    pyautogui.mouseUp()
    after_up = winapi.foreground()
    hw = [w.hwnd if w else None for w in (before, after_down, after_up)]
    if hw[0] == hw[1] == hw[2]:
        return ""
    name = lambda w: w.describe() if w else "なし"  # noqa: E731
    return f"クリック中に前面が変わった: 押す前 {name(before)} / 押した直後 {name(after_down)} / 離した直後 {name(after_up)}"


def launch(exe: str) -> str:
    """許可リストのアプリを起動する。戻り値は使った方法（ログ用）。

    1. PATH 上にあれば CreateProcess（subprocess）。Windows 11 のメモ帳などの「アプリ実行エイリアス」は
       os.startfile（ShellExecute）だと WinError 5 になることがあるため、こちらを優先する。
    2. PATH になければ os.startfile（App Paths で解決。msedge.exe など）。
    """
    path = shutil.which(exe)
    if path:
        subprocess.Popen([path], close_fds=True, creationflags=subprocess.DETACHED_PROCESS)
        return "CreateProcess"
    os.startfile(exe)
    return "ShellExecute"


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
