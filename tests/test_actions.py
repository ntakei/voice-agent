import threading

import pytest

from voice_agent import actions, executor

IMG = (1512, 851)          # モデルに送る画像
SCREEN = (1920, 1080)      # 実画面
APPS = actions.load_apps({"apps": [
    {"names": ["メモ帳", "notepad"], "launch": "notepad.exe", "process": ["notepad.exe"]},
    {"names": ["電卓", "calculator"], "launch": "calc.exe", "process": ["CalculatorApp.exe", "calc.exe"]},
    {"names": ["Edge", "Microsoft Edge"], "launch": "msedge.exe", "process": ["msedge.exe"]},
]})


def check(a):
    a = actions.normalize(a)
    return a, actions.validate(a, IMG, APPS)


# ---- 正規化 ----

def test_normalize_aliases_and_types():
    a = actions.normalize({"action": "Double-Click", "x": "100", "y": 50, "risky": "true"})
    assert a["action"] == "double_click" and a["x"] == 100.0 and a["risky"] is True
    assert actions.normalize({"action": "hotkey", "keys": "ctrl+l"})["text"] == "ctrl+l"
    assert actions.normalize({"action": "launch", "app": "メモ帳"})["action"] == "open_app"
    assert actions.normalize({"action": "open_app", "app": "メモ帳"})["text"] == "メモ帳"
    assert actions.normalize({"action": "open_app", "target_app": "Edge"})["text"] == "Edge"
    assert actions.normalize({"action": "open_app", "text": "電卓", "target_app": "Edge"})["text"] == "電卓"
    assert actions.normalize({"action": "scroll", "dy": "abc"})["dy"] == -3
    assert actions.normalize({"action": "click"})["risky"] is False


def test_parse_keys():
    assert actions.parse_keys("Ctrl + L") == ["ctrl", "l"]
    assert actions.parse_keys("Windows+R") == ["win", "r"]
    assert actions.parse_keys("Return") == ["enter"]


# ---- 検証: 正しいもの ----

@pytest.mark.parametrize("a", [
    {"action": "click", "x": 0, "y": 0},
    {"action": "click", "x": 1512, "y": 851},
    {"action": "double_click", "x": 300.5, "y": 200},
    {"action": "type", "text": "こんにちは", "target_app": "メモ帳"},
    {"action": "key", "text": "ctrl+s", "target_app": "メモ帳"},
    {"action": "key", "text": "enter", "target_app": "Edge"},
    {"action": "scroll", "dy": -5},
    {"action": "scroll", "dy": 3, "x": 700, "y": 400},
    {"action": "open_app", "text": "電卓"},
    {"action": "open_app", "text": "notepad.exe"},
    {"action": "done", "reason": "完了"},
    {"action": "fail", "reason": "見つからない"},
])
def test_valid(a):
    assert check(a)[1] is None


# ---- 検証: 誤り ----

@pytest.mark.parametrize("a,msg", [
    ({"action": "drag"}, "不明な action"),
    ({"action": "click", "x": 10}, "数値の x と y"),
    ({"action": "click", "x": "abc", "y": 10}, "数値の x と y"),
    ({"action": "click", "x": 1513, "y": 10}, "範囲外"),
    ({"action": "click", "x": 10, "y": -1}, "範囲外"),
    ({"action": "type", "text": "", "target_app": "メモ帳"}, "text"),
    ({"action": "type", "text": "x" * 2001, "target_app": "メモ帳"}, "長すぎる"),
    ({"action": "type", "text": "abc"}, "target_app"),
    ({"action": "key", "text": "ctrl+l"}, "target_app"),
    ({"action": "key", "text": "", "target_app": "Edge"}, "text"),
    ({"action": "key", "text": "ctrl+foo", "target_app": "Edge"}, "不明なキー"),
    ({"action": "key", "text": "ctrl+alt+shift+win+a", "target_app": "Edge"}, "4 個まで"),
    ({"action": "scroll", "dy": 0}, "dy"),
    ({"action": "scroll", "dy": 50}, "dy"),
    ({"action": "open_app", "text": "cmd"}, "許可されていない"),
    ({"action": "open_app", "text": "powershell.exe"}, "許可されていない"),
    ({"action": "open_app"}, "アプリ名"),
])
def test_invalid(a, msg):
    err = check(a)[1]
    assert err is not None and msg in err, err


# ---- アプリの許可リスト ----

def test_resolve_app():
    assert actions.resolve_app("メモ帳", APPS).launch == "notepad.exe"
    assert actions.resolve_app("Microsoft  Edge", APPS).launch == "msedge.exe"
    assert actions.resolve_app("calculatorapp.exe", APPS).launch == "calc.exe"
    assert actions.resolve_app("notepad", APPS).launch == "notepad.exe"
    assert actions.resolve_app("メモ", APPS) is None  # 部分一致は許さない
    # 前面アプリの表示形式「プロセス名 (呼び名)」をそのまま返してきた場合
    assert actions.resolve_app("CalculatorApp.exe (電卓)", APPS).launch == "calc.exe"
    assert actions.resolve_app("Notepad.exe（メモ帳）", APPS).launch == "notepad.exe"
    assert actions.resolve_app("CalculatorApp.exe (メモ帳)", APPS) is None   # 食い違うものは受け付けない
    assert actions.resolve_app("cmd.exe (電卓)", APPS) is None


# ---- 座標変換（送った画像 px -> 実画面 px）----

def test_to_screen_point():
    assert actions.to_screen_point(756, 425.5, IMG, SCREEN) == (960, 540)
    assert actions.to_screen_point(286, 181, IMG, SCREEN) == (363, 230)   # フェーズ3のメモ帳「ファイル」
    assert actions.to_screen_point(1512, 851, IMG, SCREEN) == (1918, 1078)  # 隅は1px内側へ
    assert actions.to_screen_point(0, 0, IMG, SCREEN) == (1, 1)
    assert actions.to_screen_point(0, 500, IMG, SCREEN)[0] == 0           # 隅以外の端はそのまま


# ---- 実行部（dry-run では何も操作しない）----

@pytest.fixture
def no_input(monkeypatch):
    """dry-run 中に pyautogui / クリップボード / 起動が呼ばれたら失敗させる。"""
    def boom(*a, **k):
        raise AssertionError("dry-run で実際の操作が呼ばれた")
    for name in ("moveTo", "click", "mouseDown", "mouseUp", "doubleClick", "hotkey", "scroll", "write", "press"):
        monkeypatch.setattr(executor.pyautogui, name, boom)
    monkeypatch.setattr(executor.pyperclip, "copy", boom)
    monkeypatch.setattr(executor.os, "startfile", boom)
    monkeypatch.setattr(executor.winapi, "activate", boom)
    monkeypatch.setattr(executor.winapi, "find_window", lambda procs: None)


@pytest.mark.parametrize("a,label_part,point", [
    ({"action": "click", "x": 286, "y": 181}, "クリック", (363, 230)),
    ({"action": "double_click", "x": 756, "y": 425.5}, "ダブルクリック", (960, 540)),
    ({"action": "type", "text": "日本語のテスト", "target_app": "メモ帳"}, "入力「日本語のテスト」", None),
    ({"action": "key", "text": "Ctrl+S", "target_app": "メモ帳"}, "キー ctrl+s", None),
    ({"action": "scroll", "dy": -3}, "スクロール 下 3", None),
    ({"action": "open_app", "text": "電卓"}, "アプリを開く", None),
    ({"action": "done"}, "完了", None),
])
def test_dry_run_does_nothing(no_input, a, label_part, point):
    a, err = check(a)
    assert err is None
    ex = executor.Executor(SCREEN, APPS, dry_run=True)
    r = ex.execute(a, IMG)
    assert r.executed is False
    assert label_part in r.label
    assert r.point == point


def test_dry_run_open_app_detail(no_input):
    a, _ = check({"action": "open_app", "text": "電卓"})
    r = executor.Executor(SCREEN, APPS, dry_run=True).execute(a, IMG)
    assert "起動する（最大化）: calc.exe" in r.detail


def test_real_mode_respects_stop(no_input):
    stop = threading.Event()
    stop.set()
    a, _ = check({"action": "click", "x": 10, "y": 10})
    r = executor.Executor(SCREEN, APPS, dry_run=False, stop_event=stop).execute(a, IMG)
    assert r.executed is False and "停止" in r.detail


def test_real_mode_calls_pyautogui(monkeypatch):
    calls = []
    monkeypatch.setattr(executor.pyautogui, "moveTo", lambda *a, **k: calls.append(("moveTo", a)))
    monkeypatch.setattr(executor.pyautogui, "mouseDown", lambda *a, **k: calls.append(("click", a)))
    monkeypatch.setattr(executor.pyautogui, "mouseUp", lambda *a, **k: None)
    monkeypatch.setattr(executor.winapi, "foreground", lambda: None)
    monkeypatch.setattr(executor.pyautogui, "hotkey", lambda *a, **k: calls.append(("hotkey", a)))
    monkeypatch.setattr(executor.pyautogui, "scroll", lambda *a, **k: calls.append(("scroll", a)))
    ex = executor.Executor(SCREEN, APPS, dry_run=False)
    ex.execute(check({"action": "click", "x": 286, "y": 181})[0], IMG)
    ex.execute(check({"action": "key", "text": "ctrl+s", "target_app": "メモ帳"})[0], IMG)
    ex.execute(check({"action": "scroll", "dy": -2})[0], IMG)
    assert calls == [("moveTo", (363, 230)), ("click", ()), ("hotkey", ("ctrl", "s")), ("scroll", (-240,))]


def test_paste_text_restores_clipboard(monkeypatch):
    clip = {"v": "元の内容"}
    pasted = []
    monkeypatch.setattr(executor.pyperclip, "paste", lambda: clip["v"])
    monkeypatch.setattr(executor.pyperclip, "copy", lambda v: clip.update(v=v))
    monkeypatch.setattr(executor.pyautogui, "hotkey", lambda *k, **kw: pasted.append((k, clip["v"])))
    monkeypatch.setattr(executor.time, "sleep", lambda s: None)
    executor.paste_text("こんにちは")
    assert pasted == [(("ctrl", "v"), "こんにちは")]
    assert clip["v"] == "元の内容"


# ---- アプリの起動方法 ----

def test_launch_prefers_createprocess(monkeypatch):
    calls = []
    monkeypatch.setattr(executor.shutil, "which", lambda exe: r"C:\Windows\System32\notepad.exe")
    monkeypatch.setattr(executor.subprocess, "Popen", lambda args, **k: calls.append(("popen", args)))
    monkeypatch.setattr(executor.os, "startfile", lambda exe: calls.append(("startfile", exe)))
    assert executor.launch("notepad.exe") == "CreateProcess"
    assert calls == [("popen", [r"C:\Windows\System32\notepad.exe"])]


def test_launch_falls_back_to_shellexecute(monkeypatch):
    calls = []
    monkeypatch.setattr(executor.shutil, "which", lambda exe: None)       # msedge.exe は PATH にない
    monkeypatch.setattr(executor.subprocess, "Popen", lambda args, **k: calls.append(("popen", args)))
    monkeypatch.setattr(executor.os, "startfile", lambda exe: calls.append(("startfile", exe)))
    assert executor.launch("msedge.exe") == "ShellExecute"
    assert calls == [("startfile", "msedge.exe")]


def test_open_app_launch_failure_is_reported_not_raised(monkeypatch):
    def denied(exe):
        raise PermissionError(5, "アクセスが拒否されました。")
    monkeypatch.setattr(executor.winapi, "find_window", lambda procs: None)
    monkeypatch.setattr(executor, "launch", denied)
    a, _ = check({"action": "open_app", "text": "メモ帳"})
    r = executor.Executor(SCREEN, APPS, dry_run=False).execute(a, IMG)
    assert r.executed is False and r.detail.startswith("起動に失敗")


# ---- open_app で最大化 ----

def open_app_real(monkeypatch, maximize=True, existing=True):
    calls = []
    w = executor.winapi.WindowInfo(42, "電卓", "CalculatorApp.exe")
    found = [w] if existing else [None, w]
    monkeypatch.setattr(executor.winapi, "find_window", lambda procs: found.pop(0) if len(found) > 1 else found[0])
    monkeypatch.setattr(executor.winapi, "maximize", lambda hwnd: calls.append(("maximize", hwnd)) or True)
    monkeypatch.setattr(executor.winapi, "activate", lambda hwnd: calls.append(("activate", hwnd)) or True)
    monkeypatch.setattr(executor, "launch", lambda exe: calls.append(("launch", exe)) or "CreateProcess")
    monkeypatch.setattr(executor.time, "sleep", lambda s: None)
    a, _ = check({"action": "open_app", "text": "電卓"})
    r = executor.Executor(SCREEN, APPS, dry_run=False, maximize=maximize).execute(a, IMG)
    return calls, r


def test_open_app_maximizes_existing_window(monkeypatch):
    calls, r = open_app_real(monkeypatch)
    assert calls == [("activate", 42), ("maximize", 42)] and "最大化" in r.detail


def test_open_app_maximizes_launched_window(monkeypatch):
    calls, r = open_app_real(monkeypatch, existing=False)
    assert calls == [("launch", "calc.exe"), ("activate", 42), ("maximize", 42)]


def test_open_app_without_maximize(monkeypatch):
    calls, r = open_app_real(monkeypatch, maximize=False)
    assert calls == [("activate", 42)] and "最大化" not in r.detail


def test_click_records_focus_change(monkeypatch):
    W = executor.winapi.WindowInfo
    seq = [W(1, "Edge", "msedge.exe"), W(2, "Program Manager", "explorer.exe"), W(2, "Program Manager", "explorer.exe")]
    monkeypatch.setattr(executor.winapi, "foreground", lambda: seq.pop(0))
    monkeypatch.setattr(executor.pyautogui, "moveTo", lambda *a, **k: None)
    monkeypatch.setattr(executor.pyautogui, "mouseDown", lambda *a, **k: None)
    monkeypatch.setattr(executor.pyautogui, "mouseUp", lambda *a, **k: None)
    r = executor.Executor(SCREEN, APPS, dry_run=False).execute(check({"action": "click", "x": 10, "y": 10})[0], IMG)
    assert r.executed and "押した直後 explorer.exe" in r.detail
