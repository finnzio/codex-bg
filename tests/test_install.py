import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class InstallTests(unittest.TestCase):
    def test_install_and_update_with_spaces_and_custom_codex_home(self):
        with tempfile.TemporaryDirectory(prefix="codex-bg-install-") as directory:
            home = Path(directory) / "codex home"
            env = {**os.environ, "CODEX_HOME": str(home)}
            command = ["bash", str(ROOT / "install.sh")]
            subprocess.run(command, env=env, check=True, capture_output=True)
            executable = home / "bin/codex-bg"
            self.assertTrue(os.access(executable, os.X_OK))
            self.assertEqual(executable.read_bytes(), (ROOT / "bin/codex-bg").read_bytes())
            self.assertTrue((home / "skills/codex-bg/SKILL.md").is_file())
            self.assertTrue((home / "skills/codex-bg/agents/openai.yaml").is_file())
            executable.write_text("old version")
            subprocess.run(command, env=env, check=True, capture_output=True)
            self.assertEqual(executable.read_bytes(), (ROOT / "bin/codex-bg").read_bytes())

    def test_no_skill_and_custom_bin_directory(self):
        with tempfile.TemporaryDirectory(prefix="codex-bg-install-") as directory:
            root = Path(directory)
            subprocess.run(["bash", str(ROOT / "install.sh"), "--bin-dir", str(root / "bin dir"),
                            "--skill-dir", str(root / "skills"), "--no-skill"],
                           check=True, capture_output=True)
            self.assertTrue((root / "bin dir/codex-bg").is_file())
            self.assertFalse((root / "skills").exists())

    def test_missing_option_value_fails(self):
        result = subprocess.run(["bash", str(ROOT / "install.sh"), "--bin-dir"], capture_output=True)
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
