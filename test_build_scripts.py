import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "native Unix shell required")
class PortableBuildScriptTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="deploy build scripts ")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for name in ("build-linux.sh", "build-deepin.sh", "build-macos.sh"):
            shutil.copyfile(Path(__file__).with_name(name), self.root / name)

    def run_script(self, name, env=None):
        return subprocess.run(["bash", str(self.root / name)], cwd=self.root.parent,
                              env=env, capture_output=True, text=True, timeout=5)

    def test_platform_guards_reject_wrong_host_before_creating_build_files(self):
        binary = self.root / "bin"
        binary.mkdir()
        uname = binary / "uname"
        env = dict(os.environ, PATH=str(binary) + os.pathsep + os.environ.get("PATH", ""))
        for script, system in (("build-linux.sh", "Darwin"), ("build-macos.sh", "Linux")):
            with self.subTest(script=script):
                uname.write_text("#!/bin/sh\necho " + system + "\n", encoding="utf-8")
                uname.chmod(0o755)
                result = self.run_script(script, env)
                self.assertEqual(result.returncode, 1)
                self.assertIn("Build failed: run", result.stderr)
                self.assertFalse((self.root / "dist").exists())

    def test_deepin_entry_preserves_legacy_package_from_shared_build(self):
        (self.root / "build-linux.sh").write_text(
            '#!/bin/bash\nset -eu\nmkdir -p dist\n'
            'printf package > "dist/DeployConsoleParallel-linux-$(uname -m).tar.gz"\n',
            encoding="utf-8",
        )
        result = self.run_script("build-deepin.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        packages = list((self.root / "dist").glob("DeployConsoleParallel-deepin25-*.tar.gz"))
        self.assertEqual(len(packages), 1)
        self.assertEqual(packages[0].read_bytes(), b"package")

    def test_deepin_entry_does_not_package_after_shared_build_fails(self):
        (self.root / "build-linux.sh").write_text("#!/bin/bash\nexit 7\n", encoding="utf-8")
        result = self.run_script("build-deepin.sh")
        self.assertEqual(result.returncode, 7)
        self.assertFalse((self.root / "dist").exists())


if __name__ == "__main__":
    unittest.main()
