import pytest

from voice_agent import coords


@pytest.mark.parametrize("text,expected", [
    ('{"x": 500, "y": 250, "found": true}', (500, 250, "xy")),
    ('```json\n{"x": "120", "y": 980}\n```', (120, 980, "xy")),
    ('{"point": [100, 200]}', (100, 200, "point")),
    ('{"bbox": [100, 200, 300, 400]}', (200, 300, "bbox")),
    ("<|begin_of_box|>[100,200,300,400]<|end_of_box|>", (200, 300, "box_token")),
    ("<|begin_of_box|>[10, 20]<|end_of_box|>", (10, 20, "box_token")),
    ("<think>maybe {\"x\": 1, \"y\": 1}</think>{\"x\": 7, \"y\": 8}", (7, 8, "xy")),
    ("the button is at [640, 360]", (640, 360, "loose")),
])
def test_parse_point(text, expected):
    assert coords.parse_point(text) == expected


@pytest.mark.parametrize("text", ["", "no idea", '{"found": false}', "{broken"])
def test_parse_point_unreadable(text):
    assert coords.parse_point(text) is None


def test_to_screen_1080p():
    assert coords.to_screen(0, 0, 1920, 1080) == (0, 0)
    assert coords.to_screen(500, 500, 1920, 1080) == (960, 540)
    assert coords.to_screen(1000, 1000, 1920, 1080) == (1919, 1079)  # 画面外にはみ出さない


def test_round_trip():
    for px, py in [(0, 0), (960, 540), (1234, 567)]:
        gx, gy = coords.to_grid(px, py, 1920, 1080)
        sx, sy = coords.to_screen(gx, gy, 1920, 1080)
        assert abs(sx - px) <= 1 and abs(sy - py) <= 1


def test_in_grid():
    assert coords.in_grid(0, 1000)
    assert not coords.in_grid(-1, 10)
    assert not coords.in_grid(10, 1001)
