"""UI Automation で、クリック位置の要素やフォーカス中の要素を調べる（安全ルール用）。

モデルの自己申告ではなく、OS が持っている実際の要素情報（名前・種類・パスワード欄か）を使う。
取得に失敗したときは None を返す（呼び出し側は「不明」として安全側に扱う）。
"""
from dataclasses import dataclass

import uiautomation as auto


@dataclass
class Element:
    name: str
    control_type: str     # 例: ButtonControl, EditControl
    is_password: bool
    automation_id: str = ""

    def describe(self) -> str:
        kind = self.control_type.replace("Control", "")
        return f"{kind}「{self.name[:40]}」" + ("（パスワード欄）" if self.is_password else "")


def _element(ctrl) -> Element | None:
    if ctrl is None:
        return None
    try:
        return Element(
            name=ctrl.Name or "",
            control_type=ctrl.ControlTypeName or "",
            is_password=bool(ctrl.IsPassword),
            automation_id=ctrl.AutomationId or "",
        )
    except Exception:
        return None


def element_at(x: int, y: int) -> Element | None:
    """実画面座標 (x, y) にある要素。"""
    try:
        with auto.UIAutomationInitializerInThread():
            return _element(auto.ControlFromPoint(x, y))
    except Exception:
        return None


def focused_element() -> Element | None:
    """キーボードのフォーカスがある要素（入力やキー操作の行き先）。"""
    try:
        with auto.UIAutomationInitializerInThread():
            return _element(auto.GetFocusedControl())
    except Exception:
        return None


# マウスのクリックが効かないとき、UI Automation で直接「押す」対象にする要素の種類
INVOKABLE = {"HyperlinkControl", "ButtonControl", "MenuItemControl", "SplitButtonControl"}


def actionable_at(x: int, y: int) -> Element | None:
    """実画面座標 (x, y) のリンク・ボタン（その点の要素か、3階層までの親）。ボタンの中のアイコンや文字を
    クリックしたとき、実際に押されるボタンを知るため（安全ルールの判定と、直接押す処理に使う）。"""
    try:
        with auto.UIAutomationInitializerInThread():
            ctrl = auto.ControlFromPoint(x, y)
            for _ in range(4):
                if ctrl is None:
                    return None
                if ctrl.ControlTypeName in INVOKABLE:
                    return _element(ctrl)
                ctrl = ctrl.GetParentControl()
    except Exception:  # noqa: BLE001
        return None
    return None


def invoke_at(x: int, y: int) -> str:
    """実画面座標 (x, y) のリンク・ボタンを、UI Automation で直接押す（Invoke）。押せたら要素の説明を返す。

    フェーズ7: Edge ではページ内のリンクをマウスでクリックしても開かなかった（Chrome では開いた）。
    リンクの文字の部分が当たったときは、その親がリンクのことがあるので、3階層まで親をたどる。
    """
    try:
        with auto.UIAutomationInitializerInThread():
            ctrl = auto.ControlFromPoint(x, y)
            for _ in range(4):
                if ctrl is None:
                    break
                if ctrl.ControlTypeName in INVOKABLE:
                    # 押した後に名前が変わる部品がある（ChatGPT の「送信」→「生成を中止」）ので、押す前に記録する
                    what = _element(ctrl).describe()
                    pat = ctrl.GetPattern(auto.PatternId.InvokePattern)
                    if pat is not None and pat.Invoke():
                        return what
                    legacy = ctrl.GetPattern(auto.PatternId.LegacyIAccessiblePattern)
                    if legacy is not None and legacy.DoDefaultAction():
                        return what
                ctrl = ctrl.GetParentControl()
    except Exception:  # noqa: BLE001  押せなければ何もしない（呼び出し側はマウスのクリックの結果のまま扱う）
        return ""
    return ""


# 画面の言葉として集める要素の種類（音声認識の補正の手がかり）
_WORD_TYPES = {"HyperlinkControl", "ButtonControl", "TabItemControl", "MenuItemControl", "ListItemControl",
               "SplitButtonControl", "TreeItemControl"}


def visible_words(hwnd: int, limit: int = 80, budget_secs: float = 0.8, max_nodes: int = 800) -> list[str]:
    """ウィンドウの中のボタン・リンク・タブなどの名前（重複なし、短いもの、上から順）。

    ブラウザのページは要素が多いので、時間（budget_secs）と数（max_nodes）で打ち切る。
    取れなければ空のリスト（補正は画面の言葉なしで行う）。
    """
    import time
    words: list[str] = []
    seen: set[str] = set()
    end = time.time() + budget_secs
    try:
        with auto.UIAutomationInitializerInThread():
            root = auto.ControlFromHandle(hwnd)
            queue = [root] if root else []
            nodes = 0
            while queue and time.time() < end and nodes < max_nodes and len(words) < limit:
                ctrl = queue.pop(0)
                nodes += 1
                try:
                    if ctrl.ControlTypeName in _WORD_TYPES and not ctrl.IsOffscreen:
                        name = " ".join((ctrl.Name or "").split())
                        if 1 < len(name) <= 30 and name not in seen:
                            seen.add(name)
                            words.append(name)
                    queue.extend(ctrl.GetChildren())
                except Exception:  # noqa: BLE001  途中で消えた要素などは飛ばす
                    continue
    except Exception:  # noqa: BLE001
        return words
    return words


# ---- 番号で選ばせる（Set-of-Mark）: 画面上の操作できる部品の一覧 ----

# 番号を付ける部品の種類（クリックや入力の対象になるもの）
MARK_TYPES = {"ButtonControl", "HyperlinkControl", "EditControl", "ComboBoxControl", "CheckBoxControl",
              "RadioButtonControl", "MenuItemControl", "TabItemControl", "ListItemControl", "TreeItemControl",
              "SplitButtonControl", "SliderControl", "SpinnerControl", "DataItemControl", "HeaderItemControl"}


@dataclass
class Mark:
    id: int
    name: str
    control_type: str
    rect: tuple[int, int, int, int]     # 実画面のピクセル（left, top, right, bottom）

    @property
    def center(self) -> tuple[int, int]:
        l, t, r, b = self.rect
        return (l + r) // 2, (t + b) // 2

    @property
    def invokable(self) -> bool:
        return self.control_type in INVOKABLE

    def element(self) -> Element:
        return Element(self.name, self.control_type, False)

    def describe(self) -> str:
        kind = self.control_type.replace("Control", "")
        return f"[{self.id}] {kind}「{self.name[:40]}」" if self.name else f"[{self.id}] {kind}"


def collect_marks(hwnd: int, area: tuple[int, int, int, int], limit: int = 80, budget_secs: float = 1.0,
                  max_nodes: int = 1500, min_size: int = 6) -> list[Mark]:
    """ウィンドウの中の、画面に見えている操作できる部品（ボタン・リンク・入力欄など）を集める。

    area は対象にする画面の範囲（ウィンドウの矩形を画面内に切り詰めたもの）。番号は上から下・左から右の順。
    ブラウザのページは部品が多いので、時間（budget_secs）・数（max_nodes）で打ち切る。取れなければ空。
    """
    import time
    found: list[tuple[str, str, tuple[int, int, int, int]]] = []
    seen_rects: set[tuple[int, int, int, int]] = set()
    al, at, ar, ab = area
    end = time.time() + budget_secs
    try:
        with auto.UIAutomationInitializerInThread():
            root = auto.ControlFromHandle(hwnd)
            queue = [root] if root else []
            nodes = 0
            while queue and time.time() < end and nodes < max_nodes:
                ctrl = queue.pop(0)
                nodes += 1
                try:
                    if ctrl.IsOffscreen:
                        continue                    # 画面外（スクロールしないと見えない）の部品と、その中は飛ばす
                    ct = ctrl.ControlTypeName
                    if ct in MARK_TYPES and ctrl.IsEnabled:
                        r = ctrl.BoundingRectangle
                        rect = (max(r.left, al), max(r.top, at), min(r.right, ar), min(r.bottom, ab))
                        if rect[2] - rect[0] >= min_size and rect[3] - rect[1] >= min_size and rect not in seen_rects:
                            seen_rects.add(rect)
                            found.append((" ".join((ctrl.Name or "").split()), ct, rect))
                    queue.extend(ctrl.GetChildren())
                except Exception:  # noqa: BLE001  途中で消えた要素などは飛ばす
                    continue
    except Exception:  # noqa: BLE001
        pass
    # 上から下・左から右（行の高さ 20px でまとめる）。多すぎるときは上のほうを優先
    found.sort(key=lambda f: (f[2][1] // 20, f[2][0]))
    return [Mark(i, name, ct, rect) for i, (name, ct, rect) in enumerate(found[:limit], 1)]
