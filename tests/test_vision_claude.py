"""The Claude vision provider's request and its answer (#167), against a faked Anthropic client."""
import unittest
from types import SimpleNamespace as NS
from unittest import mock

from coconut_barometer.vision.claude import ClaudeProvider

SCHEMA = {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]}


def provider(*content, stop_reason="tool_use"):
    p = ClaudeProvider.__new__(ClaudeProvider)
    p.model = "claude-sonnet-5-5"
    resp = NS(content=list(content), stop_reason=stop_reason, usage=NS(input_tokens=100, output_tokens=20))
    p.client = mock.Mock()
    p.client.messages.create.return_value = resp
    return p


class ClaudeProviderTest(unittest.TestCase):
    def test_the_tool_is_asked_for_not_forced(self):
        """Sonnet 5.5, Opus 5.5 and Fable 5.1 400 on tool_choice "tool" or "any"."""
        p = provider(NS(type="thinking"), NS(type="tool_use", name="record_classification", input={"title": "Pikes Peak"}))
        out = p.classify(b"jpeg", "image/jpeg", "Classify this photo.", SCHEMA)
        self.assertEqual(out, {"title": "Pikes Peak", "_usage": {"input": 100, "output": 20}})   # after a thinking block
        sent = p.client.messages.create.call_args.kwargs
        self.assertEqual(sent["tool_choice"], {"type": "auto"})
        self.assertTrue(sent["messages"][0]["content"][1]["text"].endswith("calling record_classification."))
        self.assertGreaterEqual(sent["max_tokens"], 4000)
        self.assertNotIn("temperature", sent)

    def test_no_tool_call_says_why(self):
        for stop, said in (("refusal", "declined"), ("max_tokens", "max_tokens"), ("end_turn", "stop_reason end_turn")):
            p = provider(NS(type="text", text="A mountain."), stop_reason=stop)
            with self.assertRaises(ValueError) as e:
                p.classify(b"jpeg", "image/jpeg", "Classify this photo.", SCHEMA)
            self.assertIn(said, str(e.exception))


if __name__ == "__main__":
    unittest.main()
