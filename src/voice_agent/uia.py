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


def invoke_at(x: int, y: int) -> str:
    """実画面座標 (x, y) のリンク・ボタンを、UI Automation で直接押す（Invoke）。押せたら要素の説明を返す。

    フェーズ7: Edge ではページ内のリンクをマウスでクリックしても開かなかった（Chrome では開いた）。
    リンクの文字の部分が当たったときは、その親がリンクのことがあるので、3階層まで親をたどる。
    """
    try:
        with auto.UIAutomationInitializerInThread():
            ctrl = auto.ControlFromPoint(x, y)
            for _ in range(3):
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
