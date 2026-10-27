import unittest
from video_platform.api import TaskRequest
from video_platform.inference import validate_token_budget


class PromptBudgetTests(unittest.TestCase):
    def test_wan_api_records_larger_context_without_changing_cog_profile(self):
        self.assertEqual(
            TaskRequest(prompt="rain").parameters()["max_sequence_length"], 512
        )
        self.assertEqual(
            TaskRequest(prompt="rain", model="local-cogvideo").parameters()[
                "max_sequence_length"
            ],
            226,
        )

    def test_boundary_and_negative_prompt_cannot_silently_truncate(self):
        tokenizer = lambda text, truncation=False: {"input_ids": text.split()}
        validate_token_budget(tokenizer, "a b c", "d e", 3)
        for primary, negative in [("a b c d", "e"), ("a", "b c d e")]:
            with self.assertRaisesRegex(ValueError, "超过本地模型上限3"):
                validate_token_budget(tokenizer, primary, negative, 3)
