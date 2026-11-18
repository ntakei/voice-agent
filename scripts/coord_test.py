"""フェーズ3: 接続と座標テスト（マウス・キーボードは動かさない）。

スクリーンショットを1枚撮り、指定した UI 要素の座標をモデルに答えさせ、
返ってきた座標に印を付けた画像と結果の一覧（index.html）を scripts/out/ に保存する。

例:
  python -m uv run python scripts/coord_test.py --delay 5 ^
      --target "スタートボタン@585,1040,630,1075" --target "タスクバーの時計"

  # 同じ画像で、座標形式・思考モード・画像幅を比べる
  python -m uv run python scripts/coord_test.py --image scripts/out/<前回>/screenshot.png ^
      --frames grid,pixel --modes off,low --widths 1512,0 --target "スタートボタン"

  --target  探す要素。"名前@x1,y1,x2,y2"（実画面px）で正解範囲を付けると命中判定する
  --frames  grid  = 0〜1000 の正規化座標で答えさせる（仕様どおり）
            pixel = 送った画像のピクセル座標で答えさせ、実画面に拡大換算する
  --modes   off（思考なし）/ low（reasoning_effort=low）/ on（思考あり）
  --widths  モデルに送る画像の幅。0 は縮小なし
  --lang    指示文の言語 en / ja
"""
import argparse
import html
import json
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import voice_agent  # noqa: E402  DPI 対応をここで宣言（他の import より先）
from PIL import Image  # noqa: E402

from voice_agent import config, coords, screen  # noqa: E402
from voice_agent.model_client import ModelClient  # noqa: E402

FRAME_TEXT = {
    ("grid", "en"): ("Coordinates use a normalized 0-1000 grid over the whole screenshot: (0,0) is the top-left corner, "
                     "(1000,1000) is the bottom-right corner, (500,500) is the center. Do NOT use pixel coordinates."),
    ("grid", "ja"): ("座標はスクリーンショット全体を 0〜1000 に正規化したものです。左上が (0,0)、右下が (1000,1000)、"
                     "中央が (500,500)。ピクセル座標は使わないでください。"),
    ("pixel", "en"): "Coordinates are pixel coordinates of this image, which is {w}x{h} pixels; (0,0) is the top-left corner.",
    ("pixel", "ja"): "座標はこの画像のピクセル座標です。画像の大きさは {w}x{h} ピクセル、左上が (0,0) です。",
}
PROMPTS = {
    "en": {
        "system": ("You locate UI elements on a screenshot of a Windows 11 desktop.\n{frame}\n"
                   'Reply with ONE JSON object only: {{"x": <number>, "y": <number>, "found": true|false, '
                   '"reason": "<短い日本語の説明>"}}\n'
                   "x and y are the CENTER of the element. If the element is not visible, set found to false."),
        "user": "Find this element: {target}",
    },
    "ja": {
        "system": ("あなたは Windows 11 のスクリーンショットから UI 要素の位置を答えます。\n{frame}\n"
                   'JSON を1個だけ返してください: {{"x": <数値>, "y": <数値>, "found": true|false, "reason": "<短い説明>"}}\n'
                   "x, y は要素の中心です。要素が見えない場合は found を false にしてください。"),
        "user": "次の要素を探してください: {target}",
    },
}

DEFAULT_TARGETS = ["スタートボタン（Windows ロゴ）", "タスクバーの時計", "タスクバーの検索ボックス"]


def parse_target(spec):
    """'名前@x1,y1,x2,y2' -> (名前, box) / '名前' -> (名前, None)"""
    if "@" in spec:
        name, box = spec.rsplit("@", 1)
        try:
            x1, y1, x2, y2 = (int(v) for v in box.split(","))
            return name.strip(), (x1, y1, x2, y2)
        except ValueError:
            pass
    return spec.strip(), None


def to_screen_px(x, y, frame, sent_size, screen_size):
    W, H = screen_size
    if frame == "grid":
        return coords.to_screen(x, y, W, H)
    w, h = sent_size
    return min(max(round(x * W / w), 0), W - 1), min(max(round(y * H / h), 0), H - 1)


def run_one(client, sent, target, frame, mode, lang, quality):
    p = PROMPTS[lang]
    system = p["system"].format(frame=FRAME_TEXT[(frame, lang)].format(w=sent.width, h=sent.height))
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": p["user"].format(target=target)}]
    try:
        r = client.chat(msgs, image=sent, thinking=mode, image_quality=quality)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    parsed = coords.parse_point(r["content"])
    j = coords.parse_json(r["content"]) or {}
    out = {
        "raw": r["content"], "reasoning_chars": len(r["reasoning"]), "secs": r["secs"],
        "finish_reason": r["finish_reason"], "prompt_tokens": r["usage"].get("prompt_tokens"),
        "completion_tokens": r["usage"].get("completion_tokens"), "found": j.get("found"), "reason": j.get("reason"),
    }
    if parsed:
        out.update(x=parsed[0], y=parsed[1], format=parsed[2])
    else:
        out["error"] = "座標を読み取れない返答"
    return out


def judge(px, box):
    """正解範囲への命中と、範囲の中心からの距離(px)。"""
    if not box or px is None:
        return None, None
    x1, y1, x2, y2 = box
    hit = x1 <= px[0] <= x2 and y1 <= px[1] <= y2
    dist = math.hypot(px[0] - (x1 + x2) / 2, px[1] - (y1 + y2) / 2)
    return hit, round(dist)


def write_html(out_dir, meta, results, summary):
    e = html.escape
    rows = []
    for r in results:
        res = r["result"]
        raw_pt = f'({res["x"]:.0f}, {res["y"]:.0f})' if "x" in res else "-"
        hit = {True: "◯", False: "✕", None: "-"}[r.get("hit")]
        rows.append(
            f"<tr><td>{r['n']}</td><td>{e(r['target'])}</td><td>{r['frame']}</td><td>{r['mode']}</td>"
            f"<td>{r['width'] or '原寸'}</td><td>{e(raw_pt)}</td><td>{e(str(r.get('px', '-')))}</td>"
            f"<td>{hit}</td><td>{r.get('dist', '-')}</td><td>{res.get('secs', '-')}</td>"
            f"<td>{res.get('completion_tokens', '-')}</td><td>{res.get('reasoning_chars', '-')}</td>"
            f"<td>{e(str(res.get('reason') or res.get('error') or ''))}</td>"
            f"<td><code>{e(str(res.get('raw', ''))[:200])}</code></td></tr>")
    srows = "".join(f"<tr><td>{e(k)}</td><td>{v['hits']}/{v['judged']}</td><td>{v['avg_dist']}</td>"
                    f"<td>{v['avg_secs']}</td></tr>" for k, v in summary.items())
    imgs = "".join(f'<h2>{e(name)}</h2><img src="{e(name)}">' for name in meta["marked_images"])
    page = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><title>座標テスト</title>
<style>
body{{font:14px/1.5 "Yu Gothic UI",Meiryo,sans-serif;margin:16px;background:#fff;color:#1f2328}}
table{{border-collapse:collapse;width:100%;margin-bottom:16px}} td,th{{border:1px solid #d0d7de;padding:4px 6px;vertical-align:top}}
th{{background:#f6f8fa}} img{{max-width:100%;border:1px solid #d0d7de}} code{{font-size:12px;word-break:break-all}}
</style></head><body>
<h1>座標テスト {e(meta['time'])}</h1>
<p>画面 {meta['screen'][0]}×{meta['screen'][1]} / モデル {e(meta['model'])} / 指示文 {meta['lang']} / DPI {e(meta['dpi'])}</p>
<h2>集計（正解範囲を付けた要素のみ）</h2>
<table><tr><th>座標形式 / 思考 / 画像幅</th><th>命中</th><th>平均距離(px)</th><th>平均秒</th></tr>{srows}</table>
<h2>明細</h2>
<table><tr><th>#</th><th>要素</th><th>座標形式</th><th>思考</th><th>画像幅</th><th>モデルの値</th><th>実画面px</th>
<th>命中</th><th>中心からの距離</th><th>秒</th><th>出力tok</th><th>思考文字数</th><th>理由/エラー</th><th>生の返答</th></tr>
{''.join(rows)}</table>
<p>画像の点線枠は正解範囲、丸と十字はモデルの答え（実画面pxに換算）。</p>{imgs}</body></html>"""
    (out_dir / "index.html").write_text(page, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", action="append", help="探す UI 要素（複数可）。'名前@x1,y1,x2,y2' で正解範囲")
    ap.add_argument("--image", help="既存のスクリーンショットを使う（同じ画面で比較したいとき）")
    ap.add_argument("--delay", type=float, default=0, help="撮影までの待ち秒数（画面を整える時間）")
    ap.add_argument("--frames", default="grid", help="座標形式 grid,pixel（カンマ区切り）")
    ap.add_argument("--modes", default="off", help="思考モード off,low,on（カンマ区切り）")
    ap.add_argument("--widths", default=None, help="画像幅（カンマ区切り、0=原寸）。既定は config.yaml の値")
    ap.add_argument("--lang", choices=["en", "ja"], default="en")
    args = ap.parse_args()

    cfg = config.load()
    targets = [parse_target(t) for t in (args.target or DEFAULT_TARGETS)]
    frames = [f.strip() for f in args.frames.split(",") if f.strip()]
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    widths = ([int(w) for w in args.widths.split(",")] if args.widths
              else [cfg["screen"].get("model_image_width", 1512)])
    quality = cfg["screen"].get("jpeg_quality", 90)

    out_dir = ROOT / "scripts" / "out" / time.strftime("coord-%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.image:
        full = Image.open(args.image).convert("RGB")
    else:
        if args.delay:
            print(f"{args.delay:.0f} 秒後に撮影します…", flush=True)
            time.sleep(args.delay)
        full = screen.capture()
    full.save(out_dir / "screenshot.png")
    W, H = full.size
    print(f"画面 {W}x{H}  DPI={voice_agent.DPI_MODE}  出力先 {out_dir}", flush=True)

    client = ModelClient(cfg)
    results, marked_images, summary = [], [], {}
    for width in widths:
        sent = screen.for_model(full, width)
        sent.save(out_dir / f"sent-{width or 'full'}.jpg", quality=quality)
        for frame in frames:
            for mode in modes:
                key = f"{frame} / {mode} / {width or '原寸'}"
                points, boxes, hits, dists, secs = [], [], [], [], []
                for n, (target, box) in enumerate(targets, 1):
                    res = run_one(client, sent, target, frame, mode, args.lang, quality)
                    row = {"n": n, "target": target, "box": box, "frame": frame, "mode": mode, "width": width,
                           "result": res}
                    if "x" in res:
                        row["px"] = to_screen_px(res["x"], res["y"], frame, sent.size, (W, H))
                        row["hit"], row["dist"] = judge(row["px"], box)
                        points.append((*row["px"], f"{n} {target}"))
                    else:
                        row["hit"], row["dist"] = (False, None) if box else (None, None)
                        points.append((-100, -100, ""))  # 色の対応を保つためのダミー（画面外）
                    boxes.append(box)
                    if row["hit"] is not None:
                        hits.append(row["hit"])
                        if row["dist"] is not None:
                            dists.append(row["dist"])
                    if "secs" in res:
                        secs.append(res["secs"])
                    results.append(row)
                    mark = {True: "◯", False: "✕", None: " "}[row["hit"]]
                    where = (f"({res['x']:.0f},{res['y']:.0f}) -> px {row['px']} {mark} d={row['dist']}"
                             if "x" in res else res.get("error"))
                    print(f"[{frame:>5} {mode:>3} w={width or 'full'}] {n}. {target}: {where}  "
                          f"{res.get('secs', '-')}s tok={res.get('completion_tokens', '-')}", flush=True)
                summary[key] = {"hits": sum(hits), "judged": len(hits),
                                "avg_dist": round(sum(dists) / len(dists)) if dists else "-",
                                "avg_secs": round(sum(secs) / len(secs), 2) if secs else "-"}
                name = f"marked-{frame}-{mode}-{width or 'full'}.png"
                screen.mark_points(full, points, boxes=boxes).save(out_dir / name)
                marked_images.append(name)

    print("\n集計（命中/判定数, 平均距離px, 平均秒）")
    for k, v in summary.items():
        print(f"  {k:<24} {v['hits']}/{v['judged']}  d={v['avg_dist']}  {v['avg_secs']}s")
    meta = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "screen": [W, H], "model": cfg["model"]["name"],
            "lang": args.lang, "dpi": voice_agent.DPI_MODE, "marked_images": marked_images}
    (out_dir / "results.json").write_text(
        json.dumps({"meta": meta, "summary": summary, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    write_html(out_dir, meta, results, summary)
    print(f"\n結果: {out_dir / 'index.html'}")


if __name__ == "__main__":
    main()
