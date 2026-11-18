"""アクションの JSON の正規化・検証・座標変換。

モデルの出力（JSON 1個）:
  action      click | double_click | type | key | scroll | open_app | done | fail
  x, y        送った画像のピクセル座標（click / double_click は必須、scroll は任意）
  text        type: 入力する文字列 / key: "ctrl+l" 形式 / open_app: アプリ名
  dy          scroll: 整数。正 = 上へ、負 = 下へ（1 = ホイール1目盛り）
  target_app  type / key: キーを受け取るべきアプリ名（必須）
  risky       送信・削除・購入・提出・投稿に当たるなら true
  reason      短い日本語の理由
"""
import re
from dataclasses import dataclass

import pyautogui

CLICKS = ("click", "double_click")
ACTIONS = CLICKS + ("type", "key", "scroll", "open_app", "done", "fail")
NEEDS_TARGET_APP = ("type", "key")

ACTION_ALIASES = {
    "doubleclick": "double_click", "dblclick": "double_click", "left_click": "click", "tap": "click",
    "press": "key", "hotkey": "key", "keypress": "key", "key_press": "key", "shortcut": "key",
    "input": "type", "write": "type", "type_text": "type",
    "open": "open_app", "launch": "open_app", "launch_app": "open_app", "openapp": "open_app",
    "finish": "done", "finished": "done", "complete": "done", "stop": "fail",
}

KEY_ALIASES = {
    "control": "ctrl", "ctl": "ctrl", "strg": "ctrl",
    "windows": "win", "super": "win", "meta": "win", "cmd": "win", "command": "win", "winleft": "win",
    "option": "alt",
    "return": "enter", "escape": "esc", "del": "delete", "bksp": "backspace", "back": "backspace",
    "arrowup": "up", "arrowdown": "down", "arrowleft": "left", "arrowright": "right",
    "spacebar": "space", "pgup": "pageup", "pgdn": "pagedown", "ins": "insert",
}
VALID_KEYS = set(pyautogui.KEYBOARD_KEYS)
MAX_TEXT = 2000
MAX_KEYS = 4
MAX_SCROLL = 20


def _to_bool(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1", "はい")
    return bool(v)


def normalize(a: dict) -> dict:
    """よくある書き間違いをスキーマに寄せる。元の dict は変えない。"""
    a = dict(a)
    act = str(a.get("action", "")).strip().lower().replace("-", "_").replace(" ", "_")
    a["action"] = ACTION_ALIASES.get(act, act)
    for k in ("x", "y"):
        if k in a and a[k] is not None:
            try:
                a[k] = float(a[k])
            except (TypeError, ValueError):
                pass
    # 別名のフィールド（keys / app）を text に寄せる
    if a["action"] == "key" and not a.get("text") and a.get("keys"):
        a["text"] = a["keys"]
    if a["action"] == "open_app" and not a.get("text"):
        # モデルがアプリ名を app や target_app に入れることがある（フェーズ4で確認）
        a["text"] = a.get("app") or a.get("target_app") or ""
    if a["action"] == "scroll":
        try:
            a["dy"] = int(float(a.get("dy", -3)))
        except (TypeError, ValueError):
            a["dy"] = -3
    a["risky"] = _to_bool(a.get("risky", False))
    for k in ("text", "target_app", "reason"):
        if k in a and a[k] is not None and not isinstance(a[k], str):
            a[k] = str(a[k])
    return a


def parse_keys(combo: str) -> list[str]:
    """'Ctrl + L' -> ['ctrl', 'l']。"""
    parts = [p.strip().lower() for p in re.split(r"\s*\+\s*", str(combo).strip()) if p.strip()]
    return [KEY_ALIASES.get(p, p) for p in parts]


@dataclass
class App:
    names: list[str]
    launch: str
    process: list[str]


def load_apps(cfg: dict) -> list[App]:
    return [App(list(e["names"]), e["launch"], list(e["process"])) for e in cfg.get("apps", [])]


def resolve_app(name: str, apps: list[App]) -> App | None:
    """呼び名 -> 許可リストのアプリ。完全一致（大文字小文字・空白を無視）か、プロセス名との一致のみ。"""
    def norm(s):
        return re.sub(r"\s+", "", str(s)).lower()
    want = norm(name)
    for app in apps:
        if want in {norm(n) for n in app.names} or want in {norm(p) for p in app.process} \
                or want == norm(app.launch) or want + ".exe" in {norm(p) for p in app.process}:
            return app
    return None


def validate(a: dict, image_size: tuple[int, int], apps: list[App]) -> str | None:
    """エラーの説明（日本語）を返す。正しければ None。image_size はモデルに送った画像の (幅, 高さ)。"""
    t = a.get("action")
    if t not in ACTIONS:
        return f"不明な action {t!r}（{', '.join(ACTIONS)} のどれか）"
    w, h = image_size
    has_xy = a.get("x") is not None or a.get("y") is not None
    if t in CLICKS or (t == "scroll" and has_xy):
        x, y = a.get("x"), a.get("y")
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            return f"{t} には数値の x と y が必要"
        if not (0 <= x <= w and 0 <= y <= h):
            return f"x, y が画像の範囲外（0〜{w}, 0〜{h}）"
    if t == "type":
        text = a.get("text")
        if not isinstance(text, str) or text == "":
            return "type には text（入力する文字列）が必要"
        if len(text) > MAX_TEXT:
            return f"text が長すぎる（{MAX_TEXT} 文字まで）"
    if t == "key":
        keys = parse_keys(a.get("text") or "")
        if not keys:
            return "key には text（例: ctrl+l）が必要"
        if len(keys) > MAX_KEYS:
            return f"同時に押すキーは {MAX_KEYS} 個まで"
        bad = [k for k in keys if k not in VALID_KEYS]
        if bad:
            return f"不明なキー {bad}"
    if t in NEEDS_TARGET_APP and not str(a.get("target_app") or "").strip():
        return f"{t} には target_app（キーを受け取るアプリ名）が必要"
    if t == "scroll" and not (-MAX_SCROLL <= a.get("dy", 0) <= MAX_SCROLL and a.get("dy", 0) != 0):
        return f"scroll の dy は -{MAX_SCROLL}〜{MAX_SCROLL} の 0 以外"
    if t == "open_app":
        if not a.get("text"):
            return "open_app には text（アプリ名）が必要"
        if resolve_app(a["text"], apps) is None:
            allowed = ", ".join(app.names[0] for app in apps)
            return f"許可されていないアプリ {a['text']!r}（使えるのは: {allowed}）"
    return None


def to_screen_point(x: float, y: float, image_size: tuple[int, int], screen_size: tuple[int, int]) -> tuple[int, int]:
    """送った画像のピクセル座標 -> 実画面のピクセル座標。

    画面内にクランプし、四隅ちょうどは1px内側へずらす（pyautogui の FAILSAFE が誤作動しないように）。
    """
    (w, h), (W, H) = image_size, screen_size
    px = min(max(round(x * W / w), 0), W - 1)
    py = min(max(round(y * H / h), 0), H - 1)
    if px in (0, W - 1) and py in (0, H - 1):
        px = 1 if px == 0 else W - 2
        py = 1 if py == 0 else H - 2
    return px, py


def describe(a: dict, point: tuple[int, int] | None = None) -> str:
    """ログや読み上げ用の短い日本語ラベル。"""
    t = a.get("action", "?")
    at = f" → {point}" if point else ""
    if t == "click":
        return f"クリック ({a.get('x'):.0f},{a.get('y'):.0f}){at}"
    if t == "double_click":
        return f"ダブルクリック ({a.get('x'):.0f},{a.get('y'):.0f}){at}"
    if t == "type":
        s = str(a.get("text", ""))
        return f"入力「{s[:30]}{'…' if len(s) > 30 else ''}」→ {a.get('target_app')}"
    if t == "key":
        return f"キー {'+'.join(parse_keys(a.get('text', '')))} → {a.get('target_app')}"
    if t == "scroll":
        return f"スクロール {'上' if a.get('dy', 0) > 0 else '下'} {abs(a.get('dy', 0))}{at}"
    if t == "open_app":
        return f"アプリを開く {a.get('text')}"
    if t == "done":
        return "完了"
    if t == "fail":
        return "中止"
    return str(t)
