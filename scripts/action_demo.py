"""フェーズ4: 実行部の確認（既定は dry-run。マウス・キーボードは動かさない）。

スクリーンショットを撮り、タスクに対する「次の1手」をモデルに答えさせ、
検証 → 実画面座標への換算 → dry-run 実行（表示だけ）までを行う。
クリック位置に印を付けた画像を scripts/out/action-*/ に保存する。

例:
  python -m uv run python scripts/action_demo.py --delay 5 "メモ帳を開いて「こんにちは」と入力して"
  python -m uv run python scripts/action_demo.py --json "{\"action\": \"key\", \"text\": \"ctrl+s\", \"target_app\": \"メモ帳\"}"

  --json  モデルを呼ばず、与えた JSON を検証・dry-run する
  --think 思考モード off / low / on（既定は config.yaml の thinking_normal）
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import voice_agent  # noqa: E402,F401  DPI 対応をここで宣言
from voice_agent import actions, config, coords, prompts, screen, winapi  # noqa: E402
from voice_agent.executor import Executor  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("task", nargs="?", default="")
    ap.add_argument("--json", help="モデルの代わりに使うアクションの JSON")
    ap.add_argument("--delay", type=float, default=0)
    ap.add_argument("--think", choices=["off", "low", "on"])
    args = ap.parse_args()
    if not args.task and not args.json:
        ap.error("タスク（文字列）か --json を指定してください")

    cfg = config.load()
    apps = actions.load_apps(cfg)
    out_dir = ROOT / "scripts" / "out" / time.strftime("action-%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.delay:
        print(f"{args.delay:.0f} 秒後に撮影します…", flush=True)
        time.sleep(args.delay)
    full = screen.capture()
    sent = screen.for_model(full, cfg["screen"].get("model_image_width", 1512))
    front = winapi.foreground()
    front_name = front.exe if front else ""
    print(f"画面 {full.width}x{full.height} / 送る画像 {sent.width}x{sent.height} / 前面 {front.describe() if front else '-'}")

    if args.json:
        raw = args.json
    else:
        from voice_agent.model_client import ModelClient
        mode = args.think or cfg["model"].get("thinking_normal", "off")
        msgs = [{"role": "system", "content": prompts.system_prompt(sent.size, [a.names[0] for a in apps])},
                {"role": "user", "content": prompts.user_prompt(args.task, [], front_name)}]
        r = ModelClient(cfg).chat(msgs, image=sent, thinking=mode, image_quality=cfg["screen"].get("jpeg_quality", 90))
        raw = r["content"]
        print(f"モデル: {r['secs']}s 思考={mode} 出力tok={r['usage'].get('completion_tokens')}")
    print(f"返答: {raw}")

    a = coords.parse_json(raw)
    if a is None:
        print("[NG] JSON として読めない返答")
        return
    a = actions.normalize(a)
    err = actions.validate(a, sent.size, apps)
    if err:
        print(f"[NG] 検証エラー: {err}")
        return
    ex = Executor(full.size, apps, dry_run=True)
    res = ex.execute(a, sent.size)
    print(f"[OK] 検証通過")
    print(f"[dry-run] {res.label}" + (f"  ({res.detail})" if res.detail else ""))
    print(f"          理由: {a.get('reason', '')}  risky={a.get('risky')}  target_app={a.get('target_app', '-')}")

    full.save(out_dir / "screenshot.png")
    sent.save(out_dir / "sent.jpg", quality=90)
    pts = [(*res.point, res.label)] if res.point else []
    screen.mark_points(full, pts).save(out_dir / "marked.png")
    (out_dir / "result.json").write_text(json.dumps(
        {"task": args.task, "front": front_name, "raw": raw, "action": a, "label": res.label,
         "point": res.point, "detail": res.detail}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n保存先: {out_dir}")


if __name__ == "__main__":
    main()
