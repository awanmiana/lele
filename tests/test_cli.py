import csv
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from finworld.analysis import engine
from finworld.cli.main import COMMANDS, MENU_LABELS, main
from finworld.core import registry
from finworld.fetchers.http import SourceError


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "registry.db"
        self.fixture = self.root / "import fixture.json"
        self.fixture.write_text(json.dumps({
            "entities": [
                {"key": "local:bank", "kind": "bank", "name": "Example Bank", "country": "US",
                 "attributes": {"source_url": "local:report", "currency": "USD"},
                 "metrics": [{"k": "assets", "v": 100, "period": "2025", "source": "local:report"},
                             {"k": "liabilities", "v": 75, "period": "2025", "source": "local:report"}]},
                {"key": "local:authority", "kind": "regulator", "name": "Authority", "country": "GB",
                 "attributes": {"source_url": "local:register"}},
            ],
            "relationships": [{"src": "local:bank", "rel": "regulated_by", "dst": "local:authority",
                               "source_url": "local:register", "evidence": "Listed in register"}],
        }), encoding="utf-8")
        guard = patch("finworld.fetchers.sources.HTTPClient", side_effect=AssertionError("Live network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    def invoke(self, *args, machine=True):
        argv = ["--db", str(self.db)] + (["--json"] if machine else []) + list(args)
        return self.capture(argv)

    def capture(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = main(argv)
        self.assertIs(type(status), int)
        return status, out.getvalue(), err.getvalue()

    def success(self, *args):
        status, out, err = self.invoke(*args)
        self.assertEqual((status, err), (0, ""), out)
        return json.loads(out)

    def imported(self):
        self.assertEqual(self.success("import", str(self.fixture))["entities"], 2)
        return str(self.success("list", "--kind", "bank")[0]["id"])

    def test_end_to_end(self):
        self.assertEqual(self.success("init"), {"initialized": True})
        eid = self.imported()
        rows = self.success("list", "--query", "Example", "--kind", "bank", "--country", "US", "--limit", "1")
        self.assertEqual([row["name"] for row in rows], ["Example Bank"])
        payload = self.success("show", eid)
        self.assertEqual(payload["attributes"]["source_url"], "local:report")
        report = self.success("analyze", eid)
        self.assertTrue(report["found"])
        ratios = report["fundamentals"]["groups"][0]["indicators"]
        self.assertEqual(next(r["value"] for r in ratios if r["name"] == "liabilities_to_assets"), 0.75)
        self.assertEqual(self.success("relationships", eid)[0]["other_name"], "Authority")
        self.assertEqual(self.success("countries"), ["GB", "US"])
        self.assertEqual(self.success("stats")["entities"], 2)
        signal = self.success("sentiment", eid, "--text", "The bank reported strong profit growth.", "--source", "local:report", "--ref", "page 2")
        self.assertTrue(signal["recorded"])
        self.assertEqual(signal["ref"], "page 2")
        self.assertEqual(self.success("analyze", eid)["sentiment"]["scored_signals"], 1)
        text = self.root / "sentiment text.txt"
        text.write_text("The bank reported weak losses.", encoding="utf-8")
        self.assertTrue(self.success("sentiment", eid, "--file", str(text), "--source", "local:news")["recorded"])
        self.assertEqual(len(self.success("show", eid)["signals"]), 2)
        output = self.root / "export.json"
        self.assertEqual(self.success("export", "--format", "json", "--output", str(output), "--kind", "bank")["exported"], 1)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), self.success("list", "--kind", "bank"))
        self.assertEqual(self.success("import", str(self.fixture))["entities"], 2)
        self.assertEqual(self.success("stats")["entities"], 2)

    def test_sources_catalog_without_database(self):
        catalog = self.success("sources")
        self.assertEqual([item["id"] for item in catalog], ["gleif", "fdic", "worldbank", "osfi", "sec"])
        self.assertFalse(self.db.exists())
        self.assertEqual(set(COMMANDS), {"init", "sources", "list", "show", "stats", "countries", "fetch", "import", "analyze", "sentiment", "relationships", "export", "menu"})
        self.assertEqual(set(MENU_LABELS), (set(COMMANDS) - {"menu"}) | {"help", "back", "quit"})

    def test_bounded_limits(self):
        with registry.get_conn(str(self.db)) as conn:
            for index in range(60):
                registry.upsert_entity(conn, "bank", f"Bank {index:03}")
        self.assertEqual(len(self.success("list")), 50)
        self.assertEqual(len(self.success("list", "--limit", "1")), 1)
        self.assertEqual(len(self.success("list", "--limit", "1000")), 60)
        for command in (["list"], ["fetch", "gleif"], ["export", "--format", "json", "--output", str(self.root / "out.json")]):
            for limit in ("0", "-1", "1001", "1.5", "bad", "9" * 5000):
                with self.subTest(command=command, limit=limit[:20]):
                    status, out, err = self.invoke(*command, "--limit", limit)
                    self.assertEqual((status, out), (2, ""))
                    self.assertIn("limit", err)
                    self.assertNotIn("Traceback", err)
        output = self.root / "default.json"
        self.assertEqual(self.success("export", "--format", "json", "--output", str(output))["exported"], 50)

    def test_invalid_usage_and_missing_entities(self):
        for args in (["unknown"], ["show"], ["show", "0"], ["show", "-1"], ["show", str(2**63)],
                     ["show", "NaN"], ["list", "--json"], ["list", "--db", "elsewhere"],
                     ["--js", "list"], ["list", "--lim", "2"], ["--db", "", "init"],
                     ["fetch", "other"], ["export", "--format", "xml", "--output", "out"],
                     ["export", "--format", "json"], ["import"],
                     ["sentiment", "1", "--source", "test"],
                     ["sentiment", "1", "--text", "x", "--file", "x", "--source", "test"],
                     ["sentiment", "1", "--text", "x"]):
            with self.subTest(args=args):
                status, out, err = self.invoke(*args)
                self.assertEqual((status, out), (2, ""))
                self.assertTrue(err)
                self.assertNotIn("Traceback", err)
        for command in ("show", "analyze", "relationships", "sentiment"):
            extra = ["--text", "strong profit", "--source", "test"] if command == "sentiment" else []
            status, out, err = self.invoke(command, "999", *extra)
            self.assertEqual((status, out), (1, ""))
            self.assertIn("not found", err)

    def test_fetch_dispatch_and_validation(self):
        for source in ("gleif", "fdic", "worldbank"):
            with self.subTest(source=source), patch("finworld.fetchers.sources.fetch_source", return_value={"source": source, "stored": 1}) as fetch:
                extra = ["--indicator", "NY.GDP.MKTP.CD"] if source == "worldbank" else ["--query", "Example"]
                result = self.success("fetch", source, "--country", "US", "--limit", "10", *extra)
                self.assertEqual(result["stored"], 1)
                self.assertEqual(fetch.call_args.args[1], source)
                self.assertEqual(fetch.call_args.kwargs, {"query": "" if source == "worldbank" else "Example", "country": "US", "limit": 10, "indicator": "NY.GDP.MKTP.CD", "category": "", "financials": False})
        for args in (["gleif", "--country", "USA"], ["fdic", "--country", "GB"],
                     ["worldbank", "--query", "bank"], ["worldbank", "--country", "US;GB"],
                     ["worldbank", "--indicator", "../x"], ["worldbank", "--indicator", ""],
                     ["fdic", "--indicator", "NY.GDP.MKTP.CD"], ["gleif", "--query", "x" * 201]):
            with self.subTest(args=args), patch("finworld.fetchers.sources.fetch_source") as fetch:
                status, out, err = self.invoke("fetch", *args)
                self.assertEqual((status, out), (2, ""))
                self.assertTrue(err)
                fetch.assert_not_called()

    def test_fetch_category_financials_choices_and_help(self):
        for source, flags, category, financials in (
            ("gleif", ["--category", "FUND"], "FUND", False),
            ("gleif", ["--category", "SOLE_PROPRIETOR"], "SOLE_PROPRIETOR", False),
            ("sec", ["--query", "aapl", "--financials"], "", True),
            ("sec", ["--query", "0000320193"], "", False),
        ):
            with self.subTest(source=source, flags=flags), patch("finworld.fetchers.sources.fetch_source", return_value={}) as fetch:
                self.success("fetch", source, *flags)
                self.assertEqual(fetch.call_args.kwargs["category"], category)
                self.assertEqual(fetch.call_args.kwargs["financials"], financials)
        for flags in (["gleif", "--category", "VC"], ["fdic", "--category", "FUND"],
                      ["sec", "--query", "AAPL", "--category", "FUND"],
                      ["gleif", "--financials"], ["osfi", "--financials"],
                      ["sec"], ["sec", "--query", "BRK.B"],
                      ["sec", "--query", "AAPL", "--country", "US"]):
            with self.subTest(flags=flags), patch("finworld.fetchers.sources.fetch_source") as fetch:
                status, out, err = self.invoke("fetch", *flags)
                self.assertEqual((status, out), (2, ""))
                self.assertTrue(err)
                fetch.assert_not_called()
        status, out, err = self.invoke("fetch", "--help")
        self.assertEqual((status, err), (0, ""))
        for text in ("sec", "--category", "SOLE_PROPRIETOR", "--financials", "20 MiB", "2 MiB", "previously stored", "maximum recent filings"):
            self.assertIn(text, " ".join(out.split()))

    def test_errors_do_not_leak_exception_secrets(self):
        for target, error, args in (
            ("finworld.fetchers.sources.fetch_source", SourceError("token=SECRET"), ["fetch", "fdic"]),
            ("finworld.core.registry.init_db", sqlite3.OperationalError("password=SECRET"), ["init"]),
            ("finworld.core.importer.import_json", OSError("SECRET"), ["import", str(self.fixture)]),
            ("finworld.core.importer.import_json", ValueError("SECRET"), ["import", str(self.fixture)]),
        ):
            with self.subTest(target=target), patch(target, side_effect=error):
                status, out, err = self.invoke(*args)
                self.assertEqual((status, out), (1, ""))
                self.assertTrue(err)
                self.assertNotIn("SECRET", err)
                self.assertNotIn("Traceback", err)
        self.assertEqual(self.invoke("import", str(self.root / "missing"))[0], 1)
        self.fixture.write_text('{"entities": [invalid}', encoding="utf-8")
        self.assertEqual(self.invoke("import", str(self.fixture))[0], 1)
        self.assertEqual(self.success("stats")["entities"], 0)
        self.assertEqual(self.capture(["--db", str(self.root), "init"])[0], 1)

    def test_sentiment_validation_and_insufficient_evidence(self):
        eid = self.imported()
        for text in ("", " ", "x" * (engine.MAX_TEXT_LEN + 1)):
            self.assertEqual(self.invoke("sentiment", eid, "--text", text, "--source", "test")[0], 2)
        for option, value in (("--source", " "), ("--source", "x" * (engine.MAX_SOURCE_LEN + 1)), ("--ref", "x" * (engine.MAX_REF_LEN + 1))):
            self.assertEqual(self.invoke("sentiment", eid, "--text", "strong profit", "--source", "test", option, value)[0], 2)
        path = self.root / "text.txt"
        for content in (b"\xff", b"x" * (engine.MAX_TEXT_LEN + 1)):
            path.write_bytes(content)
            self.assertEqual(self.invoke("sentiment", eid, "--file", str(path), "--source", "test")[0], 2)
        self.assertEqual(self.invoke("sentiment", eid, "--file", str(self.root / "absent"), "--source", "test")[0], 1)
        result = self.success("sentiment", eid, "--text", "The bank has assets.", "--source", "test")
        self.assertFalse(result["recorded"])
        self.assertEqual(self.success("stats")["signals"], 0)

    def test_export_csv_injection_and_quoting(self):
        names = ["=SUM(1,2)", "+1", "-1", "@example", "  =1", "\tplain", "\rplain", "\nplain", "Ordinary, \"Bank\"", "normal\nname"]
        with registry.get_conn(str(self.db)) as conn:
            for index, name in enumerate(names):
                registry.upsert_entity(conn, "bank", name, key=f"local:{index}", notes="\t=1")
        output = self.root / "entities.csv"
        self.assertEqual(self.success("export", "--format", "csv", "--output", str(output))["exported"], len(names))
        with output.open(encoding="utf-8", newline="") as stream:
            rows = {row["key"]: row for row in csv.DictReader(stream)}
        for index, name in enumerate(names):
            row = rows[f"local:{index}"]
            self.assertEqual(row["name"], ("'" if index < 8 else "") + name)
            self.assertEqual(row["notes"], "'\t=1")
            self.assertEqual(row["country"], "")
        self.assertEqual({r["name"] for r in self.success("list")}, set(names))

    def test_export_no_overwrite_force_parent_and_database_guard(self):
        self.imported()
        output = self.root / "existing.json"
        output.write_text("original", encoding="utf-8")
        args = ["export", "--format", "json", "--output", str(output)]
        self.assertEqual(self.invoke(*args)[0], 1)
        self.assertEqual(output.read_text(encoding="utf-8"), "original")
        self.assertEqual(self.success(*args, "--force")["exported"], 2)
        self.assertEqual(len(json.loads(output.read_text(encoding="utf-8"))), 2)
        absent = self.root / "missing" / "out.json"
        self.assertEqual(self.invoke("export", "--format", "json", "--output", str(absent))[0], 1)
        self.assertFalse(absent.parent.exists())
        self.assertEqual(self.invoke("export", "--format", "json", "--output", str(self.root), "--force")[0], 1)
        for destination in (self.db, Path(str(self.db) + "-wal"), Path(str(self.db) + "-shm")):
            self.assertEqual(self.invoke("export", "--format", "json", "--output", str(destination), "--force")[0], 1)
        link = self.root / "database-link"
        os.link(self.db, link)
        self.assertEqual(self.invoke("export", "--format", "json", "--output", str(link), "--force")[0], 1)
        self.assertEqual(self.success("stats")["entities"], 2)
        dangling = self.root / "dangling"
        dangling.symlink_to(self.root / "absent-target")
        self.assertEqual(self.invoke("export", "--format", "json", "--output", str(dangling))[0], 1)
        self.assertTrue(dangling.is_symlink())

    def test_export_atomic_failure_and_concurrent_creation(self):
        self.imported()
        output = self.root / "atomic.json"
        output.write_text("original", encoding="utf-8")
        args = ["export", "--format", "json", "--output", str(output)]
        for target in ("finworld.cli.main.json.dump", "finworld.cli.main.os.replace", "finworld.cli.main.os.fsync"):
            with self.subTest(target=target), patch(target, side_effect=OSError("SECRET")):
                status, out, err = self.invoke(*args, "--force")
                self.assertEqual((status, out), (1, ""))
                self.assertNotIn("SECRET", err)
            self.assertEqual(output.read_text(encoding="utf-8"), "original")
            self.assertEqual(list(self.root.glob(".finworld-*")), [])
        output.unlink()

        def race(src, dst):
            output.write_text("competitor", encoding="utf-8")
            raise FileExistsError

        with patch("finworld.cli.main.os.link", side_effect=race):
            self.assertEqual(self.invoke(*args)[0], 1)
        self.assertEqual(output.read_text(encoding="utf-8"), "competitor")
        self.assertEqual(list(self.root.glob(".finworld-*")), [])

    def test_export_parent_checked_before_registry_creates_directories(self):
        parent = self.root / "absent"
        status, out, err = self.capture([
            "--db", str(parent / "registry.db"), "export", "--format", "json",
            "--output", str(parent / "out.json"),
        ])
        self.assertEqual((status, out), (1, ""))
        self.assertIn("parent directory", err)
        self.assertFalse(parent.exists())

    def test_no_link_runtime_fails_safely(self):
        with patch("finworld.cli.main.os.link", create=True):
            del os.link
            status, out, err = self.invoke("export", "--format", "json", "--output", str(self.root / "out.json"))
        self.assertEqual((status, out), (1, ""))
        self.assertIn("runtime", err)
        self.assertFalse(self.db.exists())
        self.assertFalse((self.root / "out.json").exists())

    def test_non_tty_help_and_global_help(self):
        with patch("sys.stdin", io.StringIO("")):
            status, out, err = self.invoke(machine=False)
        self.assertEqual((status, err), (0, ""))
        self.assertIn("usage: finworld", out)
        self.assertIn("must precede the command", out)
        for command in COMMANDS:
            self.assertIn(command, out)
        self.assertFalse(self.db.exists())
        self.assertEqual(set(self.success()["commands"]), set(COMMANDS))
        self.assertEqual(self.invoke("--help", machine=False)[0], 0)

    def test_human_output_and_control_sanitization(self):
        self.imported()
        with registry.get_conn(str(self.db)) as conn:
            registry.upsert_entity(conn, "bank", "Bank\x1b[31m\nlong")
        status, out, err = self.invoke("list", machine=False)
        self.assertEqual((status, err), (0, ""))
        self.assertIn("ID", out)
        self.assertIn("Example Bank", out)
        self.assertNotIn("\x1b", out)
        self.assertIn("No results", self.invoke("list", "--query", "absent", machine=False)[1])
        for command in ("stats", "sources", "countries"):
            self.assertEqual(self.invoke(command, machine=False)[0], 0)

    def test_menu_eof_interrupt_and_default_tty(self):
        for interrupt in (EOFError, KeyboardInterrupt):
            with self.subTest(interrupt=interrupt), patch("builtins.input", side_effect=interrupt):
                status, out, err = self.invoke("menu", machine=False)
                self.assertEqual((status, out), (0, ""))
                self.assertIn("Finworld menu", err)
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=EOFError):
            self.assertEqual(self.invoke(machine=False)[0], 0)
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=AssertionError("JSON must not prompt")):
            self.assertIn("commands", self.success())

    def test_menu_reuses_dispatch_and_does_not_execute_shell(self):
        output = self.root / "menu export.json"
        inputs = ["bad", "help", "back", "show", "back", "list", "'", "import", f'"{self.fixture}"',
                  "list", "--kind bank", "export", f'--format json --output "{output}"', "stats", "quit"]
        with patch("builtins.input", side_effect=inputs), patch("os.system", side_effect=AssertionError("No shell")), patch("subprocess.run", side_effect=AssertionError("No subprocess")):
            status, out, err = self.invoke("menu")
        self.assertEqual(status, 0)
        results = [json.loads(line) for line in out.splitlines()]
        self.assertEqual(results[0]["entities"], 2)
        self.assertEqual(results[1][0]["name"], "Example Bank")
        self.assertEqual(results[2]["exported"], 2)
        self.assertEqual(results[3]["entities"], 2)
        self.assertIn("Invalid quoting", err)
        self.assertTrue(output.exists())
        for label in MENU_LABELS.values():
            self.assertIn(label, err)
        with patch("builtins.input", side_effect=["1", "quit"]):
            self.assertEqual(self.invoke("menu")[0], 0)
        with patch("builtins.input", side_effect=["import", "quit"]):
            self.assertEqual(self.invoke("menu")[0], 0)

    def test_cancelled_operation_returns_one(self):
        with patch("finworld.core.registry.init_db", side_effect=KeyboardInterrupt):
            status, out, err = self.invoke("init")
        self.assertEqual((status, out), (1, ""))
        self.assertIn("cancelled", err)

    def test_module_smoke(self):
        for args in (["--help"], ["--json", "init"], ["--json", "stats"], ["show", "999"], ["show", "bad"]):
            with self.subTest(args=args):
                result = subprocess.run([sys.executable, "-m", "finworld", "--db", str(self.db), *args],
                                        cwd=Path(__file__).resolve().parents[1], input="", capture_output=True, text=True, timeout=15)
                expected = 1 if args == ["show", "999"] else 2 if args == ["show", "bad"] else 0
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                if "--json" in args:
                    self.assertIsInstance(json.loads(result.stdout), dict)


if __name__ == "__main__":
    unittest.main()
