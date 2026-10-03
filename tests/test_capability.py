"""Offline tests for the generated capability summary.

The summary's whole value is that it cannot drift from the code, so these tests
pin contracts rather than values: every command is described, every registry
count is a count of this program's own vocabulary, and the document never claims
coverage or correctness. A test asserting the exact byte length of the file
would pass on the day it was written and fail every time a feature was added,
which is the opposite of what this module is for.
"""
import contextlib
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from lele.analysis import capability, causes, indicators, prospective, signals, volatility
from lele.cli.main import (COMMANDS, MENU_LABELS, NEVER_OPENS_REGISTRY, READ_ONLY_ACTIONS,
                           READ_ONLY_COMMANDS, build_parser, main)
from lele.core import db, registry


def build(labels=None, read_only=None, actions=None, no_registry=None, conn=None):
    parser = build_parser()
    choices = parser._subparsers._group_actions[0].choices
    return capability.build(
        conn, labels=COMMANDS if labels is None else labels,
        read_only=READ_ONLY_COMMANDS if read_only is None else read_only,
        read_only_actions=READ_ONLY_ACTIONS if actions is None else actions,
        no_registry=NEVER_OPENS_REGISTRY if no_registry is None else no_registry,
        usage_for=lambda name: capability.usage_for(choices[name]))


class CoverageContractTests(unittest.TestCase):
    """A capability added without a line in the summary is the failure mode."""

    def setUp(self):
        self.report = build()

    def test_every_command_is_described_with_its_own_syntax(self):
        described = {row["command"] for row in self.report["commands"]}
        self.assertEqual(described, set(COMMANDS),
                         "the summary must describe every command the CLI offers")
        for row in self.report["commands"]:
            self.assertTrue(row["summary"], row["command"])
            self.assertTrue(row["usage"], row["command"])

    def test_a_changed_command_is_described_without_being_written_up_by_hand(self):
        """The property that makes the file worth having at all.

        Changing a command's one-line description in the table is the only edit
        a new feature needs: the summary picks it up, and the rendered document
        changes with it.
        """
        relabelled = dict(COMMANDS)
        relabelled["stats"] = "Registry statistics, rewritten after the summary was generated"
        report = build(labels=relabelled)
        row = next(item for item in report["commands"] if item["command"] == "stats")
        self.assertEqual(row["summary"],
                         "Registry statistics, rewritten after the summary was generated")
        self.assertTrue("rewritten after the summary was generated" in
                        capability.render_markdown(report))
        self.assertNotIn("rewritten after", capability.render_markdown(self.report))

    def test_a_command_with_no_parser_is_refused_rather_than_summarised(self):
        """A table entry the parser does not know is a defect, not a capability."""
        phantom = dict(COMMANDS)
        phantom["brand-new-command"] = "Listed but never given a parser"
        with self.assertRaises(KeyError):
            build(labels=phantom)

    def test_every_menu_choice_is_reachable(self):
        """The menu runs whatever the summary lists, so the two must agree."""
        self.assertEqual(set(MENU_LABELS) - {"help", "back", "quit"},
                         set(COMMANDS) - {"menu"},
                         "the menu offers exactly the commands the summary describes, and "
                         "reaches them through the same table")

    def test_every_observation_kind_and_family_is_named(self):
        families = self.report["taxonomy"]["observation_families"]
        named = {kind for kinds in families.values() for kind in kinds}
        self.assertEqual(named, set(signals.OBSERVATION_FAMILY))
        self.assertEqual(set(families), set(signals.OBSERVATION_FAMILY.values()))

    def test_the_frozen_vocabulary_is_quoted_not_summarised(self):
        taxonomy = self.report["taxonomy"]
        self.assertEqual(tuple(taxonomy["volatility_estimators"]), volatility.ESTIMATORS)
        self.assertEqual(taxonomy["indicator_specs"], sorted(indicators.INDICATOR_SPECS))
        self.assertEqual(taxonomy["frozen_forecast_methods"], list(prospective.METHODS))
        self.assertEqual(taxonomy["headline_categories"], sorted(causes.categories()))

    def test_every_allowlisted_endpoint_group_is_named(self):
        from lele.core import constants
        self.assertEqual({group["group"] for group in self.report["endpoints"]},
                         {"SOURCES", "PRICE_ENDPOINTS", "EVIDENCE_ENDPOINTS",
                          "SANCTIONS_ENDPOINTS", "REDIRECT_ENDPOINTS"})
        total = sum(len(mapping) for mapping in
                    (constants.SOURCES, constants.PRICE_ENDPOINTS, constants.EVIDENCE_ENDPOINTS,
                     constants.SANCTIONS_ENDPOINTS, constants.REDIRECT_ENDPOINTS))
        self.assertEqual(self.report["counts"]["allowlisted_endpoints"], total)
        self.assertGreater(total, len(self.report["sources"]),
                           "the five fetch datasets are not the whole reach of the program, "
                           "and reporting only their count would understate it")

    def test_the_standing_objective_is_quoted_at_its_frozen_value(self):
        objective = self.report["standing_objective"]
        self.assertEqual(objective["target_rate"], 0.9)
        self.assertEqual(objective["status"], "not_achieved")
        self.assertEqual(sorted(objective["required_asset_classes"]),
                         ["bitcoin", "gold", "oil", "stock"])


class HonestyTests(unittest.TestCase):
    """What the document must never be able to say."""

    def test_it_does_not_claim_correctness_or_coverage(self):
        with _registry() as conn:
            text = capability.render_markdown(build(conn=conn)).lower()
        for phrase in capability.BANNED_CLAIMS:
            self.assertTrue(phrase not in text, f"the summary must never state {phrase!r}")
        self.assertTrue("not a coverage claim" in text)
        self.assertTrue("completeness: **unknown**" in text,
                        "even with every table counted, the registry section must refuse a "
                        "completeness claim")

    def test_completeness_is_unknown_even_when_every_table_is_counted(self):
        with _registry() as conn:
            report = build(conn=conn)
        self.assertEqual(report["registry"]["status"], "ok")
        self.assertEqual(report["registry"]["completeness"], "unknown")
        self.assertIn("not a count of any institution",
                      report["registry"]["completeness_note"])

    def test_an_absent_registry_is_absent_and_not_a_row_of_zeros(self):
        report = build()
        self.assertEqual(report["registry"]["status"], "not_read")
        self.assertNotIn("tables", report["registry"],
                         "no connection means no counts; zeros would be a different claim")
        self.assertTrue("no registry was opened" in capability.render_markdown(report))

    def test_an_unreadable_registry_is_reported_rather_than_zeroed(self):
        report = build()
        with _broken() as conn:
            report["registry"] = capability._registry_state(conn)
        self.assertEqual(report["registry"]["status"], "unreadable")
        self.assertNotIn("tables", report["registry"])


class AccessLabelTests(unittest.TestCase):

    def test_a_command_whose_access_depends_on_its_action_says_so(self):
        """Collapsing it either way would be a false claim about the registry."""
        report = build()
        by_name = {row["command"]: row for row in report["commands"]}
        self.assertEqual(by_name["causes"]["access"], "read_only_for_some_actions")
        self.assertEqual(sorted(by_name["causes"]["read_only_actions"]), ["context", "profile"])
        self.assertEqual(by_name["summary"]["access"], "read_only")
        label = capability._access_label(by_name["causes"])
        self.assertIn("`context`", label)
        self.assertIn("other actions write", label)
        self.assertEqual(capability._access_label(by_name["fetch"]), "may write")
        self.assertEqual(by_name["version"]["access"], "does_not_open_the_registry")
        self.assertEqual(by_name["summary"]["access"], "read_only")

    def test_a_writing_command_is_never_labelled_read_only(self):
        report = build()
        by_name = {row["command"]: row for row in report["commands"]}
        for command in ("fetch", "edges", "import", "init", "store-evidence", "backup"):
            self.assertEqual(by_name[command]["access"], "may_write", command)


class RenderTests(unittest.TestCase):

    def test_both_formats_carry_the_same_data(self):
        report = build()
        markdown = capability.render(report, "markdown")
        self.assertEqual(json.loads(capability.render(report, "json"))["counts"],
                         report["counts"])
        self.assertTrue(str(report["counts"]["commands"]) in markdown)

    def test_an_unknown_format_is_refused(self):
        for value in ("", "html", "csv", None, 7):
            with self.assertRaises(ValueError):
                capability.render(build(), value)

    def test_the_renderer_reports_its_own_method_and_is_reproducible(self):
        """Two runs of one report must be byte-identical, or the hash is noise."""
        report = build()
        self.assertEqual(capability.render_markdown(report),
                         capability.render_markdown(report))

    def test_a_command_with_no_arguments_says_so(self):
        self.assertEqual(capability.usage_for(build_parser()._subparsers
                                              ._group_actions[0].choices["doctor"]),
                         "(no arguments)")

    def test_a_huge_choice_list_is_summarised_rather_than_enumerated(self):
        self.assertIn("INT1..INT1000", capability._choices_label(range(1, 1001)))
        self.assertIn("permitted values", capability._choices_label(
            [f"value-{index}" for index in range(40)]))
        self.assertEqual(capability._choices_label(None), "")

    def test_an_empty_or_implausible_command_table_is_refused(self):
        for labels in ({}, {f"cmd{index}": "x" for index in range(600)}):
            with self.assertRaises(ValueError):
                build(labels=labels)
        with self.assertRaises(ValueError):
            capability.build(COMMANDS, usage_for=None)


class CliTests(unittest.TestCase):
    """End to end through `main`, because the guarantees that matter are the
    ones a caller sees: what lands on the console, what lands on disk, and what
    is refused."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db = str(self.root / "registry.db")
        self.out = str(self.root / "summary.md")

    def _run(self, *extra, quiet=True):
        """Run the command, returning (exit status, console text, emitted JSON)."""
        argv = ["--db", self.db]
        if quiet:
            argv.append("--json")
        argv += ["summary", "--output", self.out, *extra]
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(io.StringIO()):
            status = main(argv)
        text = stream.getvalue()
        payload = json.loads(text) if quiet and text.strip() else None
        return status, text, payload

    def test_it_prints_the_document_and_writes_the_same_bytes(self):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(io.StringIO()):
            status = main(["--db", self.db, "summary", "--output", self.out])
        written = Path(self.out).read_text(encoding="utf-8")
        self.assertEqual(status, 0)
        self.assertTrue(written.startswith("# lele "))
        self.assertTrue(written in stream.getvalue(),
                        "the console must show the document that was saved, byte for byte, "
                        "not a summary of it")

    def test_quiet_writes_the_file_without_printing_the_document(self):
        status, text, payload = self._run("--quiet")
        self.assertEqual(status, 0)
        self.assertNotIn("## Commands", text)
        self.assertTrue(payload["printed"] is False)
        self.assertTrue(Path(self.out).exists())

    def test_the_receipt_hashes_the_bytes_that_were_written(self):
        _, _, payload = self._run("--quiet")
        written = Path(self.out).read_bytes()
        self.assertEqual(payload["output_bytes"], len(written))
        self.assertEqual(payload["output_sha256"], hashlib.sha256(written).hexdigest())
        self.assertEqual(payload["output"], self.out)
        self.assertEqual(payload["commands"], len(COMMANDS))
        self.assertGreater(payload["allowlisted_endpoints"], payload["registry_sources"])

    def test_an_existing_file_is_refused_and_force_replaces_it_atomically(self):
        Path(self.out).write_text("stale", encoding="utf-8")
        status, _, _ = self._run("--quiet")
        self.assertEqual(status, 1, "an existing file must not be clobbered by accident")
        self.assertEqual(Path(self.out).read_text(encoding="utf-8"), "stale")
        status, _, _ = self._run("--quiet", "--force")
        self.assertEqual(status, 0)
        self.assertTrue(Path(self.out).read_text(encoding="utf-8").startswith("# lele "))
        self.assertEqual([p.name for p in self.root.iterdir()
                          if p.name.startswith(".lele-summary-")], [],
                         "no temporary file may be left behind")

    def test_the_output_may_not_be_the_registry_or_its_journal(self):
        for suffix in ("", "-wal", "-shm", "-journal"):
            with self.subTest(suffix=suffix or "main"):
                with contextlib.redirect_stderr(io.StringIO()):
                    status = main(["--db", self.db, "summary", "--output", self.db + suffix])
                self.assertEqual(status, 1)

    def test_a_missing_parent_directory_is_refused(self):
        status, _, _ = self._run("--quiet", "--output", str(self.root / "absent" / "s.md"))
        self.assertEqual(status, 1)
        self.assertFalse((self.root / "absent").exists())

    def test_an_unusable_output_path_is_refused(self):
        for value in ("", "   ", "x" * 5000, "a\x00b"):
            with self.subTest(value=value[:12]):
                with contextlib.redirect_stderr(io.StringIO()):
                    status = main(["--db", self.db, "summary", "--output", value])
                self.assertNotEqual(status, 0)
        self.assertEqual([p.name for p in self.root.iterdir()], [],
                         "a refused path must leave nothing behind")

    def test_it_works_without_a_registry_and_reports_that(self):
        status, _, payload = self._run("--quiet")
        self.assertEqual(status, 0)
        self.assertEqual(payload["registry_state"], "not_read")
        self.assertFalse(Path(self.db).exists(), "a summary must not create the registry")
        self.assertTrue("no registry was opened" in
                        Path(self.out).read_text(encoding="utf-8"))

    def test_json_output_is_the_same_report_as_data(self):
        target = str(self.root / "summary.json")
        status, _, _ = self._run("--quiet", "--format", "json", "--output", target)
        self.assertEqual(status, 0)
        payload = json.loads(Path(target).read_text(encoding="utf-8"))
        self.assertEqual(payload["method"], capability.METHOD)
        self.assertEqual(len(payload["commands"]), len(COMMANDS))
        self.assertEqual(payload["standing_objective"]["target_rate"], 0.9)

    def test_the_receipt_carries_no_completeness_claim(self):
        _, _, payload = self._run("--quiet")
        self.assertEqual(payload["completeness"], "unknown")
        self.assertTrue("not a coverage claim" in payload["completeness_note"])

    def test_a_registry_is_counted_and_still_never_claimed_complete(self):
        registry.init_db(self.db)
        with closing(db.connect(self.db)) as conn:
            registry.upsert_entity(conn, kind="bank", name="Example Bank", key="local:example")
            conn.commit()
        _, _, payload = self._run("--quiet")
        self.assertEqual(payload["registry_state"], "ok")
        self.assertTrue("schema version" in Path(self.out).read_text(encoding="utf-8"))


@contextlib.contextmanager
def _registry():
    """An initialized in-memory registry that closes itself.

    It used to be a function returning an open connection, and the two call sites
    never closed it. Nothing asserted on that, so it stayed invisible until a
    later `gc.collect()` in an unrelated test finalised the connection and printed
    a ResourceWarning, which failed a test about error messages instead. A leaked
    handle in a cron run is unbounded growth, which is what `test_db_guarantees`
    exists to prevent.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    registry._initialize(conn)
    try:
        yield conn
    finally:
        conn.close()


@contextlib.contextmanager
def _broken():
    """A registry holding one table out of every one the schema promises."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE entities(id INTEGER)")
    try:
        yield conn
    finally:
        conn.close()


if __name__ == "__main__":
    unittest.main()
