"""音声認識モデルの比較（同じ音声を複数のモデルで認識し、時間と結果を並べる）。

  # 読み上げ音声（SAPI で WAV に書き出す。スピーカーからは鳴らない）で比べる
  python -m uv run python scripts/stt_compare.py
  # 自分の声で比べる（F9 を押しながら話す × 回数分。録音は scripts/out/stt-*/ に保存）
  python -m uv run python scripts/stt_compare.py --record 3
  # 保存した WAV で比べる
  python -m uv run python scripts/stt_compare.py --wav a.wav b.wav

結果は scripts/out/stt-<日時>/result.json にも保存する。
"""
import argparse
import json
import sys
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from voice_agent import config, recorder, stt  # noqa: E402

PHRASES = [
    "メモ帳を開いて、こんにちはと入力して",
    "電卓で十二かける三十四を計算して",
    "エッジで天気予報を検索して",
    "ペイントを開いて",
    "全部選択して削除して",
]


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        data = w.readframes(w.getnframes())
    dtype = {1: np.uint8, 2: "<i2", 4: "<i4"}[width]
    a = np.frombuffer(data, dtype=dtype).astype(np.float32)
    a = (a - 128) / 128 if width == 1 else a / float(2 ** (8 * width - 1))
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    return recorder.resample(a, rate)


def synth(phrases: list[str], out: Path) -> list[Path]:
    """SAPI で WAV に書き出す（pyttsx3 の save_to_file は繰り返すと止まるので、SAPI を直接使う）。"""
    import win32com.client
    voice = win32com.client.Dispatch("SAPI.SpVoice")
    for tok in voice.GetVoices():
        if "japanese" in tok.GetDescription().lower() or "日本語" in tok.GetDescription():
            voice.Voice = tok
            break
    paths = []
    for i, p in enumerate(phrases, 1):
        f = out / f"tts-{i:02d}.wav"
        stream = win32com.client.Dispatch("SAPI.SpFileStream")
        stream.Open(str(f), 3)          # SSFMCreateForWrite
        voice.AudioOutputStream = stream
        voice.Speak(p)                  # 同期（ファイルに書き終わるまで待つ）。スピーカーからは鳴らない
        stream.Close()
        paths.append(f)
    return paths


def record(n: int, out: Path, key: str) -> list[Path]:
    from voice_agent.ptt import PushToTalk
    ptt = PushToTalk(key).start()
    rec = recorder.Recorder()
    paths = []
    try:
        for i in range(1, n + 1):
            print(f"[{i}/{n}] {key.upper()} を押しながら話してください…", flush=True)
            while ptt.events.get() != "down":
                pass
            rec.start()
            while ptt.events.get() != "up":
                pass
            audio = rec.stop()
            f = out / f"rec-{i:02d}.wav"
            f.write_bytes(recorder.to_wav_bytes(audio))
            print(f"    {audio.size / recorder.RATE:.1f} 秒 → {f.name}", flush=True)
            paths.append(f)
    finally:
        ptt.stop()
    return paths


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="small,large-v3-turbo")
    ap.add_argument("--wav", nargs="*", type=Path)
    ap.add_argument("--record", type=int, default=0)
    ap.add_argument("--key", default="f9")
    args = ap.parse_args()

    out = ROOT / "scripts" / "out" / f"stt-{time.strftime('%Y%m%d-%H%M%S')}"
    out.mkdir(parents=True, exist_ok=True)
    expected: list[str | None]
    if args.wav:
        files, expected = args.wav, [None] * len(args.wav)
    elif args.record:
        files = record(args.record, out, args.key)
        expected = [None] * len(files)
    else:
        files, expected = synth(PHRASES, out), list(PHRASES)
    audios = [read_wav(f) for f in files]

    cfg = config.load()
    sc = (cfg.get("voice") or {}).get("stt") or {}
    results = []
    for name in [m.strip() for m in args.models.split(",") if m.strip()]:
        tr = stt.WhisperTranscriber(model=name, compute_type=sc.get("compute_type", "int8"),
                                    beam_size=int(sc.get("beam_size", 5)), vad_filter=bool(sc.get("vad_filter", True)),
                                    initial_prompt=sc.get("initial_prompt")).load()
        tr.transcribe(np.zeros(stt.RATE, np.float32))  # 空回し
        print(f"\n== {name}（読み込み {tr.load_secs:.1f} 秒）")
        for f, a, exp in zip(files, audios, expected):
            r = tr.transcribe(a)
            rtf = r.secs / max(r.audio_secs, 0.01)
            print(f"  {r.secs:5.2f} 秒（音声 {r.audio_secs:4.1f} 秒, x{rtf:.2f}）「{r.text}」"
                  + (f"  ← 正解「{exp}」" if exp else "") + (f"  [捨てた: {r.dropped}]" if r.dropped else ""))
            results.append({"model": name, "file": f.name, "secs": round(r.secs, 2),
                            "audio_secs": round(r.audio_secs, 2), "text": r.text, "raw": r.raw, "expected": exp})
    (out / "result.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n保存: {out}")


if __name__ == "__main__":
    main()
