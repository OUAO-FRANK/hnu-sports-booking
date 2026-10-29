"""Old launch commands must reach the menu, never start booking implicitly."""
from unittest.mock import patch
import unittest

import auto


class CliEntryTests(unittest.TestCase):
    def run_entry(self, args):
        entry = getattr(auto, "cli_main", None)
        self.assertIsNotNone(entry, "CLI must default to the confirmation menu")
        with patch.object(auto, "main", return_value=0) as book, \
             patch("booking_menu.main", return_value=0) as menu:
            self.assertEqual(entry(args), 0)
        return book, menu

    def test_old_fixed_slot_launcher_cannot_start_booking(self):
        book, menu = self.run_entry(["--venue", "57", "--times",
            "20:10-21:10;21:10-22:10", "--at", "22:00:00"])
        book.assert_not_called()
        menu.assert_called_once_with()

    def test_no_arguments_opens_menu(self):
        book, menu = self.run_entry([])
        book.assert_not_called()
        menu.assert_called_once_with()

    def test_explicit_noninteractive_mode_is_required_for_cli_booking(self):
        args = ["--non-interactive", "--venue", "57", "--now"]
        book, menu = self.run_entry(args)
        book.assert_called_once_with(args)
        menu.assert_not_called()

    def test_dry_run_stays_nonbooking_preflight(self):
        book, menu = self.run_entry(["--dry-run"])
        book.assert_called_once_with(["--dry-run"])
        menu.assert_not_called()


if __name__ == "__main__":
    unittest.main()
