"""画面キャプチャ（mss）、モデル用の縮小、画面の変化検出と落ち着き待ち、注釈画像。"""
import threading
import time

import mss
from PIL import Image, ImageChops, ImageDraw, ImageFont

# 変化検出で無視する領域（画面に対する割合 x1, y1, x2, y2）。既定はタスクバー右端（秒表示の時計）。
DEFAULT_IGNORE = [(0.85, 0.95, 1.0, 1.0)]
_DIFF_SIZE = (480, 270)    # 比較用に縮小するサイズ
_DIFF_LEVEL = 24           # 明るさの差がこれを超えた画素を「変化」とみなす


def _font(size: int):
    for name in ("meiryo.ttc", "YuGothM.ttc", "msgothic.ttc", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def screen_size() -> tuple[int, int]:
    """主モニタの実ピクセルサイズ（DPI 対応宣言後の値）。"""
    with mss.MSS() as sct:
        m = sct.monitors[1]
        return m["width"], m["height"]


def capture() -> Image.Image:
    """主モニタ全体のスクリーンショット（実ピクセル、RGB）。"""
    with mss.MSS() as sct:
        shot = sct.grab(sct.monitors[1])
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def for_model(img: Image.Image, width: int) -> Image.Image:
    """モデルに送る画像: 幅 width に縮小（0 または元が小さければそのまま）。"""
    if not width or img.width <= width:
        return img.copy()
    return img.resize((width, round(img.height * width / img.width)), Image.LANCZOS)


def diff_pixels(a: Image.Image, b: Image.Image, ignore=None) -> int:
    """2枚の画像の差（縮小したグレー画像で、変化した画素の数）。ignore の領域は数えない。"""
    ga = a.convert("L").resize(_DIFF_SIZE)
    gb = b.convert("L").resize(_DIFF_SIZE)
    diff = ImageChops.difference(ga, gb).point(lambda v: 255 if v > _DIFF_LEVEL else 0)
    W, H = _DIFF_SIZE
    if ignore is None:
        ignore = DEFAULT_IGNORE
    if ignore:
        d = ImageDraw.Draw(diff)
        for x1, y1, x2, y2 in ignore:
            d.rectangle([x1 * W, y1 * H, x2 * W, y2 * H], fill=0)
    return diff.histogram()[255]


def changed(a: Image.Image | None, b: Image.Image | None, min_pixels: int = 30, ignore=None) -> bool:
    """ほぼ同じ画面なら False。誤って「変化なし」とすると、効いたクリックを捨ててしまうので閾値は小さめ。"""
    if a is None or b is None:
        return True
    return diff_pixels(a, b, ignore) >= min_pixels


def settle(stop_event: threading.Event | None = None, min_wait: float = 0.15, max_wait: float = 1.5,
           poll: float = 0.12, quiet_pixels: int = 150, ignore=None) -> Image.Image | None:
    """固定の sleep ではなく、画面の変化が止まるまで待つ。落ち着いた（または max_wait に達した）画面を返す。

    小さな動き（点滅するカーソル、スピナー）は quiet_pixels 未満なら落ち着いたとみなす。
    stop_event が立ったら None を返す。
    """
    stop_event = stop_event or threading.Event()
    end = time.time() + max_wait
    if stop_event.wait(min_wait):
        return None
    prev = capture()
    while time.time() < end:
        if stop_event.wait(poll):
            return None
        cur = capture()
        if diff_pixels(prev, cur, ignore) < quiet_pixels:
            return cur
        prev = cur
    return prev


COLORS = [(255, 40, 40), (0, 200, 255), (60, 220, 60), (255, 170, 0), (220, 80, 255), (255, 255, 0)]


def mark_points(img: Image.Image, points: list[tuple[float, float, str]], radius: int | None = None,
                boxes: list[tuple[int, int, int, int] | None] | None = None) -> Image.Image:
    """画像に印（円＋十字＋ラベル）を描いたコピーを返す。

    points は画像上のピクセル座標とラベル。boxes を渡すと正解範囲を同じ色の点線枠で描く。
    """
    out = img.copy()
    d = ImageDraw.Draw(out)
    W, H = out.size
    r = radius or max(10, W // 120)
    font = _font(max(14, W // 90))
    for i, box in enumerate(boxes or []):
        if box:
            c = COLORS[i % len(COLORS)]
            x1, y1, x2, y2 = box
            for k in range(x1, x2, 6):
                d.line([k, y1, min(k + 3, x2), y1], fill=c)
                d.line([k, y2, min(k + 3, x2), y2], fill=c)
            for k in range(y1, y2, 6):
                d.line([x1, k, x1, min(k + 3, y2)], fill=c)
                d.line([x2, k, x2, min(k + 3, y2)], fill=c)
    for i, (x, y, label) in enumerate(points):
        c = COLORS[i % len(COLORS)]
        px, py = min(max(round(x), 0), W - 1), min(max(round(y), 0), H - 1)
        d.ellipse([px - r, py - r, px + r, py + r], outline=c, width=3)
        d.line([px - 2 * r, py, px + 2 * r, py], fill=c, width=2)
        d.line([px, py - 2 * r, px, py + 2 * r], fill=c, width=2)
        tx = px + r + 4 if px < W * 0.8 else px - r - 4 - d.textlength(label, font=font)
        ty = py + r if py < H * 0.9 else py - 2 * r - font.size
        d.text((tx, ty), label, fill=c, font=font, stroke_width=3, stroke_fill=(0, 0, 0))
    return out


def caption(img: Image.Image, text: str) -> Image.Image:
    """画像の下端に説明を書いたコピー（クリック以外の操作の注釈用）。"""
    out = img.copy()
    d = ImageDraw.Draw(out)
    font = _font(max(14, out.width // 60))
    d.text((20, out.height - font.size - 24), text, fill=(0, 200, 255), font=font,
           stroke_width=3, stroke_fill=(0, 0, 0))
    return out
