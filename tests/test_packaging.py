import io
import json
import platform
import sqlite3
import sys
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from lele.cli.main import main
from lele.core import constants

ROOT = Path(__file__).resolve().parent.parent


class PackagingTests(unittest.TestCase):
    def setUp(self):
        self.project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    def test_metadata_matches_package_constants(self):
        self.assertEqual(self.project["name"], constants.APP_NAME)
        self.assertEqual(self.project["version"], constants.APP_VERSION)
        self.assertTrue(self.project["requires-python"].startswith(">=3.11"))
        self.assertEqual(self.project["scripts"], {"lele": "lele.cli.main:main"})
        self.assertEqual(self.project.get("dependencies", []), [])

    def test_dev_tools_are_optional_and_package_discovery_includes_lele(self):
        dev = self.project["optional-dependencies"]["dev"]
        self.assertTrue(any(item.startswith("ruff") for item in dev))
        self.assertTrue(any(item.startswith("mypy") for item in dev))
        include = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
            "tool"]["setuptools"]["packages"]["find"]["include"]
        self.assertIn("lele*", include)

    def test_supported_runtime(self):
        self.assertGreaterEqual(sys.version_info, (3, 11))
        self.assertGreaterEqual(sqlite3.sqlite_version_info, (3, 35, 0))
        self.assertEqual(constants.REGISTRY_SCHEMA_VERSION, 17)

    def test_version_command_json(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(main(["--json", "version"]), 0)
        self.assertEqual(err.getvalue(), "")
        result = json.loads(out.getvalue())
        self.assertEqual((result["name"], result["version"]),
                         (constants.APP_NAME, constants.APP_VERSION))
        self.assertEqual(result["python"], ".".join(str(part) for part in sys.version_info[:3]))
        self.assertEqual(result["implementation"], platform.python_implementation())
        self.assertEqual(result["sqlite"], sqlite3.sqlite_version)
        self.assertEqual(result["platform"], sys.platform)
        self.assertTrue(result["machine"])
        self.assertEqual(result["schema_version"], constants.REGISTRY_SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
