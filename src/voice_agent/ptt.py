"""押して話す（push-to-talk）キーの監視。既定は F9（config.yaml の voice.ptt_key で変更）。

- 押した／離したを、イベントのキュー（"down" / "up"）に入れる。キーリピートは無視する。
- プログラム（pyautogui）が送ったキーは無視する。
- suppress=True なら、このキーを他のアプリに渡さない（前面アプリで F9 が別の意味を持っていても動かないように）。
"""
import queue

from pynput import keyboard

_WM_KEYDOWN, _WM_KEYUP, _WM_SYSKEYDOWN, _WM_SYSKEYUP = 0x100, 0x101, 0x104, 0x105
_INJECTED = 0x10 | 0x02   # LLKHF_INJECTED | LLKHF_LOWER_IL_INJECTED

_NAMED = {"pause": 0x13, "scroll_lock": 0x91, "insert": 0x2D, "home": 0x24, "end": 0x23,
          "right_ctrl": 0xA3, "right_alt": 0xA5, "right_shift": 0xA1, "menu": 0x5D, "apps": 0x5D}


def vk_of(name: str) -> int:
    """キー名 → 仮想キーコード。f1〜f24 と、よく使う単独キー。"""
    n = name.strip().lower().replace(" ", "_")
    if n.startswith("f") and n[1:].isdigit() and 1 <= int(n[1:]) <= 24:
        return 0x70 + int(n[1:]) - 1
    if n in _NAMED:
        return _NAMED[n]
    raise ValueError(f"押して話すキーに使えない名前: {name}（f1〜f24, pause, scroll_lock, insert, right_ctrl など）")


class PushToTalk:
    def __init__(self, key: str = "f9", suppress: bool = True):
        self.key = key
        self.vk = vk_of(key)
        self.suppress = suppress
        self.events: "queue.Queue[str]" = queue.Queue()
        self.down = False
        self.listener = None
        self.on_down = None                 # 押されたときにすぐ呼ぶ関数（実行中に押されたら止める、など）

    def handle(self, msg: int, vk: int, flags: int) -> bool:
        """キーイベント1件を処理する。このキーのイベントなら True（=他のアプリに渡さない対象）。"""
        if vk != self.vk or flags & _INJECTED:
            return False
        if msg in (_WM_KEYDOWN, _WM_SYSKEYDOWN):
            if not self.down:              # 押しっぱなしのキーリピートは1回だけ
                self.down = True
                self.events.put("down")
                if self.on_down is not None:
                    try:
                        self.on_down()
                    except Exception:  # noqa: BLE001  キーボードの監視を止めない
                        pass
        elif msg in (_WM_KEYUP, _WM_SYSKEYUP):
            if self.down:
                self.down = False
                self.events.put("up")
        return True

    def _filter(self, msg, data):
        if self.handle(msg, data.vkCode, data.flags):
            if self.suppress:
                self.listener.suppress_event()   # 例外で抜けて、このキーを他のアプリに渡さない
            return False
        return True

    def start(self) -> "PushToTalk":
        if self.listener is None:
            self.listener = keyboard.Listener(win32_event_filter=self._filter)
            self.listener.daemon = True
            self.listener.start()
        return self

    def stop(self) -> None:
        if self.listener is not None:
            self.listener.stop()
            self.listener = None

    def restart(self) -> None:
        """監視を付け直す（押している最中は付け直さない）。"""
        if not self.down:
            self.stop()
            self.start()
