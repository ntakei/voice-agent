"""読み上げ（TTS）。差し替えられるよう Speaker インターフェースの裏に置く。

- Speaker.say(text): 読み上げを頼む（すぐ戻る）。読み上げ中に次が来たら、待っている分は最新の1件だけ残す
  （操作の理由を毎ステップ読むので、古いものが溜まって実際の動きより遅れないように）。
- Speaker.stop(): 今の読み上げを止め、待っている分も捨てる（押して話すとき、マイクに入らないように）。
- make_speaker(cfg) で config.yaml の voice.tts.engine から選ぶ:
    sapi    … Windows の SAPI5 を直接使う（pywin32）。既定
    pyttsx3 … pyttsx3 経由（この PC では2回目以降の読み上げが途中で切れたため既定にしていない。フェーズ7で確認）
    none    … 読み上げない
"""
import threading


class Speaker:
    """何もしない読み上げ（engine: none）。"""

    voice_name = ""

    @property
    def speaking(self) -> bool:
        """読み上げ中（または読み上げ待ちがある）なら True。"""
        return False

    def say(self, text: str, interrupt: bool = False) -> None:
        pass

    def stop(self) -> None:
        pass

    def wait(self, timeout: float = 10) -> bool:
        """読み上げが終わるまで待つ（終われば True）。"""
        return True

    def close(self) -> None:
        pass


class ThreadSpeaker(Speaker):
    """専用スレッドでエンジンを動かす読み上げの共通部分（COM のエンジンは作ったスレッドでしか触らない）。

    サブクラスは _init_engine()（スレッド内で1回）と _speak(text)（読み終わるか self._stop まで戻らない）を実装する。
    """

    def __init__(self, voice: str = "Haruka", rate: int = 0, volume: float = 1.0):
        self.voice, self.rate, self.volume = voice, rate, volume
        self._cv = threading.Condition()
        self._pending: str | None = None
        self._stop = False
        self._busy = False
        self._closed = False
        self._ready = threading.Event()
        self._error: Exception | None = None
        self._thread = threading.Thread(target=self._run, name="tts", daemon=True)
        self._thread.start()
        self._ready.wait(10)
        if self._error:
            raise self._error

    # ---- 呼び出し側 ----

    @property
    def speaking(self) -> bool:
        return self._busy or self._pending is not None

    def say(self, text: str, interrupt: bool = False) -> None:
        text = (text or "").strip()
        if not text:
            return
        with self._cv:
            if interrupt:
                self._stop = self._busy
            self._pending = text
            self._cv.notify_all()

    def stop(self) -> None:
        with self._cv:
            self._pending = None
            self._stop = self._busy
            self._cv.notify_all()

    def wait(self, timeout: float = 10) -> bool:
        with self._cv:
            return self._cv.wait_for(lambda: not self._busy and self._pending is None, timeout)

    def close(self) -> None:
        with self._cv:
            self._closed = True
            self._pending = None
            self._stop = self._busy
            self._cv.notify_all()
        self._thread.join(3)

    # ---- 読み上げスレッド ----

    def _run(self) -> None:
        try:
            import pythoncom
            pythoncom.CoInitialize()
            self._init_engine()
        except Exception as e:  # noqa: BLE001
            self._error = e
            self._ready.set()
            return
        self._ready.set()
        while True:
            with self._cv:
                self._cv.wait_for(lambda: self._pending is not None or self._closed)
                if self._closed:
                    break
                text, self._pending = self._pending, None
                self._stop = False
                self._busy = True
            try:
                self._speak(text)
            except Exception:  # noqa: BLE001  読み上げの失敗で全体を止めない
                pass
            with self._cv:
                self._busy = False
                self._cv.notify_all()

    def _init_engine(self) -> None:
        raise NotImplementedError

    def _speak(self, text: str) -> None:
        raise NotImplementedError


class SapiSpeaker(ThreadSpeaker):
    """Windows の SAPI5（SAPI.SpVoice）を直接使う。非同期で話し始め、50ms ごとに止める要求を確かめる。"""

    _ASYNC, _PURGE = 1, 2

    def _init_engine(self) -> None:
        import win32com.client
        self._sp = win32com.client.Dispatch("SAPI.SpVoice")
        tokens = list(self._sp.GetVoices())
        want = (self.voice or "").lower()
        pick = next((t for t in tokens if want and want in t.GetDescription().lower()), None)
        pick = pick or next((t for t in tokens if "japanese" in t.GetDescription().lower()
                             or "日本語" in t.GetDescription()), None)
        if pick is not None:
            self._sp.Voice = pick
        self.voice_name = self._sp.Voice.GetDescription()
        self._sp.Rate = max(-10, min(10, int(self.rate)))
        self._sp.Volume = max(0, min(100, int(round(float(self.volume) * 100))))

    def _speak(self, text: str) -> None:
        import pythoncom
        self._sp.Speak(text, self._ASYNC)
        while not self._sp.WaitUntilDone(50):
            pythoncom.PumpWaitingMessages()
            if self._stop or self._closed:
                self._sp.Speak("", self._ASYNC | self._PURGE)   # 今の読み上げを打ち切る
                self._sp.WaitUntilDone(500)
                return


class Pyttsx3Speaker(ThreadSpeaker):
    """pyttsx3 経由（仕様の当初案）。この PC では2回目以降の読み上げが途中で切れたため、既定は sapi。"""

    def _init_engine(self) -> None:
        import pyttsx3
        self._engine = engine = pyttsx3.init()
        voices = engine.getProperty("voices")
        want = (self.voice or "").lower()
        pick = next((v for v in voices if want and want in (v.name + v.id).lower()), None)
        pick = pick or next((v for v in voices if "ja" in str(getattr(v, "languages", "")).lower()), None)
        if pick:
            engine.setProperty("voice", pick.id)
            self.voice_name = pick.name
        engine.setProperty("rate", int(200 * 1.1 ** int(self.rate)))   # rate -10〜10 を語/分に換算
        engine.setProperty("volume", float(self.volume))
        engine.connect("started-word", lambda **kw: self._check_stop())

    def _check_stop(self) -> None:
        if self._stop or self._closed:
            self._stop = False
            self._engine.stop()

    def _speak(self, text: str) -> None:
        self._engine.say(text)
        self._engine.runAndWait()


ENGINES = {"sapi": SapiSpeaker, "pyttsx3": Pyttsx3Speaker}


def make_speaker(cfg: dict) -> Speaker:
    tc = (cfg.get("voice") or {}).get("tts") or {}
    engine = str(tc.get("engine", "sapi")).lower()
    if engine in ("none", "off", "false", ""):
        return Speaker()
    if engine not in ENGINES:
        raise ValueError(f"不明な読み上げエンジン: {engine}（{' / '.join(ENGINES)} / none）")
    return ENGINES[engine](voice=tc.get("voice", "Haruka"), rate=int(tc.get("rate", 0)),
                           volume=float(tc.get("volume", 1.0)))
