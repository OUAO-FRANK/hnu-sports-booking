"""Exercise cmd parsing with a harmless Python stand-in; never book a court."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(os.name == "nt", "Windows batch launcher")
class LauncherTests(unittest.TestCase):
    def test_batch_shell_has_no_non_ascii_text_for_cmd_to_misparse(self):
        source = (ROOT / "一键抢场.bat").read_bytes()
        self.assertTrue(source.isascii(), "Display Chinese from Python, keep cmd shell ASCII")
        self.assertEqual(source.count(b"\n"), source.count(b"\r\n"))

    def test_cmd_opens_interactive_menu_without_fixed_booking_arguments(self):
        source = (ROOT / "一键抢场.bat").read_bytes()
        lines = source.decode("utf-8").splitlines(keepends=True)
        # Preserve every source newline while replacing only executable calls.
        script = "".join(
            line.replace('"%HNU_PYTHON%"', 'call "%~dp0python_stub.cmd"', 1)
            if line.startswith('"%HNU_PYTHON%"') else line
            for line in lines
        )
        temp_parent = ROOT / ".venv" / "tmp"
        temp_parent.mkdir(parents=True, exist_ok=True)
        # Retain diagnostics; no real Python or network calls from the launcher.
        folder = Path(tempfile.mkdtemp(prefix="launcher-test-", dir=temp_parent))
        (folder / "launch.bat").write_bytes(script.encode("utf-8"))
        (folder / "python_stub.cmd").write_bytes(
            b'@echo off\r\necho LAUNCHER_CALL %*\r\nexit /b 0\r\n')
        for codepage in (936, 65001, 437):
            for prefix in ("call ", ""):
                with self.subTest(codepage=codepage, invocation=prefix or "direct"):
                    (folder / "run.cmd").write_bytes(
                        f'@echo off\r\nchcp {codepage} >nul\r\n{prefix}"%~dp0launch.bat"\r\n'.encode("ascii"))
                    result = subprocess.run(
                        ["cmd.exe", "/d", "/c", str(folder / "run.cmd")],
                        input=b"\r\n" * 5, capture_output=True, timeout=15,
                    )
                    self.assertEqual(result.stderr, b"", repr(result.stderr))
                    self.assertEqual(result.returncode, 0)
                    output = result.stdout.decode("utf-8")
                    self.assertIn('LAUNCHER_CALL -X utf8 -u booking_menu.py', output)
                    self.assertNotIn("--venue", output)
                    self.assertIn("HNU Reservation", output)


if __name__ == "__main__":
    unittest.main()
