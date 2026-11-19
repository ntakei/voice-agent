"""前面化で Alt / Shift などの意味のあるキーを送らないこと（キーヒントや IME 切り替えを起こさない）。"""
import win32con

from voice_agent import winapi


def test_activate_never_sends_alt_or_shift(monkeypatch):
    sent = []
    fg = {"hwnd": 100}
    monkeypatch.setattr(winapi.win32api, "keybd_event", lambda vk, *a: sent.append(vk))
    monkeypatch.setattr(winapi.win32gui, "IsIconic", lambda h: False)
    monkeypatch.setattr(winapi.win32gui, "BringWindowToTop", lambda h: None)
    monkeypatch.setattr(winapi.win32gui, "SetForegroundWindow", lambda h: None)   # 何度やっても前面にならない
    monkeypatch.setattr(winapi.win32gui, "GetForegroundWindow", lambda: fg["hwnd"])
    monkeypatch.setattr(winapi.win32process, "GetWindowThreadProcessId", lambda h: (1, 1))
    monkeypatch.setattr(winapi.ctypes.windll.user32, "AttachThreadInput", lambda *a: 0, raising=False)
    monkeypatch.setattr(winapi.time, "sleep", lambda s: None)
    assert winapi.activate(200) is False
    assert sent and all(vk == winapi._VK_UNASSIGNED for vk in sent)
    assert win32con.VK_MENU not in sent and win32con.VK_SHIFT not in sent


def test_activate_success_sends_no_keys(monkeypatch):
    sent = []
    fg = {"hwnd": 100}

    def set_fg(h):
        fg["hwnd"] = h
    monkeypatch.setattr(winapi.win32api, "keybd_event", lambda vk, *a: sent.append(vk))
    monkeypatch.setattr(winapi.win32gui, "IsIconic", lambda h: False)
    monkeypatch.setattr(winapi.win32gui, "BringWindowToTop", lambda h: None)
    monkeypatch.setattr(winapi.win32gui, "SetForegroundWindow", set_fg)
    monkeypatch.setattr(winapi.win32gui, "GetForegroundWindow", lambda: fg["hwnd"])
    monkeypatch.setattr(winapi.win32process, "GetWindowThreadProcessId", lambda h: (1, 1))
    assert winapi.activate(200) is True
    assert sent == []


def test_pywin32_names_used_in_code_exist():
    """コードが呼んでいる win32gui / win32api / win32con / win32process の関数・定数が本当にあるか。

    フェーズ7: 存在しない win32gui.IsZoomed を呼んでいて、例外が握りつぶされ、最大化が一度も効いていなかった。
    テストでは winapi をまるごと偽物にしているので、この種の誤りはここで見つける。
    """
    import re
    from pathlib import Path

    import win32api
    import win32con
    import win32gui
    import win32process
    mods = {"win32gui": win32gui, "win32api": win32api, "win32con": win32con, "win32process": win32process}
    src = Path(__file__).resolve().parents[1] / "src" / "voice_agent"
    missing = []
    for f in src.glob("*.py"):
        for mod, name in re.findall(r"\b(win32gui|win32api|win32con|win32process)\.([A-Za-z_]\w*)", f.read_text(encoding="utf-8")):
            if not hasattr(mods[mod], name):
                missing.append(f"{f.name}: {mod}.{name}")
    assert not missing, missing


def test_window_state_helpers_on_invalid_window():
    from voice_agent import winapi
    assert winapi.is_zoomed(0) is False and winapi.is_hung(0) is False
    assert winapi.maximize(0) is False         # 無効なウィンドウでも例外にならない
