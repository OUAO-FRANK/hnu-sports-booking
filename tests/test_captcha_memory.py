"""Captcha decoding stays in memory on the submission path."""
import base64
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from PIL import Image
import auto


def challenge(token="synthetic"):
    image = io.BytesIO()
    Image.new("RGB", (310, 155), "white").save(image, format="PNG")
    return {"originalImageBase64": base64.b64encode(image.getvalue()).decode(),
            "wordList": ["甲", "乙", "丙", "丁"], "token": token, "secretKey": "synthetic"}


class CaptchaMemoryTests(unittest.TestCase):
    def test_optional_contrast_deadline_is_forwarded_to_the_model(self):
        engine = Mock()
        engine.solve.return_value = ([[1, 2]] * 4, [])
        auto.infer_captcha(engine, challenge(), contrast_deadline=123.0)
        self.assertEqual(engine.solve.call_args.kwargs, {"contrast_deadline": 123.0})
        auto.infer_captcha(engine, challenge())
        self.assertEqual(engine.solve.call_args.kwargs, {})

    def test_inference_logs_quality_counts_without_words_or_tokens(self):
        engine = Mock()
        engine.solve.return_value = ([[1, 2], [3, 4], [5, 6], [7, 8]], ["甲", "乙", "错", "错"])
        with patch.object(auto, "record") as record:
            auto.infer_captcha(engine, challenge("PRIVATE_TOKEN"))
        metrics = record.call_args.kwargs
        self.assertEqual(metrics.get("points_count"), 4)
        self.assertEqual(metrics.get("exact_ocr_matches"), 2)
        raw = json.dumps(metrics, ensure_ascii=False)
        self.assertNotIn("PRIVATE_TOKEN", raw)
        self.assertNotIn("甲", raw)

    def test_solving_does_not_write_then_reread_a_temporary_png(self):
        parent = Path(__file__).resolve().parent.parent / ".venv" / "tmp"
        parent.mkdir(parents=True, exist_ok=True)
        folder = Path(tempfile.mkdtemp(prefix="captcha-memory-", dir=parent))
        cap = challenge()
        engine = Mock()
        engine.solve.return_value = ([[1, 2], [3, 4], [5, 6], [7, 8]], [])
        with patch.object(auto, "OUT", folder), \
             patch.object(auto, "get_captcha", return_value=cap), \
             patch.object(auto, "verify_captcha", return_value={"e": 0}), \
             patch.object(auto, "aes_ecb_encrypt_b64", return_value="synthetic-verification"):
            self.assertEqual(auto.solve_captcha(engine, "synthetic"), "synthetic-verification")
        self.assertEqual(engine.solve.call_args.args[0].size, (310, 155))
        self.assertEqual(list(folder.iterdir()), [], "captcha disk I/O should not delay submission")

    def test_prefetched_challenge_still_gets_verified_and_timing_stays_in_the_log(self):
        engine = Mock()
        engine.solve.return_value = ([[1, 2], [3, 4], [5, 6], [7, 8]], [])
        with patch.object(auto, "get_captcha") as get, \
             patch.object(auto, "verify_captcha", return_value={"e": 0}) as verify, \
             patch.object(auto, "aes_ecb_encrypt_b64", return_value="synthetic-verified"), \
             patch.object(auto, "record") as record, patch("builtins.print") as console:
            result = auto.solve_captcha(engine, "synthetic", initial_cap=challenge())
        self.assertEqual(result, "synthetic-verified")
        get.assert_not_called()
        verify.assert_called_once()
        self.assertEqual(verify.call_args.args[1], "synthetic")
        console.assert_not_called()
        self.assertEqual([call.kwargs["stage"] for call in record.call_args_list],
                         ["captcha_infer", "captcha_verify"])

    def test_rejected_prefetched_challenge_gets_a_fresh_challenge(self):
        engine = Mock()
        engine.solve.return_value = ([[1, 2], [3, 4], [5, 6], [7, 8]], [])
        with patch.object(auto, "get_captcha", return_value=challenge("fresh")) as get, \
             patch.object(auto, "verify_captcha", side_effect=[{"e": 1}, {"e": 0}]) as verify, \
             patch.object(auto, "aes_ecb_encrypt_b64", return_value="synthetic-verified"):
            result = auto.solve_captcha(engine, "synthetic", initial_cap=challenge("prefetched"))
        self.assertEqual(result, "synthetic-verified")
        get.assert_called_once()
        self.assertEqual([call.args[1] for call in verify.call_args_list], ["prefetched", "fresh"])

    def test_prepared_points_are_verified_without_repeating_model_inference(self):
        engine = Mock()
        points = [[1, 2], [3, 4], [5, 6], [7, 8]]
        with patch.object(auto, "get_captcha") as get, \
             patch.object(auto, "verify_captcha", return_value={"e": 0}) as verify, \
             patch.object(auto, "aes_ecb_encrypt_b64", return_value="synthetic-verified"):
            self.assertIn("initial_points", __import__("inspect").signature(auto.solve_captcha).parameters)
            result = auto.solve_captcha(engine, "synthetic", initial_cap=challenge(), initial_points=points)
        self.assertEqual(result, "synthetic-verified")
        engine.solve.assert_not_called()
        get.assert_not_called()
        verify.assert_called_once()


if __name__ == "__main__":
    unittest.main()
