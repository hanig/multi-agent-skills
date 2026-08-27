#!/usr/bin/env python3
"""Sibling-symmetry checks across the two verifiers.

Twelve defects in this repo have been the same shape: a provenance or ownership
rule applied in one place and not its twin. Every one was found by adversarial
review; none was found by a test, because a test written from the same mental
model as the code inherits the same blind spot.

This file is the mechanical version. It compares `contract.py` and
`traincontract.py` on rules that must hold in both, and it inspects the parsed
CODE rather than the source text -- a first attempt grepped the text and was
fooled by a docstring quoting the old behaviour it had just removed.

    python3 tests/test_symmetry.py
"""

import ast
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WORKFLOW = REPO / "skills" / "hanig-verified-workflow" / "scripts" / "contract.py"
TRAINING = REPO / "skills" / "hanig-verified-training" / "scripts" / "traincontract.py"


def code_only(path):
    """Source with docstrings and comments removed, as an AST dump.

    Comments never reach the AST; docstrings do, so they are stripped. A
    docstring saying "taking rows[-1] was wrong" must not read as taking
    rows[-1]."""
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = getattr(node, "body", None)
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            node.body = body[1:] or [ast.Pass()]
    return ast.dump(tree)


def function_names(path):
    tree = ast.parse(path.read_text())
    return {n.name for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


class TestVerifierSymmetry(unittest.TestCase):
    """Each rule must be present in BOTH verifiers or neither. A rule in one
    and not the other is the defect class, whichever way round it falls."""

    @classmethod
    def setUpClass(cls):
        cls.w = code_only(WORKFLOW)
        cls.t = code_only(TRAINING)

    def both(self, label, needle):
        a, b = needle in self.w, needle in self.t
        self.assertEqual(
            a, b,
            f"{label}: present in contract.py={a}, traincontract.py={b}. "
            f"Twelve defects here have been exactly this. Apply it to both, "
            f"or state in the source why one is exempt.")
        return a

    def test_scheduler_rows_are_ownership_tested(self):
        self.assertTrue(self.both("sacct_row_is_ours", "sacct_row_is_ours"))

    def test_timestamp_offsets_are_honoured(self):
        self.assertTrue(self.both("calendar.timegm", "timegm"))

    def test_ownership_slack_is_shared(self):
        self.assertTrue(self.both("OWNERSHIP_SLACK_S", "OWNERSHIP_SLACK_S"))

    def test_sub_second_freshness_is_shared(self):
        self.assertTrue(self.both("created_at_epoch", "created_at_epoch"))
        self.assertTrue(self.both("contract_epoch", "contract_epoch"))

    def test_criteria_digest_is_shared(self):
        self.assertTrue(self.both("criteria_digest", "criteria_digest"))
        self.assertTrue(self.both("DIGESTED_FIELDS", "DIGESTED_FIELDS"))

    def test_exit_code_parser_is_shared(self):
        self.assertTrue(self.both("exit_code_is_clean", "exit_code_is_clean"))

    def test_bounded_reads_are_shared(self):
        """A FIFO or a huge file must not hang either verifier."""
        self.assertTrue(self.both("read_text_bounded / read_json_bounded",
                                  "bounded"))

    def test_watchdog_is_shared(self):
        self.assertTrue(self.both("SIGALRM watchdog", "SIGALRM"))

    def test_no_scheduler_query_reads_only_one_row(self):
        """The twelfth defect: sacct_state took rows[-1] and ownership-tested
        only that row, so a later reuse displaced the honest row above it. Any
        scheduler query must iterate.

        Checked structurally: a function that calls the scheduler and also
        calls the ownership test must contain a loop over the output.
        """
        for path in (WORKFLOW, TRAINING):
            tree = ast.parse(path.read_text())
            for fn in [n for n in ast.walk(tree)
                       if isinstance(n, ast.FunctionDef)]:
                # Must actually INVOKE the scheduler: a first version matched
                # on the name and flagged sacct_row_is_ours, which is the
                # ownership test itself and has no rows to loop over.
                queries = any(
                    isinstance(c, ast.Constant) and isinstance(c.value, str)
                    and c.value in ("sacct", "squeue")
                    for call in ast.walk(fn)
                    if isinstance(call, ast.Call)
                    for c in ast.walk(call))
                tests_ownership = "sacct_row_is_ours" in ast.dump(fn)
                if not (queries and tests_ownership):
                    continue
                has_loop = any(isinstance(n, (ast.For, ast.While))
                               for n in ast.walk(fn))
                self.assertTrue(
                    has_loop,
                    f"{path.name}:{fn.name} ownership-tests a scheduler row "
                    f"but never loops: it can only be reading ONE row, and a "
                    f"reused job id produces several.")

    def test_no_terminal_state_is_left_unclassified(self):
        """Both reviewers, independently: SPECIAL_EXIT was added to the
        terminal-in-queue set and not to the failure set, so an sacct row in
        that state matched no classification and fell through to RUNNING
        forever. It was introduced by the commit that FIXED a classification
        problem, one commit earlier.

        Both files now DERIVE the terminal set from the classification sets, so
        the drift is impossible; this asserts the property directly rather than
        trusting that."""
        import importlib.util
        for path in (WORKFLOW, TRAINING):
            spec = importlib.util.spec_from_file_location(
                f"sym_{path.stem}", path)
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            terminal = set(m.SLURM_TERMINAL_IN_QUEUE)
            ok = set(getattr(m, "SLURM_OK", None) or m.SLURM_OK_END)
            failed = set(getattr(m, "SLURM_FAILED", None) or m.SLURM_BAD_END)
            unclassified = terminal - (ok | failed)
            self.assertEqual(
                unclassified, set(),
                f"{path.name}: {unclassified} are terminal but match no "
                f"classification, so a row in that state falls through to "
                f"RUNNING and never reaches a verdict")

    def test_special_exit_is_a_failure_in_both(self):
        """Named explicitly because it is the state that drifted."""
        import importlib.util
        for path in (WORKFLOW, TRAINING):
            spec = importlib.util.spec_from_file_location(
                f"se_{path.stem}", path)
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            failed = set(getattr(m, "SLURM_FAILED", None) or m.SLURM_BAD_END)
            self.assertIn("SPECIAL_EXIT", failed, path.name)

    def test_every_refusal_names_an_action(self):
        """The bias this repo had: careful about false passes, careless about
        whether a rejected user can proceed. kimi found one refusal that named
        no way out; a sweep found two more. A refusal a user cannot act on is a
        defect even when the refusal is correct.

        Heuristic by necessity -- it reads the reason strings in blocks that set
        a non-passing state and looks for an imperative. It is here to catch a
        NEW actionless refusal, not to prove the existing ones are well worded.
        """
        import re
        actionable = ("record", "bind", "submit", "re-declare", "init",
                      "Run ", "declare", "use ", "Use ", "Split", "split",
                      "raise", "SLURM_TIME_FORMAT", "Check", "Point",
                      "re-run", "Re-declare", "fix the writer", "omit")
        for path in (WORKFLOW, TRAINING):
            src = path.read_text()
            blocks = re.findall(
                r'state = "(?:INCOMPLETE_EVIDENCE|CONTRACT_VIOLATED)"\n'
                r'((?:\s+reasons\.append\((?:[^()]|\([^()]*\))*\)\n)+)', src)
            for body in blocks:
                msgs = re.findall(r'"([^"]{30,})"', body)
                text = " ".join(m for m in msgs if any(c.isalpha() for c in m))
                # Only judge blocks whose reason text this crude reader could
                # actually recover: a long whitespace run or an f-string split
                # across concatenations yields nothing to assess, and asserting
                # on that measures the regex rather than the message.
                if len(text.strip()) < 40:
                    continue
                self.assertTrue(
                    any(a in text for a in actionable),
                    f"{path.name}: a refusal names no action the user can "
                    f"take: {text[:120]}")

    def test_a_later_local_retry_decides_in_both_verifiers(self):
        """The tie-break -- the latest attempt decides, and a matching local
        record is later than the bound job -- was applied to contract.py and
        not to traincontract.py, so a failed batch job there could never be
        recovered by an honest local retry (kimi). Sixteenth instance of a rule
        reaching one verifier and not the other, which is why this file
        exists."""
        for path in (WORKFLOW, TRAINING):
            src = code_only(path)
            self.assertIn(
                "read_termination" if path is TRAINING else "last_is_local",
                src, f"{path.name}: no local-retry path at all")
        # the training verifier's BAD_END branch must consult the record
        tsrc = TRAINING.read_text()
        i = tsrc.index("SLURM_BAD_END:")
        window = tsrc[i:i + 1200]
        self.assertIn("read_termination", window,
                      "the BAD_END branch must consider a later local retry")

    def test_the_shared_helpers_really_are_identical(self):
        """Copies drift. Where a helper exists in both files under the same
        name, its CODE must match -- a fix applied to one copy and not the
        other is the same defect class one level down."""
        shared = ("sacct_row_is_ours", "artifact_is_fresh", "exit_code_is_clean",
                  "parse_iso_ts", "contract_epoch")
        wt, tt = ast.parse(WORKFLOW.read_text()), ast.parse(TRAINING.read_text())

        def body_of(tree, name):
            for n in ast.walk(tree):
                if isinstance(n, ast.FunctionDef) and n.name == name:
                    stripped = [s for s in n.body
                                if not (isinstance(s, ast.Expr)
                                        and isinstance(s.value, ast.Constant)
                                        and isinstance(s.value.value, str))]
                    return ast.dump(ast.Module(body=stripped, type_ignores=[]))
            return None

        for name in shared:
            a, b = body_of(wt, name), body_of(tt, name)
            if a is None or b is None:
                continue
            self.assertEqual(a, b, f"{name}() has drifted between the two "
                                   f"verifiers; fix both copies or extract it")


if __name__ == "__main__":
    unittest.main(verbosity=2)
