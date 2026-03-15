import stat
import tempfile
import unittest
from pathlib import Path

from tests.support import make_bridge


class OpsecTests(unittest.TestCase):
    def test_sanitize_sensitive_text_redacts_tokens_and_bearer_headers(self) -> None:
        bridge = make_bridge()
        bridge.bot_token = "123456:telegram-secret"
        bridge.openai_api_key = "sk-secret-openai"
        text = (
            "Telegram failed for https://api.telegram.org/bot123456:telegram-secret/getUpdates "
            "with Authorization: Bearer sk-secret-openai"
        )

        sanitized = bridge.sanitize_sensitive_text(text)

        self.assertNotIn("123456:telegram-secret", sanitized)
        self.assertNotIn("sk-secret-openai", sanitized)
        self.assertIn("[redacted-telegram-token]", sanitized)
        self.assertIn("Bearer [redacted]", sanitized)

    def test_write_private_text_uses_private_file_permissions(self) -> None:
        bridge = make_bridge()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "secret.txt"

            bridge.write_private_text(path, "secret-value")

            mode = stat.S_IMODE(path.stat().st_mode)
        self.assertEqual(mode, 0o600)

if __name__ == "__main__":
    unittest.main()
