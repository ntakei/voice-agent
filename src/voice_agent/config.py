"""config.yaml と .env の読み込み。

秘密情報（APIキー）は .env の VA_API_KEY からのみ読み、ログや表示には出さない。
"""
import os
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config.yaml"
RUNS_DIR = ROOT / "runs"

# .env の値で上書きできる項目: 環境変数名 -> (セクション, キー)
ENV_OVERRIDES = {
    "VA_BASE_URL": ("model", "base_url"),
    "VA_MODEL": ("model", "name"),
}


@lru_cache(maxsize=1)
def load(path: str | None = None) -> dict:
    load_dotenv(ROOT / ".env")
    with open(path or CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    for env, (sec, key) in ENV_OVERRIDES.items():
        if os.environ.get(env):
            cfg.setdefault(sec, {})[key] = os.environ[env]
    return cfg


def api_key() -> str:
    """vLLM は認証なしなので、未設定ならダミー値を返す（openai SDK が空文字を受け付けないため）。"""
    load()
    return os.environ.get("VA_API_KEY") or "EMPTY"
