import json
import unittest

import empty_guard as eg


def chat(msg, finish="stop"):
    return json.dumps({"choices": [{"message": msg, "finish_reason": finish}]}).encode()


def sse(*events):
    return b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events)


class NonStreamTests(unittest.TestCase):
    def test_signature_only_is_empty(self):
        msg = {"content": None, "provider_specific_fields": {"thought_signatures": ["x"]}}
        self.assertTrue(eg.is_empty_completion(chat(msg)))

    def test_blank_content_is_empty(self):
        self.assertTrue(eg.is_empty_completion(chat({"content": "  "})))

    def test_text_not_empty(self):
        self.assertFalse(eg.is_empty_completion(chat({"content": "hi"})))

    def test_tool_calls_not_empty(self):
        msg = {"content": None, "tool_calls": [{"id": "a", "function": {"name": "f"}}]}
        self.assertFalse(eg.is_empty_completion(chat(msg, "tool_calls")))

    def test_error_or_garbage_not_empty(self):
        self.assertFalse(eg.is_empty_completion(b'{"error": {"message": "x"}}'))
        self.assertFalse(eg.is_empty_completion(b"not json"))

    def test_responses_shapes(self):
        def resp(output):
            return json.dumps({"status": "completed", "output": output}).encode()
        reasoning = {"type": "reasoning", "summary": []}
        blank = {"type": "message", "content": [{"type": "output_text", "text": ""}]}
        text = {"type": "message", "content": [{"type": "output_text", "text": "hi"}]}
        call = {"type": "function_call", "name": "f", "call_id": "c"}
        self.assertTrue(eg.is_empty_completion(resp([reasoning, blank])))
        self.assertFalse(eg.is_empty_completion(resp([text])))
        self.assertFalse(eg.is_empty_completion(resp([reasoning, call])))


class StreamTests(unittest.TestCase):
    def run_watcher(self, data, split=7):
        w = eg.StreamOutputWatcher()
        for i in range(0, len(data), split):  # 任意切块，验证跨块缓冲
            w.feed(data[i:i + split])
        return w

    def test_chat_signature_only(self):
        d = sse({"choices": [{"delta": {"provider_specific_fields": {"thought_signatures": ["s"]}}}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]})
        self.assertTrue(self.run_watcher(d).is_empty())

    def test_chat_text(self):
        d = sse({"choices": [{"delta": {"content": "hi"}}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]})
        self.assertFalse(self.run_watcher(d).is_empty())

    def test_chat_tool_call(self):
        d = sse({"choices": [{"delta": {"tool_calls": [{"index": 0}]}}]},
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]})
        self.assertFalse(self.run_watcher(d).is_empty())

    def test_unfinished_stream_not_flagged(self):
        self.assertFalse(self.run_watcher(sse({"choices": [{"delta": {}}]})).is_empty())

    def test_responses_empty_vs_text_vs_call(self):
        done = {"type": "response.completed", "response": {}}
        item = {"type": "response.output_item.added", "item": {"type": "reasoning"}}
        self.assertTrue(self.run_watcher(sse(item, done)).is_empty())
        delta = {"type": "response.output_text.delta", "delta": "hi"}
        self.assertFalse(self.run_watcher(sse(delta, done)).is_empty())
        call = {"type": "response.output_item.added", "item": {"type": "function_call"}}
        self.assertFalse(self.run_watcher(sse(call, done)).is_empty())


if __name__ == "__main__":
    unittest.main()
