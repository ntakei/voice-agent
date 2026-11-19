"""起動口（テキスト入力 / 音声入力）。

  python -m uv run voice-agent "電卓で 12 かける 34 を計算して"      # 1回だけ実行
  python -m uv run voice-agent                                      # 対話モード（タスクを何度でも入力）
  python -m uv run voice-agent --live "…"                           # 実際にマウス・キーボードを操作する
  python -m uv run voice-agent --voice --live                       # 音声モード（F9 を押しながら話す）

既定は config.yaml の agent.dry_run（true なら操作せず表示だけ）。
"""
import argparse
import os
import threading
import time

from . import config, confirm_ui, evaluate
from .agent import Agent, Hooks
from .stopkey import StopKey


class ConsoleHooks(Hooks):
    """コンソール表示 + 確認ダイアログ（+ 読み上げ）。"""

    def __init__(self, timeout_secs: float = 30, speaker=None, speak_kinds=None, overlay=None):
        self.timeout_secs = timeout_secs
        self.speaker = speaker
        self.speak_kinds = set(speak_kinds) if speak_kinds is not None else None   # None = すべて読む
        self.overlay = overlay

    def ui(self, state: str, main: str, sub: str = "") -> None:
        if self.overlay is not None:
            self.overlay.show(state, main, sub)

    def say(self, text: str, kind: str = "info") -> None:
        if self.speaker and (self.speak_kinds is None or kind in self.speak_kinds):
            self.speaker.say(text, interrupt=kind in ("confirm", "result", "blocked"))

    def confirm(self, question: str) -> bool:
        print(question, flush=True)
        ok = confirm_ui.ask(question, timeout_secs=self.timeout_secs)
        how = {"yes": "はい（実行）", "no": "いいえ（中止）", "timeout": "時間切れ → いいえ（中止）",
               "closed": "ダイアログを閉じた → いいえ（中止）"}.get(confirm_ui.last_answer, str(ok))
        self.last_answer = confirm_ui.last_answer
        print("→ " + how, flush=True)
        return ok


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="voice-agent", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("task", nargs="*", help="タスク（省略すると対話モード）")
    ap.add_argument("--voice", action="store_true", help="音声モード（押して話すキーを押しながら話す）")
    ap.add_argument("--stt-model", help="音声認識のモデル（例: small, large-v3-turbo）。既定は config の voice.stt.model")
    ap.add_argument("--no-tts", action="store_true", help="読み上げをしない")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--live", action="store_true", help="実際に操作する（dry-run を解除）")
    g.add_argument("--dry-run", action="store_true", help="操作せず表示だけ（config より優先）")
    ap.add_argument("--max-steps", type=int, help="最大ステップ数（既定は config の agent.max_steps）")
    ap.add_argument("--delay", type=float, default=0, help="開始までの待ち秒数（その間に対象アプリを前面へ）")
    ap.add_argument("--think-normal", choices=["off", "low", "on"], help="通常ステップの思考モード")
    ap.add_argument("--think-stuck", choices=["off", "low", "on"], help="行き詰まったときの思考モード")
    return ap


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    cfg = config.load()
    dry = True if args.dry_run else (False if args.live else None)
    sc = cfg.get("safety", {})
    vc = cfg.get("voice") or {}
    speaker = None
    if args.voice and not args.no_tts:
        from .tts import make_speaker
        speaker = make_speaker(cfg)
    overlay = make_overlay(cfg, args)
    hooks = ConsoleHooks(sc.get("confirm_timeout_secs", 30), speaker,
                         (vc.get("tts") or {}).get("speak", ["start", "reason", "confirm", "blocked", "result"]),
                         overlay)
    agent = Agent(cfg, hooks=hooks, dry_run=dry, max_steps=args.max_steps,
                  think_normal=args.think_normal, think_stuck=args.think_stuck)
    stop_key = sc.get("stop_key", "esc")
    esc = threading.Event()        # Esc が押された（実行中なら停止、「うまくいきましたか？」には「いいえ」）

    def on_stop():
        agent.stop()
        esc.set()
    StopKey(on_stop, stop_key).start()
    mode = "dry-run（操作しません）" if agent.dry_run else "実際に操作します"
    print(f"モデル {cfg['model']['name']} / {mode} / 思考 通常={agent.think_normal} 行き詰まり={agent.think_stuck} "
          f"/ 最大 {agent.max_steps} ステップ")
    print(f"止めるとき: {stop_key.upper()} キー、またはマウスを画面の隅へ")

    def run_one(task: str) -> None:
        if args.delay:
            print(f"{args.delay:.0f} 秒後に開始します…", flush=True)
            time.sleep(args.delay)
        summary = agent.run(task)
        print(f"結果: {summary['result']}  {summary['steps']} ステップ  {summary['secs']} 秒")
        print(f"レポート: {os.path.join(summary['dir'], 'report.html')}")
        if (cfg.get("eval") or {}).get("ask_feedback", True) and not agent.dry_run and not args.task:
            # 対話モードのときだけ聞く（1回だけ実行するときは聞かない）
            hooks.ui("feedback", "うまくいきましたか？", "端末で y / n を入力（Enter で省略）")
            try:
                ans = input("うまくいきましたか？ [y = はい / n = いいえ / Enter = 省略] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = ""
            if ans in ("y", "yes", "はい"):
                evaluate.set_human_label(summary["dir"], "success", "text-mode input")
            elif ans in ("n", "no", "いいえ"):
                evaluate.set_human_label(summary["dir"], "failure", "text-mode input")

    try:
        if args.voice:
            run_voice(args, cfg, agent, hooks, speaker, overlay, esc)
            return
        if args.task:
            run_one(" ".join(args.task))
            return
        print("タスクを入力してください（空行で終了）。")
        while True:
            hooks.ui("ready", overlay.idle_main, "待機中")
            try:
                task = input("\nタスク> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not task:
                break
            run_one(task)
    finally:
        overlay.close()


def make_overlay(cfg: dict, args):
    """画面上の状態表示（ui.overlay.enabled: false なら表示しない）。"""
    from .overlay import NullOverlay, Overlay
    oc = (cfg.get("ui") or {}).get("overlay") or {}
    if not oc.get("enabled", True):
        return NullOverlay()
    stop_key = str((cfg.get("safety") or {}).get("stop_key", "esc")).capitalize()
    if args.voice:
        key = str((cfg.get("voice") or {}).get("ptt_key", "f9")).upper()
        ov = Overlay(cfg, hints=[f"{key} 話す", f"{stop_key} 停止"])
        ov.idle_main = f"{key} を押しながら話してください"
    else:
        ov = Overlay(cfg, hints=[f"{stop_key} 停止"])
        ov.idle_main = "端末にタスクを入力してください"
    ov.show("ready", ov.idle_main, "待機中")
    return ov.start()


def screen_context(agent, cfg):
    """聞き間違いの補正の手がかり: エージェントが操作する見込みのウィンドウ（保護対象でない、いちばん手前のアプリ）と、
    その中のボタン・リンクなどの名前。"""
    from . import uia, winapi
    cc = (cfg.get("voice") or {}).get("correction") or {}
    chrome = {"最小化", "最大化", "元に戻す（縮小）", "復元", "閉じる", "システム"}

    def context():
        target = next((w for w in winapi.app_windows()
                       if not agent.policy.is_protected(w.exe) and winapi.window_rect(w.hwnd)), None)
        if target is None:
            return "", []
        words = []
        if cc.get("use_screen_words", True):
            words = [x for x in uia.visible_words(target.hwnd, limit=int(cc.get("max_screen_words", 60)))
                     if x not in chrome]
        return agent._front_name(target), words
    return context


def run_voice(args, cfg, agent, hooks, speaker, overlay=None, esc=None) -> None:
    from . import stt
    from .correct import Corrector
    from .ptt import PushToTalk
    from .recorder import Recorder
    from .tts import Speaker
    from .voice import VoiceLoop
    vc = cfg.get("voice") or {}
    if args.stt_model:
        cfg = {**cfg, "voice": {**vc, "stt": {**(vc.get("stt") or {}), "model": args.stt_model}}}
    tr = stt.make_transcriber(cfg)
    print(f"音声認識モデル {tr.model_name}（{tr.compute_type}, CPU）を読み込んでいます…", flush=True)
    tr.load()
    tr.transcribe(__import__("numpy").zeros(stt.RATE, "float32"))   # 1回目が遅くならないよう空回し
    print(f"  読み込み {tr.load_secs:.1f} 秒", flush=True)
    if tr.vocabulary:
        n = len(tr.vocabulary) - len(tr.vocab_dropped)
        print(f"  認識の手がかり: {n} 語" + (f"（上限を超えたので使わない: {'、'.join(tr.vocab_dropped)}）"
                                            if tr.vocab_dropped else ""), flush=True)
    device = vc.get("input_device")
    print(f"マイク: {Recorder.device_name(device)}")
    if speaker is not None:
        print(f"読み上げ: {getattr(speaker, 'voice_name', '') or '既定の音声'}")
    ptt = PushToTalk(vc.get("ptt_key", "f9"), suppress=bool(vc.get("ptt_suppress", True))).start()
    mic = Recorder(device, max_secs=float(vc.get("max_secs", 30)), preroll_secs=float(vc.get("preroll_secs", 0.5)),
                   always_on=bool(vc.get("mic_always_on", True)))
    if mic.always_on:
        mic.open()   # 話し始めが切れないよう、音声モードの間はマイクを開いておく
    if overlay is not None:
        overlay.level_fn = lambda: mic.last_level
    cc = vc.get("correction") or {}
    corrector = None
    if cc.get("enabled", True):
        corrector = Corrector(agent.client, (vc.get("stt") or {}).get("vocabulary") or [],
                              timeout_secs=float(cc.get("timeout_secs", 10)))
        print("聞き間違いの補正: あり（確信度が低いときは聞き直します）" if cc.get("ask_again_on_low", True)
              else "聞き間違いの補正: あり")
    loop = VoiceLoop(cfg, agent, hooks, speaker or Speaker(), tr, ptt, mic,
                     corrector=corrector, context=screen_context(agent, cfg), esc=esc, log_dir=config.RUNS_DIR)
    try:
        loop.run()
    except KeyboardInterrupt:
        print("\n終了します。")
    finally:
        loop.running = False
        ptt.stop()
        mic.close()
        if speaker is not None:
            speaker.close()


if __name__ == "__main__":
    main()
