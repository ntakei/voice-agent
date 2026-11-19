"""測定のスクリプト（scripts/eval_report.py・scripts/asr_eval.py）のテスト。モデル・マイクは使わない。"""
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import asr_eval  # noqa: E402
import eval_report  # noqa: E402


def make_run(root: Path, name: str, **summary):
    d = root / name
    d.mkdir(parents=True)
    s = {"task": "t", "source": "voice", "result": "done", "reason": "", "steps": 3, "secs": 10.0,
         "thinking_steps": 0, "model_secs_total": 1.0, "dir": str(d), **summary}
    (d / "summary.json").write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")
    (d / "ledger.jsonl").write_text(json.dumps({"step": 1, "front_app": "msedge.exe (Edge)"}) + "\n", encoding="utf-8")
    return d


def test_report_counts_only_verified_success(tmp_path):
    make_run(tmp_path, "20261119-100000-a", verify={"achieved": "yes"}, version="abc1234")
    make_run(tmp_path, "20261119-100100-b", verify={"achieved": "no", "evidence": "404"}, version="abc1234")
    make_run(tmp_path, "20261119-100200-c")                                     # 自己申告だけ → 不明
    make_run(tmp_path, "20261119-100300-d", result="fail", reason="行き詰まった（ブロックが 4 回）")
    make_run(tmp_path, "20261119-100400-e", human_label="success", result="stopped", reason="停止要求（Esc）")
    make_run(tmp_path, "20261119-100500-f", dry_run=True)                       # dry-run は数えない
    runs = eval_report.load_runs(tmp_path)
    assert len(runs) == 5 and runs[0]["_app"] == "msedge.exe"
    r = eval_report.rate(runs)
    assert (r["success"], r["failure"], r["unknown"], r["self_done"]) == (2, 2, 1, 3)
    page = eval_report.build_html(runs, tmp_path)
    assert "50%" in page and "行き詰まり（打ち切り）" in page and "完了と言ったが未達成" in page and "abc1234" in page


def test_cer():
    assert asr_eval.cer("三井情報を開いて", "三井情報を開いて") == 0
    assert asr_eval.cer("三井情報、を開いて。", "三井情報を開いて") == 0              # 句読点は数えない
    assert abs(asr_eval.cer("密集情報", "三井情報") - 0.5) < 1e-9


def test_export_keeps_existing_truth(tmp_path):
    (tmp_path / "_voice").mkdir()
    (tmp_path / "_voice" / "a.wav").write_bytes(b"")
    (tmp_path / "voice_log.jsonl").write_text(
        json.dumps({"wav": "_voice/a.wav", "heard": "密集情報", "corrected": "三井情報", "outcome": "ran"},
                   ensure_ascii=False) + "\n", encoding="utf-8")
    out = asr_eval.export(tmp_path)
    rows = list(csv.DictReader(open(out, encoding="utf-8-sig")))
    rows[0]["truth"] = "三井情報"
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=asr_eval.FIELDS)
        w.writeheader()
        w.writerows(rows)
    asr_eval.export(tmp_path)                                                     # もう一度書き出しても
    rows = list(csv.DictReader(open(out, encoding="utf-8-sig")))
    assert rows[0]["truth"] == "三井情報" and rows[0]["heard"] == "密集情報"          # 記入した truth は残る
