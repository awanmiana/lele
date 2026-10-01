"""The single gate must actually run what it claims to run.

`.tools/check.sh` is the one command that decides whether the tree is shippable,
and it reported "all checks passed" while its bytecode step named a package
directory that the rename had already deleted. `compileall -q` on a missing path
prints "Can't list" and exits 0, so the step was a silent no-op and the lesson it
was added for -- a syntax error being the first thing noticed -- had stopped
being true.

A gate that can pass without doing its work is not a check, so the targets it
names are asserted here.
"""
import re
import subprocess
import sys
import unittest
from pathlib import Path

from lele.core import constants

ROOT = Path(__file__).resolve().parent.parent
CHECK = ROOT / ".tools" / "check.sh"

STEP = re.compile(r"^\s*run\s+(\S+)\s+(.*)$", re.MULTILINE)

# Every repository directory a step is expected to act on, and the step that
# must name it. Adding a step that reads a new directory means naming it here, so
# a renamed or deleted target cannot pass unnoticed.
EXPECTED_TARGETS = {
    "compileall": ("lele", "tests"),
    "lint-tests": ("tests",),
    "connections": ("tests",),
    "tests": ("tests",),
}


def steps() -> dict[str, list[str]]:
    return {match.group(1): match.group(2).split()
            for match in STEP.finditer(CHECK.read_text(encoding="utf-8"))}


def referenced_directories(argv: list[str]) -> set[str]:
    """The repository directories a step's arguments actually resolve to.

    Resolution rather than pattern-matching, so an interpreter, a code string or
    a discovery pattern is not mistaken for a path, and a real path is not
    excused because it does not look like one.
    """
    found = set()
    for token in argv[1:]:
        if token.startswith("-"):
            continue
        candidate = ROOT / token
        if candidate.is_dir():
            found.add(token)
    return found


class GateTests(unittest.TestCase):
    def test_expected_targets_exist_and_are_named_by_their_step(self):
        declared = steps()
        for step, targets in EXPECTED_TARGETS.items():
            self.assertIn(step, declared, f"check step {step!r} is gone; re-derive the gate")
            for target in targets:
                self.assertTrue((ROOT / target).is_dir(), f"missing gate target: {target}")
                self.assertIn(target, declared[step],
                              f"step {step!r} no longer acts on {target!r}")

    def test_package_is_compiled_not_just_imported(self):
        targets = referenced_directories(steps()["compileall"])
        self.assertIn(constants.APP_NAME, targets)
        self.assertTrue((ROOT / constants.APP_NAME / "__init__.py").is_file())

    def test_no_step_acts_on_an_undeclared_directory(self):
        declared = steps()
        for step, argv in declared.items():
            for target in referenced_directories(argv):
                self.assertIn(target, EXPECTED_TARGETS.get(step, ()),
                              f"step {step!r} acts on {target!r}, which no test covers")

    def test_compileall_exits_zero_on_a_missing_path(self):
        # Why the assertions above exist: this is the behaviour that hid the
        # no-op, pinned so a future interpreter change cannot re-arm it silently.
        result = subprocess.run(
            [sys.executable, "-m", "compileall", "-q", "no-such-package-for-this-test"],
            cwd=ROOT, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0)
        self.assertIn("Can't list", result.stdout + result.stderr)

    def test_gate_does_not_reference_the_previous_package_name(self):
        self.assertNotIn(constants.PREVIOUS_APP_NAME, CHECK.read_text(encoding="utf-8"))

    def test_the_gate_is_version_controlled(self):
        # `.tools/` was ignored, so the one command that decides whether the tree
        # is shippable existed only on the machine that wrote it -- and the step
        # above went stale there without anyone else's checkout noticing.
        self.assertTrue(CHECK.is_file(), f"the gate is missing: {CHECK}")
        patterns = [line.strip() for line in (ROOT / ".gitignore").read_text(
            encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")]
        for pattern in patterns:
            self.assertFalse(pattern in {".tools", ".tools/", "*"},
                             f".gitignore excludes the whole gate via {pattern!r}; "
                             "ignore .tools/* and re-admit .tools/check.sh instead")
        self.assertIn("!.tools/check.sh", patterns,
                      ".gitignore must re-admit the gate explicitly")


if __name__ == "__main__":
    unittest.main()
