import os
import shutil
import subprocess
import unittest

from platform_support import _ps_quote


class PowerShellQuoteTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt" and shutil.which("powershell"), "Windows PowerShell required")
    def test_real_powershell_roundtrip(self):
        for value in ("C:\\Users\\O'Brien\\app.exe", "C:\\Teô\\‘’‚‛\\app.exe",
                      "line\r\nnext\t$();", "emoji 🐒", ""):
            with self.subTest(value=value):
                expression = _ps_quote(value)
                self.assertTrue(expression.isascii())
                script = "[Console]::Write([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(" + expression + ")))"
                result = subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                    check=True, capture_output=True, text=True, timeout=15,
                )
                import base64
                self.assertEqual(base64.b64decode(result.stdout).decode("utf-8"), value)
