"""実行ごとのログフォルダと report.html。

runs/<日時>-<タスク>/
  task.txt                 タスク
  step-NN-sent.jpg         モデルが見たスクリーンショット（そのまま）
  step-NN-marked.jpg       同じ画像にクリック位置（または操作内容）を書き込んだもの
  model/step-NN.json       モデルへの指示・返答・思考・トークン数
  ledger.jsonl             1ステップ1行の記録
  final.jpg                終了時の画面
  summary.json / report.html
"""
import html
import json
import re
import time
from pathlib import Path

from . import config


def _slug(task: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\s「」『』、。]+', "-", task).strip("-")
    return s[:30] or "task"


class Session:
    def __init__(self, task: str, source: str = "text", runs_dir: Path | None = None, meta: dict | None = None):
        root = Path(runs_dir or config.RUNS_DIR)
        self.dir = root / f"{time.strftime('%Y%m%d-%H%M%S')}-{_slug(task)}"
        (self.dir / "model").mkdir(parents=True, exist_ok=True)
        self.task, self.source, self.meta = task, source, dict(meta or {})
        self.t0 = time.time()
        self.steps: list[dict] = []
        (self.dir / "task.txt").write_text(f"{task}\n\nsource: {source}\n", encoding="utf-8")

    def path(self, name: str) -> Path:
        return self.dir / name

    def log_step(self, row: dict, model_record: dict | None = None) -> None:
        row = {"t": round(time.time() - self.t0, 2), **row}
        self.steps.append(row)
        with open(self.path("ledger.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        if model_record is not None:
            (self.dir / "model" / f"step-{row['step']:02d}.json").write_text(
                json.dumps(model_record, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    def finish(self, result: str, reason: str = "", warnings: list[str] | None = None) -> dict:
        secs = round(time.time() - self.t0, 1)
        model_secs = [s["model_secs"] for s in self.steps if s.get("model_secs") is not None]
        summary = {
            "task": self.task, "source": self.source, "result": result, "reason": reason,
            "warnings": list(warnings or []),
            "steps": len(self.steps), "secs": secs,
            "thinking_steps": sum(1 for s in self.steps if s.get("thinking") not in (None, "off")),
            "model_secs_total": round(sum(model_secs), 1),
            "dir": str(self.dir), **self.meta,
        }
        self.path("summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        with open(self.dir.parent / "index.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), **summary}, ensure_ascii=False) + "\n")
        self.write_report(summary)
        return summary

    def write_report(self, s: dict) -> None:
        e = html.escape
        result_ja = {"done": "完了", "fail": "失敗", "stopped": "停止"}.get(s["result"], s["result"])
        sections = []
        for r in self.steps:
            a = r.get("action") or {}
            img = ""
            if r.get("marked") and self.path(r["marked"]).exists():
                sent = f' <a href="{e(r["sent"])}">モデルが見た画像（印なし）</a>' if r.get("sent") else ""
                img = f'<img src="{e(r["marked"])}" loading="lazy" alt="ステップ{r["step"]}">{sent}'
            meta = " · ".join(x for x in [
                f'モデル {r["model_secs"]} 秒' if r.get("model_secs") is not None else "",
                f'思考 {r["thinking"]}' if r.get("thinking") else "",
                f'出力 {r["completion_tokens"]} tok' if r.get("completion_tokens") else "",
                f'前面 {r["front_app"]}' if r.get("front_app") else "",
                {True: "直前の操作で画面が変化", False: "直前の操作で画面が変化せず"}.get(r.get("changed_before"), "")
                + (f'（{r["compared"]}）' if r.get("compared") and r.get("changed_before") is not None else ""),
                "dry-run" if r.get("dry_run") else "",
            ] if x)
            notes = "".join(f'<p class="note">{e(str(n))}</p>' for n in r.get("notes", []) + (
                [r["thinking_cut"]] if r.get("thinking_cut") else []))
            err = f'<p class="err">{e(str(r["error"]))}</p>' if r.get("error") else ""
            if r.get("warning"):
                err += f'<p class="warn">⚠ {e(str(r["warning"]))}</p>'
            detail = f'<p class="detail">{e(r["detail"])}</p>' if r.get("detail") else ""
            raw = f'<details><summary>モデルの返答</summary><pre>{e(str(r.get("raw", "")))}</pre></details>' \
                if r.get("raw") else ""
            sections.append(
                f'<section><h3>ステップ {r["step"]} — {e(r.get("label", ""))}</h3>'
                f'<p class="reason">{e(str(a.get("reason", "")))}</p><p class="meta">{e(meta)}</p>'
                f'{detail}{notes}{err}{raw}{img}</section>')
        final = '<section><h3>終了時の画面</h3><img src="final.jpg"></section>' \
            if self.path("final.jpg").exists() else ""
        audio = '<audio controls src="audio.wav"></audio>' if self.path("audio.wav").exists() else ""
        warn_box = "".join(f'<div class="warnbox"><p class="warn">⚠ 要確認: {e(w)}</p></div>'
                           for w in s.get("warnings", []))
        if s.get("warnings") and s["result"] == "done":
            result_ja = "完了（要確認）"
        page = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><title>実行レポート</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{{--bg:#fff;--fg:#1f2328;--mute:#57606a;--card:#f6f8fa;--line:#d0d7de;--ok:#1a7f37;--ng:#cf222e;--warn:#9a6700}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0d1117;--fg:#e6edf3;--mute:#8d96a0;--card:#161b22;--line:#30363d}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.6 "Yu Gothic UI",Meiryo,system-ui,sans-serif;max-width:1100px;margin:0 auto;padding:16px}}
h1{{font-size:22px;margin:0 0 4px}} .badge{{color:#fff;padding:2px 10px;border-radius:12px;font-size:13px}}
.done{{background:var(--ok)}} .fail{{background:var(--ng)}} .stopped{{background:var(--warn)}}
section{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:14px 0}}
h3{{margin:0 0 4px;font-size:16px}} .reason{{margin:2px 0}} .meta,.detail,.note{{color:var(--mute);font-size:13px;margin:2px 0}}
.err{{color:var(--ng);margin:4px 0}} .warn{{color:var(--warn);font-weight:bold;margin:4px 0}}
.warnbox{{border:2px solid var(--warn);border-radius:10px;padding:8px 12px;margin:10px 0}} img{{width:100%;border-radius:6px;border:1px solid var(--line);margin-top:8px}}
pre{{white-space:pre-wrap;word-break:break-all;font-size:12px}} a{{color:inherit;font-size:13px}}
</style></head><body>
<h1>{e(s['task'])}</h1>
<p><span class="badge {e(s['result'])}">{e(result_ja)}</span>
&nbsp;{s['steps']} ステップ · {s['secs']} 秒 · モデル合計 {s['model_secs_total']} 秒 · 思考あり {s['thinking_steps']} 回
{(' · ' + e(s['reason'])) if s['reason'] else ''}</p>
<p class="meta">{e(' · '.join(f'{k}={v}' for k, v in self.meta.items()))}</p>
{warn_box}{audio}{''.join(sections)}{final}</body></html>"""
        self.path("report.html").write_text(page, encoding="utf-8")
