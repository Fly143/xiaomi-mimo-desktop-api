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
