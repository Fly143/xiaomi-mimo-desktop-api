"""流式文本/工具历史辅助函数的回归测试。

覆盖四个会导致真实客户端故障的点：
1. 流式分块清理不得裁剪空白（换行、缩进、词间空格）；
2. 正文不得出现裸 think 标签；
3. `_split_think_from_content` 不得把 `<think>` 之前的正文吞进思考；
4. 历史工具调用必须带真实 name/arguments（曾退化为 `TOOL_CALL: None(None)`）。
"""

import json
import unittest

from app.mimo_client import MimoClient
from app.routes import _build_usage_chunk, _clean_stream_text, _strip_think_tags
from app.utils import _serialize_tool_calls, build_query_from_messages


class StreamWhitespaceTests(unittest.TestCase):
    def test_whitespace_only_chunk_survives_cleaning(self):
        for chunk in (" ", "\n", "    ", "\n\n"):
            with self.subTest(chunk=chunk):
                self.assertEqual(_clean_stream_text(chunk), chunk)

    def test_indentation_and_inner_spaces_survive_cleaning(self):
        text = "a b  c   d\n    e 四空格\n        f 八空格"
        self.assertEqual(_clean_stream_text(text), text)

    def test_tool_name_prefix_still_removed_without_touching_body(self):
        self.assertEqual(_clean_stream_text("webSearch\n结果"), "结果")

    def test_stray_think_tags_never_reach_content(self):
        self.assertEqual(_strip_think_tags("思考尾巴</think>"), "思考尾巴")
        self.assertEqual(_clean_stream_text("a<think>b</think>c"), "abc")


class SplitThinkTests(unittest.TestCase):
    def test_text_before_think_block_is_preserved(self):
        main, think = MimoClient._split_think_from_content("BEFORE <think>mid</think> AFTER")
        self.assertEqual(main, "BEFORE  AFTER")
        self.assertEqual(think, "mid")

    def test_multiple_and_unclosed_blocks(self):
        self.assertEqual(
            MimoClient._split_think_from_content("<think>a</think>X<think>b</think>Y"),
            ("XY", "a\nb"),
        )
        self.assertEqual(MimoClient._split_think_from_content("X<think>tail"), ("X", "tail"))

    def test_plain_text_untouched(self):
        self.assertEqual(MimoClient._split_think_from_content("plain text"), ("plain text", ""))


class ToolHistoryTests(unittest.TestCase):
    def test_serialize_tool_calls_keeps_name_and_arguments(self):
        calls = [{
            "id": "call_1",
            "type": "function",
            "function": {"name": "bash", "arguments": json.dumps({"command": "ls *.md | wc -l"})},
        }]
        self.assertEqual(_serialize_tool_calls(calls), "TOOL_CALL: bash(command='ls *.md | wc -l')")

    def test_history_transcript_pairs_call_with_result(self):
        class Msg:
            def __init__(self, role, content=None, tool_calls=None, tool_call_id=None):
                self.role = role
                self.content = content
                self.tool_calls = tool_calls
                self.tool_call_id = tool_call_id

        messages = [
            Msg("user", "数 .md 文件"),
            Msg("assistant", None, [{
                "id": "call_abc123def456",
                "type": "function",
                "function": {"name": "bash", "arguments": json.dumps({"command": "ls *.md | wc -l"})},
            }]),
            Msg("tool", "3", None, "call_abc123def456"),
        ]
        query = build_query_from_messages(messages)
        self.assertIn("assistant: TOOL_CALL: bash(command='ls *.md | wc -l')", query)
        self.assertIn("tool: [tool_result id=call_abc] 3", query)
        self.assertNotIn("None(None)", query)


class UsageChunkTests(unittest.TestCase):
    def test_usage_chunk_is_openai_shaped(self):
        raw = _build_usage_chunk("chatcmpl-x", "mimo", {"promptTokens": 12, "completionTokens": 5})
        payload = json.loads(raw[len("data: "):].strip())
        self.assertEqual(payload["choices"], [])
        self.assertEqual(
            payload["usage"],
            {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17},
        )


if __name__ == "__main__":
    unittest.main()



class StructuredMessagesTests(unittest.TestCase):
    """结构化历史：上游本身支持角色分离，压平会让模型每轮重新回顾任务。"""

    @staticmethod
    def _msg(**kw):
        from app.models import OpenAIMessage

        return OpenAIMessage(**kw)

    def test_roles_and_tool_pairing_are_preserved(self):
        from app.utils import build_upstream_messages

        msgs = [
            self._msg(role="system", content="SYS"),
            self._msg(role="user", content="数 .ts"),
            self._msg(role="assistant", content=None, tool_calls=[{
                "id": "call_1", "type": "function",
                "function": {"name": "bash", "arguments": "{}"},
            }]),
            self._msg(role="tool", tool_call_id="call_1", content="155"),
        ]
        out = build_upstream_messages(msgs)
        self.assertEqual([m["role"] for m in out], ["system", "user", "assistant", "tool"])
        self.assertEqual(out[2]["tool_calls"][0]["id"], "call_1")
        self.assertEqual(out[3]["tool_call_id"], "call_1")

    def test_orphan_tool_result_is_dropped(self):
        from app.utils import build_upstream_messages

        msgs = [
            self._msg(role="user", content="u"),
            self._msg(role="tool", tool_call_id="gone", content="orphan"),
        ]
        out = build_upstream_messages(msgs)
        self.assertEqual([m["role"] for m in out], ["user"])

    def test_system_messages_merged_into_first(self):
        from app.utils import build_upstream_messages

        msgs = [
            self._msg(role="system", content="A"),
            self._msg(role="user", content="u"),
            self._msg(role="system", content="B"),
        ]
        out = build_upstream_messages(msgs)
        self.assertEqual(out[0], {"role": "system", "content": "A\n\nB"})

    def test_media_attached_to_last_user_message(self):
        from app.mimo_client import MimoClient

        msgs = [
            {"role": "user", "content": "看图"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "再一张"},
        ]
        out = MimoClient._attach_media(msgs, [{"url": "https://x/img.png"}])
        self.assertEqual(out[-1]["content"][0], {"type": "text", "text": "再一张"})
        self.assertEqual(out[-1]["content"][1]["image_url"]["url"], "https://x/img.png")
        self.assertEqual(out[0]["content"], "看图")


class StructuredSwitchTests(unittest.TestCase):
    def test_default_on_and_env_off(self):
        import os

        from app.utils import structured_messages_enabled

        old = os.environ.pop("MIMO_STRUCTURED_MESSAGES", None)
        try:
            self.assertTrue(structured_messages_enabled())
            os.environ["MIMO_STRUCTURED_MESSAGES"] = "0"
            self.assertFalse(structured_messages_enabled())
        finally:
            os.environ.pop("MIMO_STRUCTURED_MESSAGES", None)
            if old is not None:
                os.environ["MIMO_STRUCTURED_MESSAGES"] = old


if __name__ == "__main__":
    unittest.main()
