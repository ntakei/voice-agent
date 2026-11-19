"""音声認識の精度を測る（録音に「正しい文」を付けて、文字の誤り率 CER を出す）。

  1. 一覧を作る:  python -m uv run python scripts/asr_eval.py --export
       runs/asr_labels.csv に、これまでの録音（runs/_voice/*.wav と各実行の audio.wav）を書き出す。
       Excel で開き、truth の列に「本当に言った文」を書いて保存する（空の行は使わない）。
       もう一度 --export すると、書いた truth は残したまま、新しい録音だけを足す。
  2. 測る:        python -m uv run python scripts/asr_eval.py --eval [--models small,large-v3-turbo]
       truth のある録音を、指定したモデル（今の設定: 手がかりの単語・VAD など）で認識し直して CER を出す。
       あわせて、記録に残っている「認識した文」「補正した文」の CER も出す（補正の効果が分かる）。
       結果は runs/asr_eval.csv にも保存する。

CER（文字誤り率）= 編集距離 ÷ 正しい文の文字数。句読点・空白・大文字小文字の違いは数えない。小さいほど良い。
"""
import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from voice_agent import config  # noqa: E402

FIELDS = ["id", "wav", "heard", "corrected", "truth"]


def norm(s: str) -> str:
    return re.sub(r"[\s、。,.!！?？・「」『』()（）\"']+", "", s or "").lower()


def edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def cer(hyp: str, truth: str) -> float:
    t = norm(truth)
    return edit_distance(norm(hyp), t) / max(1, len(t))


def collect(runs: Path) -> list[dict]:
    """録音と、そのときの認識結果の一覧。"""
    rows = []
    log = runs / "voice_log.jsonl"
    if log.exists():
        for line in log.read_text(encoding="utf-8").splitlines():
            x = json.loads(line)
            if x.get("wav"):
                rows.append({"id": Path(x["wav"]).stem, "wav": x["wav"], "heard": x.get("heard") or x.get("raw", ""),
                             "corrected": x.get("corrected") or x.get("task", "")})
    for d in sorted(runs.glob("2*-*")):
        if (d / "audio.wav").exists() and (d / "summary.json").exists():
            s = json.loads((d / "summary.json").read_text(encoding="utf-8"))
            if "heard" in s and (runs / "voice_log.jsonl").exists():
                continue          # 音声ごとの記録を入れた後の実行は、voice_log.jsonl のほうに入っている
            rows.append({"id": d.name, "wav": f"{d.name}/audio.wav", "heard": s.get("heard") or s.get("task", ""),
                         "corrected": s.get("corrected", "")})
    return rows


def export(runs: Path) -> Path:
    out = runs / "asr_labels.csv"
    old = {}
    if out.exists():
        with open(out, encoding="utf-8-sig", newline="") as f:
            old = {r["id"]: r for r in csv.DictReader(f)}
    rows = collect(runs)
    for r in rows:
        r["truth"] = (old.get(r["id"]) or {}).get("truth", "")
    with open(out, "w", encoding="utf-8-sig", newline="") as f:      # Excel で文字化けしないよう BOM 付き
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    filled = sum(1 for r in rows if r["truth"].strip())
    print(f"{out} に {len(rows)} 件を書き出しました（truth 記入済み {filled} 件）。")
    return out


def evaluate(runs: Path, models: list[str]) -> None:
    import numpy as np

    from voice_agent import stt
    sys.path.insert(0, str(ROOT / "scripts"))
    from stt_compare import read_wav
    src = runs / "asr_labels.csv"
    if not src.exists():
        sys.exit("先に --export で runs/asr_labels.csv を作り、truth を記入してください。")
    with open(src, encoding="utf-8-sig", newline="") as f:
        rows = [r for r in csv.DictReader(f) if r.get("truth", "").strip() and (runs / r["wav"]).exists()]
    if not rows:
        sys.exit("truth が記入された行がありません。")
    results = {"記録の認識結果": [cer(r["heard"], r["truth"]) for r in rows],
               "記録の補正後": [cer(r["corrected"] or r["heard"], r["truth"]) for r in rows]}
    sc = (config.load().get("voice") or {}).get("stt") or {}
    per_row = {r["id"]: {"truth": r["truth"], "heard": r["heard"], "corrected": r["corrected"]} for r in rows}
    for name in models:
        tr = stt.WhisperTranscriber(name, compute_type=sc.get("compute_type", "int8"),
                                    beam_size=int(sc.get("beam_size", 5)), vad_filter=bool(sc.get("vad_filter", True)),
                                    initial_prompt=sc.get("initial_prompt"), vocabulary=sc.get("vocabulary"),
                                    max_prompt_tokens=int(sc.get("max_prompt_tokens", 150))).load()
        tr.transcribe(np.zeros(stt.RATE, np.float32))
        scores, secs = [], []
        for r in rows:
            t = time.time()
            out = tr.transcribe(read_wav(runs / r["wav"]))
            secs.append(time.time() - t)
            scores.append(cer(out.text, r["truth"]))
            per_row[r["id"]][name] = out.text
        results[f"{name}（今の設定で認識し直し、平均 {sum(secs) / len(secs):.1f} 秒）"] = scores
    print(f"\ntruth のある録音 {len(rows)} 件の CER（小さいほど良い。0% = 完全一致）")
    for k, v in results.items():
        exact = sum(1 for x in v if x == 0)
        print(f"  {sum(v) / len(v) * 100:5.1f}%  完全一致 {exact}/{len(v)}  {k}")
    out = runs / "asr_eval.csv"
    keys = ["truth", "heard", "corrected", *models]
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", *keys])
        for i, v in per_row.items():
            w.writerow([i, *[v.get(k, "") for k in keys]])
    print(f"1件ごとの結果: {out}")


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--export", action="store_true")
    g.add_argument("--eval", action="store_true")
    ap.add_argument("--models", default="", help="認識し直すモデル（カンマ区切り）。既定は config の voice.stt.model")
    ap.add_argument("--runs", type=Path, default=config.RUNS_DIR)
    args = ap.parse_args()
    if args.export:
        export(args.runs)
    else:
        default = ((config.load().get("voice") or {}).get("stt") or {}).get("model", "small")
        evaluate(args.runs, [m.strip() for m in (args.models or default).split(",") if m.strip()])


if __name__ == "__main__":
    main()
