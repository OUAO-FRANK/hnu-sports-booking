"""Payment UI regressions without opening a window or accessing the network."""
from contextlib import ExitStack
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import qr_popup
from PIL import Image


class PopupTests(unittest.TestCase):
    def test_warmup_prepares_a_hidden_window_without_displaying_it(self):
        warmup = getattr(qr_popup, "warmup", None)
        self.assertIsNotNone(warmup, "local QR dependencies should load before release")
        root = MagicMock()
        with patch.object(qr_popup, "_prepared_root", None, create=True), \
             patch("tkinter.Tk", return_value=root) as constructor:
            warmup()
            constructor.assert_called_once()
            root.withdraw.assert_called_once()
            root.deiconify.assert_not_called()

    def test_warmed_popup_reuses_tk_and_reveals_the_real_qr(self):
        self.assertTrue(self.show(lambda: True, preloaded=True))

    def test_expired_prepared_popup_closes_without_revealing_destroyed_window(self):
        self.assertFalse(self.show(lambda: False, preloaded=True, deadline=0))

    def show(self, callback, *, manual=False, preloaded=False, deadline=None, official=False, exit_mode=None):
        root = MagicMock()
        root.winfo_screenwidth.return_value = 1920
        root.winfo_screenheight.return_value = 1080
        buttons = {}
        scheduled = []
        root.after.side_effect = lambda delay, command: scheduled.append(command)
        root.after_idle.side_effect = lambda command: scheduled.append(command)
        def protocol(*args):
            if root.destroy.call_count:
                import tkinter
                raise tkinter.TclError("window already destroyed")
        root.protocol.side_effect = protocol

        def button(*args, **kwargs):
            buttons[kwargs["text"]] = kwargs["command"]
            return MagicMock()

        def mainloop():
            if exit_mode == "error":
                raise RuntimeError("PRIVATE_UI_ERROR")
            if exit_mode == "callback_error":
                root.report_callback_exception(RuntimeError, RuntimeError("PRIVATE_CALLBACK_ERROR"), None)
                return
            if exit_mode == "interrupt":
                raise KeyboardInterrupt()
            if exit_mode == "quit":
                return
            if exit_mode == "window_close":
                root.protocol.call_args.args[1]()
                return
            if official:
                buttons["打开学校预约记录"]()
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
            if exit_mode == "error_after_close":
                raise RuntimeError("PRIVATE_UI_ERROR")

        root.mainloop.side_effect = mainloop
        def reveal():
            if root.destroy.call_count:
                import tkinter
                raise tkinter.TclError("window already destroyed")
        root.deiconify.side_effect = reveal
        with ExitStack() as stack:
            stack.enter_context(patch.object(qr_popup, "_prepared_root", None, create=True))
            constructor = Mock(return_value=root)
            for name, value in {"Tk": constructor, "Button": button,
                                "Label": MagicMock(), "Text": MagicMock(), "Frame": MagicMock()}.items():
                stack.enter_context(patch("tkinter." + name, value))
            fonts = stack.enter_context(patch("tkinter.font.Font", return_value=MagicMock()))
            photo = stack.enter_context(patch("PIL.ImageTk.PhotoImage", return_value=MagicMock()))
            stack.enter_context(patch.object(qr_popup, "_beep_loop"))
            image = Image.new("RGB", (50, 50), "white")
            stack.enter_context(patch("PIL.Image.open", return_value=image))
            if preloaded:
                qr_popup.warmup()
            result = qr_popup.show_qr("fake", on_poll=callback, png_path=Path(__file__), deadline=deadline)
            if preloaded:
                constructor.assert_called_once()
                root.withdraw.assert_called_once()
                if deadline == 0:
                    root.deiconify.assert_not_called()
                else:
                    root.deiconify.assert_called_once()
                self.assertTrue(all(call.kwargs.get("root") is root for call in fonts.call_args_list))
                self.assertIs(photo.call_args.kwargs.get("master"), root)
                self.assertIsNone(qr_popup._prepared_root)
            return result

    def test_official_records_button_is_manual_and_never_confirms_payment(self):
        with patch("webbrowser.open", return_value=True) as open_page:
            self.assertFalse(self.show(lambda: False, official=True))
        open_page.assert_called_once_with("https://eportal.hnu.edu.cn/v2/site/ucenter?showroute=myAppointment")

    def test_official_page_is_not_opened_automatically(self):
        with patch("webbrowser.open") as open_page:
            self.show(lambda: False)
        open_page.assert_not_called()

    def test_manual_paid_button_cannot_confirm_payment(self):
        self.assertFalse(self.show(lambda: False, manual=True))

    def test_server_confirmation_is_the_only_success_path(self):
        self.assertTrue(self.show(lambda: True))

    def test_visible_popup_timing_does_not_log_payment_content(self):
        with patch.object(qr_popup, "record", create=True) as record:
            self.show(lambda: True)
        visible = [call for call in record.call_args_list
                   if call.args == ("timing",) and call.kwargs.get("stage") == "qr_popup_visible"]
        self.assertEqual(len(visible), 1)
        self.assertGreaterEqual(visible[0].kwargs["duration_ms"], 0)
        self.assertEqual(set(visible[0].kwargs), {"stage", "duration_ms"})

    def test_popup_records_one_precise_exit_reason(self):
        def ended():
            raise qr_popup.PaymentStopped("synthetic expired reservation")
        for callback, kwargs, reason, paid in (
                (lambda: False, {"manual": True}, "USER_CLOSED", False),
                (lambda: False, {"exit_mode": "window_close"}, "USER_CLOSED", False),
                (lambda: False, {"deadline": 0}, "DEADLINE", False),
                (lambda: True, {}, "CONFIRMED", True),
                (ended, {}, "RESERVATION_ENDED", False),
                (lambda: False, {"exit_mode": "quit"}, "UNEXPECTED_EXIT", False)):
            with self.subTest(reason=reason, kwargs=kwargs), patch.object(qr_popup, "record") as events:
                self.assertEqual(self.show(callback, **kwargs), paid)
                closed = [call.kwargs for call in events.call_args_list if call.args == ("payment_ui",)]
                self.assertEqual(len(closed), 1)
                self.assertEqual(closed[0]["reason"], reason)
                self.assertEqual(closed[0]["outcome"], "CONFIRMED" if paid else "UNCONFIRMED")
                self.assertEqual(set(closed[0]), {"reason", "outcome", "duration_ms"})

    def test_window_exception_is_logged_without_exposing_exception_message(self):
        with patch.object(qr_popup, "record") as events:
            self.assertFalse(self.show(lambda: False, exit_mode="error"))
        closed = [call for call in events.call_args_list if call.args == ("payment_ui",)]
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].kwargs["reason"], "UI_ERROR")
        errors = [call for call in events.call_args_list if call.kwargs.get("stage") == "qr_popup_error"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].kwargs["error_type"], "RuntimeError")
        self.assertNotIn("PRIVATE_UI_ERROR", str(events.call_args_list))

    def test_keyboard_interrupt_records_exit_and_still_interrupts(self):
        with patch.object(qr_popup, "record") as events, self.assertRaises(KeyboardInterrupt):
            self.show(lambda: False, exit_mode="interrupt")
        closed = [call for call in events.call_args_list if call.args == ("payment_ui",)]
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].kwargs["reason"], "INTERRUPTED")

    def test_tk_callback_error_records_failure_and_closes_window(self):
        with patch.object(qr_popup, "record") as events:
            self.assertFalse(self.show(lambda: False, exit_mode="callback_error"))
        closed = [call for call in events.call_args_list if call.args == ("payment_ui",)]
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].kwargs["reason"], "UI_ERROR")
        errors = [call for call in events.call_args_list if call.kwargs.get("stage") == "qr_popup_error"]
        self.assertEqual(len(errors), 1)
        self.assertNotIn("PRIVATE_", str(events.call_args_list))

    def test_exception_after_confirmation_cannot_erase_confirmed_state(self):
        with patch.object(qr_popup, "record") as events:
            self.assertTrue(self.show(lambda: True, exit_mode="error_after_close"))
        closed = [call for call in events.call_args_list if call.args == ("payment_ui",)]
        self.assertEqual(closed[0].kwargs["reason"], "CONFIRMED")
        self.assertEqual(closed[0].kwargs["outcome"], "CONFIRMED")

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
