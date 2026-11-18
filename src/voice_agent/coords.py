"""モデルの座標出力の解釈と、0〜1000 正規化座標 <-> 実画面座標の変換。

モデルによって座標の返し方が違うので、寛容に読む:
  {"x": 512, "y": 300}
  {"point": [512, 300]} / {"bbox": [x1, y1, x2, y2]}（枠なら中心）
  <|begin_of_box|>[x1, y1, x2, y2]<|end_of_box|>（GLM 系の枠トークン）
  [512, 300]
"""
import json
import re

GRID = 1000

_THINK = re.compile(r"<think>.*?</think>", re.S)
_BOX = re.compile(r"<\|begin_of_box\|>(.*?)<\|end_of_box\|>", re.S)
_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def parse_json(text: str):
    """最初の '{' から最後の '}' までを JSON として読む。失敗なら None。"""
    text = _THINK.sub("", text or "")
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        v = json.loads(text[a:b + 1])
        return v if isinstance(v, dict) else None
    except json.JSONDecodeError:
        return None


def _from_numbers(nums):
    if len(nums) >= 4:
        x1, y1, x2, y2 = nums[:4]
        return (x1 + x2) / 2, (y1 + y2) / 2, "bbox"
    if len(nums) >= 2:
        return nums[0], nums[1], "point"
    return None


def parse_point(text: str):
    """モデルの返答から (x, y, 形式) を取り出す。座標はモデルの返したまま（0〜1000 想定）。読めなければ None。

    形式: "xy"（x/y キー）, "point", "bbox", "box_token", "loose"
    """
    text = _THINK.sub("", text or "")
    j = parse_json(text)
    if j is not None:
        if "x" in j and "y" in j:
            try:
                return float(j["x"]), float(j["y"]), "xy"
            except (TypeError, ValueError):
                pass
        for key in ("point", "bbox", "box", "coordinates", "position"):
            v = j.get(key)
            if isinstance(v, (list, tuple)):
                try:
                    r = _from_numbers([float(n) for n in v])
                except (TypeError, ValueError):
                    r = None
                if r:
                    return r
    m = _BOX.search(text)
    if m:
        r = _from_numbers([float(n) for n in _NUM.findall(m.group(1))])
        if r:
            return r[0], r[1], "box_token"
    m = re.search(r"\[([^\[\]]*)\]", text)
    if m:
        r = _from_numbers([float(n) for n in _NUM.findall(m.group(1))])
        if r:
            return r[0], r[1], "loose"
    return None


def in_grid(x: float, y: float) -> bool:
    return 0 <= x <= GRID and 0 <= y <= GRID


def to_screen(x: float, y: float, width: int, height: int) -> tuple[int, int]:
    """0〜1000 の正規化座標 -> 実画面のピクセル座標（画面内にクランプ）。"""
    px = round(x / GRID * width)
    py = round(y / GRID * height)
    return min(max(px, 0), width - 1), min(max(py, 0), height - 1)


def to_grid(px: float, py: float, width: int, height: int) -> tuple[int, int]:
    """実画面のピクセル座標 -> 0〜1000 の正規化座標。"""
    return round(px / width * GRID), round(py / height * GRID)
