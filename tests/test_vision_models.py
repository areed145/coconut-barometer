"""The vision provider's models, for Settings' dropdown and the check on save (#167), against faked APIs."""
import unittest
from types import SimpleNamespace as NS
from unittest import mock

import anthropic
import httpx

from coconut_barometer.vision.models import BUILT_IN, Models, choices

CLAUDE = {"provider": "claude", "model": "claude-sonnet-5-5", "api_key": "k", "base_url": None}
OLLAMA = {"provider": "ollama", "model": "llava:13b", "api_key": None, "base_url": None}


def model(id, label, images=True, lifecycle="active"):
    return NS(id=id, display_name=label, lifecycle=lifecycle, capabilities=NS(image_input=NS(supported=images)))


def claude(models: Models, listed=(), retrieved=None):
    client = mock.Mock()
    client.models.list.return_value = list(listed)
    if isinstance(retrieved, Exception):
        client.models.retrieve.side_effect = retrieved
    else:
        client.models.retrieve.return_value = retrieved
    models._claude = lambda vcfg: client
    return client


def not_found():
    return anthropic.NotFoundError("not found", response=httpx.Response(404, request=httpx.Request("GET", "https://x")), body=None)


class ModelsTest(unittest.TestCase):
    def test_claude_lists_the_models_that_take_images(self):
        m = Models()
        client = claude(m, [model("claude-sonnet-5-5", "Claude Sonnet 5.5"), model("claude-embed", "Embed", images=False),
                            model("claude-haiku-4-5", "Claude Haiku 4.5", lifecycle="retired")])
        self.assertEqual(m.available(CLAUDE), [{"id": "claude-sonnet-5-5", "label": "Claude Sonnet 5.5"}])
        m.available(CLAUDE)
        self.assertEqual(client.models.list.call_count, 1)                 # kept a while
        m.available({**CLAUDE, "api_key": "another"})
        self.assertEqual(client.models.list.call_count, 2)                 # a new key asks again

    def test_claude_check_on_save(self):
        m = Models()
        claude(m, retrieved=not_found())
        self.assertEqual(m.problem({**CLAUDE, "model": "claude-haiku-5.5"}), "vision.model: Anthropic has no model 'claude-haiku-5.5'")
        claude(m, retrieved=model("claude-embed", "Embed", images=False))
        self.assertIn("doesn't take images", m.problem({**CLAUDE, "model": "claude-embed"}))
        claude(m, retrieved=model("claude-sonnet-5-5", "Claude Sonnet 5.5"))
        self.assertIsNone(m.problem(CLAUDE))
        claude(m, retrieved=anthropic.APIConnectionError(request=httpx.Request("GET", "https://x")))
        self.assertIsNone(m.problem(CLAUDE))                               # offline: can't tell, so saved
        self.assertIsNone(Models().problem({**CLAUDE, "api_key": None}))  # no key yet: the same

    def test_ollama_lists_its_server_models(self):
        m = Models()
        resp = mock.Mock(json=lambda: {"data": [{"id": "qwen2.5vl:7b"}, {"id": "llava:13b"}]})
        with mock.patch("coconut_barometer.vision.models.requests.get", return_value=resp) as get:
            self.assertEqual([x["id"] for x in m.available(OLLAMA)], ["llava:13b", "qwen2.5vl:7b"])
            self.assertIsNone(m.problem(OLLAMA))
            self.assertIn("has no model 'llava:7b'", m.problem({**OLLAMA, "model": "llava:7b"}))
        self.assertEqual(get.call_args.args[0], "http://localhost:11434/v1/models")
        with mock.patch("coconut_barometer.vision.models.requests.get", side_effect=OSError("refused")):
            self.assertIsNone(Models().problem(OLLAMA))

    def test_choices_keep_the_current_model_and_fall_back_to_the_built_in_list(self):
        m = Models()
        claude(m, [model("claude-opus-5-5", "Claude Opus 5.5")])
        c = choices(m, CLAUDE)
        self.assertEqual(c["source"], "provider")
        self.assertEqual(c["models"][0], {"id": "claude-sonnet-5-5", "label": "claude-sonnet-5-5", "missing": True})
        down = Models()
        down._claude = mock.Mock(side_effect=LookupError("no API key yet"))
        c = choices(down, CLAUDE)
        self.assertEqual((c["source"], c["error"]), ("built-in", "no API key yet"))
        self.assertEqual([x["id"] for x in c["models"]], [x["id"] for x in BUILT_IN["claude"]])
        self.assertFalse(any(x.get("missing") for x in c["models"]))       # not asked, so not called missing
        with mock.patch("coconut_barometer.vision.models.requests.get", side_effect=OSError("refused")):
            self.assertEqual(choices(Models(), OLLAMA)["models"], [])     # typed instead


if __name__ == "__main__":
    unittest.main()
