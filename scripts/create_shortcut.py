"""デスクトップに「音声操作エージェント」のショートカット（アイコン）を作る。

  python -m uv run python scripts/create_shortcut.py           # 音声モード（本番）: start_voice.bat
  python -m uv run python scripts/create_shortcut.py --dryrun  # リハーサル: start_voice_dryrun.bat も作る

ダブルクリックすると start_voice.bat が開き、次と同じことを行う:
  cd /d <このフォルダ> && python -m uv run voice-agent --voice --live
アイコンの画像は assets/voice-agent.ico（なければ作る）。
"""
import argparse
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
ICON = ROOT / "assets" / "voice-agent.ico"


def make_icon(path: Path = ICON, size: int = 256) -> Path:
    """青い角丸の四角に白いマイク（状態表示のバーのマイクと同じ形）。"""
    s = size / 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 背景: 上から下へ明るい青 → 濃い青
    bg = Image.new("RGBA", (size, size))
    bd = ImageDraw.Draw(bg)
    for y in range(size):
        k = y / size
        bd.line([(0, y), (size, y)], fill=(int(40 - 20 * k), int(150 - 60 * k), int(255 - 40 * k), 255))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([2 * s, 2 * s, 62 * s, 62 * s], radius=int(14 * s), fill=255)
    img.paste(bg, (0, 0), mask)
    # マイク
    white = (255, 255, 255, 255)
    cx = 32 * s
    d.rounded_rectangle([cx - 8 * s, 12 * s, cx + 8 * s, 38 * s], radius=int(8 * s), fill=white)
    d.arc([cx - 15 * s, 18 * s, cx + 15 * s, 44 * s], 0, 180, fill=white, width=int(3.5 * s))
    d.line([cx, 44 * s, cx, 51 * s], fill=white, width=int(3.5 * s))
    d.line([cx - 8 * s, 52 * s, cx + 8 * s, 52 * s], fill=white, width=int(3.5 * s))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    return path


def make_shortcut(name: str, bat: str, icon: Path, description: str) -> Path:
    import win32com.client
    shell = win32com.client.Dispatch("WScript.Shell")
    desktop = Path(shell.SpecialFolders("Desktop"))       # OneDrive に移したデスクトップにも対応
    lnk = desktop / f"{name}.lnk"
    sc = shell.CreateShortcut(str(lnk))
    sc.TargetPath = str(ROOT / bat)
    sc.WorkingDirectory = str(ROOT)
    sc.IconLocation = f"{icon},0"
    sc.Description = description
    sc.WindowStyle = 1
    sc.Save()
    return lnk


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dryrun", action="store_true", help="リハーサル用（操作しない）のショートカットも作る")
    args = ap.parse_args()
    icon = ICON if ICON.exists() else make_icon()
    made = [make_shortcut("音声操作エージェント", "start_voice.bat", icon,
                          "F9 を押しながら話すと、PC を操作します（実際に操作します）")]
    if args.dryrun:
        made.append(make_shortcut("音声操作エージェント（リハーサル）", "start_voice_dryrun.bat", icon,
                                  "何をするかを表示するだけで、マウスもキーボードも動かしません"))
    for p in made:
        print(f"作成しました: {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
