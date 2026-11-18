"""DPI 対応の宣言。座標ずれを防ぐため、画面や入力のライブラリより先に呼ぶこと。

voice_agent パッケージの import 時に自動で呼ばれる（__init__.py）。
"""
import ctypes

_done = False


def enable() -> str:
    """Per-Monitor DPI 対応を宣言する。戻り値は結果の説明（ログ用）。"""
    global _done
    if _done:
        return "already"
    _done = True
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        return "per-monitor"
    except OSError:
        # すでに宣言済み（E_ACCESSDENIED）か、古い Windows
        try:
            ctypes.windll.user32.SetProcessDPIAware()
            return "system"
        except Exception:
            return "failed"
