"""Read-only health checks for a local install."""
import json
import os
import shutil
import stat
import subprocess
import sys

from . import config as config_module
from .hashing import SaltError, load_salt

LAUNCHD_LABEL = "io.github.claude-usage-collector"
MIN_CLEANUP_DAYS = 90


class Doctor:
    def __init__(self, config_path: str, environ):
        self.config_path = config_path
        self.environ = environ
        self.results = []

    def run(self) -> list:
        try:
            config = config_module.load_config(self.config_path)
        except config_module.ConfigError as error:
            self._add("FAIL", "config", str(error))
            return self.results
        self._add("OK", "config", "loaded")
        self._check_salt()
        self._check_sources(config["sources"])
        self._check_destination(config["destination"])
        self._check_launchd()
        self._check_ccusage(config["crosscheck"])
        self._check_retention()
        return self.results

    def _add(self, level: str, name: str, detail: str) -> None:
        self.results.append((level, name, detail))

    def _expand(self, path: str) -> str:
        home = self.environ.get("HOME", "~")
        if path.startswith("~"):
            path = home + path[1:]
        return os.path.abspath(path)

    def _check_salt(self) -> None:
        path = config_module.salt_path(self.config_path)
        try:
            load_salt(path)
        except SaltError as error:
            self._add("FAIL", "salt", str(error))
            return
        if stat.S_IMODE(os.stat(path).st_mode) & 0o077:
            self._add("WARN", "salt", "salt file is readable by others, chmod 600 it")
        else:
            self._add("OK", "salt", "present and private")

    def _check_sources(self, sources) -> None:
        found = [s for s in sources if os.path.isdir(self._expand(s))]
        if found:
            self._add("OK", "sources", "%d of %d found" % (len(found), len(sources)))
        else:
            self._add("WARN", "sources", "no transcript folder found yet")

    def _check_destination(self, destination: dict) -> None:
        path = self._expand(destination["path"])
        self._check_writable(path)
        if destination["type"] == "git":
            self._check_git(path, destination.get("remote", "origin"))

    def _check_writable(self, path: str) -> None:
        if os.path.isdir(path):
            level = "OK" if os.access(path, os.W_OK) else "FAIL"
            self._add(level, "destination", "writable" if level == "OK" else "not writable")
            return
        parent = os.path.dirname(path)
        while parent and not os.path.exists(parent):
            parent = os.path.dirname(parent)
        if parent and os.access(parent, os.W_OK):
            self._add("WARN", "destination", "missing, will be created on first collect")
        else:
            self._add("FAIL", "destination", "missing and cannot be created")

    def _check_git(self, path: str, remote: str) -> None:
        if not os.path.isdir(os.path.join(path, ".git")):
            self._add("FAIL", "git", "destination is not a git clone")
            return
        result = self._command(["git", "ls-remote", "--heads", remote], cwd=path)
        if result is not None and result.returncode == 0:
            self._add("OK", "git", "remote reachable")
        else:
            self._add("FAIL", "git", "remote not reachable")

    def _check_launchd(self) -> None:
        if sys.platform != "darwin" or not self._which("launchctl"):
            self._add("WARN", "launchd", "not available, schedule runs another way")
            return
        result = self._command(["launchctl", "list", LAUNCHD_LABEL])
        if result is not None and result.returncode == 0:
            self._add("OK", "launchd", "agent loaded")
        else:
            self._add("WARN", "launchd", "agent not loaded, run install/install.sh")

    def _check_ccusage(self, crosscheck: dict) -> None:
        if not crosscheck.get("enabled", True):
            self._add("OK", "ccusage", "cross-check disabled")
        elif self._which(crosscheck.get("command", "ccusage")):
            self._add("OK", "ccusage", "found on PATH")
        else:
            self._add("WARN", "ccusage", "not on PATH, monthly cross-check will be skipped")

    def _check_retention(self) -> None:
        days = _cleanup_period_days(self._expand("~/.claude/settings.json"))
        if isinstance(days, int) and days >= MIN_CLEANUP_DAYS:
            self._add("OK", "cleanupPeriodDays", "%d days" % days)
            return
        self._add(
            "WARN", "cleanupPeriodDays",
            "unset or below %d in ~/.claude/settings.json, older transcripts "
            "are deleted before they can be collected" % MIN_CLEANUP_DAYS,
        )

    def _which(self, command: str):
        return shutil.which(command, path=self.environ.get("PATH"))

    def _command(self, argv, cwd=None):
        try:
            return subprocess.run(
                argv, cwd=cwd, env=dict(self.environ), timeout=30,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None


def _cleanup_period_days(path: str):
    try:
        with open(path) as handle:
            settings = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(settings, dict):
        return None
    days = settings.get("cleanupPeriodDays")
    return days if isinstance(days, int) and not isinstance(days, bool) else None
