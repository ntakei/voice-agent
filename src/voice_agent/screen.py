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


def _region_box(size: tuple[int, int], region) -> tuple[int, int, int, int] | None:
    """実画面のピクセル矩形 (left, top, right, bottom) を、比較用の縮小画像上の矩形にする。

    画面からはみ出す部分は切り詰める。画面上に残らない（最小化など）なら None（=画面全体で比べる）。
    """
    if not region:
        return None
    w, h = size
    W, H = _DIFF_SIZE
    l, t, r, b = max(region[0], 0), max(region[1], 0), min(region[2], w), min(region[3], h)
    if r - l < 2 or b - t < 2:
        return None
    return (int(l * W / w), int(t * H / h), max(int(l * W / w) + 1, round(r * W / w)),
            max(int(t * H / h) + 1, round(b * H / h)))


def diff_pixels(a: Image.Image, b: Image.Image, ignore=None, region=None) -> int:
    """2枚の画像の差（縮小したグレー画像で、変化した画素の数）。

    ignore の領域（画面に対する割合）は数えない。region（実画面のピクセル矩形）を渡すと、その中だけを数える。
    縮小の倍率は region があってもなくても同じなので、閾値の意味は変わらない。
    """
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
    box = _region_box(a.size, region)
    if box:
        diff = diff.crop(box)
    return diff.histogram()[255]


def changed(a: Image.Image | None, b: Image.Image | None, min_pixels: int = 30, ignore=None, region=None) -> bool:
    """ほぼ同じ画面なら False。誤って「変化なし」とすると、効いたクリックを捨ててしまうので閾値は小さめ。

    region を渡すと、その範囲（前面アプリのウィンドウ）の中だけで比べる。
    """
    if a is None or b is None:
        return True
    return diff_pixels(a, b, ignore, region) >= min_pixels


def settle(stop_event: threading.Event | None = None, min_wait: float = 0.15, max_wait: float = 1.5,
           poll: float = 0.12, quiet_pixels: int = 150, ignore=None, region=None) -> Image.Image | None:
    """固定の sleep ではなく、画面の変化が止まるまで待つ。落ち着いた（または max_wait に達した）画面を返す。

    小さな動き（点滅するカーソル、スピナー）は quiet_pixels 未満なら落ち着いたとみなす。
    region は「比べる範囲を返す関数」（前面ウィンドウの矩形。None なら画面全体）。
    他のウィンドウで数字が動き続けていても、待ち時間が最大まで延びないようにするため。
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
        if diff_pixels(prev, cur, ignore, region() if region else None) < quiet_pixels:
            return cur
        prev = cur
    return prev


COLORS = [(255, 40, 40), (0, 200, 255), (60, 220, 60), (255, 170, 0), (220, 80, 255), (255, 255, 0)]
MARK_COLORS = [(230, 0, 120), (0, 110, 230), (0, 150, 70), (200, 90, 0), (120, 40, 200)]


def draw_marks(img: Image.Image, marks, screen_size: tuple[int, int]) -> Image.Image:
    """番号で選ばせる（Set-of-Mark）ための、番号付きの枠を描いたコピー。

    marks は uia.Mark（実画面のピクセルの矩形）。モデルに送る画像の大きさに合わせて縮める。
    番号の札は枠の左上の外側（はみ出すときは内側）に、小さく描く（中身の文字をなるべく隠さない）。
    """
    out = img.copy().convert("RGB")
    d = ImageDraw.Draw(out, "RGBA")
    sx, sy = out.width / screen_size[0], out.height / screen_size[1]
    font = _font(max(11, out.width // 120))
    for m in marks:
        c = MARK_COLORS[m.id % len(MARK_COLORS)]
        l, t, r, b = m.rect
        box = [l * sx, t * sy, r * sx - 1, b * sy - 1]
        d.rectangle(box, outline=(*c, 255), width=2)
        tag = str(m.id)
        tw = d.textlength(tag, font=font) + 6
        th = font.size + 4
        tx, ty = box[0], box[1] - th
        if ty < 0:
            ty = box[1]
        d.rectangle([tx, ty, tx + tw, ty + th], fill=(*c, 235))
        d.text((tx + 3, ty + 1), tag, font=font, fill=(255, 255, 255, 255))
    return out


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
