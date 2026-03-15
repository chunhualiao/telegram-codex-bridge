import json
import tempfile
import unittest
from pathlib import Path

from tests.support import make_bridge


class VoiceTranscriptionTests(unittest.TestCase):
    def test_transcribe_audio_uses_in_process_http_request(self) -> None:
        bridge = make_bridge()
        bridge.openai_api_key = "sk-unit-test"
        bridge.transcribe_model = "whisper-1"
        bridge.transcribe_prompt = "hello"
        captured: dict[str, object] = {}

        class FakeResponse:
            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps({"text": "transcribed text"}).encode("utf-8")

        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "voice.wav"
            source.write_bytes(b"fake-wav")

            import bridge as bridge_module

            original_urlopen = bridge_module.urllib.request.urlopen

            def fake_urlopen(req, timeout=0):
                captured["url"] = req.full_url
                captured["headers"] = dict(req.header_items())
                captured["data"] = req.data
                captured["timeout"] = timeout
                return FakeResponse()

            bridge_module.urllib.request.urlopen = fake_urlopen
            try:
                text = bridge.transcribe_audio(source)
            finally:
                bridge_module.urllib.request.urlopen = original_urlopen

        self.assertEqual(text, "transcribed text")
        self.assertEqual(captured["url"], "https://api.openai.com/v1/audio/transcriptions")
        headers = {str(key).lower(): str(value) for key, value in captured["headers"].items()}
        self.assertEqual(headers["authorization"], "Bearer sk-unit-test")
        self.assertIn("multipart/form-data; boundary=", headers["content-type"])
        self.assertIsInstance(captured["data"], bytes)
        self.assertIn(b'name=\"model\"', captured["data"])
        self.assertIn(b'name=\"prompt\"', captured["data"])
        self.assertIn(b'filename=\"voice.wav\"', captured["data"])


if __name__ == "__main__":
    unittest.main()
