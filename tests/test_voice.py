"""フェーズ7: 音声入出力のテスト（マイク・スピーカー・キーボードは使わない）。"""
import io
import json
import threading
import time
import wave

import numpy as np
import pytest

import importlib

from voice_agent import config, ptt, recorder, stt, tts, voice

main = importlib.import_module("voice_agent.main")   # voice_agent.main は関数名と重なるので

# ---- 押して話すキー ----

DOWN, UP, SYSDOWN = 0x100, 0x101, 0x104
F9 = 0x78


def test_vk_names():
    assert ptt.vk_of("F9") == F9 and ptt.vk_of("f1") == 0x70 and ptt.vk_of("f24") == 0x87
    assert ptt.vk_of("scroll lock") == 0x91 and ptt.vk_of("pause") == 0x13
    with pytest.raises(ValueError):
        ptt.vk_of("a")       # 文字キーは、入力に紛れるので使わせない


def test_ptt_down_up_and_repeat():
    p = ptt.PushToTalk("f9")
    assert p.handle(DOWN, F9, 0) is True
    assert p.handle(DOWN, F9, 0) is True          # キーリピート
    assert p.handle(SYSDOWN, F9, 0) is True
    assert p.handle(UP, F9, 0) is True
    assert p.handle(UP, F9, 0) is True            # 余分な離す
    assert [p.events.get_nowait() for _ in range(p.events.qsize())] == ["down", "up"]


def test_ptt_ignores_other_keys_and_injected():
    p = ptt.PushToTalk("f9")
    assert p.handle(DOWN, 0x41, 0) is False       # A キー: 他のアプリへ渡す
    assert p.handle(DOWN, F9, 0x10) is False      # プログラムが送った F9
    assert p.events.empty() and p.down is False


# ---- 録音の補助 ----

def test_resample_and_wav():
    a = np.sin(np.linspace(0, 100, 48000)).astype(np.float32)   # 48kHz で1秒
    r = recorder.resample(a, 48000)
    assert r.size == 16000 and r.dtype == np.float32
    data = recorder.to_wav_bytes(r)
    with wave.open(io.BytesIO(data)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()) == (16000, 1, 2, 16000)
    assert recorder.level(np.zeros(100, np.float32)) == 0.0 and recorder.level(a) > 0.5


# ---- 文字起こしの後処理 ----

@pytest.mark.parametrize("raw,text", [
    ("メモ帳を開いて、", "メモ帳を開いて"),
    (" 電卓で12×34を計算して。", "電卓で12×34を計算して"),
    ("ご視聴ありがとうございました", ""),
    ("ご視聴ありがとうございました。", ""),
    ("", ""),
    ("ありがとうございましたと入力して", "ありがとうございましたと入力して"),   # 指示の一部なら捨てない
])
def test_clean(raw, text):
    assert stt.clean(raw)[0] == text


# ---- 読み上げ（スレッドとキューの動き。実際の音は出さない）----

class FakeSpeaker(tts.ThreadSpeaker):
    """1件を secs 秒かけて「話す」。止める要求があればすぐやめる。"""

    def __init__(self, secs=0.3):
        self.secs, self.spoken, self.cut = secs, [], []
        super().__init__()

    def _init_engine(self):
        pass

    def _speak(self, text):
        end = time.time() + self.secs
        while time.time() < end:
            if self._stop:
                self.cut.append(text)
                return
            time.sleep(0.01)
        self.spoken.append(text)


def test_speaker_keeps_only_latest_pending():
    s = FakeSpeaker(0.3)
    s.say("一件目")
    time.sleep(0.05)
    s.say("二件目")
    s.say("三件目")                 # 話している間に来たものは最新だけ残す
    assert s.wait(3)
    assert s.spoken == ["一件目", "三件目"]
    s.close()


def test_speaker_stop_and_interrupt():
    s = FakeSpeaker(1.0)
    s.say("長い文")
    time.sleep(0.05)
    t = time.time()
    s.stop()
    assert s.wait(1) and time.time() - t < 0.3 and s.cut == ["長い文"]
    s.say("理由")
    time.sleep(0.05)
    s.say("確認してください", interrupt=True)    # 確認・結果は今の読み上げを打ち切って話す
    assert s.wait(3)
    assert s.cut == ["長い文", "理由"] and s.spoken == ["確認してください"]
    s.close()


def test_make_speaker_none_and_unknown():
    assert type(tts.make_speaker({"voice": {"tts": {"engine": "none"}}})) is tts.Speaker
    with pytest.raises(ValueError):
        tts.make_speaker({"voice": {"tts": {"engine": "foo"}}})


def test_console_hooks_speaks_selected_kinds():
    class Rec(tts.Speaker):
        def __init__(self):
            self.said = []

        def say(self, text, interrupt=False):
            self.said.append((text, interrupt))
    sp = Rec()
    h = main.ConsoleHooks(30, sp, ["start", "confirm", "result"])
    h.say("わかりました。", "start")
    h.say("メモ帳を開くため", "reason")          # reason は選ばれていないので読まない
    h.say("確認してください", "confirm")
    assert sp.said == [("わかりました。", False), ("確認してください", True)]


# ---- 音声モードのループ（偽のキー・録音・認識・エージェント）----

class FakePTT:
    def __init__(self, events):
        import queue
        self.key = "f9"
        self.down = False
        self.events = queue.Queue()
        for e in events:
            self.events.put(e)


class FakeRecorder:
    def __init__(self, audios):
        self.audios = list(audios)
        self.started = 0
        self.prerolls = []

    def start(self, preroll=True):
        self.started += 1
        self.prerolls.append(preroll)

    def stop(self):
        return self.audios.pop(0)


class FakeTranscriber:
    model_name = "fake"

    def __init__(self, texts):
        self.texts = list(texts)

    def transcribe(self, audio):
        t = self.texts.pop(0)
        text, dropped = stt.clean(t)
        return stt.Transcript(text, 0.1, audio.size / 16000, t, dropped)


class LogHooks:
    def __init__(self):
        self.lines = []

    def status(self, state, text):
        self.lines.append((state, text))


def run_loop(events, audios, texts, cfg=None):
    tasks = []
    hooks = LogHooks()
    sp = tts.Speaker()
    loop = voice.VoiceLoop(cfg or {"voice": {"min_secs": 0.4, "min_level": 0.003}}, None, hooks, sp,
                           FakeTranscriber(texts), FakePTT(events), FakeRecorder(audios),
                           run_task=lambda text, wav, meta: tasks.append((text, len(wav), meta)) or
                           {"result": "done", "steps": 1, "secs": 1.0, "dir": "x"})

    def stopper():   # キューが空になったら止める
        while not loop.ptt.events.empty():
            time.sleep(0.01)
        time.sleep(0.3)
        loop.running = False
    th = threading.Thread(target=stopper)
    th.start()
    loop.run()
    th.join()
    return tasks, hooks


def tone(secs, amp=0.1):
    return (amp * np.sin(np.linspace(0, 2000, int(16000 * secs)))).astype(np.float32)


def test_voice_loop_runs_task():
    tasks, hooks = run_loop(["down", "up"], [tone(1.5)], ["メモ帳を開いて。"])
    assert [t[0] for t in tasks] == ["メモ帳を開いて"]
    assert tasks[0][1] > 40000                       # WAV が渡っている（1.5秒 × 16kHz × 2byte）
    assert tasks[0][2]["stt_model"] == "fake" and tasks[0][2]["stt_raw"] == "メモ帳を開いて。"
    assert any(s == "heard" and "メモ帳を開いて" in t for s, t in hooks.lines)


def test_voice_loop_ignores_short_silent_and_hallucination():
    tasks, hooks = run_loop(["down", "up"] * 3,
                            [tone(0.2), np.zeros(16000, np.float32), tone(1.0)],
                            ["ご視聴ありがとうございました"])
    assert tasks == []
    texts = " ".join(t for s, t in hooks.lines if s == "ignored")
    assert "短すぎる" in texts and "音がほとんど入っていません" in texts and "聞き取れませんでした" in texts


# ---- エージェント: 音声の記録 ----

def test_agent_saves_audio(monkeypatch, tmp_path):
    from voice_agent import agent as agent_mod
    from voice_agent import screen, winapi
    from PIL import Image
    monkeypatch.setattr(screen, "capture", lambda: Image.new("RGB", (1920, 1080)))
    monkeypatch.setattr(screen, "screen_size", lambda: (1920, 1080))
    monkeypatch.setattr(winapi, "foreground", lambda: None)
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path)

    class Done:
        def chat(self, *a, **k):
            return {"content": json.dumps({"action": "done"}), "reasoning": "", "finish_reason": "stop",
                    "usage": {}, "secs": 0.0}
    cfg = {"model": {"name": "fake"}, "apps": [], "agent": {}}
    ag = agent_mod.Agent(cfg, hooks=agent_mod.Hooks(), dry_run=True, client=Done())
    monkeypatch.setattr(ag, "failsafe_check", lambda: None)
    s = ag.run("メモ帳を開いて", source="voice", audio_wav=recorder.to_wav_bytes(tone(1.0)),
               extra_meta={"stt_model": "small"})
    d = tmp_path / s["dir"].replace("\\", "/").split("/")[-1]
    assert (d / "audio.wav").exists() and s["stt_model"] == "small" and s["source"] == "voice"
    assert '<audio controls src="audio.wav">' in (d / "report.html").read_text(encoding="utf-8")


# ---- 実際のモデルでの認識（models/ に small があるときだけ。音は鳴らさない）----

def _small_present():
    return any((config.ROOT / "models").glob("models--Systran--faster-whisper-small"))


@pytest.mark.skipif(not _small_present(), reason="models/ に small がない")
def test_whisper_small_on_synthesized_speech(tmp_path):
    import win32com.client
    sp = win32com.client.Dispatch("SAPI.SpVoice")
    for tok in sp.GetVoices():
        if "japanese" in tok.GetDescription().lower():
            sp.Voice = tok
    f = tmp_path / "a.wav"
    stream = win32com.client.Dispatch("SAPI.SpFileStream")
    stream.Open(str(f), 3)
    sp.AudioOutputStream = stream
    sp.Speak("メモ帳を開いて")
    stream.Close()
    with wave.open(str(f)) as w:
        rate = w.getframerate()
        a = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768
    r = stt.WhisperTranscriber("small").transcribe(recorder.resample(a, rate))
    assert "メモ帳" in r.text and "開" in r.text, r


# ---- 話し始めを切らない（直前の音を含める）----

def fake_mic(monkeypatch, preroll_secs=0.5):
    r = recorder.Recorder(preroll_secs=preroll_secs)
    monkeypatch.setattr(r, "open", lambda: r)          # 実際のマイクは開かない
    return r


def feed(r, value, secs, block=160):
    for _ in range(int(secs * 16000 / block)):
        r._callback(np.full((block, 1), value, np.float32), block, None, None)


def test_preroll_includes_audio_before_press(monkeypatch):
    r = fake_mic(monkeypatch, 0.5)
    feed(r, 0.1, 2.0)              # 押す前の2秒（直前の0.5秒だけ残る）
    r.start()
    feed(r, 0.3, 1.0)              # 押している間
    a = r.stop()
    assert abs(a.size / 16000 - 1.5) < 0.02
    assert np.allclose(a[:100], 0.1) and np.allclose(a[-100:], 0.3)


def test_preroll_off_when_requested(monkeypatch):
    r = fake_mic(monkeypatch, 0.5)
    feed(r, 0.1, 1.0)
    r.start(preroll=False)         # 読み上げ中に押されたときなど
    feed(r, 0.3, 1.0)
    a = r.stop()
    assert abs(a.size / 16000 - 1.0) < 0.02 and np.allclose(a, 0.3)
    r.start()                      # 録音の後は直前の音をためなおす
    assert r.stop().size == 0


def test_max_secs(monkeypatch):
    r = fake_mic(monkeypatch, 0.0)
    r.max_secs = 1.0
    r.start()
    feed(r, 0.2, 3.0)
    assert abs(r.stop().size / 16000 - 1.0) < 0.02


def test_no_preroll_while_speaking():
    class Busy(tts.Speaker):
        speaking = True
    events = ["down", "up"]
    hooks = LogHooks()
    mic = FakeRecorder([tone(1.0)])
    loop = voice.VoiceLoop({"voice": {}}, None, hooks, Busy(), FakeTranscriber(["メモ帳を開いて"]),
                           FakePTT(events), mic, run_task=lambda *a: None)
    loop.listen_once()
    assert mic.prerolls == [False]


def test_normalize_quiet_audio():
    a = tone(1.0, amp=0.05)
    n = recorder.normalize(a)
    assert abs(float(np.abs(n).max()) - 0.7) < 0.01
    assert float(np.abs(recorder.normalize(tone(1.0, amp=0.001))).max()) < 0.03   # 増幅は20倍まで


# ---- 認識の手がかりの文をそのまま返したら捨てる ----

PROMPT = "Chrome、Edge、GitHub、Google、Yahoo! JAPAN、NVIDIA、Go to file、アドレスバー、検索ボックス、タブ、メモ帳、電卓、ペイント、スクロール"


@pytest.mark.parametrize("raw,keep", [
    ("Chrome、Edge、GitHub", False),
    ("メモ帳、電卓、ペイント", False),
    ("ペイントを操作します", True),                  # 手がかりの文に含まれない指示は残す
    ("Yahoo! JAPANをクリック", True),
    ("GitHub", True),                                # 言葉1つは指示かもしれないので残す
    ("スクロール", True),
    ("Google、Yahoo! JAPAN", False),                 # 2つ以上並べただけ
    ("ドレスバー", False),                           # 言葉の切れ端
    ("タブ", True),                                  # 4文字未満は比べない
    ("GitHub、NVIDIA、Chrome、Edge", False),         # 手がかりの言葉を順不同で並べただけ
    ("、Wi-Fi、Wi-Fi、Wi-Fi、Edge", False),
    ("MOMI、MOMI、MOMI、MOMI", False),               # 同じ言葉の繰り返し
    ("ペイント、Close", True),                       # 手がかり以外の言葉が入っていれば残す
    ("Chrome、Edge", False),
    ("アドレスバーでNVIDIAを検索", True),
])
def test_prompt_echo(raw, keep):
    text, dropped = stt.clean(raw, PROMPT)
    assert bool(text) is keep, dropped


def test_build_prompt_respects_token_budget_and_order():
    count = len                                       # 1文字 = 1トークンとみなす
    text, dropped = stt.build_prompt(["Chrome", "Edge", "", "GitHub", "メモ帳"], count, max_tokens=13)
    assert text == "Chrome、Edge" and dropped == ["GitHub", "メモ帳"]   # 空の項目は飛ばす
    text, dropped = stt.build_prompt(["Chrome", "Edge"], count, max_tokens=100)
    assert text == "Chrome、Edge" and dropped == []


def test_config_vocabulary_fits():
    voc = config.load()["voice"]["stt"]["vocabulary"]
    assert "Chrome" in voc and "Yahoo! JAPAN" in voc and all(isinstance(w, str) for w in voc)
    assert not any("します" in w for w in voc)       # 文を書かない（手がかりの文をそのまま返す事故の元）
