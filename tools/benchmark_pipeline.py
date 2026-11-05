"""Synthetic/local comparison only: no school requests, credentials or bookings."""
import argparse
import json
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from captcha_prefetch import CaptchaPrefetch


def compare_pipeline(samples, query_ms, captcha_ms, inference_ms):
    results = {}
    for mode in ("serial_query_then_challenge_preparation", "overlapped_query_and_challenge_preparation"):
        timings = []
        for _ in range(samples):
            def captcha_get():
                time.sleep(captcha_ms / 1000)
                time.sleep(inference_ms / 1000)
                return {"synthetic": True}
            started = time.perf_counter()
            if mode.startswith("serial"):
                time.sleep(query_ms / 1000)
                challenge = captcha_get()
            else:
                prefetch = CaptchaPrefetch(captcha_get)
                time.sleep(query_ms / 1000)
                challenge = prefetch.take()
            if challenge != {"synthetic": True}:
                raise RuntimeError("Synthetic challenge was not consumed")
            timings.append((time.perf_counter() - started) * 1000)
        results[mode] = {"median_ms": round(statistics.median(timings), 3),
                         "max_ms": round(max(timings), 3)}
    return {"simulated_query_ms": query_ms, "simulated_challenge_get_ms": captcha_ms,
            "simulated_inference_ms": inference_ms,
            "scope": "query + challenge GET + local preparation only; no verification/submission",
            "results": results}


def compare_tensor(samples):
    import cv2
    import numpy as np
    from PIL import Image
    from captcha_engine import _Yolo

    class Capture:
        def run(self, names, feed):
            self.input = feed["images"]
            return [np.zeros((1, 1, 6), dtype=np.float32)]

    image = Image.fromarray(np.random.default_rng(9).integers(0, 256, (155, 310, 3), dtype=np.uint8))
    probe = object.__new__(_Yolo)
    probe.in_h = probe.in_w = 640
    probe.in_name = "images"
    probe.sess = Capture()

    def previous():
        bgr = np.asarray(image.convert("RGB"))[:, :, ::-1]
        canvas = np.full((640, 640, 3), 114, np.uint8)
        canvas[160:480, :] = cv2.resize(bgr, (640, 320), interpolation=cv2.INTER_LINEAR)
        return canvas[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255.0

    def current():
        probe.infer(image)
        return probe.sess.input

    results = {}
    for mode, operation in (("previous_strided_input", previous), ("current_contiguous_input", current)):
        for _ in range(5):
            operation()
        timings = []
        for _ in range(samples):
            started = time.perf_counter()
            tensor = operation()
            timings.append((time.perf_counter() - started) * 1000)
        results[mode] = {"median_ms": round(statistics.median(timings), 3),
                         "c_contiguous": bool(tensor.flags.c_contiguous)}
    return {"scope": "synthetic image preprocessing; ONNX inference stubbed",
            "equal_model_inputs": bool(np.array_equal(previous(), current())), "results": results}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--query-ms", type=float, default=150)
    parser.add_argument("--captcha-ms", type=float, default=100)
    parser.add_argument("--inference-ms", type=float, default=50)
    args = parser.parse_args()
    if not (1 <= args.samples <= 20 and all(0 <= value <= 1000 for value in
            (args.query_ms, args.captcha_ms, args.inference_ms))):
        parser.error("samples must be 1-20; simulated delays must be 0-1000 ms")
    print(json.dumps({"external_requests": 0,
                      "pipeline": compare_pipeline(args.samples, args.query_ms, args.captcha_ms, args.inference_ms),
                      "tensor": compare_tensor(100)}, indent=2))
