"""One conservative contrast retry; OCR always reads the original image."""
import unittest
from unittest.mock import Mock, patch

from PIL import Image
import auto
from captcha_engine import CaptchaEngine
from test_captcha_memory import challenge

WORDS = ["甲", "乙", "丙", "丁"]


def boxes(count):
    return [[10 + 60 * i, 20, 42 + 60 * i, 52, .8, 0] for i in range(count)]


class CaptchaContrastTests(unittest.TestCase):
    def setUp(self):
        clock = patch("captcha_engine.monotonic", return_value=100.0, create=True)
        clock.start()
        self.addCleanup(clock.stop)

    def engine(self, *, initial=None, enhanced=None, heard=None):
        engine = object.__new__(CaptchaEngine)
        engine.yolo = Mock(infer=Mock(side_effect=[boxes(3) if initial is None else initial,
                                                  boxes(4) if enhanced is None else enhanced]))
        engine.ocr = Mock(read=Mock(side_effect=WORDS if heard is None else heard))
        return engine

    def test_enhanced_four_boxes_require_exact_ocr_and_keep_hint_order(self):
        engine = self.engine(heard=list(reversed(WORDS)))
        image = Image.new("RGB", (310, 155), (90, 100, 110))
        points, heard = engine.solve(image, WORDS, contrast_deadline=101)
        self.assertEqual(points, [[206, 36], [146, 36], [86, 36], [26, 36]])
        self.assertEqual(heard, list(reversed(WORDS)))
        self.assertEqual(engine.yolo.infer.call_count, 2)
        self.assertIs(engine.yolo.infer.call_args_list[0].args[0], image)
        enhanced = engine.yolo.infer.call_args_list[1].args[0]
        self.assertIsNot(enhanced, image)
        self.assertNotEqual(enhanced.getpixel((0, 0)), image.getpixel((0, 0)))
        for call in engine.ocr.read.call_args_list:
            self.assertEqual(call.args[0].getpixel((0, 0)), image.getpixel((0, 0)))

    def test_mismatched_duplicate_or_multicharacter_ocr_rejects_enhancement(self):
        for heard in (["甲", "乙", "丙", "错"], ["甲", "乙", "丙", "丙"],
                      ["甲", "乙", "丙", "丁丁"]):
            with self.subTest(heard=heard):
                engine = self.engine(heard=heard)
                self.assertEqual(engine.solve(Image.new("RGB", (310, 155)), WORDS, contrast_deadline=101)[0], [])
                self.assertEqual(engine.yolo.infer.call_count, 2)
                self.assertEqual(engine.ocr.read.call_count, 4)

    def test_five_enhanced_candidates_are_not_arbitrarily_truncated(self):
        engine = self.engine(enhanced=boxes(5))
        self.assertEqual(engine.solve(Image.new("RGB", (310, 155)), WORDS, contrast_deadline=101), ([], []))
        self.assertEqual(engine.yolo.infer.call_count, 2)
        engine.ocr.read.assert_not_called()

    def test_enhanced_geometry_must_pass_existing_size_tiers(self):
        engine = self.engine(enhanced=boxes(3) + [[200, 20, 280, 100, .9, 0]])
        self.assertEqual(engine.solve(Image.new("RGB", (310, 155)), WORDS, contrast_deadline=101), ([], []))
        self.assertEqual(engine.yolo.infer.call_count, 2)
        engine.ocr.read.assert_not_called()

    def test_only_four_distinct_single_character_hints_enable_enhancement(self):
        for words in (WORDS[:3], WORDS + ["戊"], ["甲", "乙", "丙", "丙"],
                      ["甲乙", "乙", "丙", "丁"]):
            with self.subTest(words=words):
                engine = self.engine()
                engine.solve(Image.new("RGB", (310, 155)), words, contrast_deadline=101)
                engine.yolo.infer.assert_called_once()

    def test_normal_four_boxes_preserve_partial_ocr_behavior_and_one_inference(self):
        engine = self.engine(initial=boxes(4), heard=["甲", "错", "错", "错"])
        points, _ = engine.solve(Image.new("RGB", (310, 155)), WORDS, contrast_deadline=101)
        self.assertEqual(points, [[26, 36], [86, 36], [146, 36], [206, 36]])
        engine.yolo.infer.assert_called_once()

    def test_incomplete_enhancement_never_runs_another_detection(self):
        engine = self.engine(enhanced=boxes(3))
        self.assertEqual(engine.solve(Image.new("RGB", (310, 155)), WORDS, contrast_deadline=101), ([], []))
        self.assertEqual(engine.yolo.infer.call_count, 2)
        engine.ocr.read.assert_not_called()

    def test_missing_equal_or_expired_deadline_disables_extra_inference(self):
        for deadline in (None, 100, 99):
            with self.subTest(deadline=deadline):
                engine = self.engine()
                kwargs = {} if deadline is None else {"contrast_deadline": deadline}
                result = engine.solve(Image.new("RGB", (310, 155)), WORDS, **kwargs)
                self.assertEqual(result, ([], []))
                engine.yolo.infer.assert_called_once()
                engine.ocr.read.assert_not_called()

    def test_enhanced_points_still_require_normal_server_verification(self):
        engine = self.engine()
        points, _ = engine.solve(Image.new("RGB", (310, 155)), WORDS, contrast_deadline=101)
        with patch.object(auto, "verify_captcha", return_value={"e": 0}) as verify, \
             patch.object(auto, "get_captcha", side_effect=AssertionError("no network")), \
             patch.object(auto, "aes_ecb_encrypt_b64", return_value="synthetic"):
            result = auto.solve_captcha(engine, "synthetic", tries=1,
                                        initial_cap=challenge(), initial_points=points)
        self.assertEqual(result, "synthetic")
        verify.assert_called_once()
        self.assertEqual(engine.yolo.infer.call_count, 2)


if __name__ == "__main__":
    unittest.main()
