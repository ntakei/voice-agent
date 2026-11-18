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


def test_for_model_size():
    assert screen.for_model(img(), 1512).size == (1512, 850)
    assert screen.for_model(img(), 0).size == (1920, 1080)
