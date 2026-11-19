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
