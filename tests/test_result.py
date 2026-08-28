#!/usr/bin/env python3
"""test_result.py — result.py, and the single-decider lint criterion 30 requires.

Python 3.8+, stdlib only.
"""

import ast
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "skills" / "hanig-reproducible-result" / "scripts" / "result.py"

spec = importlib.util.spec_from_file_location("result", SCRIPT)
result = importlib.util.module_from_spec(spec)
spec.loader.exec_module(result)


def cli(*argv, cwd=None):
    return subprocess.run([sys.executable, str(SCRIPT), *argv],
                          capture_output=True, text=True, cwd=cwd, timeout=180)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp,
                        ignore_errors=True)
        (self.tmp / "in.tsv").write_text("gene\tval\nTP53\t4.2\n")

    def declare(self, *extra, command="wc -l < in.tsv > fig.txt"):
        return cli("declare", str(self.tmp), "--command", command,
                   "--output", "fig.txt", "--input", "in.tsv", *extra,
                   cwd=str(self.tmp))


class TestSingleDecider(unittest.TestCase):
    """Criterion 30. The mechanism for the dominant defect class.

    Defect #19 was two functions in ONE file inlining the same quorum
    comparison: escalate() and decide_state(). Extraction cannot see it (both
    in one file), byte symmetry cannot (different names, same file), and the
    cross-tool conformance suite cannot (intra-tool, and review.py is not one
    of the three tools). It was found live by a plan review, by no test.

    STATED LIMIT, and it is a real one: this catches DUPLICATION of a state
    decision, never a single WRONG one. It is a lint on a chosen shape, and it
    works only because result.py was written in that shape deliberately."""

    def setUp(self):
        self.tree = ast.parse(SCRIPT.read_text())
        self.funcs = {n.name: n for n in ast.walk(self.tree)
                      if isinstance(n, ast.FunctionDef)}

    def deciding_functions(self):
        """Every function that RETURNS a state name or an exit code drawn from
        STATES. That set must be exactly one."""
        names = set(result.STATES)
        deciders = set()
        for fname, fn in self.funcs.items():
            for node in ast.walk(fn):
                if not isinstance(node, ast.Return) or node.value is None:
                    continue
                for sub in ast.walk(node.value):
                    # `return "FAILED", STATES["FAILED"], ...`
                    if isinstance(sub, ast.Subscript) and \
                            isinstance(sub.value, ast.Name) and \
                            sub.value.id == "STATES":
                        deciders.add(fname)
                    if isinstance(sub, ast.Constant) and sub.value in names:
                        deciders.add(fname)
        return deciders

    def test_exactly_one_function_decides_the_state(self):
        deciders = self.deciding_functions()
        self.assertEqual(
            deciders, {"evaluate"},
            f"more than one function chooses a state: {sorted(deciders)}. "
            f"That is the shape of defect #19 -- two sites for one decision, "
            f"one of which will be fixed and the other missed. Gather evidence "
            f"in handlers and call evaluate().")

    def test_every_gate_appears_exactly_once_in_the_registry(self):
        ids = [g["id"] for g in result.GATES]
        dupes = {i for i in ids if ids.count(i) > 1}
        self.assertFalse(dupes, f"gate id(s) declared twice: {sorted(dupes)}")

    def test_every_gate_has_a_role_a_state_and_a_reason(self):
        for g in result.GATES:
            self.assertIn(g["role"], ("trust", "disqualifier", "achievement"))
            self.assertIn(g["state"], result.STATES, f"{g['id']}: unknown state")
            self.assertTrue(g["why"].strip(), f"{g['id']}: no reason")

    def test_every_non_achievement_gate_names_an_action(self):
        """A refusal a user cannot act on is a defect even when correct."""
        for g in result.GATES:
            if g["role"] == "achievement":
                continue
            self.assertTrue(
                (g.get("action") or "").strip(),
                f"gate {g['id']} refuses and names no action the user can take")

    def test_the_lint_would_catch_a_second_decider(self):
        """Non-vacuity: a test that cannot fail is worthless, and this repo has
        shipped ten of those."""
        injected = ast.parse(
            'def sneaky():\n    return "FAILED", STATES["FAILED"], []\n')
        self.funcs["sneaky"] = injected.body[0]
        self.assertNotEqual(self.deciding_functions(), {"evaluate"},
                            "the lint does not notice a second decider")


class TestEvaluatorPrecedence(unittest.TestCase):
    """The order that earlier plan versions got wrong, twice."""

    def test_trust_gate_beats_a_failure(self):
        """v5 ranked FAILED above the trust gate, so an attempt left over from
        a previous contract reported 'the build failed' for a contract that was
        never built. The two carry opposite actions."""
        st, rc, _ = result.evaluate({"no_attempt", "command_failed"})
        self.assertEqual(st, "INCOMPLETE_EVIDENCE")
        self.assertEqual(rc, 6)

    def test_a_disqualifier_beats_every_achievement(self):
        """A reviewed result whose inputs changed must not stay REVIEWED."""
        st, _, _ = result.evaluate(
            {"outputs_exist", "checks_passed", "accepted_by_person",
             "inputs_changed_since_build"})
        self.assertEqual(st, "STALE")

    def test_achievements_are_not_ranked_against_disqualifiers(self):
        """REVIEWED's condition contains VALIDATED's, so a single ranking made
        a fully reviewed result exit 1."""
        st, rc, _ = result.evaluate(
            {"outputs_exist", "checks_passed", "accepted_by_person"})
        self.assertEqual((st, rc), ("REVIEWED", 0))

    def test_nothing_established_is_not_a_pass(self):
        st, rc, _ = result.evaluate(set())
        self.assertEqual(st, "INCOMPLETE_EVIDENCE")
        self.assertNotEqual(rc, 0)

    def test_the_receipt_lists_every_finding_not_only_the_deciding_one(self):
        _, _, reasons = result.evaluate(
            {"command_failed", "inputs_changed_since_build"})
        self.assertGreaterEqual(
            len(reasons), 2,
            "only the deciding finding was reported, so a user who fixes it "
            "cannot see what is next")

    def test_success_is_exit_zero(self):
        self.assertEqual(result.STATES["REVIEWED"], 0)


class TestProductionGate(Base):
    """Gate 2, and the reason plan v8 exists. Integrity is not production."""

    def test_an_unproduced_output_is_refused_even_when_checks_pass(self):
        (self.tmp / "fig.txt").write_text("I was here all along\n")
        r = self.declare("--check",
                         json.dumps({"kind": "min_size", "path": "fig.txt",
                                     "bytes": 1}),
                         command="true")
        self.assertEqual(r.returncode, 0, r.stderr)
        cli("build", str(self.tmp), cwd=str(self.tmp))
        r = cli("check", str(self.tmp), cwd=str(self.tmp))
        self.assertEqual(
            r.returncode, result.STATES["INCOMPLETE_EVIDENCE"],
            f"a pre-existing output the command never wrote was accepted:\n"
            f"{r.stdout}")
        self.assertIn("did not write it", r.stdout)

    def test_an_honest_run_is_not_refused(self):
        """The counter-claim. A verifier that cries wolf gets switched off."""
        r = self.declare("--check",
                         json.dumps({"kind": "min_size", "path": "fig.txt",
                                     "bytes": 1}))
        self.assertEqual(r.returncode, 0, r.stderr)
        cli("build", str(self.tmp), cwd=str(self.tmp))
        r = cli("check", str(self.tmp), cwd=str(self.tmp))
        self.assertEqual(r.returncode, result.STATES["VALIDATED"],
                         f"an honest run was refused:\n{r.stdout}")

    def test_the_window_records_content_not_time(self):
        """Never a timestamp: a timestamp supports an inference, never an
        attribution, and this repo has retired three rules for forgetting it."""
        self.declare()
        cli("build", str(self.tmp), cwd=str(self.tmp))
        man = json.loads((self.tmp / "result-manifest.json").read_text())
        rec = man["outputs"]["fig.txt"]
        for field in ("before", "after", "written_in_window"):
            self.assertIn(field, rec)
        self.assertNotIn("mtime", json.dumps(rec).lower())


class TestBindingAndDrift(Base):
    def test_a_build_from_a_previous_contract_instance_is_not_trusted(self):
        """`declare --force` mints a new contract_id, so the prior attempt is
        unbound. The TRUST GATE fires before the manifest is even read, which
        is the correct precedence: nothing from an unbound attempt is used, so
        there is no need to reason about its manifest at all.

        My first version of this test asserted the manifest message and was
        wrong about the code, not the other way round."""
        self.declare()
        cli("build", str(self.tmp), cwd=str(self.tmp))
        self.declare("--force")          # new contract_id, same criteria
        r = cli("check", str(self.tmp), cwd=str(self.tmp))
        self.assertEqual(r.returncode, result.STATES["INCOMPLETE_EVIDENCE"],
                         r.stdout)
        self.assertIn("no attempt names this contract instance", r.stdout)

    def test_a_manifest_naming_another_instance_is_refused(self):
        """The manifest branch itself, reached by keeping the attempt bound and
        corrupting only the manifest."""
        self.declare()
        cli("build", str(self.tmp), cwd=str(self.tmp))
        mp = self.tmp / "result-manifest.json"
        man = json.loads(mp.read_text())
        man["contract_id"] = "0" * 16
        mp.write_text(json.dumps(man))
        r = cli("check", str(self.tmp), cwd=str(self.tmp))
        self.assertNotEqual(r.returncode, result.STATES["VALIDATED"])
        self.assertIn("different contract instance", r.stdout)

    def test_an_edited_contract_is_refused(self):
        self.declare()
        c = json.loads((self.tmp / "result-contract.json").read_text())
        c["command"] = "something else"
        (self.tmp / "result-contract.json").write_text(json.dumps(c))
        r = cli("check", str(self.tmp), cwd=str(self.tmp))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("edited", r.stderr + r.stdout)

    def test_a_changed_input_is_stale_by_content(self):
        self.declare("--check", json.dumps({"kind": "exists",
                                            "path": "fig.txt"}))
        cli("build", str(self.tmp), cwd=str(self.tmp))
        (self.tmp / "in.tsv").write_text("gene\tval\nMYC\t9.9\n")
        r = cli("check", str(self.tmp), cwd=str(self.tmp))
        self.assertEqual(r.returncode, result.STATES["STALE"], r.stdout)


class TestRefusalsAndScope(Base):
    def test_declare_refuses_a_typod_check_key(self):
        r = self.declare("--check", json.dumps({"kind": "min_lines",
                                                "path": "fig.txt", "min": 3}))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("lines", r.stderr)

    def test_declare_accepts_the_reserved_note_key(self):
        r = self.declare("--check", json.dumps({"kind": "min_lines",
                                                "path": "fig.txt", "lines": 1,
                                                "note": "header row"}))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_declare_refuses_no_outputs(self):
        r = cli("declare", str(self.tmp), "--command", "true",
                cwd=str(self.tmp))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--output", r.stderr)

    def test_review_refuses_an_unchecked_result(self):
        self.declare()
        r = cli("review", str(self.tmp), "--by", "Hani", cwd=str(self.tmp))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("check", r.stderr)

    def test_the_receipt_states_its_scope_machine_readably(self):
        """Criterion 27: a consumer must see the claim's boundary without
        parsing prose."""
        self.declare()
        cli("build", str(self.tmp), cwd=str(self.tmp))
        cli("check", str(self.tmp), cwd=str(self.tmp))
        r = json.loads((self.tmp / "verification.json").read_text())
        scope = r["provenance_scope"]
        self.assertEqual(scope["covers"], "declared inputs only")
        self.assertTrue(scope["structure_only"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
