"""聞き間違いの補正（correct.py）と、画面上の状態表示（overlay.py）のテスト。

モデル・画面・マイクは使わない（偽物）。状態表示はウィンドウを出さず、描画だけを確かめる。
"""
import json
import queue

import numpy as np
import pytest

from voice_agent import agent as agent_mod
from voice_agent import correct, prompts, stt, tts, voice
from voice_agent.overlay import STATES, Renderer, premultiplied_bgra


class FakeLLM:
    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.calls = reply, error, []

    def chat(self, messages, image=None, thinking="off", **kw):
        self.calls.append({"messages": messages, "image": image, "thinking": thinking})
        if self.error:
            raise self.error
        return {"content": self.reply if isinstance(self.reply, str) else json.dumps(self.reply, ensure_ascii=False)}


# ---- 補正 ----

def test_correct_uses_vocabulary_screen_and_recent_as_hints():
    llm = FakeLLM({"task": "三井情報のホームページを開いて", "confidence": "high", "reason": "密集情報→三井情報"})
    c = correct.Corrector(llm, ["三井情報", "Chrome"]).correct(
        "密集情報のホームページを開いて", ["Edgeを開いて"], "msedge.exe (Edge)", ["資料ダウンロード", "戻る"])
    assert c.task == "三井情報のホームページを開いて" and c.changed and c.confidence == "high"
    prompt = llm.calls[0]["messages"][-1]["content"]
    for hint in ("密集情報のホームページを開いて", "三井情報", "資料ダウンロード", "Edgeを開いて", "msedge.exe"):
        assert hint in prompt
    assert llm.calls[0]["image"] is None and llm.calls[0]["thinking"] == "off"   # 画像なし・思考なしで速く


def test_correct_keeps_heard_text_on_error_or_bad_reply():
    c = correct.Corrector(FakeLLM(error=TimeoutError("遅い")), []).correct("メモ帳を開いて")
    assert c.task == "メモ帳を開いて" and not c.changed and "TimeoutError" in c.error
    c = correct.Corrector(FakeLLM("JSON ではない"), []).correct("メモ帳を開いて")
    assert c.task == "メモ帳を開いて" and c.error


def test_correct_rejects_answers_that_add_steps():
    long = "メモ帳を開いて、こんにちはと入力して保存し、デスクトップに名前を付けて保存してから閉じて、電卓も開いて"
    c = correct.Corrector(FakeLLM({"task": long, "confidence": "high"}), []).correct("メモ帳を開いて")
    assert c.task == "メモ帳を開いて" and "長すぎる" in c.error


def test_correct_normalizes_confidence():
    c = correct.Corrector(FakeLLM({"task": "閉じて", "confidence": "たぶん"}), []).correct("クローズ")
    assert c.confidence == "medium"


def test_to_meta():
    m = correct.to_meta(correct.Correction("密集情報", "三井情報", "high", "音が近い", 1.2))
    assert m["heard"] == "密集情報" and m["corrected"] == "三井情報" and m["correction_confidence"] == "high"


# ---- 音声モードのループで補正する ----

class ListEvents:
    """キーのイベント列。実行後の「実行中に押されたキーを捨てる」処理（get_nowait）では捨てさせない。"""

    def __init__(self, events):
        self.items = list(events)

    def get(self, timeout=None):
        if self.items:
            return self.items.pop(0)
        import time
        time.sleep(timeout or 0)
        raise queue.Empty

    def get_nowait(self):
        raise queue.Empty

    def empty(self):
        return not self.items


class FakePTT:
    def __init__(self, events):
        self.key, self.down, self.events = "f9", False, ListEvents(events)


class FakeMic:
    def __init__(self, n):
        self.audios = [(0.1 * np.sin(np.linspace(0, 2000, 16000))).astype(np.float32)] * n

    def start(self, preroll=True):
        pass

    def stop(self):
        return self.audios.pop(0)


class FakeSTT:
    model_name = "fake"

    def __init__(self, texts):
        self.texts = list(texts)

    def transcribe(self, audio):
        t = self.texts.pop(0)
        return stt.Transcript(t, 0.1, 1.0, t, "")


class UiHooks:
    def __init__(self):
        self.lines, self.uis = [], []

    def status(self, state, text):
        self.lines.append((state, text))

    def ui(self, state, main, sub=""):
        self.uis.append((state, main, sub))


def run_voice(heard, replies, context=("msedge.exe (Edge)", ["戻る"])):
    import threading
    import time
    tasks, hooks = [], UiHooks()
    llm = FakeLLM()
    replies = list(replies)

    def chat(messages, **kw):
        llm.calls.append(messages)
        return {"content": json.dumps(replies.pop(0), ensure_ascii=False)}
    llm.chat = chat
    loop = voice.VoiceLoop({"voice": {}}, None, hooks, tts.Speaker(), FakeSTT(heard),
                           FakePTT(["down", "up"] * len(heard)), FakeMic(len(heard)),
                           run_task=lambda text, wav, meta: tasks.append((text, meta)) or None,
                           corrector=correct.Corrector(llm, ["三井情報"]), context=lambda: context)

    def stopper():
        while not loop.ptt.events.empty():
            time.sleep(0.01)
        time.sleep(0.3)
        loop.running = False
    th = threading.Thread(target=stopper)
    th.start()
    loop.run()
    th.join()
    return tasks, hooks, llm


def test_voice_runs_corrected_task_and_records_heard_text():
    tasks, hooks, _ = run_voice(["エッジブラウザーのオドロボタンを押して"],
                                [{"task": "エッジブラウザーの戻るボタンを押して", "confidence": "high", "reason": "オドロ→戻る"}])
    assert tasks[0][0] == "エッジブラウザーの戻るボタンを押して"
    assert tasks[0][1]["heard"] == "エッジブラウザーのオドロボタンを押して" and tasks[0][1]["corrected"]
    assert any(s == "corrected" for s, _ in hooks.lines)
    assert [u[0] for u in hooks.uis][:4] == ["ready", "listening", "transcribing", "correcting"]


def test_voice_asks_again_when_confidence_is_low():
    tasks, hooks, _ = run_voice(["お持ち賛同の天気を検索"],
                                [{"task": "お持ち賛同の天気を検索", "confidence": "low", "reason": "地名が分からない"}])
    assert tasks == []                                    # 実行しない
    assert any(s == "ignored" and "もう一度" in t for s, t in hooks.lines)
    assert any(u[0] == "ignored" for u in hooks.uis)


def test_voice_passes_recent_commands_to_corrector():
    tasks, _, llm = run_voice(["Edgeを開いて", "オドロボタンを押して"],
                              [{"task": "Edgeを開いて", "confidence": "high"},
                               {"task": "戻るボタンを押して", "confidence": "high"}])
    assert [t for t, _ in tasks] == ["Edgeを開いて", "戻るボタンを押して"]
    assert "RECENT COMMANDS (oldest first): Edgeを開いて" in llm.calls[1][-1]["content"]


# ---- エージェントに「音声認識の結果」と伝える ----

def test_speech_note_in_prompt():
    note = prompts.speech_note("密集情報を検索", "三井情報を検索", ["三井情報", "Chrome"])
    assert "speech recognition" in note and "密集情報を検索" in note and "三井情報、Chrome" in note
    p = prompts.user_prompt("三井情報を検索", [], "msedge.exe", note=note)
    assert p.splitlines()[0] == "TASK: 三井情報を検索" and p.splitlines()[1] == note
    assert "NOTE" not in prompts.user_prompt("三井情報を検索", [], "msedge.exe")


def test_system_prompt_forbids_guessing_urls():
    assert "Do not guess a website's address" in prompts.system_prompt((1512, 850), ["Edge"])


# ---- 状態表示の描画 ----

@pytest.mark.parametrize("state", list(STATES))
def test_renderer_draws_every_state(state):
    r = Renderer(760, 76, 1.0, ["F9 話す", "Esc 停止"])
    img = r.render(state, "とても長い指示の文がここに入ります。" * 5, "補足の文 ・ ステップ 3/25", t=1.0,
                   levels=[0.01, 0.05, 0.1])
    assert img.size == (760, 76) and img.getpixel((380, 2))[3] > 0      # 角丸の内側は描かれている
    assert img.getpixel((0, 0))[3] == 0                                  # 角の外は透明


def test_premultiplied_bgra():
    from PIL import Image
    img = Image.new("RGBA", (1, 1), (200, 100, 50, 128))
    assert premultiplied_bgra(img) == bytes([50 * 128 // 255, 100 * 128 // 255, 200 * 128 // 255, 128])


def test_friendly_labels():
    from voice_agent.uia import Element
    assert agent_mod.friendly({"action": "click"}, Element("資料ダウンロード", "HyperlinkControl", False)) == \
        "クリック「資料ダウンロード」"
    assert agent_mod.friendly({"action": "click"}) == "クリック"
    assert agent_mod.friendly({"action": "type", "text": "三井情報"}) == "入力「三井情報」"
    assert agent_mod.friendly({"action": "open_app", "text": "Edge"}) == "Edge を開く"


# ---- 測定: 実行のあとの「うまくいきましたか？」と、音声ごとの記録 ----

def make_run_dir(tmp_path):
    d = tmp_path / "20261119-000000-test"
    d.mkdir()
    (d / "summary.json").write_text(json.dumps({"task": "t", "source": "voice", "result": "done", "reason": "",
                                                "steps": 1, "secs": 1.0, "thinking_steps": 0,
                                                "model_secs_total": 0.1, "dir": str(d)}), encoding="utf-8")
    (d / "ledger.jsonl").write_text("", encoding="utf-8")
    return d


def run_with_feedback(tmp_path, events_after, esc_pressed=False):
    import threading
    d = make_run_dir(tmp_path)
    esc = threading.Event()
    hooks = UiHooks()
    ptt = FakePTT(["down", "up"])

    def run_task(text, wav, meta):
        ptt.events.items += events_after                 # 実行のあとに押されるキー
        if esc_pressed:
            threading.Timer(0.2, esc.set).start()
        return {"result": "done", "steps": 1, "secs": 1.0, "dir": str(d)}
    loop = voice.VoiceLoop({"voice": {}, "eval": {"feedback_secs": 1}}, None, hooks, tts.Speaker(),
                           FakeSTT(["メモ帳を開いて"]), ptt, FakeMic(1), run_task=run_task, esc=esc, log_dir=tmp_path)

    def stopper():
        import time
        time.sleep(1.8)
        loop.running = False
    th = threading.Thread(target=stopper)
    th.start()
    loop.run()
    th.join()
    return json.loads((d / "summary.json").read_text(encoding="utf-8")), hooks, tmp_path


def test_feedback_f9_means_success(tmp_path):
    s, hooks, _ = run_with_feedback(tmp_path, ["down", "up"])
    assert s["human_label"] == "success"
    assert any(u[0] == "feedback" for u in hooks.uis)


def test_feedback_esc_means_failure(tmp_path):
    s, _, _ = run_with_feedback(tmp_path, [], esc_pressed=True)
    assert s["human_label"] == "failure"


def test_feedback_skipped_when_no_answer(tmp_path):
    s, _, _ = run_with_feedback(tmp_path, [])
    assert "human_label" not in s


def test_voice_log_keeps_audio_and_text(tmp_path):
    _, _, root = run_with_feedback(tmp_path, [])
    entries = [json.loads(line) for line in (root / "voice_log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert entries[0]["outcome"] == "ran" and entries[0]["heard"] == "メモ帳を開いて"
    assert (root / entries[0]["wav"]).exists()
