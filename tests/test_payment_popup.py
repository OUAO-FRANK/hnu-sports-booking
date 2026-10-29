"""Payment UI regressions without opening a window or accessing the network."""
from contextlib import ExitStack
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import qr_popup
from PIL import Image


class PopupTests(unittest.TestCase):
    def show(self, callback, *, manual=False):
        root = MagicMock()
        root.winfo_screenwidth.return_value = 1920
        root.winfo_screenheight.return_value = 1080
        buttons = {}
        scheduled = []
        root.after.side_effect = lambda delay, command: scheduled.append(command)

        def button(*args, **kwargs):
            buttons[kwargs["text"]] = kwargs["command"]
            return MagicMock()

        def mainloop():
            if manual:
                paid_button = next(command for label, command in buttons.items() if "付款" in label)
                paid_button()
            else:
                # Run UI callbacks while the real worker thread supplies its result.
                for _ in range(40):
                    if root.destroy.call_count or not scheduled:
                        break
                    time.sleep(0.001)
                    scheduled.pop(0)()
            if not root.destroy.call_count:
                buttons["关闭"]()

        root.mainloop.side_effect = mainloop
        with ExitStack() as stack:
            for name, value in {"Tk": lambda: root, "Button": button,
                                "Label": MagicMock(), "Text": MagicMock()}.items():
                stack.enter_context(patch("tkinter." + name, value))
            stack.enter_context(patch("tkinter.font.Font", return_value=MagicMock()))
            stack.enter_context(patch("PIL.ImageTk.PhotoImage", return_value=MagicMock()))
            stack.enter_context(patch.object(qr_popup, "_beep_loop"))
            image = Image.new("RGB", (50, 50), "white")
            stack.enter_context(patch("PIL.Image.open", return_value=image))
            return qr_popup.show_qr("fake", on_poll=callback, png_path=Path(__file__))

    def test_manual_paid_button_cannot_confirm_payment(self):
        self.assertFalse(self.show(lambda: False, manual=True))

    def test_server_confirmation_is_the_only_success_path(self):
        self.assertTrue(self.show(lambda: True))

    def test_failed_status_query_does_not_confirm_payment(self):
        def query():
            raise RuntimeError("offline")
        self.assertFalse(self.show(query))

    def test_server_expiry_closes_without_success(self):
        def query():
            raise qr_popup.PaymentStopped("expired")
        self.assertFalse(self.show(query))

    def test_network_check_runs_outside_ui_thread_and_does_not_overlap(self):
        check_class = getattr(qr_popup, "_PaymentCheck", None)
        self.assertIsNotNone(check_class, "payment checks need a background worker")
        entered, release = threading.Event(), threading.Event()
        calls = []

        def slow_check():
            calls.append(threading.get_ident())
            entered.set()
            release.wait(1)
            return False

        check = check_class(slow_check)
        check.start()
        self.assertTrue(entered.wait(1))
        check.start()
        self.assertEqual(len(calls), 1)
        self.assertNotEqual(calls[0], threading.get_ident())
        release.set()


if __name__ == "__main__":
    unittest.main()
