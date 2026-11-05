"""Keep exact model pixels while eliminating strided tensor copies."""
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cv2
import numpy as np
from PIL import Image
from captcha_engine import _Yolo, _Ocr


class CaptureSession:
    def run(self, outputs, feed):
        self.tensor = feed["images"]
        return [np.zeros((1, 1, 6), dtype=np.float32)]


class ModelInputTests(unittest.TestCase):
    def thread_options(self, cpu_count):
        session = Mock()
        session.get_inputs.return_value = [SimpleNamespace(name="images", shape=[1, 3, 640, 640])]
        with patch("captcha_engine.cpu_count", return_value=cpu_count, create=True), \
             patch("onnxruntime.InferenceSession", return_value=session) as create, \
             patch.object(_Ocr, "_load", return_value=[""]):
            _Yolo(Path("synthetic.onnx"))
            _Ocr(Path("synthetic.onnx"), Path("synthetic.txt"))
        return [call.kwargs["sess_options"].intra_op_num_threads for call in create.call_args_list]

    def test_model_thread_pools_are_bounded_on_large_and_small_cpus(self):
        for cpus in (1, 2, 3, 16):
            with self.subTest(cpus=cpus):
                budgets = self.thread_options(cpus)
                self.assertEqual(len(budgets), 2)
                self.assertTrue(all(1 <= value <= min(4, cpus) for value in budgets), budgets)

    def test_unknown_cpu_count_uses_one_thread_per_model(self):
        self.assertEqual(self.thread_options(None), [1, 1])

    def test_rgb_letterbox_and_normalization_are_exact_and_contiguous(self):
        rng = np.random.default_rng(19)
        for h0, w0 in [(155, 310), (137, 91)]:
            image = Image.fromarray(rng.integers(0, 256, (h0, w0, 3), dtype=np.uint8))
            yolo = object.__new__(_Yolo)
            yolo.in_h = yolo.in_w = 640
            yolo.in_name = "images"
            yolo.sess = CaptureSession()
            yolo.infer(image)
            actual = yolo.sess.tensor

            # Independent reference for the previous BGR -> resize -> RGB contract.
            scale = min(640 / w0, 640 / h0)
            nw, nh = int(w0 * scale), int(h0 * scale)
            dx, dy = (640 - nw) // 2, (640 - nh) // 2
            reference = np.full((640, 640, 3), 114, np.uint8)
            reference[dy:dy+nh, dx:dx+nw] = cv2.resize(
                np.asarray(image)[:, :, ::-1], (nw, nh), interpolation=cv2.INTER_LINEAR)
            expected = reference[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255.0
            np.testing.assert_array_equal(actual, expected)
            self.assertTrue(actual.flags.c_contiguous, "ONNX should receive an already contiguous tensor")


if __name__ == "__main__":
    unittest.main()
