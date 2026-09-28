"""The rename from finworld to lele must not lose anything.

A rename is a correctness problem, not a cosmetic one. The package, the console
script, the home directory and six environment variables all changed name. If
any of those changes silently, a user who upgrades finds an empty tool: a new
home directory with no registry, a config that stopped being read, a clock that
stopped being pinned.

So the current names are the ones used everywhere, and the previous names are
still honoured with the current one winning. Nothing is copied or moved,
because a rename that relocates a database behind the user's back is how a
rename destroys work.

No network. Temporary files only.
"""
import importlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent


def fresh(module="lele.core.constants"):
    """Re-import a module so its environment reads are re-evaluated."""
    return importlib.reload(importlib.import_module(module))


class NameTests(unittest.TestCase):

    def setUp(self):
        patcher = {key: os.environ.pop(key, None)
                   for key in ("LELE_DB", "FINWORLD_DB", "LELE_NOW", "FINWORLD_NOW")}
        self.addCleanup(lambda: [os.environ.__setitem__(k, v) if v is not None
                                 else os.environ.pop(k, None)
                                 for k, v in patcher.items()])

    def test_the_current_name_is_the_only_one_in_the_code(self):
        from lele.core import constants
        self.assertEqual(constants.APP_NAME, "lele")
        self.assertEqual(constants.PREVIOUS_APP_NAME, "finworld")
        self.assertTrue(constants.LOGO.startswith(" _"))
        for line in constants.LOGO.splitlines():
            self.assertEqual(len(line), 27, "every row of the wordmark is the same width")
        self.assertIn("lele", constants.USER_AGENT)
        self.assertNotIn("finworld", constants.USER_AGENT)

    def test_the_logo_says_lele(self):
        """The wordmark is generated from letter blocks, so this is checkable."""
        letters = {"L": [" _    ", "| |   ", "| |   ", "| |___", "|____|"],
                   "E": [" ___  ", "| __| ", "| _|  ", "| __| ", "|___| "]}
        constants_rows = fresh().LOGO.splitlines()
        self.assertEqual(len(constants_rows), 5)
        # The offsets are derived from the block width, not written down, so the
        # test cannot be wrong about alignment in the same way twice.
        width, gap = 6, 1
        for index, name in enumerate("LELE"):
            start = index * (width + gap)
            got = [row[start:start + width] for row in constants_rows]
            self.assertEqual(got, letters[name], f"letter {index + 1} is not a {name}")
        self.assertEqual(len(constants_rows[0]),
                         4 * width + 3 * gap,
                         "the wordmark is four letters wide with a single space between")

    def test_the_logo_file_matches_the_constant(self):
        from lele.core import constants
        file_rows = (PROJECT / "LOGO").read_text(encoding="utf-8").splitlines()[:5]
        self.assertEqual(constants.LOGO, "\n".join(file_rows),
                         "the wordmark must have one definition, not two")

    def test_the_previous_database_path_is_still_found(self):
        """A registry at the old location must not become invisible."""
        with tempfile.TemporaryDirectory() as root:
            home = Path(root) / ".finworld"
            home.mkdir()
            (home / "finworld.db").touch()
            constants = fresh()
            path, source = constants._default_registry(
                os.path.join(root, ".lele"), os.path.join(root, ".finworld"))
            self.assertEqual(path, str(home / "finworld.db"))
            self.assertEqual(source, "previous_name",
                             "the reason for using the old path must be reported")

    def test_a_new_location_wins_over_the_old_one(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / ".lele").mkdir()
            (Path(root) / ".lele" / "lele.db").touch()
            (Path(root) / ".finworld").mkdir()
            (Path(root) / ".finworld" / "finworld.db").touch()
            constants = fresh()
            path, source = constants._default_registry(
                os.path.join(root, ".lele"), os.path.join(root, ".finworld"))
            self.assertEqual(path, str(Path(root) / ".lele" / "lele.db"))
            self.assertEqual(source, "default")

    def test_with_neither_present_the_new_path_is_used(self):
        with tempfile.TemporaryDirectory() as root:
            constants = fresh()
            path, source = constants._default_registry(
                os.path.join(root, ".lele"), os.path.join(root, ".finworld"))
            self.assertEqual(path, str(Path(root) / ".lele" / "lele.db"))
            self.assertEqual(source, "default",
                             "a fresh install must not look for a registry that was "
                             "never there")

    def test_the_current_environment_variable_wins(self):
        with tempfile.TemporaryDirectory() as root:
            os.environ["LELE_DB"] = str(Path(root) / "new.db")
            os.environ["FINWORLD_DB"] = str(Path(root) / "old.db")
            self.assertEqual(fresh().DB_PATH, str(Path(root) / "new.db"))
        del os.environ["LELE_DB"]
        os.environ["FINWORLD_DB"] = str(Path(root) / "old.db")
        self.assertEqual(fresh().DB_PATH, str(Path(root) / "old.db"),
                         "the previous variable must still be honoured on its own")

    def test_the_tuning_helper_reads_both_prefixes(self):
        constants = fresh()
        os.environ.pop("FINWORLD_RATE_LIMIT", None)
        os.environ["LELE_RATE_LIMIT"] = "2.5"
        self.assertEqual(constants.env("RATE_LIMIT", 1.0), "2.5")
        os.environ["FINWORLD_RATE_LIMIT"] = "9"
        self.assertEqual(constants.env("RATE_LIMIT", 1.0), "2.5",
                         "the current prefix wins over the previous one")
        del os.environ["LELE_RATE_LIMIT"]
        self.assertEqual(constants.env("RATE_LIMIT", 1.0), "9")
        del os.environ["FINWORLD_RATE_LIMIT"]
        self.assertEqual(constants.env("RATE_LIMIT", 1.0), 1.0)

    def test_the_clock_accepts_both_prefixes(self):
        from lele.core import clock
        os.environ["LELE_NOW"] = "2030-01-01T00:00:00+00:00"
        self.assertEqual(clock.now().year, 2030)
        del os.environ["LELE_NOW"]
        os.environ["FINWORLD_NOW"] = "2031-02-03T04:05:06+00:00"
        self.assertEqual(clock.now().year, 2031)
        del os.environ["FINWORLD_NOW"]

    def test_debug_accepts_both_prefixes(self):
        from lele.cli import main as cli
        os.environ.pop("FINWORLD_DEBUG", None)
        os.environ["LELE_DEBUG"] = "1"
        self.assertTrue(cli._debug())
        del os.environ["LELE_DEBUG"]
        os.environ["FINWORLD_DEBUG"] = "yes"
        self.assertTrue(cli._debug())
        del os.environ["FINWORLD_DEBUG"]
        self.assertFalse(cli._debug())


class DoctorReportingTests(unittest.TestCase):

    def test_doctor_says_which_name_and_which_path(self):
        """A surprising default must be explainable, not just correct."""
        from lele.core import db
        with tempfile.TemporaryDirectory() as root:
            report = db.health_check(str(Path(root) / "absent.db"))
            self.assertEqual(report["application"]["name"], "lele")
            self.assertEqual(report["application"]["previous_name"], "finworld")
            self.assertIn("path_source", report["registry"])
            self.assertIn("LELE_DB", str(report["registry"]["path_note"]) +
                          str(report["registry"]["path_source"]))

    def test_a_previous_location_is_named_in_the_note(self):
        from lele.core import db
        constants = fresh()
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / ".finworld").mkdir()
            (Path(root) / ".finworld" / "finworld.db").touch()
            previous = db._path_note("previous_name")
            self.assertIn("previous location", previous)
            self.assertIn("nothing was moved", previous)
            self.assertIn("LELE_DB", previous)
            self.assertEqual(db._path_note("default"),
                             f"the default location for {constants.APP_NAME}")


class PackagingNameTests(unittest.TestCase):

    def test_the_distribution_and_console_script_are_renamed(self):
        text = (PROJECT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('name = "lele"', text)
        self.assertIn('lele = "lele.cli.main:main"', text)
        self.assertIn('include = ["lele*"]', text)
        self.assertNotIn("finworld", text)

    def test_the_package_imports_under_the_new_name(self):
        import lele
        self.assertEqual(lele.__doc__.split()[0], "lele")
        self.assertTrue(hasattr(lele, "__version__"))

    def test_the_version_matches_the_package_metadata(self):
        from lele import __version__
        from lele.core import constants
        self.assertEqual(__version__, constants.APP_VERSION)
        text = (PROJECT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn(f'version = "{constants.APP_VERSION}"', text)

    def test_the_module_runs_as_lele(self):
        """`python3 -m lele version` must work, and name itself lele."""
        result = subprocess.run(
            [sys.executable, "-m", "lele", "--json", "version"],
            cwd=PROJECT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        import json
        payload = json.loads(result.stdout)
        self.assertEqual(payload["name"], "lele")
        self.assertTrue(payload["logo"])
        self.assertEqual(payload["tagline"], "a public-source financial research registry")

    def test_no_source_file_mentions_the_previous_name_outside_the_support_code(self):
        """One deliberate exception only: the constants module, which defines it."""
        offenders = []
        for path in sorted((PROJECT / "lele").rglob("*.py")):
            if path.name == "constants.py":
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if "FINWORLD" in line or "finworld" in line:
                    offenders.append(f"{path}:{number}: {line.strip()[:80]}")
        # These four read or name the previous prefix on purpose: the two that
        # still honour it as a fallback, and the two whose user-facing text has
        # to be able to tell a user which variable to change.
        allowed = ("lele/core/clock.py", "lele/fetchers/http.py", "lele/cli/main.py",
                   "lele/core/db.py")
        unexpected = [item for item in offenders
                      if not any(f"/{name}" in item for name in allowed)]
        self.assertEqual(unexpected, [],
                         "the previous name may only be read for compatibility")


if __name__ == "__main__":
    unittest.main()
