"""音声で操作する Windows 用コンピュータ操作エージェント。"""
from . import dpi

# プロセス開始時（このパッケージを最初に import した時点）で DPI 対応を宣言する。
DPI_MODE = dpi.enable()


def main() -> None:
    from .main import main as _main  # フェーズ5で作成
    _main()
