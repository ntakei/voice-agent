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
                    pat = ctrl.GetPattern(auto.PatternId.InvokePattern)
                    if pat is not None and pat.Invoke():
                        return _element(ctrl).describe()
                    legacy = ctrl.GetPattern(auto.PatternId.LegacyIAccessiblePattern)
                    if legacy is not None and legacy.DoDefaultAction():
                        return _element(ctrl).describe()
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
