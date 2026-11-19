"""音声モード: 押して話す → 録音 → 文字起こし → エージェント実行 → 読み上げ、を繰り返す。

エージェント本体とは Hooks（status / say / confirm）と agent.run() だけでつながる（疎結合）。
"""
import json
import queue
import threading
import time
from pathlib import Path

from . import evaluate
from . import recorder as rec
from .correct import to_meta
from .tts import Speaker


class VoiceLoop:
    def __init__(self, cfg: dict, agent, hooks, speaker: Speaker, transcriber, ptt, recorder: rec.Recorder,
                 run_task=None, corrector=None, context=None, esc: threading.Event | None = None,
                 log_dir: Path | None = None, stop_run=None, refresh_hooks=None):
        """corrector: 聞き間違いの補正（correct.Corrector）。None なら補正しない。
        context: () -> (前面アプリ名, 画面の言葉のリスト)。補正の手がかり。
        esc: Esc が押されたら立つイベント（「うまくいきましたか？」に「いいえ」と答える）。
        log_dir: 音声ごとの記録（voice_log.jsonl と録音）を残すフォルダ。None なら残さない。"""
        vc = cfg.get("voice") or {}
        ec = cfg.get("eval") or {}
        self.ask_feedback = bool(ec.get("ask_feedback", True)) and esc is not None
        self.feedback_secs = float(ec.get("feedback_secs", 6))
        self.esc = esc
        self.log_dir = Path(log_dir) if log_dir and ec.get("voice_log", True) else None
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
        self.busy = False                         # エージェントが実行中か
        self.stop_run = stop_run                  # 実行中に F9 が押されたら呼ぶ（止める）
        self.refresh_hooks = refresh_hooks        # 待機中にときどきキーボードの監視を付け直す
        self.refresh_secs = float(vc.get("hook_refresh_secs", 120))
        self.esc_quit = bool(vc.get("esc_quit", True))
        self._last_refresh = time.time()
        self._quit_armed = 0.0
        try:
            self.ptt.on_down = self._on_ptt_down
        except Exception:  # noqa: BLE001
            pass

    def _on_ptt_down(self) -> None:
        """キーボードの監視のスレッドから呼ばれる。実行中に F9 が押されたら、実行を止める（Esc と同じ）。"""
        if self.busy and self.stop_run is not None:
            threading.Thread(target=self.stop_run, daemon=True).start()

    def _esc_pressed(self) -> bool:
        return self.esc is not None and self.esc.is_set()

    def _cancelled(self, where: str, audio=None, entry: dict | None = None) -> None:
        """認識・補正の途中で Esc が押されたので、何もせずに待機に戻る。"""
        self.esc.clear()
        self.speaker.stop()
        if entry is not None:
            self._log({**entry, "outcome": "cancelled"}, audio)
        self.hooks.status("ignored", f"取り消しました（{where}中に Esc）")
        self._ui("ignored", "取り消しました", f"{where}中に Esc が押されました")

    def _log(self, entry: dict, audio=None) -> None:
        """音声ごとの記録（音声認識の精度の測定用。scripts/asr_eval.py で使う）。"""
        if self.log_dir is None:
            return
        try:
            ts = time.strftime("%Y%m%d-%H%M%S")
            if audio is not None and audio.size:
                (self.log_dir / "_voice").mkdir(parents=True, exist_ok=True)
                wav = self.log_dir / "_voice" / f"{ts}-{int(time.time() * 1000) % 1000:03d}.wav"
                wav.write_bytes(rec.to_wav_bytes(audio))
                entry["wav"] = str(wav.relative_to(self.log_dir))
            with open(self.log_dir / "voice_log.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), **entry}, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001  記録に失敗しても動作は続ける
            pass

    def _feedback(self, summary: dict) -> None:
        """実行のあとに「うまくいきましたか？」と聞く（F9 = はい / Esc = いいえ / 答えなければ判定なし）。"""
        if not self.ask_feedback or not summary or not summary.get("dir") or summary.get("dry_run"):
            return
        key = self.ptt.key.upper()
        self._drain()
        self.esc.clear()
        self.hooks.status("feedback", f"うまくいきましたか？（{key} = はい / Esc = いいえ / "
                                      f"{self.feedback_secs:.0f} 秒で省略）")
        self._ui("feedback", "うまくいきましたか？", f"{key} = はい ・ Esc = いいえ ・ {self.feedback_secs:.0f} 秒で省略")
        end = time.time() + self.feedback_secs
        label = None
        while self.running and time.time() < end and label is None:
            if self.esc.is_set():
                label = "failure"
                break
            try:
                if self.ptt.events.get(timeout=0.1) == "down":
                    label = "success"
            except queue.Empty:
                pass
        if label:
            evaluate.set_human_label(summary["dir"], label, "voice-mode key")
            self.hooks.status("feedback", "→ " + ("成功として記録しました" if label == "success" else "失敗として記録しました"))
        self._drain()

    def _ui(self, state: str, main: str, sub: str = "") -> None:
        ui = getattr(self.hooks, "ui", None)
        if ui:
            ui(state, main, sub)

    def _next_event(self, timeout: float | None = None) -> str | None:
        """キーのイベントを待つ（Ctrl+C で抜けられるよう、短い間隔で待つ）。"""
        end = None if timeout is None else time.time() + timeout
        while self.running:
            if self._esc_pressed():
                return "esc"
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
            if ev == "esc":
                audio = self.recorder.stop()
                self._cancelled("録音")
                return None, audio
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
        if self._esc_pressed():
            self._cancelled("認識", audio, {"heard": tr.text, "raw": tr.raw, "audio_secs": round(tr.audio_secs, 2)})
            return None, audio
        if not tr.text:
            self._log({"outcome": "dropped", "heard": tr.raw, "dropped": tr.dropped,
                       "audio_secs": round(tr.audio_secs, 2)}, audio)
            self.hooks.status("ignored", f"聞き取れませんでした（{tr.dropped}）")
            self.speaker.say("聞き取れませんでした。もう一度お願いします。")
            self._ui("ignored", "聞き取れませんでした", "もう一度お願いします")
            return None, audio
        self.hooks.status("heard", f"認識: 「{tr.text}」（認識 {tr.secs:.1f} 秒 / 音声 {tr.audio_secs:.1f} 秒）")
        return tr, audio

    def run(self) -> None:
        key = self.ptt.key.upper()
        quit_hint = "Esc を2回押す" if self.esc_quit else "Ctrl+C"
        self.hooks.status("ready", f"準備ができました。{key} を押している間に話してください（終了は {quit_hint}）。")
        self.speaker.say(f"準備ができました。{key}を押しながら話してください。")
        self._ui("ready", f"{key} を押しながら話してください", "待機中")
        while self.running:
            ev = self._next_event(timeout=5)
            if ev is None:
                if self.refresh_hooks is not None and time.time() - self._last_refresh > self.refresh_secs:
                    try:
                        self.refresh_hooks()          # キーボードの監視が外れていても、ここで付け直す
                    except Exception:  # noqa: BLE001
                        pass
                    self._last_refresh = time.time()
                continue
            if ev == "esc":
                self._on_idle_esc(key)
                continue
            if ev != "down":
                continue
            self._quit_armed = 0.0
            tr, audio = self.listen_once()
            if tr is None:
                continue
            meta = {"stt_model": getattr(self.transcriber, "model_name", ""), "stt_secs": round(tr.secs, 2),
                    "audio_secs": round(tr.audio_secs, 1)}
            if tr.raw != tr.text:
                meta["stt_raw"] = tr.raw
            task = tr.text
            entry = {"heard": tr.text, "raw": tr.raw, "audio_secs": round(tr.audio_secs, 2),
                     "stt_model": meta.get("stt_model", "")}
            if self.corrector is not None:
                task = self._correct(tr.text, meta)
                entry.update({k: meta[k] for k in ("corrected", "correction_confidence", "correction_reason")
                              if k in meta})
                if self._esc_pressed():
                    self._cancelled("補正", audio, entry)
                    continue
                if task is None:
                    self._log({**entry, "outcome": "asked_again"}, audio)
                    continue
            self.recent.append(task)
            self.busy = True
            try:
                summary = self.run_task(task, rec.to_wav_bytes(audio), meta)
            finally:
                self.busy = False
                if self.esc is not None:
                    self.esc.clear()              # 実行を止めるために押した Esc を、ここで使い終える
            self._log({**entry, "outcome": "ran", "task": task,
                       "run": Path(summary["dir"]).name if summary and summary.get("dir") else ""}, audio)
            self._feedback(summary)
            if summary:
                self.hooks.status("result", f"結果: {summary['result']}  {summary['steps']} ステップ  "
                                            f"{summary['secs']} 秒\nレポート: {summary['dir']}\\report.html")
            self._drain()                         # 実行中に押されたキーは捨てる
            self.hooks.status("ready", f"\n次の指示をどうぞ（{key} を押しながら話す）。")
            self._ui("ready", f"{key} を押しながら話してください", "待機中")

    def _on_idle_esc(self, key: str) -> None:
        """待機中の Esc: 1回目で「もう一度 Esc で終了」と出し、3秒以内にもう一度押されたら終了する。"""
        self.esc.clear()
        self.speaker.stop()
        if not self.esc_quit:
            return
        if self._quit_armed and time.time() - self._quit_armed < 3.0:
            self.hooks.status("quit", "終了します。")
            self._ui("stopped", "終了します", "")
            self.running = False
            return
        self._quit_armed = time.time()
        self.hooks.status("quit", "もう一度 Esc を押すと終了します（3秒以内）。")
        self._ui("quit", "もう一度 Esc で終了します", f"続けるときは {key} を押しながら話してください")

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
