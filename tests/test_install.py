import os
import plistlib
import subprocess
import sys
import unittest

from tests import support

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "install", "install.sh")
LABEL = "io.github.claude-usage-collector"
ON_MAC = sys.platform == "darwin"


class InstallScriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = support.TempDir()
        self.home = self.tmp.join("home")
        self.fake_bin = self.tmp.join("fakebin")
        os.makedirs(self.home)
        os.makedirs(self.fake_bin)
        self.calls = self.tmp.join("launchctl.log")
        launchctl = os.path.join(self.fake_bin, "launchctl")
        with open(launchctl, "w") as handle:
            handle.write('#!/bin/sh\necho "$@" >> "%s"\nexit 0\n' % self.calls)
        os.chmod(launchctl, 0o755)

    def tearDown(self):
        self.tmp.cleanup()

    def install(self, *args, path=None, **extra_env):
        env = {"HOME": self.home, "PATH": path or self.fake_bin + ":/usr/bin:/bin"}
        env.update(extra_env)
        return subprocess.run(
            ["/bin/sh", SCRIPT] + list(args), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    @property
    def link(self):
        return os.path.join(self.home, ".local", "bin", "claude-usage")

    @property
    def plist(self):
        return os.path.join(self.home, "Library", "LaunchAgents", LABEL + ".plist")

    def launchctl_calls(self):
        if not os.path.exists(self.calls):
            return []
        with open(self.calls) as handle:
            return handle.read().splitlines()

    def test_install_links_wrapper(self):
        result = self.install("--schedule", "07:05")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            os.path.realpath(self.link),
            os.path.realpath(os.path.join(REPO, "bin", "claude-usage")),
        )

    @unittest.skipUnless(ON_MAC, "launchd is macOS only")
    def test_install_renders_plist_with_schedule_and_absolute_python(self):
        self.install("--schedule", "07:05")
        with open(self.plist, "rb") as handle:
            plist = plistlib.load(handle)
        self.assertEqual(plist["Label"], LABEL)
        self.assertEqual(plist["StartCalendarInterval"], {"Hour": 7, "Minute": 5})
        python = plist["ProgramArguments"][0]
        self.assertTrue(os.path.isabs(python))
        self.assertEqual(plist["ProgramArguments"][1:], ["-m", "usage_collector", "run"])
        self.assertEqual(plist["EnvironmentVariables"]["PYTHONPATH"], REPO)
        self.assertEqual(
            plist["StandardOutPath"],
            os.path.join(self.home, "Library", "Logs", "claude-usage-collector.log"),
        )
        self.assertTrue(any("bootstrap" in call for call in self.launchctl_calls()))

    @unittest.skipUnless(ON_MAC, "launchd is macOS only")
    def test_install_twice_is_idempotent(self):
        self.install("--schedule", "07:05")
        with open(self.plist) as handle:
            first = handle.read()
        result = self.install("--schedule", "07:05")
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(self.plist) as handle:
            self.assertEqual(handle.read(), first)

    def load_plist(self):
        with open(self.plist, "rb") as handle:
            return plistlib.load(handle)

    @unittest.skipUnless(ON_MAC, "launchd is macOS only")
    def test_zero_hour_or_minute_installs(self):
        for schedule, expected in (
            (None, {"Hour": 18, "Minute": 0}),
            ("00:00", {"Hour": 0, "Minute": 0}),
            ("09:08", {"Hour": 9, "Minute": 8}),
        ):
            with self.subTest(schedule=schedule):
                result = self.install(*(["--schedule", schedule] if schedule else []))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.load_plist()["StartCalendarInterval"], expected)
        agents = os.path.dirname(self.plist)
        self.assertEqual(os.listdir(agents), [LABEL + ".plist"])

    @unittest.skipUnless(ON_MAC, "launchd is macOS only")
    def test_values_with_xml_characters_make_a_valid_plist(self):
        odd = self.tmp.join("a&b<c>")
        os.makedirs(odd)
        result = self.install("--schedule", "07:05",
                              path=self.fake_bin + ":" + odd + ":/usr/bin:/bin")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(odd, self.load_plist()["EnvironmentVariables"]["PATH"])

    @unittest.skipUnless(ON_MAC, "launchd is macOS only")
    def test_skip_launchctl_writes_the_plist_but_loads_nothing(self):
        for args in (["--schedule", "07:05"], ["--uninstall"]):
            result = self.install(*args, CLAUDE_USAGE_SKIP_LAUNCHCTL="1")
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.launchctl_calls(), [])

    def test_uninstall_removes_link_and_agent_and_can_repeat(self):
        self.assertEqual(self.install().returncode, 0)
        for _ in range(2):
            result = self.install("--uninstall")
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(os.path.lexists(self.link))
        self.assertFalse(os.path.exists(self.plist))
        self.assertIn("kept", result.stdout)

    def test_uninstall_says_when_it_keeps_a_link_it_does_not_own(self):
        os.makedirs(os.path.dirname(self.link))
        os.symlink("/bin/echo", self.link)
        result = self.install("--uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(os.path.lexists(self.link))
        self.assertIn("kept %s" % self.link, result.stdout)
        self.assertNotIn("removed the wrapper link", result.stdout)

    def test_bad_schedule_is_rejected(self):
        for schedule in ("7pm", "24:00", "07:60"):
            with self.subTest(schedule=schedule):
                self.assertNotEqual(self.install("--schedule", schedule).returncode, 0)
        self.assertFalse(os.path.lexists(self.link))

    @unittest.skipUnless(ON_MAC, "launchd is macOS only")
    def test_schedule_defaults_to_config_value(self):
        config = os.path.join(self.home, ".config", "claude-usage-collector")
        os.makedirs(config)
        with open(os.path.join(config, "config.json"), "w") as handle:
            handle.write('{"account": "work", "schedule": "21:30"}')
        self.install()
        with open(self.plist, "rb") as handle:
            plist = plistlib.load(handle)
        self.assertEqual(plist["StartCalendarInterval"], {"Hour": 21, "Minute": 30})


if __name__ == "__main__":
    unittest.main()
