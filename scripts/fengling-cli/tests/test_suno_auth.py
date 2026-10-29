import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


CLI_ROOT = Path(__file__).resolve().parents[1]
BACKEND = CLI_ROOT.parent / "fengling-backend" / "scripts" / "suno_auto_recut_upload.py"
FAKE_OPENCLI = Path(__file__).parent / "fixtures" / "fake_opencli.js"
FAKE_TOKEN = ("eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9."
              "eyJpc3MiOiJodHRwczovL2NsZXJrLnN1bm8uY29tIiwiZXhwIjo0MTAyNDQ0ODAwfQ.signature")


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for the OpenCLI fixture")
class SunoLoginTests(unittest.TestCase):
    def run_login(self, scenario, existing_token_mode=None, current_project=False):
        with tempfile.TemporaryDirectory(prefix="fengling-auth-test-") as td:
            root = Path(td)
            app = root / "app"
            (app / "scripts").mkdir(parents=True)
            shutil.copy2(BACKEND, app / "scripts" / BACKEND.name)
            shutil.copy2(Path(__file__).parent / "fixtures" / "fake_requests.py",
                         app / "scripts" / "requests.py")
            token_file = app / "evidence" / "network" / "current_browser_token.json"
            if existing_token_mode is not None:
                token_file.parent.mkdir(parents=True)
                token_file.write_text("{}")
                token_file.chmod(existing_token_mode)

            bin_dir = root / "bin"
            bin_dir.mkdir()
            fake_opencli = bin_dir / "opencli"
            shutil.copy2(FAKE_OPENCLI, fake_opencli)
            fake_opencli.chmod(0o755)
            # Prevent a failed test from starting the real desktop browser.
            for name in ("open", "xdg-open"):
                fake_open = bin_dir / name
                fake_open.write_text("#!/bin/sh\nprintf called > \"$FENGLING_TEST_FALLBACK_MARKER\"\nexit 23\n")
                fake_open.chmod(0o755)
            python = bin_dir / "isolated-python"
            python.write_text(
                "#!/bin/sh\nexport SUNO_USE_CDP_BROWSER=0\nexec "
                + shlex.quote(sys.executable)
                + ' "$@"\n'
            )
            python.chmod(0o755)

            env = os.environ.copy()
            env.update({
                "PATH": str(bin_dir) + os.pathsep + env.get("PATH", ""),
                "FENGLING_APP_ROOT": str(app),
                "FENGLING_PYTHON": str(python),
                "FENGLING_TEST_SCENARIO": scenario,
                "FENGLING_TEST_PROBE_MARKER": str(root / "probe-called"),
                "FENGLING_TEST_FALLBACK_MARKER": str(root / "fallback-called"),
                "FENGLING_TEST_STATE_FILE": str(root / "opencli-state"),
                "SUNO_USE_OPENCLI_BROWSER": "1",
                "SUNO_REUSE_LOGIN_BROWSER": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            })
            if current_project:
                source = root / "source.wav"
                source.touch()
                args = ["run", "upload", str(source), "--project-mode", "current",
                        "--work-dir", str(root / "runs"), "--login-timeout", "10", "--execute"]
            else:
                args = ["raw", "script", "--execute", "--", "--login-only", "--login-timeout", "10"]
            result = subprocess.run(
                [sys.executable, "-m", "fengling_cli.main", "--json", *args],
                cwd=CLI_ROOT, env=env, capture_output=True, text=True, timeout=15,
            )
            result.token_mode = (stat.S_IMODE(token_file.stat().st_mode)
                                 if token_file.exists() else None)
            result.token_exists = token_file.exists()
            result.probe_called = (root / "probe-called").exists()
            result.probe_count = int((root / "probe-called").read_text()) if result.probe_called else 0
            result.fallback_called = (root / "fallback-called").exists()
            return result

    def test_login_reuses_current_suno_session_without_global_clerk(self):
        result = self.run_login("current")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertIn('"loginOk": true', payload["stdout_tail"])
        self.assertNotIn(FAKE_TOKEN, result.stdout + result.stderr)

    def test_login_still_reuses_legacy_clerk_session(self):
        result = self.run_login("legacy")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertIn('"loginOk": true', payload["stdout_tail"])
        self.assertNotIn(FAKE_TOKEN, result.stdout + result.stderr)

    def test_login_waits_for_the_current_page_to_initialize(self):
        result = self.run_login("delayed")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["ok"])
        self.assertNotIn(FAKE_TOKEN, result.stdout + result.stderr)

    def test_login_does_not_print_sensitive_page_url_parameters(self):
        result = self.run_login("sensitive-url")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("synthetic-sensitive-value", result.stdout + result.stderr)
        self.assertNotIn("#private", result.stdout + result.stderr)

    def test_missing_supported_session_explains_the_fallback(self):
        result = self.run_login("missing")
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stderr)
        self.assertIn("OpenCLI did not provide a supported Suno login",
                      payload["error"]["details"]["stdout_tail"])
        self.assertNotIn(FAKE_TOKEN, result.stdout + result.stderr)

    def test_unavailable_app_getter_fails_without_exposing_credentials(self):
        result = self.run_login("changed")
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stderr)
        self.assertIn("OpenCLI did not provide a supported Suno login",
                      payload["error"]["details"]["stdout_tail"])
        self.assertNotIn(FAKE_TOKEN, result.stdout + result.stderr)

    def test_app_getter_value_must_pass_suno_authentication(self):
        result = self.run_login("invalid-jwt")
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stderr)
        self.assertIn("OpenCLI Suno session verification failed",
                      payload["error"]["details"]["stdout_tail"])
        self.assertNotIn("invalidsignature", result.stdout + result.stderr)
        self.assertTrue(result.probe_called)

    def test_app_getter_does_not_send_an_unrelated_string_to_suno(self):
        result = self.run_login("invalid-token")
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stderr)
        self.assertIn("OpenCLI did not provide a supported Suno login",
                      payload["error"]["details"]["stdout_tail"])
        self.assertNotIn("not-a-credential", result.stdout + result.stderr)
        self.assertFalse(result.probe_called)

    def test_three_unrelated_segments_are_not_sent_to_suno(self):
        result = self.run_login("bad-three")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(result.probe_called)
        self.assertNotIn("foo.bar.baz", result.stdout + result.stderr)

    def test_current_project_rechecks_the_refreshed_session_before_saving(self):
        result = self.run_login("project-second-invalid", current_project=True)
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stderr)
        self.assertIn("OpenCLI Suno session verification failed: HTTP 401",
                      payload["error"]["details"]["stdout_tail"])
        self.assertFalse(result.token_exists)
        self.assertNotIn("invalidsignature", result.stdout + result.stderr)

    def test_expired_candidate_is_refreshed_through_opencli_before_fallback(self):
        result = self.run_login("expired-first")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.probe_count, 2)
        self.assertFalse(result.fallback_called)
        self.assertNotIn("expiredsignature", result.stdout + result.stderr)

    def test_network_error_does_not_save_or_expose_the_candidate(self):
        result = self.run_login("network-error")
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stderr)
        self.assertIn("Suno session verification temporarily unavailable",
                      payload["error"]["details"]["stderr_tail"])
        self.assertFalse(result.token_exists)
        self.assertFalse(result.fallback_called)
        self.assertNotIn(FAKE_TOKEN, result.stdout + result.stderr)
        self.assertNotIn("synthetic-sensitive-error", result.stdout + result.stderr)

    def test_transient_probe_errors_retry_without_login_fallback(self):
        for scenario in ("transient-503", "transient-429", "transient-network"):
            with self.subTest(scenario=scenario):
                result = self.run_login(scenario)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.probe_count, 2)
                self.assertFalse(result.fallback_called)

    def test_non_feed_response_does_not_claim_login_or_open_fallback(self):
        for scenario in ("html-response", "wrong-json"):
            with self.subTest(scenario=scenario):
                result = self.run_login(scenario)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(result.token_exists)
                self.assertFalse(result.fallback_called)
                self.assertNotIn(FAKE_TOKEN, result.stdout + result.stderr)

    def test_access_denial_or_persistent_service_error_does_not_open_fallback(self):
        for scenario in ("forbidden", "permanent-503"):
            with self.subTest(scenario=scenario):
                result = self.run_login(scenario)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(result.token_exists)
                self.assertFalse(result.fallback_called)
                self.assertNotIn(FAKE_TOKEN, result.stdout + result.stderr)

    def test_long_rate_limit_does_not_retry_before_server_allows_it(self):
        for scenario in ("rate-limited-long", "rate-limited-date", "rate-limited-invalid"):
            with self.subTest(scenario=scenario):
                result = self.run_login(scenario)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.probe_count, 1)
                self.assertFalse(result.fallback_called)
                payload = json.loads(result.stderr)
                self.assertIn("Suno session verification rate limited; retry later",
                              payload["error"]["details"]["stderr_tail"])

    @unittest.skipUnless(os.name == "posix", "POSIX file modes are required")
    def test_saved_login_is_private_even_when_old_file_was_readable(self):
        result = self.run_login("current", existing_token_mode=0o644)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.token_mode, 0o600)

    @unittest.skipUnless(os.name == "posix", "POSIX file modes are required")
    def test_reusing_saved_login_makes_existing_cache_private(self):
        with tempfile.TemporaryDirectory(prefix="fengling-auth-test-") as td:
            app = Path(td) / "app"
            scripts = app / "scripts"
            scripts.mkdir(parents=True)
            shutil.copy2(BACKEND, scripts / BACKEND.name)
            shutil.copy2(Path(__file__).parent / "fixtures" / "fake_requests.py",
                         scripts / "requests.py")
            token_file = app / "evidence" / "network" / "current_browser_token.json"
            token_file.parent.mkdir(parents=True)
            token_file.write_text(json.dumps({
                "token": FAKE_TOKEN, "tokenSource": "opencli-clerk",
                "href": "https://suno.com/", "ts": int(time.time() * 1000),
            }))
            token_file.chmod(0o644)
            env = os.environ.copy()
            env.update({"FENGLING_APP_ROOT": str(app), "FENGLING_PYTHON": sys.executable,
                        "SUNO_NO_BROWSER_ON_UPLOAD": "1", "PYTHONDONTWRITEBYTECODE": "1"})
            result = subprocess.run(
                [sys.executable, "-m", "fengling_cli.main", "--json", "raw", "script",
                 "--execute", "--", "--scan-uploaded-clips"],
                cwd=CLI_ROOT, env=env, capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('"cleanupScanOk": true', json.loads(result.stdout)["stdout_tail"])
            self.assertEqual(stat.S_IMODE(token_file.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
