"""OpenAI 互換 API（vLLM）のクライアント。思考モードの切り替えと画像添付を扱う。"""
import base64
import io
import time

from openai import OpenAI

from . import config

THINKING_MODES = ("off", "low", "on")


def normalize_mode(mode) -> str:
    """YAML の off/on は真偽値になるので、False -> "off"、True -> "on" に戻す。"""
    if mode is False:
        return "off"
    if mode is True:
        return "on"
    return str(mode).strip().lower()


def thinking_params(mode) -> dict:
    """思考モード -> リクエストに足す extra_body。

    off: chat_template_kwargs.enable_thinking=false（思考トークンなし）
    low: reasoning_effort="low"（短い思考）
    on : 何も足さない（モデル既定 = 思考あり）
    """
    mode = normalize_mode(mode)
    if mode == "off":
        return {"chat_template_kwargs": {"enable_thinking": False}}
    if mode == "low":
        return {"reasoning_effort": "low"}
    if mode == "on":
        return {}
    raise ValueError(f"unknown thinking mode {mode!r} (use one of {THINKING_MODES})")


def image_to_data_url(img, fmt: str = "JPEG", quality: int = 90) -> str:
    buf = io.BytesIO()
    img.save(buf, format=fmt, **({"quality": quality} if fmt.upper() == "JPEG" else {}))
    return f"data:image/{fmt.lower()};base64," + base64.b64encode(buf.getvalue()).decode()


class ModelClient:
    def __init__(self, cfg: dict | None = None):
        self.cfg = (cfg or config.load())["model"]
        self.client = OpenAI(base_url=self.cfg["base_url"], api_key=config.api_key(), max_retries=2)

    def chat(self, messages: list[dict], image=None, thinking: str = "off", max_tokens: int | None = None,
             json_mode: bool = True, image_quality: int = 90) -> dict:
        """1回の呼び出し。image(PIL) は最後の user メッセージに添付する。

        戻り値: content, reasoning, finish_reason, usage(dict), secs, thinking
        """
        thinking = normalize_mode(thinking)
        messages = [dict(m) for m in messages]
        if image is not None:
            last = messages[-1]
            last["content"] = [
                {"type": "text", "text": last["content"]},
                {"type": "image_url", "image_url": {"url": image_to_data_url(image, "JPEG", image_quality)}},
            ]
        if max_tokens is None:
            max_tokens = 1500 if thinking == "off" else 16000
        extra = thinking_params(thinking)
        if json_mode:
            extra["response_format"] = {"type": "json_object"}
        t0 = time.time()
        r = self.client.chat.completions.create(
            model=self.cfg["name"],
            messages=messages,
            max_tokens=max_tokens,
            temperature=self.cfg.get("temperature", 0.2),
            timeout=self.cfg.get("timeout_secs_thinking" if thinking != "off" else "timeout_secs", 60),
            extra_body=extra,
        )
        choice = r.choices[0]
        msg = choice.message
        extra_fields = msg.model_extra or {}
        return {
            "content": msg.content or "",
            "reasoning": extra_fields.get("reasoning_content") or extra_fields.get("reasoning") or "",
            "finish_reason": choice.finish_reason,
            "usage": r.usage.model_dump(exclude_none=True) if r.usage else {},
            "secs": round(time.time() - t0, 2),
            "thinking": thinking,
        }
