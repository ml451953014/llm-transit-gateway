"""检测 Gemini 偶发的「只回思维签名、没有正文」空响应。

思维签名不算正文；tool_calls / function_call / 图片 / 音频都算正常输出，不会被判空。
"""
import json


def _text_nonempty(v) -> bool:
    if isinstance(v, str):
        return bool(v.strip())
    return bool(v)  # list/dict 形式的多模态内容视为有输出


def _chat_has_output(msg: dict) -> bool:
    """Chat Completions 的 message 或流式 delta 里是否有任何实际输出。"""
    if msg.get("tool_calls") or msg.get("function_call"):
        return True
    if msg.get("images") or msg.get("audio") or msg.get("refusal"):
        return True
    return _text_nonempty(msg.get("content"))


def _responses_item_has_output(item: dict) -> bool:
    t = item.get("type")
    if t == "reasoning":
        return False
    if t != "message":
        return True  # function_call、图片生成等都算输出
    for part in item.get("content") or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") != "output_text" or _text_nonempty(part.get("text")):
            return True
    return False


def is_empty_completion(raw: bytes) -> bool:
    """非流式 200 响应是否为「正常结束但没有任何输出」。解析失败一律不算空。"""
    try:
        j = json.loads(raw)
    except Exception:
        return False
    if not isinstance(j, dict) or j.get("error"):
        return False
    choices = j.get("choices")
    if isinstance(choices, list):
        if not choices:
            return False
        return not any(_chat_has_output(c.get("message") or {}) for c in choices if isinstance(c, dict))
    out = j.get("output")
    if isinstance(out, list) and j.get("status") == "completed":
        return not any(_responses_item_has_output(i) for i in out if isinstance(i, dict))
    return False


class StreamOutputWatcher:
    """旁路观察 SSE 字节流，不改动内容；结束后用 is_empty() 判断是否「正常收尾却无输出」。"""

    def __init__(self):
        self._buf = b""
        self.has_output = False
        self.finished = False

    def feed(self, chunk: bytes) -> None:
        self._buf += chunk
        *lines, self._buf = self._buf.split(b"\n")
        for line in lines:
            self._line(line.strip())

    def _line(self, line: bytes) -> None:
        if not line.startswith(b"data:") or line.endswith(b"[DONE]"):
            return
        try:
            j = json.loads(line[5:])
        except Exception:
            return
        if not isinstance(j, dict):
            return
        t = j.get("type")
        if isinstance(t, str):
            self._responses_event(t, j)
        for c in j.get("choices") or []:
            if not isinstance(c, dict):
                continue
            if _chat_has_output(c.get("delta") or {}):
                self.has_output = True
            if c.get("finish_reason"):
                self.finished = True

    def _responses_event(self, t: str, j: dict) -> None:
        if t == "response.completed":
            self.finished = True
        elif t == "response.output_text.delta":
            if _text_nonempty(j.get("delta")):
                self.has_output = True
        elif t in ("response.output_item.added", "response.output_item.done"):
            item = j.get("item") or {}
            if item.get("type") not in ("message", "reasoning"):
                self.has_output = True
        elif t == "response.function_call_arguments.delta":
            self.has_output = True

    def is_empty(self) -> bool:
        return self.finished and not self.has_output
