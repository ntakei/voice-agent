"""OpenAI 互換 API（vLLM）のクライアント。思考モードの切り替えと画像添付を扱う。"""
import base64
import io
import threading
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


class Cancelled(Exception):
    """cancel() で打ち切った。"""


class ModelClient:
    """ストリーミングで受け取る。止める（cancel）・時間切れのときは接続を切るので、
    サーバー（vLLM）側の生成もそこで止まる（フェーズ7: 長い思考で GPU が数分使われ続けた）。"""

    def __init__(self, cfg: dict | None = None):
        self.cfg = (cfg or config.load())["model"]
        self._lock = threading.Lock()
        self._active = None          # 進行中のストリーム
        self._cancel = threading.Event()

    def _client(self) -> OpenAI:
        return OpenAI(base_url=self.cfg["base_url"], api_key=config.api_key(), max_retries=2)

    def cancel(self) -> None:
        """進行中の呼び出しを打ち切る（別スレッドから呼んでよい）。"""
        self._cancel.set()
        with self._lock:
            stream = self._active
        if stream is not None:
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass

    def chat(self, messages: list[dict], image=None, thinking: str = "off", max_tokens: int | None = None,
             json_mode: bool = True, image_quality: int = 90) -> dict:
        """1回の呼び出し。image(PIL) は最後の user メッセージに添付する。

        戻り値: content, reasoning, finish_reason, usage(dict), secs, thinking
        時間切れは TimeoutError、cancel() されたら Cancelled。
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
            max_tokens = 1500 if thinking == "off" else int(self.cfg.get("max_tokens_thinking", 3000))
        extra = thinking_params(thinking)
        if json_mode:
            extra["response_format"] = {"type": "json_object"}
        limit = float(self.cfg.get("timeout_secs_thinking" if thinking != "off" else "timeout_secs", 60))
        self._cancel.clear()
        t0 = time.time()
        content, reasoning, finish, usage = [], [], None, {}
        client = self._client()
        try:
            stream = client.chat.completions.create(
                model=self.cfg["name"], messages=messages, max_tokens=max_tokens,
                temperature=self.cfg.get("temperature", 0.2), timeout=limit, stream=True,
                stream_options={"include_usage": True}, extra_body=extra)
            with self._lock:
                self._active = stream
            if self._cancel.is_set():
                raise Cancelled()
            for chunk in stream:
                if self._cancel.is_set():
                    raise Cancelled()
                if time.time() - t0 > limit:
                    raise TimeoutError(f"{limit:.0f} 秒を超えた（思考 {thinking}）")
                if chunk.usage:
                    usage = chunk.usage.model_dump(exclude_none=True)
                for ch in chunk.choices or []:
                    d = ch.delta
                    if d is not None:
                        if d.content:
                            content.append(d.content)
                        ex = d.model_extra or {}
                        r = ex.get("reasoning_content") or ex.get("reasoning")
                        if r:
                            reasoning.append(r)
                    if ch.finish_reason:
                        finish = ch.finish_reason
        except Exception:
            if self._cancel.is_set():
                raise Cancelled() from None
            raise
        finally:
            with self._lock:
                stream, self._active = self._active, None
            for c in (stream, client):
                try:
                    if c is not None:
                        c.close()        # 接続を切る（途中ならサーバー側の生成も止まる）
                except Exception:  # noqa: BLE001
                    pass
        return {
            "content": "".join(content),
            "reasoning": "".join(reasoning),
            "finish_reason": finish,
            "usage": usage,
            "secs": round(time.time() - t0, 2),
            "thinking": thinking,
        }
