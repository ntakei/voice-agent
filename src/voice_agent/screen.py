"""画面キャプチャ（mss）、モデル用の縮小、注釈画像。

画面の変化検出と「落ち着くまで待つ」処理はフェーズ5で追加する。
"""
import mss
from PIL import Image, ImageDraw, ImageFont


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
