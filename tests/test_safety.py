"""安全ルールのテスト（ルールごと）。"""
import json

import pytest

from voice_agent import actions, config, safety
from voice_agent.stopkey import StopKey
from voice_agent.uia import Element
from voice_agent.winapi import WindowInfo

CFG = config.load()   # 実際の config.yaml の安全ルールでテストする
APPS = actions.load_apps(CFG)
POLICY = safety.SafetyPolicy(CFG, APPS)

NOTEPAD = WindowInfo(1, "無題 - メモ帳", "Notepad.exe")
EDGE = WindowInfo(2, "Bing - Microsoft Edge", "msedge.exe")
CALC = WindowInfo(3, "電卓", "CalculatorApp.exe")
TERMINAL = WindowInfo(4, "コマンド プロンプト", "WindowsTerminal.exe")
CLAUDE = WindowInfo(5, "Claude", "claude.exe")
FIELD = Element("本文", "EditControl", False)
PW_FIELD = Element("", "EditControl", True)


def A(**k):
    a = actions.normalize(k)
    assert actions.validate(a, (1512, 850), APPS) is None, actions.validate(a, (1512, 850), APPS)
    return a


def check(a, front=NOTEPAD, focused=FIELD, clicked=None):
    return POLICY.check(a, front, focused=focused, clicked=clicked)


# ==== ルール1: 送信・削除・購入・提出・投稿 / risky=true は確認 ====

@pytest.mark.parametrize("a,clicked", [
    (A(action="click", x=10, y=10, risky=True, reason="ボタンを押す"), None),
    (A(action="click", x=10, y=10, reason="メールを送信する"), None),
    (A(action="click", x=10, y=10, reason="ファイルを削除"), None),
    (A(action="click", x=10, y=10, reason="カートの商品を購入"), None),
    (A(action="click", x=10, y=10, reason="レポートを提出"), None),
    (A(action="click", x=10, y=10, reason="コメントを投稿"), None),
    (A(action="click", x=10, y=10, reason="Click the Submit button"), None),
    (A(action="click", x=10, y=10, reason="ボタンを押す"), Element("送信", "ButtonControl", False)),
    (A(action="click", x=10, y=10, reason="ボタンを押す"), Element("Delete", "ButtonControl", False)),
    (A(action="click", x=10, y=10, reason="次へ"), Element("今すぐ購入", "ButtonControl", False)),
    (A(action="double_click", x=10, y=10, reason="開く"), Element("ゴミ箱を空にする", "MenuItemControl", False)),
])
def test_risky_click_needs_confirm(a, clicked):
    assert check(a, clicked=clicked).kind == safety.CONFIRM


@pytest.mark.parametrize("combo", ["delete", "shift+delete", "ctrl+enter", "alt+s", "Ctrl + D", "alt+f4"])
def test_risky_keys_need_confirm(combo):
    assert check(A(action="key", text=combo, target_app="メモ帳", reason="次へ")).kind == safety.CONFIRM


def test_risky_typed_text_needs_confirm():
    assert check(A(action="type", text="この内容で送信します", target_app="メモ帳", reason="入力")).kind == safety.CONFIRM


@pytest.mark.parametrize("a,clicked", [
    (A(action="click", x=10, y=10, reason="ファイルメニューを開く"), Element("ファイル", "MenuItemControl", False)),
    (A(action="click", x=10, y=10, reason="アドレスバーを選ぶ"), None),
    (A(action="type", text="12*34=", target_app="電卓", reason="数式を入力"), None),
    (A(action="key", text="enter", target_app="Edge", reason="検索を実行"), None),
    (A(action="scroll", dy=-3, reason="下へ"), None),
    (A(action="open_app", text="メモ帳", reason="メモ帳を開く"), None),
    (A(action="click", x=10, y=10, reason="posting を見る"), None),          # 英語は単語単位（post ≠ posting）
    (A(action="click", x=10, y=10, reason="ボタンを押す"), Element("Repost", "ButtonControl", False)),
])
def test_harmless_is_allowed(a, clicked):
    front = {"電卓": CALC, "Edge": EDGE}.get(a.get("target_app"), NOTEPAD)
    assert check(a, front=front, clicked=clicked).kind == safety.ALLOW


def test_open_app_and_scroll_ignore_future_steps_in_reason():
    # reason に先の手が書かれていても、アプリを開く・スクロール自体は確認しない
    assert check(A(action="open_app", text="メモ帳", reason="メモ帳を開いて内容を削除する")).kind == safety.ALLOW
    assert check(A(action="scroll", dy=-3, reason="送信ボタンまで下へ")).kind == safety.ALLOW
    # ただし risky=true の申告は確認する
    assert check(A(action="open_app", text="メモ帳", risky=True, reason="開く")).kind == safety.CONFIRM


def test_done_is_never_confirmed():
    assert check(A(action="done", reason="メールを送信しました")).kind == safety.ALLOW


# ==== ルール2: パスワード入力は拒否 ====

def test_type_into_password_field_stops():
    d = check(A(action="type", text="hunter2", target_app="Edge", reason="入力する"), front=EDGE, focused=PW_FIELD)
    assert d.kind == safety.STOP and "パスワード" in d.reason


def test_type_with_password_reason_stops():
    d = check(A(action="type", text="abc", target_app="Edge", reason="パスワードを入力"), front=EDGE)
    assert d.kind == safety.STOP


def test_type_when_focused_name_is_password_stops():
    d = check(A(action="type", text="abc", target_app="Edge", reason="入力"), front=EDGE,
              focused=Element("Password", "EditControl", False))
    assert d.kind == safety.STOP


def test_keys_in_password_field_stop_except_leaving():
    assert check(A(action="key", text="a", target_app="Edge"), front=EDGE, focused=PW_FIELD).kind == safety.STOP
    assert check(A(action="key", text="ctrl+v", target_app="Edge"), front=EDGE, focused=PW_FIELD).kind == safety.STOP
    assert check(A(action="key", text="tab", target_app="Edge"), front=EDGE, focused=PW_FIELD).kind == safety.ALLOW


def test_clicking_password_field_is_allowed():
    # クリックしてフォーカスを移すだけなら許す（その後の入力は拒否される）
    assert check(A(action="click", x=10, y=10, reason="欄を選ぶ"), clicked=PW_FIELD).kind == safety.ALLOW


# ==== ルール3: キー入力の前に前面アプリと target_app を照合 ====

@pytest.mark.parametrize("target,front", [
    ("メモ帳", NOTEPAD), ("notepad", NOTEPAD), ("Notepad.exe", NOTEPAD),
    ("電卓", CALC), ("CalculatorApp.exe", CALC), ("Microsoft Edge", EDGE), ("msedge", EDGE),
])
def test_target_app_matches(target, front):
    assert check(A(action="type", text="abc", target_app=target, reason="入力"), front=front).kind == safety.ALLOW


@pytest.mark.parametrize("target,front", [("メモ帳", EDGE), ("電卓", NOTEPAD), ("Edge", CALC), ("Chrome", EDGE)])
def test_target_app_mismatch_blocks(target, front):
    d = check(A(action="type", text="abc", target_app=target, reason="入力"), front=front)
    assert d.kind == safety.BLOCK and "前面" in d.reason


def test_title_is_not_trusted():
    # Edge のページタイトルに「メモ帳」と出ていても、プロセスは msedge.exe なので一致しない
    fake = WindowInfo(9, "メモ帳 - 検索 - Microsoft Edge", "msedge.exe")
    assert check(A(action="type", text="abc", target_app="メモ帳", reason="入力"), front=fake).kind == safety.BLOCK


@pytest.mark.parametrize("front", [TERMINAL, CLAUDE, WindowInfo(6, "cmd", "cmd.exe"),
                                   WindowInfo(7, "検索", "SearchHost.exe")])
def test_protected_front_blocks_even_if_target_matches(front):
    d = check(A(action="key", text="enter", target_app=front.exe, reason="実行"), front=front)
    assert d.kind == safety.BLOCK and "保護対象" in d.reason


def test_unknown_front_blocks():
    assert check(A(action="type", text="a", target_app="メモ帳", reason="入力"), front=None).kind == safety.BLOCK


@pytest.mark.parametrize("combo", ["win", "win+r", "Windows+X", "ctrl+shift+esc", "ctrl+esc"])
def test_blocked_keys(combo):
    d = check(A(action="key", text=combo, target_app="メモ帳", reason="開く"))
    assert d.kind == safety.BLOCK


def test_clicks_do_not_need_front_match():
    assert check(A(action="click", x=10, y=10, reason="選ぶ"), front=TERMINAL).kind == safety.ALLOW


# ==== ルール4: 25ステップで強制停止（test_agent.py にもあり）====

def test_config_cap():
    assert CFG["agent"]["max_steps"] <= 25


# ==== ルール5: Esc で即時停止（人が押したときだけ）====

def test_stop_key_human_esc_stops():
    from pynput import keyboard
    hits = []
    sk = StopKey(lambda: hits.append(1), "esc")
    sk.on_press(keyboard.Key.esc, False)
    assert hits == [1]


def test_stop_key_ignores_injected_esc():
    from pynput import keyboard
    hits = []
    sk = StopKey(lambda: hits.append(1), "esc")
    sk.on_press(keyboard.Key.esc, True)      # pyautogui が送った Esc
    sk.on_press(keyboard.Key.enter, False)   # 別のキー
    sk.on_press(keyboard.KeyCode.from_char("e"), False)
    assert hits == []


def test_failsafe_enabled():
    from voice_agent import executor
    assert executor.pyautogui.FAILSAFE is True


# ==== ループに組み込んだときの動作 ====

from tests.test_agent import FakeClient, QuietHooks, env, make, CFG as AGENT_CFG  # noqa: E402,F401
from voice_agent import agent as agent_mod  # noqa: E402


def J(**k):
    return json.dumps(k, ensure_ascii=False)


class ConfirmHooks(QuietHooks):
    def __init__(self, answer):
        super().__init__()
        self.answer, self.questions = answer, []

    def confirm(self, question):
        self.questions.append(question)
        return self.answer


def run_with(replies, answer, env, dry_run=False):
    cfg = {**AGENT_CFG, "safety": CFG["safety"]}
    hooks = ConfirmHooks(answer)
    client = FakeClient(replies)
    ag = agent_mod.Agent(cfg, hooks=hooks, dry_run=dry_run, client=client)
    return ag.run("テスト"), hooks, client


def test_loop_confirm_declined_stops_without_acting(env):
    s, hooks, _ = run_with([J(action="click", x=10, y=10, reason="メールを送信")], False, env)
    assert s["result"] == "stopped" and env["acted"] == [] and len(hooks.questions) == 1
    assert "送信" in hooks.questions[0]


def test_loop_confirm_accepted_acts(env):
    s, hooks, _ = run_with([J(action="click", x=10, y=10, risky=True, reason="投稿"), J(action="done")], True, env)
    assert s["result"] == "done" and "click" in env["acted"] and len(hooks.questions) == 1


def test_loop_dry_run_does_not_ask(env):
    s, hooks, _ = run_with([J(action="click", x=10, y=10, reason="削除する"), J(action="done")], False, env,
                           dry_run=True)
    assert s["result"] == "done" and hooks.questions == [] and env["acted"] == []


def test_loop_password_stops(env):
    env["el"]["focused"] = PW_FIELD
    s, _, _ = run_with([J(action="type", text="secret", target_app="メモ帳", reason="入力")], True, env)
    assert s["result"] == "stopped" and "パスワード" in s["reason"] and env["acted"] == []


def test_loop_wrong_front_blocks_then_continues(env):
    s, _, client = run_with([J(action="type", text="abc", target_app="Edge", reason="入力"), J(action="done")],
                            True, env)
    assert s["result"] == "done" and env["acted"] == []
    assert "open_app" in client.calls[1]["prompt"]   # モデルに「先に open_app」と伝わる


def test_loop_clicked_element_triggers_confirm(env):
    env["el"]["clicked"] = Element("送信", "ButtonControl", False)
    s, hooks, _ = run_with([J(action="click", x=10, y=10, reason="ボタンを押す")], False, env)
    assert s["result"] == "stopped" and "クリック先の要素" in hooks.questions[0]


# ---- ログイン・アカウントを選ぶ操作は確認（ChatGPT の実地テストで、確認なしに押していた）----

@pytest.mark.parametrize("name", ["Nobuyuki として続行, someone@example.com", "Google でログイン", "Sign in",
                                  "Continue as Taro", "同意する", "taro.yamada+test@example.co.jp"])
def test_sign_in_and_account_buttons_need_confirm(name):
    d = check(A(action="click", x=10, y=10, reason="ボタンを押す"), clicked=Element(name, "ButtonControl", False))
    assert d.kind == safety.CONFIRM, (name, d)


def test_wait_is_allowed_even_with_risky_words_in_reason():
    assert check(A(action="wait", seconds=3, reason="送信の完了を待つ")).kind == safety.ALLOW
