"""Voice-transcription boundary tests; no network call is made."""
from __future__ import annotations

import json
import os
import unittest
from unittest.mock import MagicMock, patch

os.environ["NLP_SEMANTIC_ENABLED"] = "0"

from backend.services.chatbot import transcribe_audio


class VoiceTests(unittest.TestCase):
    def test_transcript_is_text_only_and_keeps_hinglish(self):
        response = MagicMock()
        response.read.return_value = json.dumps({"candidates": [{"content": {"parts": [{"text": "mujhe bahut dar lag raha hai"}]}}]}).encode()
        response.__enter__.return_value = response
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}), patch("backend.services.chatbot.request.urlopen", return_value=response) as urlopen:
            transcript = transcribe_audio(b"audio", "audio/webm", [{"role": "user", "content": "Earlier message"}])
        payload = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(transcript, "mujhe bahut dar lag raha hai")
        self.assertIn("inlineData", payload["contents"][-1]["parts"][1])

    def test_unsupported_audio_is_rejected_without_network(self):
        self.assertIsNone(transcribe_audio(b"audio", "text/plain"))
