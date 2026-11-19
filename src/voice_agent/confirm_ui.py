"""人間の確認ダイアログ（最前面の「はい／いいえ」）。

- 既定のボタンは「いいえ」。時間切れ・ウィンドウを閉じる・Esc も「いいえ」。
- 閉じた後、ダイアログを出す前の前面ウィンドウにフォーカスを戻す（その後のキー入力が正しい宛先に届くように）。
- last_answer に「どう答えたか」（yes / no / timeout / closed）を残す（ログ用）。
"""
import gc
import tkinter as tk

from . import winapi

last_answer = ""


def ask(question: str, title: str = "操作の確認", timeout_secs: float = 30) -> bool:
    global last_answer
    prev = winapi.foreground()
    answer = _run_dialog(question, title, timeout_secs)
    # Tk のオブジェクトは循環参照で残る。別スレッド（モデル呼び出し）の GC で回収されると
    # 「Tcl_AsyncDelete: async handler deleted by the wrong thread」でプロセスごと落ちるので、
    # 作ったこのスレッドで今すぐ回収しておく（フェーズ6で発生）。
    gc.collect()
    last_answer = answer["how"]
    if prev is not None:
        winapi.activate(prev.hwnd)
    return answer["ok"]


def _run_dialog(question: str, title: str, timeout_secs: float) -> dict:
    answer = {"ok": False, "how": "closed"}

    root = tk.Tk()
    root.title(title)
    root.attributes("-topmost", True)
    root.resizable(False, False)
    font = ("Yu Gothic UI", 14)

    frame = tk.Frame(root, padx=24, pady=20)
    frame.pack()
    tk.Label(frame, text=question, font=font, justify="left", wraplength=640).pack(anchor="w")
    remain = tk.Label(frame, text="", font=("Yu Gothic UI", 10), fg="#666")
    remain.pack(anchor="w", pady=(8, 0))
    buttons = tk.Frame(frame, pady=16)
    buttons.pack()

    def finish(ok: bool, how: str):
        answer.update(ok=ok, how=how)
        root.destroy()

    yes = tk.Button(buttons, text="はい、実行する", font=font, width=14, command=lambda: finish(True, "yes"))
    no = tk.Button(buttons, text="いいえ（中止）", font=font, width=14, command=lambda: finish(False, "no"))
    yes.pack(side="left", padx=8)
    no.pack(side="left", padx=8)
    root.protocol("WM_DELETE_WINDOW", lambda: finish(False, "closed"))
    root.bind("<Escape>", lambda e: finish(False, "no"))

    # 画面中央に出して、いいえにフォーカス
    root.update_idletasks()
    w, h = root.winfo_width(), root.winfo_height()
    root.geometry(f"+{(root.winfo_screenwidth() - w) // 2}+{(root.winfo_screenheight() - h) // 3}")
    root.lift()
    root.update()
    winapi.activate(int(root.wm_frame(), 16))  # 他のアプリの裏に隠れないよう前面化
    root.focus_force()
    no.focus_set()

    left = {"s": int(timeout_secs)}

    def tick():
        if left["s"] <= 0:
            finish(False, "timeout")
            return
        remain.config(text=f"{left['s']} 秒以内に答えがなければ中止します")
        left["s"] -= 1
        root.after(1000, tick)
    tick()
    root.mainloop()
    return answer


if __name__ == "__main__":  # 見た目の確認用: python -m voice_agent.confirm_ui
    r = ask("次の操作を実行してよいですか？\n\nクリック (100,200)\n理由: 送信ボタンを押す\n\n"
            "確認が必要な理由: 理由に「送信」が含まれる", timeout_secs=15)
    print(r, last_answer)
