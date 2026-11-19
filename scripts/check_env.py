"""環境確認スクリプト（マウス・キーボードは動かさない）。

  python -m uv run python scripts/check_env.py [--base-url URL]   # 省略時は config.yaml / .env の値

確認内容: DPI対応 / import / 画面サイズ / 前面ウィンドウ / 日本語音声（SAPI）/ マイク / 音声認識モデル / モデルAPI
"""
import argparse
import ctypes
import importlib
import json
import urllib.request

# DPI 対応は他の何よりも先に宣言する
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    DPI_RESULT = None
except OSError as e:
    DPI_RESULT = e


def report(good, name, detail=""):
    print(f"[{'OK' if good else 'NG'}]   {name} {detail}".rstrip())


def check(name, fn):
    try:
        good, detail = fn()
    except Exception as e:
        good, detail = False, f"{type(e).__name__}: {e}"
    report(good, name, detail)


def check_imports():
    bad = []
    for mod in ["openai", "mss", "pyautogui", "win32gui", "win32process", "pyperclip", "PIL", "yaml",
                "dotenv", "pynput", "uiautomation", "faster_whisper", "sounddevice", "numpy", "pyttsx3"]:
        try:
            importlib.import_module(mod)
        except Exception as e:
            bad.append(f"{mod} ({type(e).__name__}: {e})")
    return not bad, "全モジュール import 成功" if not bad else "; ".join(bad)


def check_screen():
    import mss
    import pyautogui
    with mss.MSS() as sct:
        mons = [(m["width"], m["height"]) for m in sct.monitors[1:]]
    pg = pyautogui.size()
    dpi = ctypes.windll.user32.GetDpiForSystem()
    good = len(mons) == 1 and mons[0] == (pg.width, pg.height)
    return good, f"mss={mons} pyautogui={pg.width}x{pg.height} DPI={dpi}（100%=96）"


def check_front():
    import win32api
    import win32con
    import win32gui
    import win32process
    hwnd = win32gui.GetForegroundWindow()
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    h = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    try:
        exe = win32process.GetModuleFileNameEx(h, 0)
    finally:
        win32api.CloseHandle(h)
    return True, f"title={win32gui.GetWindowText(hwnd)[:40]!r} exe={exe.rsplit(chr(92), 1)[-1]}"


def check_voice():
    import win32com.client
    names = [t.GetDescription() for t in win32com.client.Dispatch("SAPI.SpVoice").GetVoices()]
    ja = [n for n in names if "japanese" in n.lower() or "日本語" in n]
    return bool(ja), f"日本語={ja} 全音声={names}"


def check_stt_model():
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "src"))
    from faster_whisper.utils import _MODELS
    from voice_agent import config
    name = ((config.load().get("voice") or {}).get("stt") or {}).get("model", "small")
    repo = _MODELS.get(name, name)
    found = list((root / "models").glob(f"models--{repo.replace('/', '--')}/snapshots/*/model.bin"))
    return bool(found), (f"{name}（models/ にあり）" if found else
                         f"{name} が models/ にない → scripts/download_models.py を実行")


def check_mic():
    import sounddevice as sd
    ins = [d["name"] for d in sd.query_devices() if d["max_input_channels"] > 0]
    return bool(ins), str(ins[:5])


def check_model(base):
    with urllib.request.urlopen(base.rstrip("/") + "/models", timeout=10) as r:
        data = json.loads(r.read())
    return True, str([m.get("id") for m in data.get("data", [])])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", help="省略時は config.yaml（.env の VA_BASE_URL があればそちら）")
    args = ap.parse_args()
    if not args.base_url:
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        from voice_agent import config
        args.base_url = config.load()["model"]["base_url"]

    report(DPI_RESULT is None, "SetProcessDpiAwareness(2)", str(DPI_RESULT or ""))
    check("import", check_imports)
    check("画面サイズ", check_screen)
    check("前面ウィンドウ", check_front)
    check("日本語音声 (SAPI5)", check_voice)
    check("マイク入力デバイス", check_mic)
    check("音声認識モデル", check_stt_model)
    check("モデルAPI /models", lambda: check_model(args.base_url))
