"""音声モード: 押して話す → 録音 → 文字起こし → エージェント実行 → 読み上げ、を繰り返す。

エージェント本体とは Hooks（status / say / confirm）と agent.run() だけでつながる（疎結合）。
"""
import queue
import time

from . import recorder as rec
from .tts import Speaker


class VoiceLoop:
    def __init__(self, cfg: dict, agent, hooks, speaker: Speaker, transcriber, ptt, recorder: rec.Recorder,
                 run_task=None):
        vc = cfg.get("voice") or {}
        self.min_secs = float(vc.get("min_secs", 0.4))
        self.max_secs = float(vc.get("max_secs", 30))
        self.min_level = float(vc.get("min_level", 0.003))
        self.agent, self.hooks, self.speaker = agent, hooks, speaker
        self.transcriber, self.ptt, self.recorder = transcriber, ptt, recorder
        self.run_task = run_task or (lambda text, wav, meta: agent.run(text, source="voice", audio_wav=wav,
                                                                         extra_meta=meta))
        self.running = True

    def _next_event(self, timeout: float | None = None) -> str | None:
        """キーのイベントを待つ（Ctrl+C で抜けられるよう、短い間隔で待つ）。"""
        end = None if timeout is None else time.time() + timeout
        while self.running:
            try:
                return self.ptt.events.get(timeout=0.2)
            except queue.Empty:
                if end is not None and time.time() >= end:
                    return None
        return None

    def _drain(self) -> None:
        """実行中に押されたキーは捨てる（終わった直後に勝手に録音が始まらないように）。"""
        while True:
            try:
                self.ptt.events.get_nowait()
            except queue.Empty:
                break
        self.ptt.down = False

    def listen_once(self):
        """押してから離すまで録音して、(文字起こし, 音声) を返す。使えない録音なら (None, 音声)。"""
        # 読み上げ中に押されたら、直前の音（読み上げの声）は含めない
        preroll = not self.speaker.speaking
        self.speaker.stop()                       # 読み上げがマイクに入らないように
        self.recorder.start(preroll=preroll)
        self.hooks.status("listening", "● 録音中…（キーを離すと認識します）")
        t0 = time.time()
        ev = None
        while ev != "up" and self.running:
            ev = self._next_event(timeout=max(0.1, self.max_secs + 0.5 - (time.time() - t0)))
            if ev is None:
                break                             # 最大秒数に達した
        audio = self.recorder.stop()
        secs = audio.size / rec.RATE
        if secs < self.min_secs:
            self.hooks.status("ignored", f"短すぎるので無視しました（{secs:.1f} 秒）。キーを押したまま話してください。")
            return None, audio
        if rec.level(audio) < self.min_level:
            self.hooks.status("ignored", f"音がほとんど入っていません（音量 {rec.level(audio):.4f}）。マイクを確認してください。")
            self.speaker.say("声が聞こえませんでした。マイクを確認してください。")
            return None, audio
        self.hooks.status("transcribing", f"認識しています…（音声 {secs:.1f} 秒）")
        tr = self.transcriber.transcribe(audio)
        if not tr.text:
            self.hooks.status("ignored", f"聞き取れませんでした（{tr.dropped}）")
            self.speaker.say("聞き取れませんでした。もう一度お願いします。")
            return None, audio
        self.hooks.status("heard", f"認識: 「{tr.text}」（認識 {tr.secs:.1f} 秒 / 音声 {tr.audio_secs:.1f} 秒）")
        return tr, audio

    def run(self) -> None:
        key = self.ptt.key.upper()
        self.hooks.status("ready", f"準備ができました。{key} を押している間に話してください（終了は Ctrl+C）。")
        self.speaker.say(f"準備ができました。{key}を押しながら話してください。")
        while self.running:
            ev = self._next_event()
            if ev != "down":
                continue
            tr, audio = self.listen_once()
            if tr is None:
                continue
            meta = {"stt_model": getattr(self.transcriber, "model_name", ""), "stt_secs": round(tr.secs, 2),
                    "audio_secs": round(tr.audio_secs, 1)}
            if tr.raw != tr.text:
                meta["stt_raw"] = tr.raw
            summary = self.run_task(tr.text, rec.to_wav_bytes(audio), meta)
            if summary:
                self.hooks.status("result", f"結果: {summary['result']}  {summary['steps']} ステップ  "
                                            f"{summary['secs']} 秒\nレポート: {summary['dir']}\\report.html")
            self._drain()                         # 実行中に押されたキーは捨てる
            self.hooks.status("ready", f"\n次の指示をどうぞ（{key} を押しながら話す）。")
