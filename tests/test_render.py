import json
import math
import shutil
import struct
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageChops

from scam_autopsy.render import HEIGHT, WIDTH, Scene, _caption, load_case, main, render_frame, render_video


def sample_case():
    visuals = ["phone chat", "warning", "flow", "payment", "profile", "source receipt", "cards"]
    return {
        "id": "test-case", "title": "A test scam", "hook": "Check the message",
        "description": "A sourced case study", "format": "short",
        "source_urls": ["https://www.ftc.gov/consumer-alerts"],
        "scenes": [
            {"narration": "This is a narrated sentence for scene one.", "heading": f"Scene {i + 1}",
             "visual": visual, "items": ["A request", "A warning", "A safer step"], "label": "THE CASE"}
            for i, visual in enumerate(visuals)
        ],
    }


class RendererTests(unittest.TestCase):
    def test_preview_is_vertical_and_distinct_from_another_scene(self):
        with tempfile.TemporaryDirectory() as tmp:
            case_path = Path(tmp) / "case.json"
            output = Path(tmp) / "preview.png"
            case_path.write_text(json.dumps(sample_case()), encoding="utf-8")
            self.assertEqual(main([str(case_path), "--preview", "--output", str(output)]), 0)
            with Image.open(output) as preview:
                self.assertEqual(preview.size, (WIDTH, HEIGHT))
                warning = render_frame(load_case(case_path), 1, 2.1)
                self.assertIsNotNone(ImageChops.difference(preview, warning).getbbox())

    def test_sources_and_scene_count_are_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            case_path = Path(tmp) / "case.json"
            case = sample_case()
            case["source_urls"] = []
            case_path.write_text(json.dumps(case), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "source_urls"):
                load_case(case_path)
            case["source_urls"] = ["https://www.ftc.gov/consumer-alerts"]
            case["scenes"].pop()
            case_path.write_text(json.dumps(case), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "seven"):
                load_case(case_path)

    def test_caption_cues_include_lead_in(self):
        scene = Scene("Wait for the clue", "Clue", "phone", [], "Illustration")
        scene.cues = [(0.32, 0.55, "Wait"), (0.56, 0.84, "for"), (0.85, 1.12, "the"), (1.13, 1.43, "clue")]
        self.assertEqual(_caption(scene, 0.25), "")
        self.assertEqual(_caption(scene, 0.32), "Wait for the clue")
        self.assertEqual(_caption(scene, 1.7), "")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_ffmpeg_output_has_video_and_audio_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            case_path = Path(tmp) / "case.json"
            case = sample_case()
            case["format"] = "long"
            case["scenes"] = case["scenes"][:1]
            case_path.write_text(json.dumps(case), encoding="utf-8")
            loaded = load_case(case_path)

            def fake_speech(scenes, path):
                scenes[0].duration = 1.0
                with wave.open(str(path), "wb") as out:
                    out.setnchannels(1)
                    out.setsampwidth(2)
                    out.setframerate(24000)
                    out.writeframes(b"".join(
                        struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / 24000)))
                        for i in range(24000)
                    ))

            with patch("scam_autopsy.render.synthesize_scenes", fake_speech):
                receipt = render_video(loaded, Path(tmp) / "test.mp4")
            self.assertEqual(receipt["video_codec"], "h264")
            self.assertEqual(receipt["audio_codec"], "aac")
            self.assertEqual(receipt["pixel_format"], "yuv420p")
            self.assertAlmostEqual(receipt["duration_seconds"], 1.0, delta=0.15)
            self.assertGreater(receipt["integrated_lufs"], -30)
            self.assertIsNotNone(receipt["true_peak_dbfs"])


if __name__ == "__main__":
    unittest.main()
