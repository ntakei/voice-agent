"""音声モード: 押して話す → 録音 → 文字起こし → エージェント実行 → 読み上げ、を繰り返す。

エージェント本体とは Hooks（status / say / confirm）と agent.run() だけでつながる（疎結合）。
"""
import queue
import time

from . import recorder as rec
from .correct import to_meta
from .tts import Speaker


class VoiceLoop:
    def __init__(self, cfg: dict, agent, hooks, speaker: Speaker, transcriber, ptt, recorder: rec.Recorder,
                 run_task=None, corrector=None, context=None):
        """corrector: 聞き間違いの補正（correct.Corrector）。None なら補正しない。
        context: () -> (前面アプリ名, 画面の言葉のリスト)。補正の手がかり。"""
        vc = cfg.get("voice") or {}
        cc = vc.get("correction") or {}
        self.corrector, self.context = corrector, context
        self.ask_again_on_low = bool(cc.get("ask_again_on_low", True))
        self.recent: list[str] = []
        self.min_secs = float(vc.get("min_secs", 0.4))
        self.max_secs = float(vc.get("max_secs", 30))
        self.min_level = float(vc.get("min_level", 0.003))
        self.agent, self.hooks, self.speaker = agent, hooks, speaker
        self.transcriber, self.ptt, self.recorder = transcriber, ptt, recorder
        self.run_task = run_task or (lambda text, wav, meta: agent.run(text, source="voice", audio_wav=wav,
                                                                         extra_meta=meta))
        self.running = True

    def _ui(self, state: str, main: str, sub: str = "") -> None:
        ui = getattr(self.hooks, "ui", None)
        if ui:
            ui(state, main, sub)

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
        self._ui("listening", "聞いています…", "キーを離すと認識します")
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
            self._ui("ignored", "短すぎました", f"{self.ptt.key.upper()} を押したまま話してください")
            return None, audio
        if rec.level(audio) < self.min_level:
            self.hooks.status("ignored", f"音がほとんど入っていません（音量 {rec.level(audio):.4f}）。マイクを確認してください。")
            self.speaker.say("声が聞こえませんでした。マイクを確認してください。")
            self._ui("ignored", "声が聞こえませんでした", "マイクを確認してください")
            return None, audio
        self.hooks.status("transcribing", f"認識しています…（音声 {secs:.1f} 秒）")
        self._ui("transcribing", "認識しています…", f"音声 {secs:.1f} 秒")
        tr = self.transcriber.transcribe(audio)
        if not tr.text:
            self.hooks.status("ignored", f"聞き取れませんでした（{tr.dropped}）")
            self.speaker.say("聞き取れませんでした。もう一度お願いします。")
            self._ui("ignored", "聞き取れませんでした", "もう一度お願いします")
            return None, audio
        self.hooks.status("heard", f"認識: 「{tr.text}」（認識 {tr.secs:.1f} 秒 / 音声 {tr.audio_secs:.1f} 秒）")
        return tr, audio

    def run(self) -> None:
        key = self.ptt.key.upper()
        self.hooks.status("ready", f"準備ができました。{key} を押している間に話してください（終了は Ctrl+C）。")
        self.speaker.say(f"準備ができました。{key}を押しながら話してください。")
        self._ui("ready", f"{key} を押しながら話してください", "待機中")
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
            task = tr.text
            if self.corrector is not None:
                task = self._correct(tr.text, meta)
                if task is None:
                    continue
            self.recent.append(task)
            summary = self.run_task(task, rec.to_wav_bytes(audio), meta)
            if summary:
                self.hooks.status("result", f"結果: {summary['result']}  {summary['steps']} ステップ  "
                                            f"{summary['secs']} 秒\nレポート: {summary['dir']}\\report.html")
            self._drain()                         # 実行中に押されたキーは捨てる
            self.hooks.status("ready", f"\n次の指示をどうぞ（{key} を押しながら話す）。")
            self._ui("ready", f"{key} を押しながら話してください", "待機中")

    def _correct(self, heard: str, meta: dict) -> str | None:
        """聞き間違いを補正する。実行する指示を返す（確信度が低くて聞き直すときは None）。"""
        self.hooks.status("correcting", "聞き間違いがないか確認しています…")
        self._ui("correcting", f"「{heard}」", "聞き間違いがないか確認しています…")
        front, words = self.context() if self.context else ("", [])
        c = self.corrector.correct(heard, self.recent[-3:], front, words)
        meta.update(to_meta(c))
        if c.error:
            self.hooks.status("correcting", f"（補正できなかったので、認識した文のまま実行します: {c.error}）")
        if c.confidence == "low" and self.ask_again_on_low:
            self.hooks.status("ignored", f"指示が分かりませんでした: 「{heard}」（{c.reason}）。もう一度お願いします。")
            self._ui("ignored", "もう一度お願いします", f"「{heard}」— {c.reason}")
            self.speaker.say("うまく聞き取れませんでした。もう一度お願いします。")
            return None
        if c.changed:
            self.hooks.status("corrected", f"補正: 「{heard}」→「{c.task}」（{c.reason}、{c.secs:.1f} 秒）")
            self.speaker.say(f"{c.task}、を実行します。")
        return c.task
