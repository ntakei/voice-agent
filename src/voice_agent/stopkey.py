"""停止キー（既定 Esc）の監視。人が押したときだけ停止し、プログラム（pyautogui）が送ったキーは無視する。"""
from pynput import keyboard


def _key_name(key) -> str:
    if isinstance(key, keyboard.Key):
        return key.name
    ch = getattr(key, "char", None)
    return (ch or "").lower()


class StopKey:
    def __init__(self, on_stop, key: str = "esc"):
        self.on_stop = on_stop
        self.key = key.lower()
        self.listener = None

    def on_press(self, key, injected: bool = False):
        """pynput のコールバック。injected=True はプログラムが送ったキー（自分の操作）なので無視する。"""
        if not injected and _key_name(key) == self.key:
            self.on_stop()

    def start(self) -> "StopKey":
        if self.listener is None:
            self.listener = keyboard.Listener(on_press=self.on_press)
            self.listener.daemon = True
            self.listener.start()
        return self

    def stop(self) -> None:
        if self.listener is not None:
            self.listener.stop()
            self.listener = None
