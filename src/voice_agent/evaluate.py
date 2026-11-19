"""実行の結果を「本当に成功したか」で測るための部品。

モデルの「完了（done）」は自己申告なので、それとは別に次を記録する（summary.json に追記し、report.html にも出す）。
  - verify:      終了時の画面を、操作したのとは別のモデル呼び出しで見て「達成されたか」を判定（自動）
  - human_label: 実行のあとに人が答えた「成功／失敗」（いちばん確か）
  - version:     どのバージョンのコードで動かしたか（修正の前後で成功率を比べるため）
集計は scripts/eval_report.py で行う。
"""
import json
import subprocess
import time
from pathlib import Path

from . import config
from .coords import parse_json

VERIFY_SYSTEM = """You check whether a task on a Windows PC was actually completed, by looking at the FINAL screenshot.
The agent that did the task claims it is done; do not trust the claim. Judge only from what is visible on the screen.

Reply with ONE JSON object and nothing else:
{"achieved": "yes" | "no" | "unclear", "evidence": "<画面から分かる根拠を短い日本語で（40文字以内）>"}

- "yes": the screen clearly shows the requested result (e.g. the page or app the task asked for, the typed text,
  the calculated value).
- "no": the screen shows the result is missing or wrong (an error page, a different page, empty field, etc.).
- "unclear": the result cannot be seen on this screen (e.g. the task was to close something), or you are not sure."""

ACHIEVED = ("yes", "no", "unclear")
LABELS = ("success", "failure")


def code_version() -> str:
    """git のコミット（短い形）。コミットしていない変更があれば末尾に + を付ける。分からなければ空。"""
    try:
        h = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=config.ROOT, capture_output=True,
                           text=True, timeout=5).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=config.ROOT,
                               capture_output=True, text=True, timeout=5).stdout.strip()
        return h + ("+" if dirty else "") if h else ""
    except Exception:  # noqa: BLE001
        return ""


def verify_task(client, task: str, image, claim: str = "", front_app: str = "", spoken: list[str] | None = None) -> dict:
    """終了時の画面で、タスクが達成されたかをモデルに判定させる。戻り値: achieved / evidence / secs。"""
    t = time.time()
    lines = [f"TASK: {task}"]
    if claim:
        lines.append(f"AGENT'S CLAIM: {claim}")
    if front_app:
        lines.append(f"FRONT APP: {front_app}")
    if spoken:
        lines.append("AGENT SAID ALOUD: " + " / ".join(spoken))
        lines.append("If the task asked to read aloud or tell something, judge whether what the agent said aloud "
                     "matches what the screen shows.")
    lines.append("Return the JSON.")
    try:
        r = client.chat([{"role": "system", "content": VERIFY_SYSTEM}, {"role": "user", "content": "\n".join(lines)}],
                        image=image, thinking="off", max_tokens=200)
    except Exception as e:  # noqa: BLE001
        return {"achieved": "unclear", "evidence": "", "error": f"{type(e).__name__}: {e}", "secs": round(time.time() - t, 2)}
    a = parse_json(r.get("content", "")) or {}
    achieved = str(a.get("achieved", "unclear")).lower()
    return {"achieved": achieved if achieved in ACHIEVED else "unclear", "evidence": str(a.get("evidence", ""))[:80],
            "secs": round(time.time() - t, 2)}


def update_summary(run_dir: str | Path, extra: dict) -> dict:
    """summary.json に項目を足して書き直し、report.html も作り直す。"""
    from .logbook import Session
    d = Path(run_dir)
    p = d / "summary.json"
    s = json.loads(p.read_text(encoding="utf-8"))
    s.update(extra)
    p.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        Session.reload(d).write_report(s)
    except Exception:  # noqa: BLE001  レポートが作れなくても summary.json は残る
        pass
    return s


def set_human_label(run_dir: str | Path, label: str, how: str = "") -> dict:
    """人の判定（success / failure）を記録する。"""
    assert label in LABELS, label
    return update_summary(run_dir, {"human_label": label, "human_label_how": how,
                                    "human_label_at": time.strftime("%Y-%m-%d %H:%M:%S")})


def outcome(s: dict) -> str:
    """集計に使う結果: success / failure / unknown。人の判定 → 自動判定 → 不明 の順に使う。

    自己申告の done だけでは success にしない（モデルの「完了」は検証されていないため）。
    途中で止めた（stopped）・失敗（fail）は、人の判定がなければ failure とする。
    """
    if s.get("human_label") in LABELS:
        return s["human_label"]
    if s.get("result") in ("fail", "stopped"):
        return "failure"
    v = (s.get("verify") or {}).get("achieved")
    if v == "yes":
        return "success"
    if v == "no":
        return "failure"
    return "unknown"
