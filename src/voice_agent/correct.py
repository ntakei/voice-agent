"""音声認識の結果を、実行する前に直す（聞き間違いの補正）。

モデル（画像なし・思考なし）に次を渡して、「本来の指示」と「確信度」を返してもらう。
  - 認識した文
  - 手がかりの単語一覧（config の voice.stt.vocabulary。三井情報・Chrome など）
  - 直前の数件の指示（言い直しや続きの指示の手がかり）
  - 前面のアプリと、画面にあるボタン・リンクなどの名前（UI Automation）
音の近さ・文の中のほかの言葉・画面・直前の指示・一般的な知識から推測する（フェーズ7の振り返りで、
人が「密集情報→三井情報」「オドロ→戻る」を見分けた手がかりと同じもの）。

- 聞き間違いだけを直す。手順を足したり、内容を変えたりはさせない（長くなりすぎた答えは使わない）。
- 確信度が low なら実行せずに聞き直す（voice.correction.ask_again_on_low）。
- 失敗・時間切れのときは、認識した文をそのまま使う。
"""
import json
import time
from dataclasses import dataclass

from .coords import parse_json

SYSTEM = """You fix speech-recognition errors in short Japanese voice commands for an agent that operates a Windows PC.
The speech recognizer often mishears words, especially proper nouns, katakana words and English words
(for example it writes a similar-sounding but wrong word, or drops the first sound).

Reply with ONE JSON object and nothing else:
{"task": "<the corrected command in Japanese>", "confidence": "high" | "medium" | "low", "reason": "<何を直したか、または直さなかった理由（短い日本語）>"}

Rules:
- Fix ONLY misheard words. Keep the user's intent, wording and order. Do NOT add steps, details, URLs or explanations.
- Compare the sounds in kana. Prefer a KNOWN WORD or a word VISIBLE ON SCREEN when it sounds similar.
- Use the recent commands and general knowledge (place names, company names, websites) as hints.
- If the command already makes sense, return it unchanged with "high".
- "high": you are sure. "medium": a likely fix. "low": you cannot tell what the user meant.

Examples (not related to the current screen):
- heard "メモ超を開いて" -> {"task": "メモ帳を開いて", "confidence": "high", "reason": "メモ超→メモ帳"}
- heard "電卓を拾います" -> {"task": "電卓を開きます", "confidence": "high", "reason": "拾います→開きます"}
- heard "ユーチューブでネコの動画を検索" -> {"task": "YouTubeで猫の動画を検索", "confidence": "high", "reason": "表記をそろえた"}
- heard "あの、ええと" -> {"task": "あの、ええと", "confidence": "low", "reason": "指示が分からない"}"""

CONFIDENCE = ("high", "medium", "low")


@dataclass
class Correction:
    heard: str             # 認識した文
    task: str              # 実行する指示（直した後）
    confidence: str        # high / medium / low
    reason: str = ""
    secs: float = 0.0
    error: str = ""        # 補正できなかった理由（あれば。task は heard のまま）

    @property
    def changed(self) -> bool:
        return self.task != self.heard


def build_prompt(heard: str, vocabulary: list[str], recent: list[str], front_app: str, screen_words: list[str]) -> str:
    lines = [f"HEARD (speech recognition result): {heard}", ""]
    if vocabulary:
        lines.append("KNOWN WORDS: " + "、".join(vocabulary))
    if front_app:
        lines.append(f"FRONT APP: {front_app}")
    if screen_words:
        lines.append("VISIBLE ON SCREEN (buttons, links, tabs): " + "、".join(screen_words))
    if recent:
        lines.append("RECENT COMMANDS (oldest first): " + " / ".join(recent))
    lines.append("\nReturn the JSON.")
    return "\n".join(lines)


class Corrector:
    def __init__(self, client, vocabulary: list[str] | None = None, timeout_secs: float = 10,
                 max_growth: int = 20):
        self.client = client
        self.vocabulary = [str(v) for v in (vocabulary or []) if str(v).strip()]
        self.timeout_secs = timeout_secs
        self.max_growth = max_growth   # 直した文が、元の文よりこの文字数＋元の長さ以上に長くなったら使わない

    def correct(self, heard: str, recent: list[str] | None = None, front_app: str = "",
                screen_words: list[str] | None = None) -> Correction:
        t = time.time()
        prompt = build_prompt(heard, self.vocabulary, recent or [], front_app, screen_words or [])
        try:
            r = self.client.chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                                 image=None, thinking="off", max_tokens=300)
        except Exception as e:  # noqa: BLE001  補正できなくても、認識した文で続ける
            return Correction(heard, heard, "medium", secs=time.time() - t, error=f"{type(e).__name__}: {e}")
        secs = time.time() - t
        a = parse_json(r.get("content", ""))
        if not isinstance(a, dict) or not str(a.get("task", "")).strip():
            return Correction(heard, heard, "medium", secs=secs, error="補正の返答が読めない")
        task = str(a["task"]).strip()
        conf = str(a.get("confidence", "medium")).lower()
        conf = conf if conf in CONFIDENCE else "medium"
        reason = str(a.get("reason", ""))[:60]
        if len(task) > len(heard) * 2 + self.max_growth:
            # 手順や説明を足した答えは使わない（聞き間違いの補正だけをさせる）
            return Correction(heard, heard, conf, reason, secs, error="補正した文が長すぎる（使わない）")
        return Correction(heard, task, conf, reason, secs)


def to_meta(c: Correction) -> dict:
    """実行の記録（summary.json・report.html）に残す項目。"""
    meta = {"heard": c.heard, "correction_confidence": c.confidence, "correction_secs": round(c.secs, 2)}
    if c.changed:
        meta["corrected"] = c.task
    if c.reason:
        meta["correction_reason"] = c.reason
    if c.error:
        meta["correction_error"] = c.error
    return meta


if __name__ == "__main__":  # 試し: python -m voice_agent.correct "密集情報のホームページを開いて"
    import sys

    from . import config
    from .model_client import ModelClient
    cfg = config.load()
    voc = ((cfg.get("voice") or {}).get("stt") or {}).get("vocabulary") or []
    c = Corrector(ModelClient(cfg), voc).correct(" ".join(sys.argv[1:]) or "密集情報のホームページを開いて")
    print(json.dumps(c.__dict__, ensure_ascii=False, indent=2))
