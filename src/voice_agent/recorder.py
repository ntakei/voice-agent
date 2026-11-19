"""マイク録音（押している間だけ）。16 kHz モノラル float32 の numpy 配列を返す。

話し始めが切れないように、既定ではマイクを開いたままにして、直前 preroll_secs 秒の音を常に覚えておき、
キーを押したときにその分も含める（フェーズ7の実地テストで、押した瞬間の音が欠けていた。
押してからマイクを開くと、開くまでの間と開いた直後の数十〜数百ミリ秒が無音になる）。

デバイスが 16 kHz で開けないとき（一部のデバイス）は、そのデバイスの既定のサンプリングレートで録って変換する。
"""
import collections
import io
import threading
import wave

import numpy as np
import sounddevice as sd

RATE = 16000  # Whisper の入力


def resample(audio: np.ndarray, src_rate: int, dst_rate: int = RATE) -> np.ndarray:
    """線形補間での変換（音声認識の前処理には十分）。"""
    if src_rate == dst_rate or audio.size == 0:
        return audio.astype(np.float32)
    n = int(round(audio.size * dst_rate / src_rate))
    x_old = np.linspace(0, 1, audio.size, endpoint=False)
    x_new = np.linspace(0, 1, n, endpoint=False)
    return np.interp(x_new, x_old, audio).astype(np.float32)


def to_wav_bytes(audio: np.ndarray, rate: int = RATE) -> bytes:
    """ログ（report.html の audio.wav）用の 16bit WAV。"""
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def level(audio: np.ndarray) -> float:
    """音の大きさ（RMS）。無音の判定に使う。"""
    return float(np.sqrt(np.mean(np.square(audio)))) if audio.size else 0.0


def normalize(audio: np.ndarray, target_peak: float = 0.7, max_gain: float = 20.0) -> np.ndarray:
    """小さすぎる録音を聞き取りやすい大きさにそろえる（増幅は max_gain 倍まで）。"""
    peak = float(np.abs(audio).max()) if audio.size else 0.0
    if peak <= 0:
        return audio
    return (audio * min(max_gain, target_peak / peak)).astype(np.float32)


class Recorder:
    def __init__(self, device=None, max_secs: float = 30, preroll_secs: float = 0.5, always_on: bool = True):
        self.device = device if device not in ("", "default") else None
        self.max_secs = max_secs
        self.preroll_secs = preroll_secs if always_on else 0.0
        self.always_on = always_on
        self._lock = threading.Lock()
        self._stream = None
        self._rate = RATE
        self._recording = False
        self._chunks: list[np.ndarray] = []
        self._frames = 0
        self._ring: collections.deque[np.ndarray] = collections.deque()
        self._ring_frames = 0

    # ---- マイク ----

    def _callback(self, indata, frames, time_info, status):
        chunk = indata[:, 0].copy()
        with self._lock:
            if self._recording:
                if self._frames < self.max_secs * self._rate:
                    self._chunks.append(chunk)
                    self._frames += frames
            elif self.preroll_secs > 0:
                self._ring.append(chunk)
                self._ring_frames += frames
                while self._ring and self._ring_frames - self._ring[0].size >= self.preroll_secs * self._rate:
                    self._ring_frames -= self._ring.popleft().size

    def _open_stream(self, rate: int):
        return sd.InputStream(samplerate=rate, channels=1, dtype="float32", device=self.device,
                              callback=self._callback, blocksize=0)

    def open(self) -> "Recorder":
        """マイクを開く（always_on なら音声モードの間ずっと開いておく）。"""
        if self._stream is None:
            try:
                self._rate = RATE
                self._stream = self._open_stream(RATE)
            except Exception:  # noqa: BLE001  16 kHz で開けないデバイス
                self._rate = int(sd.query_devices(self.device, "input")["default_samplerate"])
                self._stream = self._open_stream(self._rate)
            self._stream.start()
        return self

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            finally:
                self._stream = None
        with self._lock:
            self._ring.clear()
            self._ring_frames = 0

    # ---- 録音 ----

    def start(self, preroll: bool = True) -> None:
        """録音を始める。preroll=True なら、押す直前の音（preroll_secs 秒）も含める。"""
        self.open()
        with self._lock:
            self._chunks = list(self._ring) if preroll else []
            self._frames = sum(c.size for c in self._chunks)
            self._ring.clear()
            self._ring_frames = 0
            self._recording = True

    def stop(self) -> np.ndarray:
        """録音を止めて、16 kHz の音声を返す。"""
        with self._lock:
            self._recording = False
            audio = np.concatenate(self._chunks) if self._chunks else np.zeros(0, np.float32)
            self._chunks, self._frames = [], 0
        if not self.always_on:
            self.close()
        return resample(audio, self._rate)

    @property
    def recording(self) -> bool:
        return self._recording

    @staticmethod
    def device_name(device=None) -> str:
        try:
            return sd.query_devices(device if device not in ("", "default") else None, "input")["name"]
        except Exception as e:  # noqa: BLE001
            return f"（入力デバイスなし: {e}）"
