"""Offline checks for read-only local-clip Gloo vision scenarios."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

import video_eval
import gloo_client
import replay_synthetic_video


class VideoScenarios(unittest.TestCase):
    def test_replay_rejects_non_loopback_rtmp_destination(self):
        with self.assertRaisesRegex(ValueError, "loopback"):
            replay_synthetic_video.replay_clip(
                Path("unused.mp4"), "rtmp://192.168.1.5:1935/live/drone", loops=1,
            )

    def test_replay_watermark_marks_frames_as_simulated(self):
        frame = np.zeros((360, 640, 3), dtype=np.uint8)
        marked = replay_synthetic_video.watermark(frame.copy())
        self.assertEqual(marked.shape, frame.shape)
        self.assertFalse(np.array_equal(marked, frame))

    def test_synthetic_clips_round_trip_with_manifest(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory)
            manifest = video_eval.generate_synthetic_clips(output_dir, fps=2, duration_s=1)
            self.assertEqual([item["expected"] for item in manifest], list(video_eval.SYNTHETIC_LABELS))
            self.assertTrue(all("synthetic" in item["source"] for item in manifest))
            self.assertTrue((output_dir / "manifest.json").is_file())
            capture = video_eval.cv2.VideoCapture(str(output_dir / manifest[2]["clip"]))
            try:
                ok, frame = capture.read()
            finally:
                capture.release()
            self.assertTrue(ok)
            self.assertEqual(frame.shape[:2], (360, 640))

    def test_frame_is_classified_without_alert_or_flight_tools(self):
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        client = unittest.mock.MagicMock()
        client.chat.completions.create.return_value.choices[0].message.content = json.dumps({
            "label": "CLEAR", "evidence": "No activity visible.",
        })
        with patch.object(video_eval.gloo_client, "_v2_client", return_value=client):
            result = video_eval.classify_frame(frame)
        self.assertEqual(result["label"], "CLEAR")
        request = client.chat.completions.create.call_args.kwargs
        self.assertEqual(request["model"], video_eval.gloo_client.MODELS["patrol"])
        self.assertEqual(request["messages"][1]["content"][1]["type"], "image_url")
        self.assertEqual(request["response_format"], {"type": "json_object"})

    def test_missing_clip_never_calls_model(self):
        with patch.object(video_eval.gloo_client, "_v2_client") as call:
            with self.assertRaises(FileNotFoundError):
                video_eval.evaluate_clip(Path("__missing_clip__.mp4"), "CLEAR", 4, 3)
        call.assert_not_called()

    def test_invalid_model_label_rejected(self):
        frame = np.zeros((16, 16, 3), dtype=np.uint8)
        client = unittest.mock.MagicMock()
        client.chat.completions.create.return_value.choices[0].message.content = json.dumps({
            "label": "LAUNCH", "evidence": "Do it.",
        })
        with patch.object(video_eval.gloo_client, "_v2_client", return_value=client):
            with self.assertRaises(ValueError):
                video_eval.classify_frame(frame)

    def test_empty_model_response_reports_sanitized_diagnostics(self):
        frame = np.zeros((16, 16, 3), dtype=np.uint8)
        client = unittest.mock.MagicMock()
        client.chat.completions.create.return_value.choices[0].message.content = ""
        with patch.object(video_eval.gloo_client, "_v2_client", return_value=client):
            with self.assertRaisesRegex(ValueError, "returned no vision text"):
                video_eval.classify_frame(frame)

    def test_non_json_text_fails_with_bounded_message(self):
        frame = np.zeros((16, 16, 3), dtype=np.uint8)
        client = unittest.mock.MagicMock()
        client.chat.completions.create.return_value.choices[0].message.content = "I cannot classify this."
        with patch.object(video_eval.gloo_client, "_v2_client", return_value=client):
            with self.assertRaisesRegex(ValueError, "non-JSON vision text"):
                video_eval.classify_frame(frame)

    def test_gloo_responses_text_block_is_parsed(self):
        response = unittest.mock.MagicMock()
        response.json.return_value = {"output": [{
            "type": "message",
            "content": [{"type": "text", "text": '{"label":"CLEAR","evidence":"Empty scene."}'}],
        }]}
        with patch.object(gloo_client.requests, "post", return_value=response), \
                patch.object(gloo_client, "_bearer", return_value="test-token"):
            parsed = gloo_client.responses_create(
                "patrol", "system", "test", use_tradition=False, cache=None,
            )
        self.assertIn('"label":"CLEAR"', parsed["text"])


if __name__ == "__main__":
    unittest.main()