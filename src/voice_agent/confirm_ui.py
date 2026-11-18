"""人間の確認ダイアログ（最前面の「はい／いいえ」）。

- 既定のボタンは「いいえ」。時間切れ・ウィンドウを閉じる・Esc も「いいえ」。
- 閉じた後、ダイアログを出す前の前面ウィンドウにフォーカスを戻す（その後のキー入力が正しい宛先に届くように）。
"""
import tkinter as tk

from . import winapi


def ask(question: str, title: str = "操作の確認", timeout_secs: float = 30) -> bool:
    prev = winapi.foreground()
    answer = {"ok": False}

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

    def finish(ok: bool):
        answer["ok"] = ok
        root.destroy()

    yes = tk.Button(buttons, text="はい、実行する", font=font, width=14, command=lambda: finish(True))
    no = tk.Button(buttons, text="いいえ（中止）", font=font, width=14, command=lambda: finish(False))
    yes.pack(side="left", padx=8)
    no.pack(side="left", padx=8)
    root.protocol("WM_DELETE_WINDOW", lambda: finish(False))
    root.bind("<Escape>", lambda e: finish(False))

    # 画面中央に出して、いいえにフォーカス
    root.update_idletasks()
    w, h = root.winfo_width(), root.winfo_height()
    root.geometry(f"+{(root.winfo_screenwidth() - w) // 2}+{(root.winfo_screenheight() - h) // 3}")
    root.lift()
    root.focus_force()
    no.focus_set()

    left = {"s": int(timeout_secs)}

    def tick():
        if left["s"] <= 0:
            finish(False)
            return
        remain.config(text=f"{left['s']} 秒以内に答えがなければ中止します")
        left["s"] -= 1
        root.after(1000, tick)
    tick()
    root.mainloop()

    if prev is not None:
        winapi.activate(prev.hwnd)
    return answer["ok"]


if __name__ == "__main__":  # 見た目の確認用: python -m voice_agent.confirm_ui
    print(ask("次の操作を実行してよいですか？\n\nクリック (100,200)\n理由: 送信ボタンを押す\n\n確認が必要な理由: 理由に「送信」が含まれる",
              timeout_secs=15))
