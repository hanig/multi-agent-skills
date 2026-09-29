#!/usr/bin/env python3
"""Require the round-two claim-scope filter to fail the exit-7 assertion."""

import hashlib
import importlib.util
import io
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


TEST_PATH = Path(__file__).with_name("test_arc1163_claim_scope.py")
SPEC = importlib.util.spec_from_file_location("arc1163_mutation_cases", TEST_PATH)
cases = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cases)


def main():
    baseline = unittest.TextTestRunner(stream=io.StringIO()).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(cases.TestCounterClaimScope))
    if not baseline.wasSuccessful() or baseline.testsRun != 4:
        print(json.dumps({"baseline_passed": False, "tests": baseline.testsRun,
                          "failures": [trace for _test, trace in baseline.failures],
                          "errors": [trace for _test, trace in baseline.errors]}, indent=2))
        return 1
    source_bytes = cases.SCRIPT.read_bytes()
    source = source_bytes.decode("utf-8")
    original = '            if norm(c.get("status")) == "refuted":'
    replacement = ('            if norm(c.get("status")) == "refuted" '
                   'and c.get("in_scope") is not False:')
    if source.count(original) != 1:
        print(json.dumps({"error": "mutation target is not unique", "count": source.count(original)}))
        return 1
    mutant = types.ModuleType("arc1163_scope_mutant")
    mutant.__file__ = str(cases.SCRIPT)
    exec(compile(source.replace(original, replacement), str(cases.SCRIPT), "exec"), mutant.__dict__)
    method = "test_000_wrongful_refusal_with_false_scope_still_blocks"
    with patch.object(cases, "review", mutant):
        result = unittest.TextTestRunner(stream=io.StringIO()).run(
            unittest.TestSuite([cases.TestCounterClaimScope(method)]))
    last_lines = [trace.splitlines()[-1] for _test, trace in result.failures]
    expected = "AssertionError: 0 != 7 : " + cases.EXIT_ASSERTION
    targeted = (result.testsRun == 1 and len(result.failures) == 1
                and not result.errors and not result.skipped
                and last_lines == [expected])
    unchanged = cases.SCRIPT.read_bytes() == source_bytes
    print(json.dumps({"source_sha256": hashlib.sha256(source_bytes).hexdigest(),
                      "source_unchanged": unchanged, "baseline_tests": baseline.testsRun,
                      "baseline_passed": True,
                      "mutation": "filter refuted claims with reviewer in_scope false",
                      "test": method, "expected_assertion": expected,
                      "assertions": last_lines,
                      "errors": [trace for _test, trace in result.errors],
                      "targeted_exit_code_failure": targeted}, indent=2))
    return 0 if targeted and unchanged else 1


if __name__ == "__main__":
    sys.exit(main())
