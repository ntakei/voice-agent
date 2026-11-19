"""安全ルール。プロンプトではなく、実行の直前にコードで判定する。

判定の順番:
  1. パスワード: パスワード欄への入力・キー操作は拒否してタスクを止める（stop）
  2. キーの拒否: Windows キーの組み合わせなど（block）
  3. 前面アプリ: 文字入力・キー操作は、前面アプリが保護対象なら拒否、target_app と違っても拒否（block）
  4. 確認: risky=true、送信・削除・購入・提出・投稿に当たる語やキー → 人間の確認（confirm）
判定に使う情報は、モデルの申告ではなく OS から取ったもの（前面のプロセス名、UI Automation の要素）を優先する。
"""
import re
from dataclasses import dataclass

from . import actions
from .uia import Element
from .winapi import WindowInfo

ALLOW, CONFIRM, BLOCK, STOP = "allow", "confirm", "block", "stop"
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# パスワード欄にフォーカスがあっても許すキー（欄から離れるためのもの）
PASSWORD_SAFE_KEYS = {"tab", "shift+tab", "esc"}


@dataclass
class Decision:
    kind: str           # allow | confirm | block | stop
    reason: str = ""
    clicked: Element | None = None   # 判定に使ったクリック先の要素（ログ用）
    focused: Element | None = None   # 判定に使ったフォーカス中の要素（ログ用）

    @property
    def allowed(self) -> bool:
        return self.kind == ALLOW


def _has_word(text: str, words: list[str]) -> str | None:
    """text に含まれる語を返す。日本語などは部分一致、英字だけの語は単語単位（大文字小文字は無視）。"""
    if not text:
        return None
    low = text.lower()
    for w in words:
        wl = str(w).lower()
        if re.fullmatch(r"[a-z0-9 ]+", wl):
            if re.search(r"(?<![a-z0-9])" + re.escape(wl) + r"(?![a-z0-9])", low):
                return w
        elif wl in low:
            return w
    return None


def _combo(a: dict) -> str:
    return "+".join(actions.parse_keys(a.get("text", "")))


def _key_set(combo: str) -> frozenset:
    return frozenset(actions.parse_keys(combo))


def app_matches(target_app: str, front_exe: str, apps: list[actions.App]) -> bool:
    """target_app（呼び名かプロセス名）が、前面のプロセスを指しているか。ウィンドウタイトルは使わない（偽装できるため）。"""
    if not target_app or not front_exe:
        return False
    front = front_exe.lower()
    app = actions.resolve_app(target_app, apps)
    if app is not None:
        return front in {p.lower() for p in app.process}
    base = re.sub(r"\.exe$", "", target_app.strip().lower())
    return base == re.sub(r"\.exe$", "", front)


class SafetyPolicy:
    def __init__(self, cfg: dict, apps: list[actions.App]):
        sc = cfg.get("safety", {})
        self.apps = apps
        self.risky_words = list(sc.get("risky_words", []))
        self.password_words = list(sc.get("password_words", []))
        self.risky_keys = {_key_set(k) for k in sc.get("risky_keys", [])}
        self.blocked_keys = [_key_set(k) for k in sc.get("blocked_keys", [])]
        self.protected = {p.lower() for p in sc.get("protected_processes", [])}

    def is_protected(self, exe: str) -> bool:
        """エージェント自身の環境（端末・Claude・エクスプローラーなど）のプロセスか。"""
        return bool(exe) and exe.lower() in self.protected

    def check(self, a: dict, front: WindowInfo | None, focused: Element | None = None,
              clicked: Element | None = None) -> Decision:
        """a: 検証済みのアクション / front: 前面ウィンドウ / focused: フォーカス中の要素 / clicked: クリック先の要素"""
        t = a["action"]
        reason = str(a.get("reason", ""))

        # 1. パスワード
        if t in ("type", "key"):
            pw_field = focused is not None and focused.is_password
            pw_word = (_has_word(reason, self.password_words)
                       or (focused and _has_word(focused.name, self.password_words)))
            if t == "type" and (pw_field or pw_word):
                where = focused.describe() if pw_field else f"「{pw_word}」を含む操作"
                return Decision(STOP, f"パスワードの入力は拒否します（{where}）")
            if t == "key" and pw_field and _combo(a) not in PASSWORD_SAFE_KEYS:
                return Decision(STOP, f"パスワード欄でのキー操作は拒否します（{focused.describe()}）")

        # 2. 拒否するキー
        if t == "key":
            keys = _key_set(a["text"])
            if "win" in keys or any(keys == b for b in self.blocked_keys):
                return Decision(BLOCK, f"キー {_combo(a)} は使えません（許可リスト外のアプリを起動できるため）")

        # 3. 前面アプリ
        if t in ("type", "key"):
            exe = front.exe if front else ""
            if not exe:
                return Decision(BLOCK, "前面のアプリが分からないため、キー入力をしません")
            if exe.lower() in self.protected:
                return Decision(BLOCK, f"{exe} にはキー入力をしません（保護対象のアプリ）")
            if not app_matches(a.get("target_app", ""), exe, self.apps):
                return Decision(BLOCK, f"キーの宛先は {a.get('target_app')} ですが、前面は {exe} です。"
                                       "先に open_app で目的のアプリを前面にしてください")

        # 4. 確認が必要な操作
        if t in ("done", "fail"):
            return Decision(ALLOW)
        if a.get("risky"):
            return Decision(CONFIRM, "モデルが risky=true と判断")
        if t == "key" and _key_set(a["text"]) in self.risky_keys:
            return Decision(CONFIRM, f"送信・削除などに使われるキー {_combo(a)}")
        if t in ("open_app", "scroll", "wait"):
            # アプリを開く・スクロールはそれ自体では送信も削除もしない。
            # reason には先の手（「開いて削除する」など）が書かれることがあるので、語では判定しない。
            return Decision(ALLOW)
        if clicked is not None and EMAIL.search(clicked.name or ""):
            # アカウントを選ぶボタン（「〇〇として続行, xxx@example.com」など）。フェーズ7で確認なしに押していた
            return Decision(CONFIRM, "クリック先にメールアドレスがある（アカウントを選ぶ・ログインする操作）")
        for label, text in (("入力する文字列", a.get("text") if t == "type" else ""),
                            ("理由", reason),
                            ("クリック先の要素", clicked.name if clicked else "")):
            w = _has_word(str(text or ""), self.risky_words)
            if w:
                return Decision(CONFIRM, f"{label}に「{w}」が含まれる")
        return Decision(ALLOW)
