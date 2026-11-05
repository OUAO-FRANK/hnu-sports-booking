"""Only complete exact OCR can rescue a previously rejected four-character challenge."""
import unittest
from unittest.mock import Mock, patch
from PIL import Image
import auto
from captcha_engine import CaptchaEngine
from test_captcha_memory import challenge
WORDS = ['甲', '乙', '丙', '丁']

class ConservativeRescueTests(unittest.TestCase):

    def engine(self, confidences=(0.8, 0.8, 0.8, 0.22), heard=WORDS, extra=()):
        engine = object.__new__(CaptchaEngine)
        raw = [[x, 20, x + 32, 52, score, 0] for x, score in zip((10, 70, 130, 190), confidences)]
        engine.yolo = Mock(infer=Mock(return_value=raw + list(extra)))
        engine.ocr = Mock(read=Mock(side_effect=heard))
        return engine

    def test_low_confidence_fourth_box_requires_all_exact_hint_matches(self):
        engine = self.engine()
        points, heard = engine.solve(Image.new('RGB', (310, 155)), WORDS)
        self.assertEqual(points, [[26, 36], [86, 36], [146, 36], [206, 36]])
        self.assertEqual(heard, WORDS)
        engine.yolo.infer.assert_called_once()

    def test_ambiguous_or_duplicate_rescue_ocr_is_rejected(self):
        for heard in (['甲', '乙', '丙', '错'], ['甲', '乙', '丙', '丙'], ['甲', '乙', '丙', '丁丁']):
            with self.subTest(heard=heard):
                points, _ = self.engine(heard=heard).solve(Image.new('RGB', (310, 155)), WORDS)
                self.assertEqual(points, [])

    def test_rescue_keeps_hint_order_and_needs_four_distinct_hints(self):
        engine = self.engine(heard=['丁', '丙', '乙', '甲'])
        points, _ = engine.solve(Image.new('RGB', (310, 155)), WORDS)
        self.assertEqual(points, [[206, 36], [146, 36], [86, 36], [26, 36]])
        points, _ = self.engine().solve(Image.new('RGB', (310, 155)), ['甲', '乙', '丙', '丙'])
        self.assertEqual(points, [])

    def test_extra_low_candidate_does_not_get_arbitrarily_truncated(self):
        engine = self.engine(extra=([250, 20, 282, 52, 0.23, 0],))
        points, _ = engine.solve(Image.new('RGB', (310, 155)), WORDS)
        self.assertEqual(points, [])
        engine.ocr.read.assert_not_called()

    def test_tiny_ghost_or_oversized_box_cannot_supply_the_missing_character(self):
        for extra in ([250, 20, 254, 24, 0.22, 0], [200, 5, 300, 105, 0.23, 0]):
            with self.subTest(extra=extra):
                engine = self.engine(confidences=(0.8, 0.8, 0.8, 0.02), extra=(extra,))
                self.assertEqual(engine.solve(Image.new('RGB', (310, 155)), WORDS)[0], [])
                engine.ocr.read.assert_not_called()

    def test_normal_four_box_behavior_is_unchanged_even_if_ocr_is_incomplete(self):
        engine = self.engine(confidences=(0.8, 0.8, 0.8, 0.8), heard=['甲', '错', '错', '错'], extra=([250, 20, 282, 52, 0.23, 0],))
        points, _ = engine.solve(Image.new('RGB', (310, 155)), WORDS)
        self.assertEqual(len(points), 4)
        engine.yolo.infer.assert_called_once()

    def test_rescued_points_still_use_normal_server_verification(self):
        engine = self.engine()
        with patch.object(auto, 'verify_captcha', return_value={'e': 0}) as verify, patch.object(auto, 'get_captcha', side_effect=AssertionError('no extra GET')), patch.object(auto, 'aes_ecb_encrypt_b64', return_value='synthetic'):
            self.assertEqual(auto.solve_captcha(engine, 'fake', tries=1, initial_cap=challenge()), 'synthetic')
        verify.assert_called_once()

    def test_rejected_rescue_gets_a_fresh_challenge_instead_of_replaying(self):
        engine = self.engine(heard=WORDS * 2)
        with patch.object(auto, 'verify_captcha', side_effect=[{'e': 1}, {'e': 0}]) as verify, patch.object(auto, 'get_captcha', return_value=challenge('fresh')) as get, patch.object(auto, 'aes_ecb_encrypt_b64', return_value='synthetic'):
            self.assertEqual(auto.solve_captcha(engine, 'fake', tries=2, initial_cap=challenge('first')), 'synthetic')
        get.assert_called_once()
        self.assertEqual([call.args[1] for call in verify.call_args_list], ['first', 'fresh'])
if __name__ == '__main__':
    unittest.main()
