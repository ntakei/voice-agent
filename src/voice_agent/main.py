"""起動口（テキスト入力）。

  python -m uv run voice-agent "電卓で 12 かける 34 を計算して"      # 1回だけ実行
  python -m uv run voice-agent                                      # 対話モード（タスクを何度でも入力）
  python -m uv run voice-agent --live "…"                           # 実際にマウス・キーボードを操作する

既定は config.yaml の agent.dry_run（true なら操作せず表示だけ）。
"""
import argparse
import os
import time

from . import config, confirm_ui
from .agent import Agent, Hooks
from .stopkey import StopKey


class ConsoleHooks(Hooks):
    """コンソール表示 + 確認ダイアログ。"""

    def __init__(self, timeout_secs: float = 30):
        self.timeout_secs = timeout_secs

    def confirm(self, question: str) -> bool:
        print(question, flush=True)
        ok = confirm_ui.ask(question, timeout_secs=self.timeout_secs)
        print("→ " + ("はい（実行）" if ok else "いいえ（中止）"), flush=True)
        return ok


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="voice-agent", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("task", nargs="*", help="タスク（省略すると対話モード）")
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
    hooks = ConsoleHooks(sc.get("confirm_timeout_secs", 30))
    agent = Agent(cfg, hooks=hooks, dry_run=dry, max_steps=args.max_steps,
                  think_normal=args.think_normal, think_stuck=args.think_stuck)
    stop_key = sc.get("stop_key", "esc")
    StopKey(agent.stop, stop_key).start()
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

    if args.task:
        run_one(" ".join(args.task))
        return
    print("タスクを入力してください（空行で終了）。")
    while True:
        try:
            task = input("\nタスク> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not task:
            break
        run_one(task)


if __name__ == "__main__":
    main()
