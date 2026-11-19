"""確認ダイアログの後始末（Tk を別スレッドで回収しても落ちないこと）。

フェーズ6の実地テストで、確認ダイアログの後のモデル呼び出し中に
「Tcl_AsyncDelete: async handler deleted by the wrong thread」でプロセスごと落ちた。
落ちると pytest ごと止まるので、別プロセスで再現して終了コードを見る。
ダイアログは timeout_secs=0 で一瞬だけ出て、すぐ「時間切れ」で閉じる（フォーカスは奪わない）。
"""
import subprocess
import sys
import textwrap

SCRIPT = textwrap.dedent("""
    import gc, threading, tkinter
    from voice_agent import confirm_ui, winapi
    winapi.activate = lambda h: True        # 前面化はしない
    winapi.foreground = lambda: None
    tkinter.Misc.focus_force = lambda self: None
    gc.disable()                            # 自動 GC を止め、下で別スレッドから回収させる
    ok = confirm_ui.ask("test", timeout_secs=0)
    t = threading.Thread(target=gc.collect)
    t.start(); t.join()
    print(ok, confirm_ui.last_answer)
""")


def test_dialog_survives_gc_in_other_thread():
    p = subprocess.run([sys.executable, "-c", SCRIPT], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60)
    assert p.returncode == 0, p.stderr
    assert "Tcl_AsyncDelete" not in p.stderr
    assert p.stdout.split() == ["False", "timeout"]
