"""README 用に、状態表示のバー（overlay.py）の見本画像を作る。ウィンドウは出さず、描画だけを行う。

  python -m uv run python scripts/render_overlay_states.py      # docs/overlay_states.png を作り直す

バーの見た目を変えたら、これを実行して画像を更新する。
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from voice_agent.overlay import Renderer  # noqa: E402

WAVE = [0.01, 0.03, 0.05, 0.08, 0.06, 0.02, 0.07, 0.09, 0.04, 0.02] * 4
ROWS = [
    ("ready", "F9 を押しながら話してください", "待機中", None),
    ("listening", "聞いています…  キーを離すと認識します", "", WAVE),
    ("correcting", "「密集情報のホームページを開いて」", "聞き間違いがないか確認しています…", None),
    ("looking", "画面を見ています…", "三井情報のホームページを開いて ・ ステップ 3/25", None),
    ("thinking", "考え直しています…", "三井情報のホームページを開いて ・ ステップ 6/25", None),
    ("acting", "クリック「資料ダウンロード」", "資料ダウンロードのページを開く ・ ステップ 4/25", None),
    ("confirm", "確認してください", "ダイアログで答えてください ・ キー delete（送信・削除などに使われるキー）", None),
    ("done", "完了しました", "三井情報の資料ダウンロードページが表示された", None),
    ("fail", "うまくできませんでした", "行き詰まった（ブロックが 4 回）", None),
    ("stopped", "停止しました", "停止要求（Esc）", None),
]


def main(out: Path = ROOT / "docs" / "overlay_states.png") -> Path:
    r = Renderer(760, 76, 1.0, ["F9 話す", "Esc 停止"])
    pad, gap = 20, 14
    h = pad * 2 + len(ROWS) * r.h + (len(ROWS) - 1) * gap
    # 背景はデスクトップのような落ち着いたグラデーション（バーの半透明が分かるように）
    bg = Image.new("RGBA", (r.w + pad * 2, h), (0, 0, 0, 255))
    d = ImageDraw.Draw(bg)
    for y in range(h):
        k = y / h
        d.line([(0, y), (bg.width, y)], fill=(int(70 + 40 * k), int(95 + 35 * k), int(140 + 30 * k), 255))
    for i, (state, main_, sub, levels) in enumerate(ROWS):
        # 回転の表示が分かりやすい角度で描く（t は秒。弧の位置が変わる）
        bg.alpha_composite(r.render(state, main_, sub, t=0.55, levels=levels), (pad, pad + i * (r.h + gap)))
    out.parent.mkdir(parents=True, exist_ok=True)
    bg.convert("RGB").save(out, optimize=True)
    return out


if __name__ == "__main__":
    print(main())
