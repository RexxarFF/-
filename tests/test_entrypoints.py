import os
import subprocess
import sys
import unittest

from classdiary.config import ROOT


class EntrypointTests(unittest.TestCase):
    def check_entrypoint(self, *args):
        result = subprocess.run([sys.executable, *args, "--check"], cwd=ROOT,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OK:", result.stdout)

    def test_run_script(self):
        self.check_entrypoint("run.py")

    def test_host_main_script(self):
        self.check_entrypoint("main.py")

    def test_package_module(self):
        self.check_entrypoint("-m", "classdiary")

    def test_bothost_detected_app_module(self):
        self.check_entrypoint("-m", "classdiary.app")

    def test_app_module_attempts_real_startup_instead_of_silent_success(self):
        env = {**os.environ, "BOT_TOKEN": ""}
        result = subprocess.run([sys.executable, "-m", "classdiary.app"], cwd=ROOT,
                                env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertIn("BOT_TOKEN", result.stderr)
