import os
import runpy
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


class WindowsBuildSpecTests(unittest.TestCase):
    def test_windows_build_and_self_checks_use_the_release_directory(self):
        script = Path(__file__).with_name("build.bat").read_text(encoding="utf-8")
        self.assertIn("--distpath release", script)
        for check in ("credentials", "selenium"):
            self.assertIn(r"release\DeployConsoleBatchSafe.exe --self-test-" + check, script)

    @unittest.skipUnless(sys.platform == "win32", "Windows DLL search path")
    def test_native_tool_paths_are_removed_before_dependency_analysis(self):
        retained = [r"C:\Windows\System32", r"C:\Program Files\Python311"]
        tool_path = r"C:\Users\test\.cache\CODEX-RUNTIMES\runtime\native\poppler\bin"
        analysis = SimpleNamespace(pure=[], scripts=[], binaries=[], datas=[])

        def analyze(*args, **kwargs):
            self.assertEqual(os.environ["PATH"].split(os.pathsep), retained)
            return analysis

        with patch.dict(os.environ, {"PATH": os.pathsep.join([retained[0], tool_path, retained[1]])}), patch(
            "PyInstaller.utils.hooks.collect_all", return_value=([], [], [])
        ):
            runpy.run_path(str(Path(__file__).with_name("DeployConsole.spec")), init_globals={
                "Analysis": analyze, "PYZ": Mock(), "EXE": Mock(),
            })


if __name__ == "__main__":
    unittest.main()
