import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ai_clients


class AIClientTests(unittest.TestCase):
    def test_load_local_env_does_not_override_existing_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text("DEEPSEEK_API_KEY=file-value\nQWEN_API_KEY=qwen-value\n", encoding="utf-8")
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "process-value"}, clear=True):
                ai_clients.load_local_env(env_file)
                self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "process-value")
                self.assertEqual(os.environ["QWEN_API_KEY"], "qwen-value")

    def test_model_status_never_exposes_api_keys(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "secret-a", "QWEN_API_KEY": "secret-b"}, clear=True):
            status = ai_clients.model_status()
        self.assertTrue(status["deepseek"]["configured"])
        self.assertTrue(status["qwen"]["configured"])
        self.assertNotIn("secret", str(status))


if __name__ == "__main__":
    unittest.main()
