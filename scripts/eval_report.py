"""実行の記録（runs/）を集計して、成功率のレポート（runs/eval.html）を作る。

  python -m uv run python scripts/eval_report.py                    # 集計して runs/eval.html を作る
  python -m uv run python scripts/eval_report.py --verify-missing   # 自動判定がない「完了」の実行を、先に判定する

成功・失敗は evaluate.outcome() で決める（人の判定 → 自動判定 → 不明の順。モデルの「完了」だけでは成功にしない）。
dry-run の実行は数えない。
"""
import argparse
import collections
import html
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from voice_agent import config, evaluate  # noqa: E402

RESULT_JA = {"done": "完了", "fail": "失敗", "stopped": "停止"}
OUT_JA = {"success": "成功", "failure": "失敗", "unknown": "不明"}


def load_runs(runs_dir: Path) -> list[dict]:
    protected = {p.lower() for p in (config.load().get("safety") or {}).get("protected_processes", [])}
    runs = []
    for d in sorted(runs_dir.glob("2*-*")):
        p = d / "summary.json"
        if not d.is_dir() or not p.exists():
            continue
        s = json.loads(p.read_text(encoding="utf-8"))
        if s.get("dry_run"):
            continue
        app = ""
        ledger = d / "ledger.jsonl"
        if ledger.exists():
            for line in ledger.read_text(encoding="utf-8").splitlines():
                fa = (json.loads(line).get("front_app") or "").split(" ")[0]
                if fa and fa.lower() not in protected:
                    app = fa
                    break
        s.update(_dir=d, _app=app or "（なし）", _outcome=evaluate.outcome(s), _time=d.name[:13])
        runs.append(s)
    return runs


def verify_missing(runs: list[dict]) -> int:
    """自動判定がない「完了」の実行を判定して、summary.json に書き足す。"""
    from PIL import Image

    from voice_agent.model_client import ModelClient
    client = ModelClient(config.load())
    n = 0
    for s in runs:
        final = s["_dir"] / "final.jpg"
        if s.get("result") != "done" or s.get("verify") or not final.exists():
            continue
        v = evaluate.verify_task(client, s["task"], Image.open(final).convert("RGB"), s.get("reason", ""), s["_app"])
        evaluate.update_summary(s["_dir"], {"verify": v})
        s["verify"], s["_outcome"] = v, None
        s["_outcome"] = evaluate.outcome(s)
        n += 1
        print(f"  判定 {v['achieved']:7s} {s['task'][:40]}（{v.get('evidence', '')}）", flush=True)
    return n


def rate(rows: list[dict]) -> dict:
    c = collections.Counter(r["_outcome"] for r in rows)
    judged = c["success"] + c["failure"]
    return {"n": len(rows), "success": c["success"], "failure": c["failure"], "unknown": c["unknown"],
            "rate": (c["success"] / judged) if judged else None,
            "self_done": sum(1 for r in rows if r.get("result") == "done"),
            "secs": sum(r.get("secs") or 0 for r in rows) / len(rows) if rows else 0,
            "steps": sum(r.get("steps") or 0 for r in rows) / len(rows) if rows else 0}


def failure_cause(s: dict) -> str:
    reason = str(s.get("reason", ""))
    if s.get("result") == "stopped":
        if "確認" in reason:
            return "確認で中止"
        if "Esc" in reason or "Ctrl+C" in reason or "FAILSAFE" in reason:
            return "人が停止"
        return "停止（安全ルール）"
    if s.get("result") == "fail":
        if "行き詰まった" in reason:
            return "行き詰まり（打ち切り）"
        if "最大ステップ" in reason:
            return "最大ステップに到達"
        if "モデル呼び出し" in reason:
            return "モデル呼び出しエラー"
        return "モデルが失敗と判断"
    if (s.get("verify") or {}).get("achieved") == "no" or s.get("human_label") == "failure":
        return "完了と言ったが未達成"
    return ""


def table(title: str, groups: dict[str, list[dict]]) -> str:
    e = html.escape
    rows = []
    for k, g in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        r = rate(g)
        pct = f"{r['rate'] * 100:.0f}%" if r["rate"] is not None else "—"
        rows.append(f"<tr><td>{e(k)}</td><td>{r['n']}</td><td class='num big'>{pct}</td><td>{r['success']}</td>"
                    f"<td>{r['failure']}</td><td>{r['unknown']}</td><td>{r['self_done']}</td>"
                    f"<td>{r['steps']:.1f}</td><td>{r['secs']:.0f}</td></tr>")
    return (f"<h2>{e(title)}</h2><table><tr><th></th><th>実行</th><th>成功率</th><th>成功</th><th>失敗</th>"
            f"<th>不明</th><th>自己申告の完了</th><th>平均ステップ</th><th>平均秒</th></tr>{''.join(rows)}</table>")


def voice_stats(runs_dir: Path) -> str:
    p = runs_dir / "voice_log.jsonl"
    if not p.exists():
        return "<h2>音声</h2><p class='mute'>まだ記録がありません（voice_log.jsonl）。</p>"
    entries = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
    c = collections.Counter(x.get("outcome") for x in entries)
    corrected = sum(1 for x in entries if x.get("corrected"))
    n = len(entries) or 1
    return (f"<h2>音声（発話 {len(entries)} 回）</h2><table><tr><th>実行した</th><th>聞き直した（補正で不明）</th>"
            f"<th>捨てた（無音・幻の文など）</th><th>補正で直した</th></tr><tr><td>{c['ran']}</td>"
            f"<td>{c['asked_again']}</td><td>{c['dropped']}</td><td>{corrected}（{corrected / n * 100:.0f}%）</td></tr></table>"
            "<p class='mute'>音声認識の精度は scripts/asr_eval.py で測ります。</p>")


def build_html(runs: list[dict], runs_dir: Path) -> str:
    e = html.escape
    total = rate(runs)
    human = sum(1 for r in runs if r.get("human_label"))
    verified = sum(1 for r in runs if r.get("verify"))
    pct = f"{total['rate'] * 100:.0f}%" if total["rate"] is not None else "—"
    causes = collections.Counter(c for c in (failure_cause(r) for r in runs) if c)
    agree = collections.Counter(
        (RESULT_JA.get(r.get("result"), r.get("result")), (r.get("verify") or {}).get("achieved", "未判定"))
        for r in runs)
    recent = []
    for r in reversed(runs[-60:]):
        v = r.get("verify") or {}
        heard = r.get("heard", "")
        task = e(r["task"]) + (f"<br><span class='mute'>認識: {e(heard)}</span>" if heard and heard != r["task"] else "")
        link = (r["_dir"] / "report.html").relative_to(runs_dir).as_posix()
        recent.append(
            f"<tr><td>{e(r['_time'])}</td><td>{task}</td><td>{e(r['_app'])}</td>"
            f"<td>{e(RESULT_JA.get(r.get('result'), r.get('result', '')))}</td>"
            f"<td>{e({'yes': '達成', 'no': '未達成', 'unclear': '不明'}.get(v.get('achieved'), '—'))}"
            f"<br><span class='mute'>{e(v.get('evidence', ''))}</span></td>"
            f"<td>{e({'success': '成功', 'failure': '失敗'}.get(r.get('human_label'), '—'))}</td>"
            f"<td class='o-{r['_outcome']}'>{OUT_JA[r['_outcome']]}</td><td>{r.get('steps', '')}</td>"
            f"<td>{r.get('secs', '')}</td><td><a href='{e(link)}'>レポート</a></td></tr>")
    by = lambda key: collections.defaultdict(list)  # noqa: E731
    groups = {name: by(name) for name in ("source", "version", "app", "day")}
    for r in runs:
        groups["source"][{"voice": "音声", "text": "テキスト"}.get(r.get("source"), r.get("source", "?"))].append(r)
        groups["version"][r.get("version") or "（記録なし: 測定を入れる前）"].append(r)
        groups["app"][r["_app"]].append(r)
        groups["day"][r["_time"][:8]].append(r)
    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><title>成功率レポート</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{{--bg:#fff;--fg:#1f2328;--mute:#57606a;--card:#f6f8fa;--line:#d0d7de;--ok:#1a7f37;--ng:#cf222e;--warn:#9a6700}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0d1117;--fg:#e6edf3;--mute:#8d96a0;--card:#161b22;--line:#30363d;--ok:#3fb950;--ng:#f85149;--warn:#d29922}}}}
body{{background:var(--bg);color:var(--fg);font:14px/1.6 "Yu Gothic UI",Meiryo,system-ui,sans-serif;max-width:1200px;margin:0 auto;padding:16px}}
h1{{font-size:22px;margin:0}} h2{{font-size:17px;margin:24px 0 6px}}
.kpis{{display:flex;gap:12px;flex-wrap:wrap;margin:12px 0}} .kpi{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 16px;min-width:140px}}
.kpi b{{display:block;font-size:26px}} .mute{{color:var(--mute);font-size:12px}}
table{{border-collapse:collapse;width:100%;font-size:13px}} th,td{{border-bottom:1px solid var(--line);padding:5px 8px;text-align:left;vertical-align:top}}
th{{color:var(--mute);font-weight:normal}} .big{{font-weight:bold}}
.o-success{{color:var(--ok);font-weight:bold}} .o-failure{{color:var(--ng);font-weight:bold}} .o-unknown{{color:var(--mute)}}
a{{color:inherit}}
</style></head><body>
<h1>成功率レポート</h1>
<p class="mute">成功・失敗は「人の判定 → 終了時の画面による自動判定 → 不明」の順に決めています。モデルの「完了」だけでは成功に数えません。
途中で止めた・失敗した実行は失敗に数えます。成功率 = 成功 ÷（成功 ＋ 失敗）。dry-run は数えません。</p>
<div class="kpis">
<div class="kpi"><span class="mute">成功率</span><b>{pct}</b><span class="mute">成功 {total['success']} / 失敗 {total['failure']}</span></div>
<div class="kpi"><span class="mute">実行</span><b>{total['n']}</b><span class="mute">不明 {total['unknown']}</span></div>
<div class="kpi"><span class="mute">モデルの自己申告の完了</span><b>{total['self_done']}</b><span class="mute">{total['self_done'] / max(1, total['n']) * 100:.0f}%</span></div>
<div class="kpi"><span class="mute">人の判定 / 自動判定</span><b>{human} / {verified}</b><span class="mute">判定済みの実行</span></div>
</div>
<h2>失敗の原因</h2><table><tr><th>原因</th><th>回数</th></tr>{''.join(f'<tr><td>{e(k)}</td><td>{v}</td></tr>' for k, v in causes.most_common())}</table>
<h2>モデルの自己申告と自動判定</h2><table><tr><th>モデルの結果</th><th>自動判定</th><th>回数</th></tr>{''.join(f'<tr><td>{e(a)}</td><td>{e({"yes": "達成", "no": "未達成", "unclear": "不明"}.get(b, b))}</td><td>{v}</td></tr>' for (a, b), v in sorted(agree.items()))}</table>
{table("入力の種類別", groups["source"])}
{table("コードのバージョン別（修正の前後の比較）", groups["version"])}
{table("操作したアプリ別", groups["app"])}
{table("日別", groups["day"])}
{voice_stats(runs_dir)}
<h2>最近の実行（新しい順、最大60件）</h2>
<table><tr><th>日時</th><th>指示</th><th>アプリ</th><th>モデルの結果</th><th>自動判定</th><th>人の判定</th><th>集計</th><th>ステップ</th><th>秒</th><th></th></tr>
{''.join(recent)}</table>
</body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify-missing", action="store_true", help="自動判定がない「完了」の実行を先に判定する")
    ap.add_argument("--runs", type=Path, default=config.RUNS_DIR)
    args = ap.parse_args()
    runs = load_runs(args.runs)
    if args.verify_missing:
        print("自動判定がない実行を判定しています…", flush=True)
        print(f"  {verify_missing(runs)} 件を判定しました")
    out = args.runs / "eval.html"
    out.write_text(build_html(runs, args.runs), encoding="utf-8")
    t = rate(runs)
    pct = f"{t['rate'] * 100:.0f}%" if t["rate"] is not None else "—"
    print(f"実行 {t['n']} 件 / 成功率 {pct}（成功 {t['success']}・失敗 {t['failure']}・不明 {t['unknown']}）"
          f" / モデルの自己申告の完了 {t['self_done']} 件")
    print(f"レポート: {out}")


if __name__ == "__main__":
    main()
