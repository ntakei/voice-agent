"""音声認識（faster-whisper、CPU、日本語固定）。差し替えられるよう Transcriber の裏に置く。

モデルはプロジェクトの models/ に置く（初回だけダウンロード。以後はオフラインで動く）。
"""
import re
import time
from dataclasses import dataclass

import numpy as np

from . import config, recorder

RATE = 16000

# 無音や雑音だけのとき Whisper がよく作ってしまう文（全体がこれだけなら捨てる）
HALLUCINATIONS = ["ご視聴ありがとうございました", "ご清聴ありがとうございました", "チャンネル登録",
                  "おやすみなさい", "ありがとうございました", "字幕", "最後までご視聴"]


@dataclass
class Transcript:
    text: str
    secs: float          # 認識にかかった時間
    audio_secs: float    # 音声の長さ
    raw: str = ""        # 後処理の前の文字列
    dropped: str = ""    # 捨てた理由（あれば）


def _plain(s: str) -> str:
    """比較用: 句読点・記号・空白を除き、小文字に。"""
    return re.sub(r"[\s、。,.!！?？・「」『』()（）]+", "", s or "").lower()


def clean(text: str, prompt: str | None = None) -> tuple[str, str]:
    """前後の空白・句点を整え、幻の文なら捨てる。戻り値は (文, 捨てた理由)。

    prompt（認識の手がかりの文）をそのまま返してきた場合も捨てる。聞き取れないとき Whisper は
    手がかりの文を繰り返すことがある（フェーズ7で「ペイントを操作します」が出て、ペイントが開いた）。
    """
    t = (text or "").strip().strip("　").rstrip("。.、,").strip()
    if not t:
        return "", "認識結果が空"
    core = _plain(t)
    for h in HALLUCINATIONS:
        if core == h or (core.startswith(h) and len(core) <= len(h) + 2):
            return "", f"無音時に出やすい文（{t}）"
    parts = [_plain(x) for x in re.split(r"[、,\s]+", t) if _plain(x)]
    if len(parts) >= 3 and max(parts.count(x) for x in parts) >= 3:
        # 同じ言葉の繰り返し（「Wikipedia、Wikipedia、Wikipedia…」）。聞き取れないときの典型的な誤り
        return "", f"同じ言葉の繰り返し（{t}）"
    if prompt:
        words = {_plain(w) for w in re.split(r"[、,]", prompt) if _plain(w)}
        if len(core) >= 4 and core in _plain(prompt) and core not in words:
            # 手がかりの言葉を並べただけ（や言葉の切れ端）。言葉1つ（例:「スクロール」）は指示かもしれないので残す
            return "", f"認識の手がかりの文をそのまま返した（{t}）"
        if len(parts) >= 2 and all(x in words for x in parts):
            return "", f"認識の手がかりの言葉を並べただけ（{t}）"
    return t, ""


SEP = "、"


def build_prompt(vocabulary: list[str], count_tokens, max_tokens: int = 150) -> tuple[str, list[str]]:
    """手がかりの単語一覧を、トークン数の上限まで先頭から順に入れた文字列にする。戻り値は (文字列, 入らなかった単語)。

    Whisper の手がかりは約 223 トークンまで（超えると先頭から切られる）。長いほど手がかりの言葉を
    そのまま返す誤りも増えるので、上限は控えめにしておく。
    """
    used: list[str] = []
    for i, w in enumerate(vocabulary):
        w = str(w).strip()
        if not w:
            continue
        if count_tokens(SEP.join(used + [w])) > max_tokens:
            return SEP.join(used), [str(x) for x in vocabulary[i:]]
        used.append(w)
    return SEP.join(used), []


class Transcriber:
    def transcribe(self, audio: np.ndarray) -> Transcript:
        raise NotImplementedError


class WhisperTranscriber(Transcriber):
    def __init__(self, model: str = "small", compute_type: str = "int8", cpu_threads: int = 0,
                 beam_size: int = 5, vad_filter: bool = True, initial_prompt: str | None = None,
                 vocabulary: list[str] | None = None, max_prompt_tokens: int = 150):
        self.model_name = model
        self.compute_type = compute_type
        self.cpu_threads = cpu_threads
        self.beam_size = beam_size
        self.vad_filter = vad_filter
        self.initial_prompt = initial_prompt or None
        self.vocabulary = list(vocabulary or [])
        self.max_prompt_tokens = max_prompt_tokens
        self.vocab_dropped: list[str] = []
        self._model = None
        self.load_secs = 0.0

    def load(self) -> "WhisperTranscriber":
        if self._model is None:
            from faster_whisper import WhisperModel
            t = time.time()
            root = str(config.ROOT / "models")
            kw = dict(device="cpu", compute_type=self.compute_type, cpu_threads=self.cpu_threads,
                      download_root=root)
            try:
                self._model = WhisperModel(self.model_name, local_files_only=True, **kw)
            except Exception:  # noqa: BLE001  まだ models/ にない → ダウンロード
                self._model = WhisperModel(self.model_name, **kw)
            self.load_secs = time.time() - t
            if self.vocabulary and not self.initial_prompt:
                tok = self._model.hf_tokenizer
                self.initial_prompt, self.vocab_dropped = build_prompt(
                    self.vocabulary, lambda s: len(tok.encode(s).ids), self.max_prompt_tokens)
        return self

    def transcribe(self, audio: np.ndarray) -> Transcript:
        self.load()
        t = time.time()
        segments, _info = self._model.transcribe(
            recorder.normalize(audio.astype(np.float32)), language="ja", task="transcribe", beam_size=self.beam_size,
            vad_filter=self.vad_filter, initial_prompt=self.initial_prompt,
            condition_on_previous_text=False, without_timestamps=True)
        raw = "".join(s.text for s in segments)
        text, dropped = clean(raw, self.initial_prompt)
        return Transcript(text, time.time() - t, audio.size / RATE, raw.strip(), dropped)


def make_transcriber(cfg: dict) -> Transcriber:
    sc = (cfg.get("voice") or {}).get("stt") or {}
    return WhisperTranscriber(model=sc.get("model", "small"), compute_type=sc.get("compute_type", "int8"),
                              cpu_threads=int(sc.get("cpu_threads", 0)), beam_size=int(sc.get("beam_size", 5)),
                              vad_filter=bool(sc.get("vad_filter", True)),
                              initial_prompt=sc.get("initial_prompt"), vocabulary=sc.get("vocabulary"),
                              max_prompt_tokens=int(sc.get("max_prompt_tokens", 150)))
