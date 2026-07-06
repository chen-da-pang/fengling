import tempfile
import unittest
from pathlib import Path

from fengling_cli.main import Config, backend_command, parse_summary_from_stdout


class FenglingCliTests(unittest.TestCase):
    def test_parse_summary_from_stdout(self):
        text = """
noise
{
  "workDir": "C:/run",
  "renderId": "abc",
  "songUrl": "https://suno.com/song/abc"
}
"""
        self.assertEqual(parse_summary_from_stdout(text)["renderId"], "abc")

    def test_local_backend_command_defaults_to_python3_when_runtime_missing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "scripts").mkdir()
            cfg = Config(app_root=str(root))
            cmd = backend_command(cfg, ["--preheat-browser"])
            self.assertIsInstance(cmd, list)
            self.assertIn("suno_auto_recut_upload.py", cmd[1])
            self.assertEqual(cmd[-1], "--preheat-browser")
            self.assertNotIn("python.exe", cmd[0])

    def test_backend_command_is_plain_local_python(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "scripts").mkdir()
            cfg = Config(app_root=str(root))
            cmd = backend_command(cfg, ["--preheat-browser"])
            joined = " ".join(cmd)
            self.assertNotIn("ssh", joined)
            self.assertIn("suno_auto_recut_upload.py", joined)


if __name__ == "__main__":
    unittest.main()
