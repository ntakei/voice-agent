"""操作ループ: スクリーンショット → モデルが次の1手を JSON で返す → 検証 → 実行 → 画面が落ち着くのを待つ → 繰り返し。

音声・読み上げ・確認ダイアログとは Hooks を通してだけやり取りする（疎結合）。
安全ルールは check_safety()（safety.py）で、実行の直前にコードで掛ける。
"""
import threading
import time

import pyautogui

from . import actions, config, prompts, safety, screen, uia, winapi
from .coords import parse_json
from .executor import Executor
from .logbook import Session
from .model_client import ModelClient


HARD_MAX_STEPS = 25  # 設定や引数でこれより大きくはできない


class Stopped(Exception):
    """停止要求（Esc など）でループを抜ける。"""


class Hooks:
    """エージェントから外部（画面表示・読み上げ・確認）への通知。既定はコンソール表示のみ。"""

    def status(self, state: str, text: str) -> None:
        """state: start | looking | thinking | acting | blocked | done | fail | stopped"""
        print(text, flush=True)

    def say(self, text: str, kind: str = "info") -> None:
        """読み上げ。kind: start | reason | confirm | blocked | result（読み上げる種類を選べるように）。"""

    def confirm(self, question: str) -> bool:
        """人間の確認。True なら実行してよい。既定（つながっていない場合）は拒否。"""
        return False


def unverified_done(history: list[dict]) -> str:
    """モデルが done と言ったとき、効果を確かめられない操作があれば警告文を返す（なければ ""）。

    「完了」はモデルの自己申告なので、次の操作を疑う（結果は書き換えず、警告だけ出す）:
    - 画面が変わらなかった入力・キー操作（内容を変えるはずの操作）
    - done の直前の操作で、画面が変わらなかったもの
    """
    done_actions = [h for h in history if h.get("action")]
    suspects = [h for h in done_actions if h["changed"] is False and h["action"] in ("type", "key")]
    if done_actions and done_actions[-1]["changed"] is False and done_actions[-1] not in suspects:
        suspects.append(done_actions[-1])
    if not suspects:
        return ""
    labels = "、".join(h["label"] for h in suspects)
    return f"完了と判断されましたが、画面が変わらなかった操作があります（{labels}）。結果を確認してください。"


class Agent:
    def __init__(self, cfg: dict | None = None, hooks: Hooks | None = None, dry_run: bool | None = None,
                 max_steps: int | None = None, think_normal: str | None = None, think_stuck: str | None = None,
                 client: ModelClient | None = None):
        self.cfg = cfg or config.load()
        ac = self.cfg.get("agent", {})
        mc = self.cfg.get("model", {})
        self.hooks = hooks or Hooks()
        self.dry_run = ac.get("dry_run", True) if dry_run is None else dry_run
        self.max_steps = min(max_steps or ac.get("max_steps", HARD_MAX_STEPS), HARD_MAX_STEPS)
        self.think_normal = think_normal or mc.get("thinking_normal", "off")
        self.think_stuck = think_stuck or mc.get("thinking_stuck", "on")
        self.apps = actions.load_apps(self.cfg)
        self.policy = safety.SafetyPolicy(self.cfg, self.apps)
        self.client = client or ModelClient(self.cfg)
        self.stop_event = threading.Event()

    # ---- 外部から ----

    def stop(self) -> None:
        """即時停止の要求。進行中のモデル呼び出しは待たずに捨てる。"""
        self.stop_event.set()

    # ---- 安全ルール ----

    def check_safety(self, a: dict, front: winapi.WindowInfo | None, point: tuple[int, int] | None) -> safety.Decision:
        """OS から要素情報を取って安全ルールを判定する。"""
        clicked = uia.element_at(*point) if point and a["action"] in actions.CLICKS else None
        focused = uia.focused_element() if a["action"] in ("type", "key") else None
        d = self.policy.check(a, front, focused=focused, clicked=clicked)
        d.clicked, d.focused = clicked, focused  # ログ用
        return d

    def failsafe_check(self) -> None:
        """マウスが画面の隅にあれば FailSafeException（ステップの合間でも止められるように）。"""
        pyautogui.failSafeCheck()

    # ---- 内部 ----

    def _call(self, fn, *args, **kwargs):
        """モデル呼び出しを別スレッドで行い、停止要求が来たら結果を待たずに抜ける。"""
        box = {}

        def work():
            try:
                box["r"] = fn(*args, **kwargs)
            except Exception as e:  # noqa: BLE001
                box["e"] = e
        th = threading.Thread(target=work, daemon=True)
        th.start()
        while th.is_alive():
            if self.stop_event.wait(0.03):
                raise Stopped()
        if "e" in box:
            raise box["e"]
        return box["r"]

    @staticmethod
    def _front_rect():
        f = winapi.foreground()
        return winapi.window_rect(f.hwnd) if f else None

    @staticmethod
    def _compare(prev_img, img, prev_front: int | None, front: winapi.WindowInfo | None) -> tuple[bool | None, str]:
        """直前の操作で画面が変わったか。前面アプリのウィンドウの中だけで比べる。

        他のウィンドウ（時計・監視ツール・通知など）の変化で「変わった」と誤判定しないため。
        前面ウィンドウ自体が切り替わったら「変わった」。矩形が取れなければ画面全体で比べる。
        """
        if prev_img is None:
            return None, ""
        if prev_front is not None and front is not None and front.hwnd != prev_front:
            return True, "前面ウィンドウが切り替わった"
        region = winapi.window_rect(front.hwnd) if front else None
        return screen.changed(prev_img, img, region=region), ("前面ウィンドウ内" if region else "画面全体")

    def _front_name(self, front: winapi.WindowInfo | None) -> str:
        if not front:
            return ""
        app = actions.app_for_process(front.exe, self.apps)
        return f"{front.exe} ({app.names[0]})" if app else front.exe

    def run(self, task: str, source: str = "text", audio_wav: bytes | None = None,
            extra_meta: dict | None = None) -> dict:
        self.stop_event.clear()
        ac = self.cfg.get("agent", {})
        sc = self.cfg.get("screen", {})
        width = sc.get("model_image_width", 1512)
        quality = sc.get("jpeg_quality", 90)
        hist_n = ac.get("history_steps", 8)
        screen_size = screen.screen_size()
        ex = Executor(screen_size, self.apps, dry_run=self.dry_run, stop_event=self.stop_event)
        s = Session(task, source, meta={
            "model": self.cfg["model"]["name"], "dry_run": self.dry_run, "max_steps": self.max_steps,
            "think_normal": self.think_normal, "think_stuck": self.think_stuck,
            "image_width": width, "screen": f"{screen_size[0]}x{screen_size[1]}", **(extra_meta or {})})
        if audio_wav:
            s.path("audio.wav").write_bytes(audio_wav)   # 音声で指示したとき、レポートで聞き直せるように
        self.session = s

        history: list[dict] = []          # {"line": str, "changed": bool|None}
        blocked: list[tuple] = []         # 効果がなかった操作の signature
        blocked_labels: list[str] = []
        fails: dict[tuple, int] = {}
        done_sigs: list[tuple] = []
        prev_img, prev_sig, next_img, prev_front = None, None, None, None
        warnings: list[str] = []
        no_change_run = bad_json_run = blocked_run = 0
        result, reason = "fail", f"最大ステップ数（{self.max_steps}）に達した"

        self.hooks.status("start", f"タスク: {task}" + ("（dry-run）" if self.dry_run else ""))
        self.hooks.say("わかりました。", "start")
        try:
            for step in range(1, self.max_steps + 1):
                if self.stop_event.is_set():
                    raise Stopped()
                self.failsafe_check()
                img = next_img if next_img is not None else screen.capture()
                next_img = None
                front = winapi.foreground()
                front_name = self._front_name(front)
                changed, compare = self._compare(prev_img, img, prev_front, front)
                prev_front = front.hwnd if front else None
                if prev_sig is not None and history:
                    history[-1]["changed"] = changed
                    if changed is False and not self.dry_run:
                        no_change_run += 1
                        fails[prev_sig] = fails.get(prev_sig, 0) + 1
                        if fails[prev_sig] >= 2 and prev_sig not in blocked:
                            blocked.append(prev_sig)
                            blocked_labels.append(history[-1]["label"])
                    else:
                        no_change_run = 0
                prev_img = img

                sent = screen.for_model(img, width)
                stuck = (no_change_run >= ac.get("think_after_no_change", 2)
                         or bad_json_run >= ac.get("think_after_bad_json", 2)
                         or blocked_run >= ac.get("think_after_blocked", 2))
                thinking = self.think_stuck if stuck else self.think_normal
                recent = history[-hist_n:]
                lines = [h["line"] + {True: "  [screen changed]", False: "  [screen did NOT change]"}.get(h["changed"], "")
                         for h in recent]
                prompt = prompts.user_prompt(task, lines, front_name, changed if prev_sig else None,
                                             blocked_labels, first_index=len(history) - len(recent) + 1)
                system = prompts.system_prompt(sent.size, [a.names[0] for a in self.apps])
                self.hooks.status("thinking" if stuck else "looking",
                                  f"[{step:02d}] {'行き詰まったので考え直しています…' if stuck else '画面を見ています…'}")

                sent_name, marked_name = f"step-{step:02d}-sent.jpg", f"step-{step:02d}-marked.jpg"
                sent.save(s.path(sent_name), quality=quality)
                base = {"step": step, "thinking": thinking, "front_app": front_name, "changed_before": changed,
                        "dry_run": self.dry_run, "sent": sent_name}
                if compare:
                    base["compared"] = compare
                try:
                    r = self._call(self.client.chat,
                                   [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                                   image=sent, thinking=thinking, image_quality=quality)
                except Stopped:
                    raise
                except Exception as e:  # noqa: BLE001
                    s.log_step({**base, "label": "モデル呼び出しエラー", "error": f"{type(e).__name__}: {e}"})
                    result, reason = "fail", f"モデル呼び出しエラー: {type(e).__name__}"
                    break
                record = {"system": system, "prompt": prompt, "reply": r["content"], "reasoning": r["reasoning"],
                          "finish_reason": r["finish_reason"], "usage": r["usage"], "thinking": thinking}
                base.update(model_secs=r["secs"], completion_tokens=r["usage"].get("completion_tokens"),
                            raw=r["content"])

                a = parse_json(r["content"])
                if a is None:
                    bad_json_run += 1
                    history.append({"line": "invalid reply (not JSON)", "changed": None, "label": "無効な返答"})
                    s.log_step({**base, "label": "無効な返答", "error": "JSON として読めない"}, record)
                    prev_sig = None
                    continue
                a = actions.normalize(a)
                err = actions.validate(a, sent.size, self.apps)
                if err:
                    bad_json_run += 1
                    history.append({"line": f"invalid action: {err}", "changed": None, "label": "無効な操作"})
                    s.log_step({**base, "action": a, "label": "無効な操作", "error": err}, record)
                    prev_sig = None
                    continue
                bad_json_run = 0

                notes = []
                if a["action"] == "type" and (a.get("x") is not None or a.get("y") is not None):
                    notes.append(f"type に座標 ({a.get('x')}, {a.get('y')}) が付いていた（使わずに無視）")

                # 注釈画像（モデルが見た画像に印）
                label = actions.describe(a)
                if a["action"] in actions.CLICKS or (a["action"] == "scroll" and a.get("x") is not None):
                    marked = screen.mark_points(sent, [(a["x"], a["y"], label)])
                else:
                    marked = screen.caption(sent, label)
                marked.save(s.path(marked_name), quality=85)
                row = {**base, "action": a, "label": label, "marked": marked_name, "notes": notes}

                t = a["action"]
                if t in ("done", "fail"):
                    if t == "done" and not self.dry_run:
                        warn = unverified_done(history)
                        if warn:
                            row["warning"] = warn
                            warnings.append(warn)
                    s.log_step(row, record)
                    result, reason = ("done", a.get("reason", "")) if t == "done" else ("fail", a.get("reason", ""))
                    break

                # 繰り返しの抑止
                sig = actions.signature(a)
                if sig in blocked:
                    blocked_run += 1
                    row["error"] = "ブロック: 効果がなかった操作の繰り返し"
                    history.append({"line": f"{label} -> BLOCKED (already tried; it did not change the screen)",
                                    "changed": None, "label": label})
                    s.log_step(row, record)
                    self.hooks.status("blocked", f"[{step:02d}] {row['error']}: {label}")
                    prev_sig = None
                    continue
                if len(done_sigs) >= 2 and done_sigs[-1] == sig and done_sigs[-2] == sig:
                    blocked_run += 1
                    row["error"] = "ブロック: 同じ操作の3回連続"
                    history.append({"line": f"{label} -> BLOCKED: done twice already. Do the NEXT step of the task.",
                                    "changed": None, "label": label})
                    s.log_step(row, record)
                    self.hooks.status("blocked", f"[{step:02d}] {row['error']}: {label}")
                    prev_sig = None
                    continue

                # 安全ルール（コードで強制）
                point = (actions.to_screen_point(a["x"], a["y"], sent.size, screen_size)
                         if a.get("x") is not None and t in actions.CLICKS + ("scroll",) else None)
                d = self.check_safety(a, front, point)
                for k in ("clicked", "focused"):
                    el = getattr(d, k, None)
                    if el is not None:
                        row[k] = el.describe()
                if d.kind in (safety.BLOCK, safety.STOP):
                    blocked_run += 1
                    row.update(error=f"ブロック: {d.reason}", guard=d.kind)
                    history.append({"line": f"{label} -> BLOCKED: {d.reason}", "changed": None, "label": label})
                    s.log_step(row, record)
                    self.hooks.status("blocked", f"[{step:02d}] ブロック: {d.reason}")
                    if d.kind == safety.STOP:
                        self.hooks.say("この操作はできません。", "blocked")
                        result, reason = "stopped", d.reason
                        break
                    prev_sig = None
                    continue
                if d.kind == safety.CONFIRM:
                    row["confirm_reason"] = d.reason
                    if self.dry_run:
                        row["notes"].append(f"本番では実行前に確認が必要: {d.reason}")
                        self.hooks.status("confirm", f"[{step:02d}] [dry-run] 本番では確認が必要: {d.reason}")
                    else:
                        question = (f"次の操作を実行してよいですか？\n\n{label}\n"
                                    f"理由: {a.get('reason', '')}\n\n確認が必要な理由: {d.reason}")
                        self.hooks.status("confirm", f"[{step:02d}] 確認待ち: {label}（{d.reason}）")
                        self.hooks.say("この操作を実行してよいか、確認してください。", "confirm")
                        ok = bool(self.hooks.confirm(question))
                        row["confirmed"] = ok
                        how = getattr(self.hooks, "last_answer", "")
                        if how:
                            row["confirm_answer"] = how  # yes / no / timeout / closed
                        if not ok:
                            s.log_step(row, record)
                            result, reason = "stopped", (f"確認が時間切れ: {label}" if how == "timeout"
                                                         else f"確認で拒否された: {label}")
                            break
                blocked_run = 0

                if self.stop_event.is_set():
                    s.log_step(row, record)
                    raise Stopped()
                self.hooks.status("acting", f"[{step:02d}] {r['secs']:.1f}s {'(思考 ' + thinking + ') ' if thinking != 'off' else ''}"
                                            f"{label} — {a.get('reason', '')}")
                self.hooks.say(a.get("reason", ""), "reason")
                try:
                    res = ex.execute(a, sent.size)
                except pyautogui.FailSafeException:
                    s.log_step({**row, "error": "FAILSAFE"}, record)
                    raise
                except Exception as e:  # noqa: BLE001  実行中の失敗もそのステップのログに残してから止める
                    row["error"] = f"実行中のエラー: {type(e).__name__}: {e}"
                    s.log_step(row, record)
                    result, reason = "fail", row["error"]
                    break
                row.update(label=res.label, point=res.point, executed=res.executed, detail=res.detail)
                if self.dry_run:
                    self.hooks.status("acting", f"     [dry-run] {res.label}" + (f"（{res.detail}）" if res.detail else ""))
                elif res.detail:
                    self.hooks.status("acting", f"     {res.detail}")
                done_sigs.append(sig)
                line = f"{label} — {a.get('reason', '')}"
                if not self.dry_run and not res.executed and res.detail.startswith("起動に失敗"):
                    line += f" -> FAILED: {res.detail}"

                if not self.dry_run:
                    wait = ac.get("settle_max_open_app", 4.0) if t == "open_app" else ac.get("settle_max", 1.5)
                    next_img = screen.settle(self.stop_event, max_wait=wait, region=self._front_rect)
                    if next_img is None:
                        s.log_step(row, record)
                        raise Stopped()
                    if t == "open_app":
                        app = actions.resolve_app(a["text"], self.apps)
                        now = winapi.foreground()
                        if app and not (now and now.exe.lower() in {p.lower() for p in app.process}):
                            line += f" — but {now.exe if now else 'another app'} is still in front"
                            row["notes"].append(f"open_app 後も前面は {now.describe() if now else '不明'}")
                s.log_step(row, record)
                history.append({"line": line, "changed": None, "label": label, "action": t})
                prev_sig = sig
        except Stopped:
            result, reason = "stopped", "停止要求（Esc）"
        except pyautogui.FailSafeException:
            result, reason = "stopped", "マウスが画面の隅に移動した（FAILSAFE）"
        except Exception as e:  # noqa: BLE001  何が起きてもレポートは残す
            result, reason = "fail", f"{type(e).__name__}: {e}"

        try:
            final = screen.for_model(screen.capture(), width)
            final.save(s.path("final.jpg"), quality=85)
        except Exception:  # noqa: BLE001
            pass
        summary = s.finish(result, reason, warnings=warnings)
        msg = {"done": "完了しました。", "fail": "うまくできませんでした。", "stopped": "停止しました。"}[result]
        if result != "stopped":
            self.hooks.say("完了しましたが、結果を確認してください。" if warnings and result == "done" else msg,
                           "result")
        self.hooks.status(result, f"{msg} {reason}".strip())
        for w in warnings:
            self.hooks.status("warn", f"注意: {w}")
        return summary
