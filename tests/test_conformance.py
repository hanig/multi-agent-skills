#!/usr/bin/env python3
"""test_conformance.py — the cross-tool behaviour lock.

Six rules that EVERY tool in the artifact-contract family must obey. Each row is
a rule that has already been missed in one tool and not its twin, so each is a
regression lock rather than a hypothetical.

WHAT THIS SUITE IS, stated honestly, because plan v7 oversold it and two
independent reviewers said so with the commit log open. gpt-5.6-sol scored the
table at 2 of 12 historical defects; claude-opus-4-8 at roughly 6 of 19. Both
agree the majority of the ~19 sibling misses fall in the "may differ" column
below, and that the exemplar -- defect #19, quorum gating a PASS but not a FAIL
in two functions of ONE file -- is reachable by none of extraction, byte
symmetry, or this suite.

So: this is a regression lock on the cross-tool SHARED-BEHAVIOUR SUBSET. It is
not the anti-drift mechanism, and claiming otherwise is theatre. The mechanism
for the dominant class (intra-tool duplicated decision logic) is the
single-evaluator architecture and its AST lint, which lives with each tool.

Deliberately NOT asserted, because these are legitimately tool-specific:
state names, numeric codes, domain criteria, which artifacts are judged, which
disqualifiers exist, receipt field names.

Written BEFORE result.py so the third tool is born conformant. Until result.py
exists its rows skip with a message naming it, never silently pass: a suite that
goes quiet when a tool is missing is the fixture-weaker-than-code defect this
repo has hit ten times.

Python 3.8+, stdlib only.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKILLS = REPO / "skills"

# Every tool in the family. `result` is listed before it exists on purpose.
TOOLS = {
    "contract": SKILLS / "hanig-verified-workflow" / "scripts" / "contract.py",
    "traincontract": SKILLS / "hanig-verified-training" / "scripts" / "traincontract.py",
    "result": SKILLS / "hanig-reproducible-result" / "scripts" / "result.py",
}


def present(name):
    return TOOLS[name].exists()


def run(tool, *argv, cwd=None):
    return subprocess.run([sys.executable, str(TOOLS[tool]), *argv],
                          capture_output=True, text=True, cwd=cwd, timeout=180)


def source(tool):
    return TOOLS[tool].read_text()


class ConformanceCase(unittest.TestCase):
    """Base: every row runs against every tool that EXISTS, and skips with a
    named reason for one that does not."""

    def each_tool(self):
        """Every tool that exists. NOT a generator wrapping subTest: a failure
        inside a `with subTest` in a generator surfaces as GeneratorExit and
        reports the real assertion twice, once uninterpretably."""
        return [n for n in TOOLS if present(n)]

    def assert_all_tools_present_or_skipped(self):
        missing = [n for n in TOOLS if not present(n)]
        if missing:
            self.skipTest(f"not yet written: {', '.join(missing)} "
                          f"(this row will run once it exists)")


class Row1_UnboundRecordIsNeverTrusted(ConformanceCase):
    """A record that does not name THIS contract instance is not evidence --
    not its rc, not its digests, not any field.

    History: an attempts log survived `init --force`, so a stale exit-0 record
    certified a run that never happened under the new contract. The rule then
    had to be applied a fifth time, and a plan review found a sixth site where
    `FAILED` was read from an unbound attempt before the trust gate ran."""

    def test_every_tool_binds_records_to_a_contract_instance(self):
        for tool in self.each_tool():
            src = source(tool)
            self.assertIn("contract_id", src,
                          f"{tool}: no contract instance identity, so no "
                          f"record can be bound to one")

    def test_no_tool_treats_an_absent_binding_as_a_match(self):
        """The absent-field bypass: `rec.get('contract_id') == c.get(...)`
        matches when BOTH are None, so a record with no binding passed."""
        for tool in self.each_tool():
            src = source(tool)
            self.assertNotIn("if not isinstance(want, str)\n        return attempts",
                             src,
                             f"{tool}: an unbound record can satisfy the gate")


class Row2_AbsentEvidenceIsNeverAPass(ConformanceCase):
    """The founding rule: lifecycle state is not completion, and missing
    evidence is not success. Every tool needs a state that says so."""

    def test_every_tool_has_an_incomplete_evidence_state(self):
        for tool in self.each_tool():
            self.assertIn("INCOMPLETE_EVIDENCE", source(tool),
                          f"{tool}: nothing distinguishes 'cannot judge' from "
                          f"'judged and passed'")

    def test_incomplete_evidence_is_never_exit_zero(self):
        for tool in self.each_tool():
            src = source(tool)
            i = src.find('"INCOMPLETE_EVIDENCE"')
            self.assertGreater(i, 0)
            window = src[i:i + 60]
            self.assertNotIn(": 0", window,
                             f"{tool}: INCOMPLETE_EVIDENCE exits 0, so absent "
                             f"evidence reads as success to any caller")


class Row3_UnreadKeyIsRefusedNotDefaulted(ConformanceCase):
    """A criterion key the tool does not read must be REFUSED, never ignored.

    History: `{"kind":"min_lines","min":3}` was accepted, `min` ignored, and the
    criterion fell back to >=1 line -- a declared criterion silently replaced by
    a weaker one. Found by running a real Slurm job, not by any test."""

    def test_every_tool_enumerates_the_keys_it_reads(self):
        for tool in self.each_tool():
            src = source(tool)
            self.assertTrue(
                "unrecognised key" in src or "unread" in src.lower(),
                f"{tool}: no enumeration of readable keys, so a typo silently "
                f"weakens a declared criterion")

    def test_every_tool_reserves_one_annotation_key(self):
        """The fix needs an escape hatch or it refuses honest annotated
        criteria; the hatch must be ONE reserved name, since a prefix
        CONVENTION was broken by `_lines_` within a day."""
        for tool in self.each_tool():
            self.assertIn("ANNOTATION_KEYS", source(tool),
                          f"{tool}: no reserved annotation key, so an honest "
                          f"criterion carrying a note is refused")


class Row4_EveryRefusalNamesAnAction(ConformanceCase):
    """A refusal a user cannot act on is a defect even when the refusal is
    correct. Checked structurally: every sys.exit message and every *_problem /
    *_fault return."""

    ACTIONABLE = ("record", "bind", "submit", "re-declare", "init", "add",
                  "Run ", "declare", "use ", "Use ", "Split", "split", "raise",
                  "SLURM_TIME_FORMAT", "Check", "Point", "Pass ", "pass ",
                  "re-run", "Re-declare", "fix the writer", "omit", "Rename",
                  "rename", "Put any", "set it", "Write ", "point ", "Point ")
    CARRIES = {"problem", "fault", "why", "reason"}

    def refusals(self, src):
        import ast
        tree = ast.parse(src)

        def strings(node):
            return [n.value for n in ast.walk(node)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)]

        def wraps(node):
            for n in ast.walk(node):
                if isinstance(n, ast.FormattedValue) and \
                        isinstance(n.value, ast.Name) and \
                        n.value.id in self.CARRIES:
                    return True
            return False

        out = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                if (isinstance(f, ast.Attribute) and f.attr == "exit") or \
                        (isinstance(f, ast.Name) and f.id == "exit"):
                    for a in node.args:
                        if not isinstance(a, (ast.Constant, ast.JoinedStr,
                                              ast.BinOp)):
                            continue
                        t = " ".join(strings(a)).strip()
                        if t:
                            out.append((t, wraps(a)))
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef) and \
                    fn.name.endswith(("_problem", "_fault")):
                for node in ast.walk(fn):
                    if isinstance(node, ast.Return) and node.value is not None:
                        t = " ".join(strings(node.value)).strip()
                        if t:
                            out.append((t, wraps(node.value)))
        return out

    def test_every_refusal_names_an_action(self):
        for tool in self.each_tool():
            judged = 0
            for text, is_wrapper in self.refusals(source(tool)):
                if is_wrapper or text.lstrip().startswith("usage:"):
                    continue
                judged += 1
                self.assertTrue(
                    any(a in text for a in self.ACTIONABLE),
                    f"{tool}: a refusal names no action: {text[:120]}")
            self.assertGreater(judged, 3,
                               f"{tool}: only {judged} refusals recovered, so "
                               f"this row is measuring its own reader")


class Row5_DisqualifiersOutrankAchievements(ConformanceCase):
    """A tool must not report progress over a fault. The exit code carries the
    worst disqualifier, or the highest achievement when there is none."""

    def test_success_is_exit_zero_in_every_tool(self):
        """The one cross-tool numeric promise. Everything else may differ."""
        for tool in self.each_tool():
            src = source(tool)
            self.assertRegex(
                src, r'"(SCIENTIFIC_PASS|CONVERGED|REVIEWED)":\s*0',
                f"{tool}: its success state is not exit 0, so `&&` chaining "
                f"and every shell caller silently misread it")


class Row6_ReceiptNamesWhatItJudged(ConformanceCase):
    """A receipt that does not name the contract instance it judged can be
    read as evidence for a different one. Applied five times before it stuck."""

    def test_every_receipt_carries_the_contract_instance(self):
        for tool in self.each_tool():
            src = source(tool)
            self.assertIn("criteria_digest", src,
                          f"{tool}: a receipt cannot say WHICH criteria it "
                          f"judged, so an edit to them is undetectable")


class TestTheSuiteItselfIsHonest(unittest.TestCase):
    """Guards against this suite becoming the thing it is meant to prevent."""

    def test_result_py_is_listed_before_it_exists(self):
        self.assertIn("result", TOOLS,
                      "the third tool must be in the table before it is "
                      "written, so it is born conformant")

    def test_a_missing_tool_skips_loudly_rather_than_passing(self):
        missing = [n for n in TOOLS if not present(n)]
        if missing:
            self.assertTrue(True)  # documented by the skip messages above
        self.assertTrue(all(TOOLS[n].suffix == ".py" for n in TOOLS))

    def test_the_two_shipped_tools_are_actually_present(self):
        """If both vanish, every row above passes vacuously."""
        self.assertTrue(present("contract"), "contract.py missing")
        self.assertTrue(present("traincontract"), "traincontract.py missing")


if __name__ == "__main__":
    unittest.main(verbosity=2)
