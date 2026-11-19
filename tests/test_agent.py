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
    "eval": {"verify": False},   # 自動判定は専用のテストで確かめる（呼び出しの数を変えないように）
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
    front = {"w": winapi.WindowInfo(1, "無題 - メモ帳", "notepad.exe"), "rect": None}
    monkeypatch.setattr(winapi, "foreground", lambda: front["w"])
    monkeypatch.setattr(winapi, "window_rect", lambda hwnd: front["rect"])   # None = 画面全体で比べる
    monkeypatch.setattr(winapi, "find_window", lambda procs: None)
    acted = []
    for name in ("moveTo", "click", "doubleClick", "hotkey", "scroll"):
        monkeypatch.setattr(executor.pyautogui, name, lambda *a, _n=name, **k: acted.append(_n))
    monkeypatch.setattr(executor.pyautogui, "mouseDown", lambda *a, **k: acted.append("click"))
    monkeypatch.setattr(executor.pyautogui, "mouseUp", lambda *a, **k: None)
    monkeypatch.setattr(executor, "paste_text", lambda text: acted.append("paste"))
    # アプリの起動・前面化・最大化も記録だけにする（テストで実際のアプリが開かないように）
    monkeypatch.setattr(executor, "launch", lambda exe: acted.append(f"launch:{exe}") or "CreateProcess")
    monkeypatch.setattr(executor, "LAUNCH_WAIT", 0.1)
    monkeypatch.setattr(winapi, "activate", lambda hwnd: acted.append("activate") or True)
    monkeypatch.setattr(winapi, "maximize", lambda hwnd: acted.append("maximize") or True)
    clicked_win = {"w": None}
    monkeypatch.setattr(winapi, "root_window_at", lambda x, y: clicked_win["w"])
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path)
    # UI Automation と FAILSAFE の確認も偽物に（テスト中の実際のマウス位置に左右されないように）
    el = {"clicked": None, "focused": None}
    monkeypatch.setattr(uia, "element_at", lambda x, y: el["clicked"])
    monkeypatch.setattr(uia, "focused_element", lambda: el["focused"])
    # UI Automation で直接押す処理も記録だけ（実際の画面のボタンを押さないように）
    monkeypatch.setattr(uia, "invoke_at", lambda x, y: acted.append("invoke") or "Hyperlink「リンク」")
    el["owner"] = None   # ボタンの中のアイコンをクリックしたときの、押されるボタン（既定はなし）
    monkeypatch.setattr(uia, "actionable_at", lambda x, y: el["owner"])
    el["marks"] = []     # 番号で選ばせる部品（既定はなし）
    monkeypatch.setattr(agent_mod.Agent, "_marks", lambda self, front, size: list(el["marks"]))
    monkeypatch.setattr(agent_mod.Agent, "failsafe_check", lambda self: None)
    return {"frames": frames, "acted": acted, "tmp": tmp_path, "el": el, "front": front, "clicked_win": clicked_win}


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
        def check_safety(self, a, front, point, mark=None):
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


# ---- B: 前面ウィンドウの中だけで変化を見る / C: 確かめられない「完了」の警告 ----

def with_monitor_noise(i):
    """前面ウィンドウ（左上）は同じまま、右側の監視ツールの数字だけが毎回変わる画面。"""
    from PIL import ImageDraw
    im = solid((0, 0, 0))
    ImageDraw.Draw(im).rectangle([1500, 100, 1500 + 40 * (i % 5 + 1), 400], fill=(255, 255, 255))
    return im


def rows_of(env, s):
    d = env["tmp"] / s["dir"].replace("\\", "/").split("/")[-1]
    return d, [json.loads(l) for l in (d / "ledger.jsonl").read_text(encoding="utf-8").splitlines()]


def test_noise_outside_window_counts_as_no_change(env):
    env["front"]["rect"] = (0, 0, 1000, 800)
    env["frames"] += [with_monitor_noise(i) for i in range(10)]
    ag, client = make([J(action="type", text="テスト", target_app="メモ帳"),
                       J(action="key", text="ctrl+a", target_app="メモ帳"),
                       J(action="key", text="delete", target_app="メモ帳"),
                       J(action="done", reason="削除完了")], dry_run=False)
    s = ag.run("テスト")
    assert "did NOT change" in client.calls[1]["prompt"]
    assert client.calls[2]["thinking"] == "on"            # 変化なしが2回続いたので思考オン
    # C: 完了と言っても、効かなかった入力・キーがあるので警告（結果は done のまま）
    assert s["result"] == "done" and len(s["warnings"]) == 1
    assert "入力「テスト」" in s["warnings"][0] and "ctrl+a" in s["warnings"][0]
    d, rows = rows_of(env, s)
    assert rows[1]["compared"] == "前面ウィンドウ内"
    html = (d / "report.html").read_text(encoding="utf-8")
    assert "完了（要確認）" in html and "要確認" in html


def test_same_noise_without_rect_counts_as_change(env):
    # 矩形が取れないときは従来どおり画面全体（数字の変化も「変わった」になる）
    env["frames"] += [with_monitor_noise(i) for i in range(10)]
    ag, client = make([J(action="type", text="テスト", target_app="メモ帳"), J(action="done")], dry_run=False)
    s = ag.run("テスト")
    assert "[screen changed]" in client.calls[1]["prompt"] and s["warnings"] == []


def test_front_window_switch_counts_as_change(env):
    env["front"]["rect"] = (0, 0, 1000, 800)
    calc = winapi.WindowInfo(2, "電卓", "CalculatorApp.exe")

    class SwitchOnAct(QuietHooks):
        def status(self, state, text):
            if state == "acting":
                env["front"]["w"] = calc
    client = FakeClient([J(action="open_app", text="メモ帳"), J(action="done")])
    ag = agent_mod.Agent(CFG, hooks=SwitchOnAct(), dry_run=False, client=client)
    s = ag.run("テスト")   # 画面の画像は同じでも、前面が切り替わったら「変わった」
    assert "[screen changed]" in client.calls[1]["prompt"]
    assert env["acted"][0] == "launch:notepad.exe"      # 実際には起動していない（記録だけ）
    _, rows = rows_of(env, s)
    assert rows[1]["compared"] == "前面ウィンドウが切り替わった" and s["warnings"] == []


def test_effective_run_has_no_warning(env):
    env["front"]["rect"] = (0, 0, 1000, 800)
    env["frames"] += [solid((255 * (i % 2),) * 3) for i in range(6)]   # ウィンドウ内も毎回変わる（白黒交互）
    ag, _ = make([J(action="type", text="テスト", target_app="メモ帳"), J(action="done")], dry_run=False)
    assert ag.run("テスト")["warnings"] == []


def test_dry_run_never_warns(env):
    ag, _ = make([J(action="type", text="テスト", target_app="メモ帳"), J(action="done")])
    assert ag.run("テスト")["warnings"] == []


@pytest.mark.parametrize("history,expect", [
    ([], ""),
    ([{"action": "click", "changed": False, "label": "クリックA"},
      {"action": "click", "changed": True, "label": "クリックB"}], ""),            # 試行錯誤のクリックは問わない
    ([{"action": "click", "changed": True, "label": "クリックA"},
      {"action": "click", "changed": False, "label": "クリックB"}], "クリックB"),   # 最後の操作が効いていない
    ([{"action": "type", "changed": False, "label": "入力X"},
      {"action": "click", "changed": True, "label": "クリック"}], "入力X"),         # 効かなかった入力
    ([{"action": "type", "changed": None, "label": "入力X"},                         # ブロックされた行は対象外
      {"label": "無効な返答", "changed": None}], ""),
])
def test_unverified_done(history, expect):
    w = agent_mod.unverified_done(history)
    assert (expect in w and w) if expect else w == ""


# ---- 行き詰まりの打ち切り・思考の上限・停止時の接続切断 ----

def test_gives_up_after_repeated_blocks(env):
    same_click = J(action="click", x=100, y=200, reason="押す")
    ag, client = make([same_click] * 20, dry_run=False)
    s = ag.run("テスト")
    assert s["result"] == "fail" and "行き詰まった" in s["reason"]
    assert env["acted"].count("click") == 2          # 効かなかった2回だけ実行
    assert len(client.calls) == 2 + 4                # 2回実行 + ブロック4回で打ち切り（それ以上は聞かない）


def test_give_up_can_be_disabled(env):
    cfg = {**CFG, "agent": {**CFG["agent"], "give_up_after_blocks": 0}}
    client = FakeClient([J(action="click", x=100, y=200)] * 7 + [J(action="done")])
    ag = agent_mod.Agent(cfg, hooks=QuietHooks(), dry_run=False, client=client)
    assert ag.run("テスト")["result"] == "done"


def test_thinking_cut_by_length_falls_back_to_off(env):
    class LengthWhenThinking(FakeClient):
        def chat(self, messages, image=None, thinking="off", **kw):
            if thinking != "off":
                self.calls.append({"thinking": thinking, "prompt": messages[-1]["content"], "image_size": image.size})
                return {"content": "", "reasoning": "とても長い思考…", "finish_reason": "length",
                        "usage": {"completion_tokens": 3000}, "secs": 60.0, "thinking": thinking}
            return super().chat(messages, image=image, thinking=thinking, **kw)
    client = LengthWhenThinking(["壊れた", "{壊れた", J(action="done", reason="完了")])
    ag = agent_mod.Agent(CFG, hooks=QuietHooks(), dry_run=True, client=client)
    s = ag.run("テスト")
    assert s["result"] == "done"
    assert [c["thinking"] for c in client.calls] == ["off", "off", "on", "off"]   # 思考が切れたら思考なしで聞き直す
    d = env["tmp"] / s["dir"].replace("\\", "/").split("/")[-1]
    rows = [json.loads(l) for l in (d / "ledger.jsonl").read_text(encoding="utf-8").splitlines()]
    assert "思考なしで聞き直した" in rows[-1]["thinking_cut"] and rows[-1]["model_secs"] >= 60


def test_stop_cancels_model_call(env):
    import threading
    import time

    class Slow(FakeClient):
        def __init__(self):
            super().__init__([])
            self.cancelled = threading.Event()

        def chat(self, *a, **k):
            self.cancelled.wait(5)
            raise RuntimeError("切断された")

        def cancel(self):
            self.cancelled.set()
    client = Slow()
    ag = agent_mod.Agent(CFG, hooks=QuietHooks(), dry_run=False, client=client)
    threading.Timer(0.3, ag.stop).start()
    t = time.time()
    s = ag.run("テスト")
    assert s["result"] == "stopped" and client.cancelled.is_set() and time.time() - t < 2



# ---- クリック後に前面を奪われたら戻す / 間をはさんだ繰り返し ----

EDGE = winapi.WindowInfo(7, "Yahoo - Microsoft Edge", "msedge.exe")
CLAUDE = winapi.WindowInfo(8, "Claude", "claude.exe")
SAFETY_CFG = {**CFG, "safety": {"protected_processes": ["claude.exe", "explorer.exe", "WindowsTerminal.exe"]}}


def run_click(env, new_front):
    env["front"]["w"] = EDGE
    env["clicked_win"]["w"] = EDGE

    class StealOnAct(QuietHooks):
        def status(self, state, text):
            if state == "acting":
                env["front"]["w"] = new_front
    env["frames"] += [solid((255 * (i % 2),) * 3) for i in range(6)]
    client = FakeClient([J(action="click", x=100, y=200), J(action="done")])
    ag = agent_mod.Agent(SAFETY_CFG, hooks=StealOnAct(), dry_run=False, client=client)
    s = ag.run("テスト")
    _, rows = rows_of(env, s)
    return client, rows


def test_focus_stolen_by_own_environment_is_restored(env):
    client, rows = run_click(env, CLAUDE)
    assert env["acted"] == ["moveTo", "click", "activate"]
    assert any("前面に戻した" in n for n in rows[0]["notes"])
    assert "focus was stolen" in client.calls[1]["prompt"]


def test_switch_to_other_app_is_left_alone(env):
    calc = winapi.WindowInfo(9, "電卓", "CalculatorApp.exe")   # 保護対象でないアプリへの切り替わりは意図したものかもしれない
    client, rows = run_click(env, calc)
    assert env["acted"] == ["moveTo", "click"] and not rows[0]["notes"]


def test_interleaved_repeat_is_blocked(env):
    env["frames"] += [solid((255 * (i % 2),) * 3) for i in range(30)]   # 毎回画面は変わる
    click = J(action="click", x=500, y=60, reason="アドレスバー")
    opn = J(action="open_app", text="メモ帳", reason="前面へ")
    ag, client = make([click, opn, click, opn, click, opn, click, J(action="done")], dry_run=False)
    s = ag.run("テスト")
    assert env["acted"].count("click") == 3                      # 4回目のクリックはブロック
    assert "already done 3 times" in client.calls[-1]["prompt"]


def test_ctrl_c_still_writes_report(env):
    class CtrlC(QuietHooks):
        def status(self, state, text):
            if state == "acting":
                raise KeyboardInterrupt
    client = FakeClient([J(action="click", x=1, y=1)])
    ag = agent_mod.Agent(CFG, hooks=CtrlC(), dry_run=False, client=client)
    with pytest.raises(KeyboardInterrupt):
        ag.run("テスト")
    d = next(p for p in env["tmp"].iterdir() if p.is_dir())
    s = json.loads((d / "summary.json").read_text(encoding="utf-8"))
    assert s["result"] == "stopped" and "Ctrl+C" in s["reason"] and (d / "report.html").exists()



# ---- 実行直前の前面の確認 / クリックが効かないときの直接押し ----

def test_front_changed_while_thinking_is_not_executed(env):
    term = winapi.WindowInfo(5, "コマンド プロンプト", "WindowsTerminal.exe")

    class UserTouchesTerminal(FakeClient):
        def chat(self, messages, **kw):
            r = super().chat(messages, **kw)
            if len(self.calls) == 1:
                env["front"]["w"] = term          # 考えている間に人が端末をクリックした
            return r
    client = UserTouchesTerminal([J(action="key", text="ctrl+l", target_app="メモ帳"), J(action="done")])
    ag = agent_mod.Agent(CFG, hooks=QuietHooks(), dry_run=False, client=client)
    s = ag.run("テスト")
    assert "hotkey" not in env["acted"]           # 端末に Ctrl+L を送らない
    assert "NOT EXECUTED" in client.calls[1]["prompt"] and "WindowsTerminal.exe" in client.calls[1]["prompt"]
    _, rows = rows_of(env, s)
    assert rows[0]["guard"] == "front_changed"


def test_click_on_link_without_effect_is_invoked(env):
    from voice_agent.uia import Element
    env["el"]["clicked"] = Element("三井情報（MKI）", "HyperlinkControl", False)
    ag, client = make([J(action="click", x=190, y=261, reason="リンクを開く"), J(action="done")], dry_run=False)
    s = ag.run("テスト")                          # 画面は変わらない（偽の画面は同じ画像のまま）
    assert env["acted"] == ["moveTo", "click", "invoke"]
    _, rows = rows_of(env, s)
    assert any("直接押した" in n for n in rows[0]["notes"])
    assert "activated directly" in client.calls[1]["prompt"]


def test_click_with_effect_is_not_invoked(env):
    from voice_agent.uia import Element
    env["el"]["clicked"] = Element("三井情報（MKI）", "HyperlinkControl", False)
    env["frames"] += [solid((0, 0, 0)), solid((255, 255, 255))]   # クリックで画面が変わった
    ag, _ = make([J(action="click", x=190, y=261), J(action="done")], dry_run=False)
    ag.run("テスト")
    assert "invoke" not in env["acted"]


def test_click_on_non_invokable_is_not_invoked(env):
    from voice_agent.uia import Element
    env["el"]["clicked"] = Element("", "PaneControl", False)
    ag, _ = make([J(action="click", x=190, y=261), J(action="done")], dry_run=False)
    ag.run("テスト")
    assert "invoke" not in env["acted"]


# ---- Edge のリンクは直接押す / ページの題名でリンクの効果を判定 ----

def edge_link_env(env, title_after=None):
    from voice_agent.uia import Element
    edge = winapi.WindowInfo(7, "三井情報（MKI） - Microsoft Edge", "msedge.exe")
    env["front"]["w"] = edge
    env["el"]["clicked"] = Element(" 資料ダウンロード", "HyperlinkControl", False)

    class ChangeTitleOnAct(QuietHooks):
        def status(self, state, text):
            if state == "acting" and title_after:
                env["front"]["w"] = winapi.WindowInfo(7, title_after, "msedge.exe")
    return ChangeTitleOnAct()


def test_edge_link_is_pressed_directly_once(env):
    hooks = edge_link_env(env, title_after="資料ダウンロード | 三井情報 - Microsoft Edge")
    client = FakeClient([J(action="click", x=1259, y=101, target_app="Edge"), J(action="done")])
    ag = agent_mod.Agent(CFG, hooks=hooks, dry_run=False, client=client)
    s = ag.run("テスト")
    assert env["acted"] == ["moveTo", "invoke"]          # マウスのクリックはせず、1回だけ直接押す
    _, rows = rows_of(env, s)
    assert "直接押した" in rows[0]["detail"] and not rows[0]["notes"]


def test_link_with_same_title_counts_as_no_effect(env):
    hooks = edge_link_env(env, title_after=None)        # 題名が変わらない（ページが開いていない）
    env["frames"] += [solid((255 * (i % 2),) * 3) for i in range(6)]   # 動く画像で画面は毎回変わる
    client = FakeClient([J(action="click", x=1259, y=101), J(action="done", reason="押した")])
    ag = agent_mod.Agent(CFG, hooks=hooks, dry_run=False, client=client)
    s = ag.run("テスト")
    assert "did NOT change" in client.calls[1]["prompt"] and "the page did not change" in client.calls[1]["prompt"]
    assert s["warnings"] and "画面が変わらなかった操作" in s["warnings"][0]   # 「完了」に要確認の警告


# ---- ボタンの中のアイコン / 待つ / ログインの確認（ChatGPT の実地テストから）----

def test_icon_inside_button_is_pressed_when_click_has_no_effect(env):
    from voice_agent.uia import Element
    env["el"]["clicked"] = Element("", "ImageControl", False)          # 送信ボタンの中の矢印の画像
    env["el"]["owner"] = Element("プロンプトを送信する", "ButtonControl", False)

    class Yes(QuietHooks):
        def confirm(self, q):
            return True
    client = FakeClient([J(action="click", x=1172, y=453, reason="質問を送る"), J(action="done")])
    ag = agent_mod.Agent(SAFETY_CFG, hooks=Yes(), dry_run=False, client=client)
    ag.run("テスト")                                                   # 画面は変わらない
    assert env["acted"] == ["moveTo", "click", "invoke"]


def test_button_name_of_icon_is_checked_by_safety(env):
    from voice_agent.uia import Element
    env["el"]["clicked"] = Element("", "ImageControl", False)
    env["el"]["owner"] = Element("送信", "ButtonControl", False)        # アイコンの名前は空でも、ボタンは「送信」
    cfg = {**CFG, "safety": {"risky_words": ["送信"]}}
    client = FakeClient([J(action="click", x=10, y=10, reason="押す")])
    ag = agent_mod.Agent(cfg, hooks=QuietHooks(), dry_run=False, client=client)   # 確認は既定で「いいえ」
    s = ag.run("テスト")
    assert s["result"] == "stopped" and env["acted"] == []


def test_wait_action(env):
    import threading
    waited = []

    class RecordingEvent(threading.Event):
        def wait(self, timeout=None):
            if timeout and timeout >= 1:              # wait 操作の待ち（短いポーリングはそのまま）
                waited.append(timeout)
                return False
            return super().wait(timeout)
    ag, client = make([J(action="wait", seconds=3, reason="回答を待つ"), J(action="done")], dry_run=False)
    ag.stop_event = RecordingEvent()
    s = ag.run("テスト")
    assert s["result"] == "done" and 3.0 in waited and env["acted"] == []
    assert "待つ 3 秒" in client.calls[1]["prompt"]



# ---- 番号で選ばせる（Set-of-Mark）----

def mk(i, name, ct, rect):
    from voice_agent.uia import Mark
    return Mark(i, name, ct, rect)


def test_element_number_is_pressed_directly(env):
    env["front"]["w"] = winapi.WindowInfo(7, "三井情報 - 検索 - Microsoft Edge", "msedge.exe")
    env["el"]["marks"] = [mk(1, "戻る", "ButtonControl", (0, 40, 40, 80)),
                          mk(2, "三井情報（MKI）", "HyperlinkControl", (160, 310, 330, 350))]
    ag, client = make([J(action="click", element=2, reason="リンクを開く"), J(action="done")], dry_run=False)
    s = ag.run("テスト")
    assert env["acted"] == ["moveTo", "invoke"]                          # マウスでクリックせず、直接押す
    assert "[2] Hyperlink「三井情報（MKI）」" in client.calls[0]["prompt"]   # 一覧をモデルに渡している
    _, rows = rows_of(env, s)
    assert rows[0]["label"].startswith("クリック #2「三井情報（MKI）」")
    assert rows[0]["point"] == [245, 330]                                # 部品の中心


def test_element_number_for_edit_field_is_clicked(env):
    env["el"]["marks"] = [mk(1, "検索", "EditControl", (100, 100, 500, 130))]
    ag, _ = make([J(action="click", element=1), J(action="done")], dry_run=False)
    ag.run("テスト")
    assert env["acted"] == ["moveTo", "click"]                           # 入力欄はマウスでクリックしてフォーカス


def test_unknown_element_number_is_invalid(env):
    env["el"]["marks"] = [mk(1, "戻る", "ButtonControl", (0, 40, 40, 80))]
    ag, client = make([J(action="click", element=9), J(action="done")], dry_run=False)
    ag.run("テスト")
    assert env["acted"] == [] and "element 9" in client.calls[1]["prompt"]


def test_element_name_is_checked_by_safety(env):
    env["el"]["marks"] = [mk(1, "Nobuyuki として続行, someone@example.com", "ButtonControl", (0, 0, 300, 40))]
    ag, _ = make([J(action="click", element=1, reason="閉じる")], dry_run=False)
    s = ag.run("テスト", )
    assert s["result"] == "stopped" and env["acted"] == []               # 確認で「いいえ」（既定）→ 押さない


def test_marks_drawn_and_prompt_without_marks(env):
    from PIL import Image
    from voice_agent import screen as scr
    img = scr.draw_marks(Image.new("RGB", (1512, 850), (255, 255, 255)),
                         [mk(12, "送信", "ButtonControl", (1200, 560, 1260, 600))], (1920, 1080))
    assert img.getpixel((int(1200 * 1512 / 1920) + 1, int(580 * 850 / 1080))) != (255, 255, 255)
    ag, client = make([J(action="done")])
    ag.run("テスト")
    assert "ELEMENTS (numbered on the screenshot): none" in client.calls[0]["prompt"]



# ---- 測定: バージョン・自動判定・人の判定 ----

def test_done_is_verified_from_final_screen(env):
    class Verifying(FakeClient):
        def chat(self, messages, image=None, thinking="off", **kw):
            if "You check whether a task" in messages[0]["content"]:
                self.calls.append({"verify": True, "prompt": messages[-1]["content"]})
                return {"content": J(achieved="no", evidence="404 のページ")}
            return super().chat(messages, image=image, thinking=thinking, **kw)
    cfg = {**CFG, "eval": {"verify": True}}
    client = Verifying([J(action="done", reason="ページが表示された")])
    ag = agent_mod.Agent(cfg, hooks=QuietHooks(), dry_run=False, client=client)
    s = ag.run("三井情報のページを開いて")
    assert client.calls[-1].get("verify") and "AGENT'S CLAIM: ページが表示された" in client.calls[-1]["prompt"]
    assert s["verify"]["achieved"] == "no" and s["warnings"]                  # 「達成されていない」を警告に
    d, _ = rows_of(env, s)
    saved = json.loads((d / "summary.json").read_text(encoding="utf-8"))
    assert saved["verify"]["achieved"] == "no" and "version" in saved
    assert "自動判定: 未達成" in (d / "report.html").read_text(encoding="utf-8")


def test_human_label_is_saved_and_shown(env):
    from voice_agent import evaluate
    ag, _ = make([J(action="done")], dry_run=False)
    s = ag.run("テスト")
    evaluate.set_human_label(s["dir"], "failure", "test")
    d = env["tmp"] / s["dir"].replace("\\", "/").split("/")[-1]
    saved = json.loads((d / "summary.json").read_text(encoding="utf-8"))
    assert saved["human_label"] == "failure" and evaluate.outcome(saved) == "failure"
    assert "人の判定: 失敗" in (d / "report.html").read_text(encoding="utf-8")


@pytest.mark.parametrize("summary,expect", [
    ({"result": "done"}, "unknown"),                                             # 自己申告だけでは成功にしない
    ({"result": "done", "verify": {"achieved": "yes"}}, "success"),
    ({"result": "done", "verify": {"achieved": "no"}}, "failure"),
    ({"result": "done", "verify": {"achieved": "yes"}, "human_label": "failure"}, "failure"),   # 人の判定が優先
    ({"result": "stopped"}, "failure"),
    ({"result": "fail", "human_label": "success"}, "success"),
])
def test_outcome(summary, expect):
    from voice_agent import evaluate
    assert evaluate.outcome(summary) == expect


# ---- 読み上げ（say）----

def test_say_reads_aloud_and_is_passed_to_verify(env):
    class SpeakHooks(QuietHooks):
        def __init__(self):
            super().__init__()
            self.said, self.waited = [], []

        def say(self, text, kind="info"):
            self.said.append((text, kind))

        def wait_speech(self, timeout):
            self.waited.append(timeout)

    class Verifying(FakeClient):
        def chat(self, messages, image=None, thinking="off", **kw):
            if "You check whether a task" in messages[0]["content"]:
                self.verify_prompt = messages[-1]["content"]
                return {"content": J(achieved="yes", evidence="晴れ 17℃")}
            return super().chat(messages, image=image, thinking=thinking, **kw)
    hooks = SpeakHooks()
    client = Verifying([J(action="say", text="表参道は晴れ、17度です。", reason="答えを読み上げる"), J(action="done")])
    ag = agent_mod.Agent({**CFG, "eval": {"verify": True}}, hooks=hooks, dry_run=False, client=client)
    s = ag.run("ChatGPT の答えを読み上げて")
    assert ("表参道は晴れ、17度です。", "answer") in hooks.said and hooks.waited
    assert not any(k == "reason" and t == "答えを読み上げる" for t, k in hooks.said)   # 理由は読まずに答えだけ
    assert env["acted"] == [] and s["result"] == "done"
    assert "AGENT SAID ALOUD: 表参道は晴れ、17度です。" in client.verify_prompt
    assert "did NOT change" not in client.calls[1]["prompt"]                          # 読み上げは「効果なし」に数えない


def test_agent_stop_cancels_model_call_immediately(env):
    import threading

    class Cancellable(FakeClient):
        def __init__(self):
            super().__init__([])
            self.cancelled = threading.Event()

        def cancel(self):
            self.cancelled.set()
    client = Cancellable()
    ag = agent_mod.Agent(CFG, hooks=QuietHooks(), client=client)
    ag.stop()                                         # 待機中でも、補正・自動判定の呼び出しを切れるように
    assert client.cancelled.wait(1) and ag.stop_event.is_set()
