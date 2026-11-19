from PIL import Image, ImageDraw

from voice_agent import screen


def img(color=(30, 30, 30)):
    return Image.new("RGB", (1920, 1080), color)


def test_identical_is_not_changed():
    assert not screen.changed(img(), img())


def test_clock_area_is_ignored():
    a, b = img(), img()
    ImageDraw.Draw(b).rectangle([1700, 1040, 1910, 1075], fill=(255, 255, 255))  # 時計の数字が変わった
    assert not screen.changed(a, b)
    assert screen.diff_pixels(a, b, ignore=[]) > 0


def test_window_change_is_changed():
    a, b = img(), img()
    ImageDraw.Draw(b).rectangle([400, 300, 900, 700], fill=(255, 255, 255))
    assert screen.changed(a, b)


def test_tiny_change_is_quiet():
    a, b = img(), img()
    ImageDraw.Draw(b).rectangle([500, 500, 502, 520], fill=(255, 255, 255))  # 点滅カーソル程度
    assert screen.diff_pixels(a, b) < 150


WINDOW = (600, 300, 1400, 800)   # 前面ウィンドウの矩形（実画面ピクセル）


def test_change_outside_front_window_is_ignored():
    a, b = img(), img()
    ImageDraw.Draw(b).rectangle([1500, 200, 1800, 450], fill=(255, 255, 255))  # 横の監視ツールの数字
    assert screen.changed(a, b)                       # 画面全体なら「変わった」
    assert not screen.changed(a, b, region=WINDOW)    # 前面ウィンドウの中だけなら「変わらない」


def test_change_inside_front_window_is_changed():
    a, b = img(), img()
    ImageDraw.Draw(b).rectangle([620, 330, 700, 350], fill=(255, 255, 255))   # 「テスト」程度の文字
    assert screen.changed(a, b, region=WINDOW)


def test_region_offscreen_falls_back_to_full_screen():
    a, b = img(), img()
    ImageDraw.Draw(b).rectangle([400, 300, 900, 700], fill=(255, 255, 255))
    assert screen.changed(a, b, region=(-32000, -32000, -31800, -31900))   # 最小化中の座標
    assert screen.changed(a, b, region=(-8, -8, 1928, 1088))               # 最大化（はみ出し）は切り詰め


def test_for_model_size():
    assert screen.for_model(img(), 1512).size == (1512, 850)
    assert screen.for_model(img(), 0).size == (1920, 1080)
