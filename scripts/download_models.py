"""音声認識のモデルを models/ にダウンロードする（展示会場でネットに出られなくても動くように、事前に1回実行）。

  python -m uv run python scripts/download_models.py                  # config.yaml の voice.stt.model
  python -m uv run python scripts/download_models.py large-v3-turbo   # 名前を指定（複数可）
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from voice_agent import config  # noqa: E402


def main():
    names = sys.argv[1:] or [((config.load().get("voice") or {}).get("stt") or {}).get("model", "small")]
    from faster_whisper.utils import download_model
    for name in names:
        t = time.time()
        print(f"{name} をダウンロードしています（初回は数百MB〜1.6GB）…", flush=True)
        path = download_model(name, cache_dir=str(ROOT / "models"))
        print(f"  完了（{time.time() - t:.0f} 秒）: {path}", flush=True)


if __name__ == "__main__":
    main()
