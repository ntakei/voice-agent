"""操作ループのテスト（偽のモデルと偽の画面。実際の操作もモデル呼び出しもしない）。"""
import json

import pytest
from PIL import Image

from voice_agent import agent as agent_mod
from voice_agent import config, executor, safety, screen, uia, winapi

SCREEN = (1920, 1080)
CFG = {
    "model": {"name": "fake", "thinking_normal": "off", "thinking_stuck": "on"},
    "screen": {"model_image_width": 1512, "jpeg_quality": 80},
    "agent": {"dry_run": True, "max_steps": 25, "history_steps": 8, "settle_max": 0.1,
              "think_after_no_change": 2, "think_after_bad_json": 2, "think_after_blocked": 2},
    "apps": [{"names": ["メモ帳"], "launch": "notepad.exe", "process": ["notepad.exe"]}],
}


class FakeClient:
    """決められた返答を順に返す。受け取った思考モードと指示文を記録する。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, messages, image=None, thinking="off", **kw):
        self.calls.append({"thinking": thinking, "prompt": messages[-1]["content"], "image_size": image.size})
        content = self.replies.pop(0) if self.replies else json.dumps({"action": "done", "reason": "終わり"})
        return {"content": content, "reasoning": "", "finish_reason": "stop",
                "usage": {"completion_tokens": 10}, "secs": 0.01, "thinking": thinking}


def solid(color):
    return Image.new("RGB", SCREEN, color)


@pytest.fixture
def env(monkeypatch, tmp_path):
    """画面・前面ウィンドウ・実行をすべて偽物にする。frames に画像を積むと capture/settle が順に返す。"""
    frames = []
    state = {"last": solid((0, 0, 0))}

    def capture():
        if frames:
            state["last"] = frames.pop(0)
        return state["last"]
    monkeypatch.setattr(screen, "capture", capture)
    monkeypatch.setattr(screen, "screen_size", lambda: SCREEN)
    monkeypatch.setattr(screen, "settle", lambda stop_event=None, **k: capture())
    monkeypatch.setattr(winapi, "foreground", lambda: winapi.WindowInfo(1, "無題 - メモ帳", "notepad.exe"))
    monkeypatch.setattr(winapi, "find_window", lambda procs: None)
    acted = []
    for name in ("moveTo", "click", "doubleClick", "hotkey", "scroll"):
        monkeypatch.setattr(executor.pyautogui, name, lambda *a, _n=name, **k: acted.append(_n))
    monkeypatch.setattr(executor, "paste_text", lambda text: acted.append("paste"))
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path)
    # UI Automation と FAILSAFE の確認も偽物に（テスト中の実際のマウス位置に左右されないように）
    el = {"clicked": None, "focused": None}
    monkeypatch.setattr(uia, "element_at", lambda x, y: el["clicked"])
    monkeypatch.setattr(uia, "focused_element", lambda: el["focused"])
    monkeypatch.setattr(agent_mod.Agent, "failsafe_check", lambda self: None)
    return {"frames": frames, "acted": acted, "tmp": tmp_path, "el": el}


class QuietHooks(agent_mod.Hooks):
    def __init__(self):
        self.events = []

    def status(self, state, text):
        self.events.append(state)


def make(replies, dry_run=True, **kw):
    client = FakeClient(replies)
    a = agent_mod.Agent(CFG, hooks=QuietHooks(), dry_run=dry_run, client=client, **kw)
    return a, client


def J(**k):
    return json.dumps(k, ensure_ascii=False)


def test_click_then_done_writes_report(env):
    ag, client = make([J(action="click", x=100, y=200, reason="ボタンを押す"), J(action="done", reason="完了")])
    s = ag.run("テスト")
    assert s["result"] == "done" and s["steps"] == 2
    d = env["tmp"] / s["dir"].split("\\")[-1].split("/")[-1]
    for name in ("report.html", "summary.json", "ledger.jsonl", "step-01-sent.jpg", "step-01-marked.jpg",
                 "model/step-01.json", "final.jpg"):
        assert (d / name).exists(), name
    assert client.calls[0]["image_size"] == (1512, 850)
    assert env["acted"] == []  # dry-run なので何も操作していない
    html = (d / "report.html").read_text(encoding="utf-8")
    assert "ボタンを押す" in html and "完了" in html


def test_live_mode_executes(env):
    env["frames"] += [solid((0, 0, 0)), solid((255, 255, 255))]
    ag, _ = make([J(action="click", x=100, y=200), J(action="done")], dry_run=False)
    assert ag.run("テスト")["result"] == "done"
    assert env["acted"] == ["moveTo", "click"]


def test_max_steps(env):
    # 画面が毎回変わる（=効果あり）スクロールを返し続ける
    env["frames"] += [solid((i, i, i)) for i in range(0, 250, 10)]
    ag, client = make([J(action="scroll", dy=-1 - (i % 3)) for i in range(30)], dry_run=False, max_steps=5)
    s = ag.run("テスト")
    assert s["result"] == "fail" and "最大ステップ数" in s["reason"] and len(client.calls) == 5


def test_default_cap_is_25(env):
    ag, _ = make([])
    assert ag.max_steps == 25


def test_cap_cannot_exceed_25(env):
    ag, _ = make([], max_steps=100)
    assert ag.max_steps == 25
    cfg = {**CFG, "agent": {**CFG["agent"], "max_steps": 99}}
    assert agent_mod.Agent(cfg, hooks=QuietHooks(), client=FakeClient([])).max_steps == 25


def test_invalid_json_escalates_thinking(env):
    ag, client = make(["これは JSON ではない", "{壊れた", J(action="done")])
    s = ag.run("テスト")
    assert s["result"] == "done"
    assert [c["thinking"] for c in client.calls] == ["off", "off", "on"]


def test_invalid_action_is_not_executed(env):
    ag, client = make([J(action="click", x=5000, y=10), J(action="open_app", text="cmd"), J(action="done")],
                      dry_run=False)
    s = ag.run("テスト")
    assert s["result"] == "done" and env["acted"] == []
    assert "invalid action" in client.calls[1]["prompt"]


def test_no_change_blocks_and_escalates(env):
    # 画面がまったく変わらないクリックを繰り返す -> 2回目の後にブロック、思考を強める
    same_click = J(action="click", x=100, y=200, reason="押す")
    ag, client = make([same_click, same_click, same_click, J(action="done")], dry_run=False)
    s = ag.run("テスト")
    assert s["result"] == "done"
    assert env["acted"].count("click") == 2          # 3回目はブロックされて実行されない
    assert client.calls[2]["thinking"] == "on"         # 変化なしが2回続いたので思考オン
    assert "BLOCKED" in client.calls[3]["prompt"]
    assert "did NOT change" in client.calls[2]["prompt"]


def test_three_in_a_row_blocked_even_if_screen_changes(env):
    env["frames"] += [solid((i * 40, 0, 0)) for i in range(6)]
    k = J(action="key", text="down", target_app="メモ帳")
    ag, client = make([k, k, k, J(action="done")], dry_run=False)
    ag.run("テスト")
    assert env["acted"].count("hotkey") == 2


def test_history_limited_to_8(env):
    env["frames"] += [solid((i * 9, 0, 0)) for i in range(28)]   # 毎回画面が変わる
    replies = [J(action="scroll", dy=i + 1, reason=f"手{i}") for i in range(11)] + [J(action="done")]
    ag, client = make(replies, dry_run=False)
    ag.run("テスト")
    last = client.calls[-1]["prompt"]
    assert "手10" in last and "手3" in last and "手2" not in last  # 直近8件（手3〜手10）だけ
    assert "\n4. " in last  # 番号は通し番号


def test_type_with_coordinates_is_noted(env):
    ag, _ = make([J(action="type", text="12", target_app="メモ帳", x=10, y=10), J(action="done")])
    s = ag.run("テスト")
    d = env["tmp"] / s["dir"].replace("\\", "/").split("/")[-1]
    rows = [json.loads(l) for l in (d / "ledger.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any("座標" in n for n in rows[0]["notes"])


def test_stop_before_start(env):
    class StopAtFirstLook(QuietHooks):
        def __init__(self, ag_ref):
            super().__init__()
            self.ag_ref = ag_ref

        def status(self, state, text):
            if state == "looking":
                self.ag_ref[0].stop()
    ref = []
    client = FakeClient([J(action="click", x=1, y=1)])
    ag = agent_mod.Agent(CFG, hooks=StopAtFirstLook(ref), dry_run=False, client=client)
    ref.append(ag)
    s = ag.run("テスト")
    assert s["result"] == "stopped" and env["acted"] == []


def test_safety_hook_blocks(env):
    class Guarded(agent_mod.Agent):
        def check_safety(self, a, front, point):
            return safety.Decision(safety.BLOCK if a["action"] == "click" else safety.ALLOW, "テストでブロック")
    client = FakeClient([J(action="click", x=1, y=1), J(action="done")])
    ag = Guarded(CFG, hooks=QuietHooks(), dry_run=False, client=client)
    assert ag.run("テスト")["result"] == "done"
    assert env["acted"] == [] and "テストでブロック" in client.calls[1]["prompt"]


def test_failsafe_stops(env, monkeypatch):
    import pyautogui

    def boom(*a, **k):
        raise pyautogui.FailSafeException("corner")
    monkeypatch.setattr(executor.pyautogui, "moveTo", boom)
    ag, _ = make([J(action="click", x=1, y=1)], dry_run=False)
    s = ag.run("テスト")
    assert s["result"] == "stopped" and "FAILSAFE" in s["reason"]


def test_execute_error_is_logged_then_fails(env, monkeypatch):
    def broken(*a, **k):
        raise OSError("壊れた")
    monkeypatch.setattr(executor.pyautogui, "hotkey", broken)
    ag, _ = make([J(action="key", text="ctrl+s", target_app="メモ帳")], dry_run=False)
    s = ag.run("テスト")
    assert s["result"] == "fail" and "実行中のエラー" in s["reason"]
    d = env["tmp"] / s["dir"].replace("\\", "/").split("/")[-1]
    rows = [json.loads(l) for l in (d / "ledger.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["step"] == 1 and "壊れた" in rows[-1]["error"]   # 失敗したステップもログに残る


def test_open_app_failure_is_told_to_model(env, monkeypatch):
    def denied(exe):
        raise PermissionError(5, "アクセスが拒否されました。")
    monkeypatch.setattr(executor, "launch", denied)
    ag, client = make([J(action="open_app", text="メモ帳"), J(action="done")], dry_run=False)
    assert ag.run("テスト")["result"] == "done"
    assert "FAILED" in client.calls[1]["prompt"]


def test_confirm_timeout_is_recorded(env):
    class TimeoutHooks(QuietHooks):
        last_answer = "timeout"

        def confirm(self, q):
            return False
    client = FakeClient([J(action="click", x=1, y=1, risky=True, reason="送る")])
    ag = agent_mod.Agent(CFG, hooks=TimeoutHooks(), dry_run=False, client=client)
    s = ag.run("テスト")
    assert s["result"] == "stopped" and "時間切れ" in s["reason"]
