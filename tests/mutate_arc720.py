#!/usr/bin/env python3
"""Run ARC-720's targeted mutations without changing the working tree.

Each replacement must identify exactly one production site. Compile that
changed source into a fresh module and run its assigned consumer test. A kill
only counts if that test fails at the named decision/delivery assertion;
import errors, unrelated failures and surviving mutants are harness failures.
"""

import io
import hashlib
import json
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests import test_arc720_contamination as cases


MUTATIONS = (('exit constant executed',
  'test_000_context_signatures_are_stripped',
  '[positive-strip-dispatch]',
  '"REVIEW_PASS": 0,',
  '"REVIEW_PASS": 44,'),
 ('strip decision bypassed',
  'test_000_context_signatures_are_stripped',
  '[positive-strip-removal]',
  '        disclosure = prior_decision(text, cursor)',
  '        disclosure = None'),
 ('context delivery uses original',
  'test_000_context_signatures_are_stripped',
  '[positive-strip-removal]',
  '    args.context = strip_review_input(args.context, "--context", args.input_redactions)',
  '    args.context = args.context'),
 ('matched span retained',
  'test_000_context_signatures_are_stripped',
  '[positive-strip-removal]',
  '        parts.append(text[cursor:start])',
  '        parts.append(text[cursor:end])'),
 ('live evidence overstripped',
  'test_010_live_dispute_reaches_panel_verbatim',
  '[live-context-acceptance]',
  '    cursor = 0',
  '    if "REACHED_FDB" in text:\n        return ""\n    cursor = 0'),
 ('disputed summary dropped',
  'test_010_live_dispute_reaches_panel_verbatim',
  '[disputed-summary-delivery]',
  '            out.append(f"  - {entry[\'location\']}: {entry[\'summary\']}")',
  '            out.append("summary omitted")'),
 ('disputed reason dropped',
  'test_010_live_dispute_reaches_panel_verbatim',
  '[disputed-reason-delivery]',
  '            out.append(f"    reason: {entry[\'reason\']}")',
  '            out.append("reason omitted")'),
 ('file strip bypassed',
  'test_020_every_file_is_scanned_before_truncation',
  '[file-strip]',
  '        text = strip_review_input(text, f"--file {f}", args.input_redactions)',
  '        text = text'),
 ('only first file stripped',
  'test_020_every_file_is_scanned_before_truncation',
  '[file-strip]',
  '        text = strip_review_input(text, f"--file {f}", args.input_redactions)',
  '        if f == args.file[0]:\n'
  '            text = strip_review_input(text, f"--file {f}", args.input_redactions)'),
 ('file clipped before strip',
  'test_020_every_file_is_scanned_before_truncation',
  '[file-before-truncation]',
  '        text = strip_review_input(text, f"--file {f}", args.input_redactions)',
  '        text = strip_review_input(text[:MAX_CHARS], f"--file {f}", args.input_redactions)'),
 ('code evidence overstripped',
  'test_030_code_measurements_are_not_panel_decisions',
  '[code-evidence-acceptance]',
  '    cursor = 0',
  '    if "failed" in text:\n        return ""\n    cursor = 0'),
 ('disposition strip bypassed',
  'test_040_dispositions_strip_only_matched_spans',
  '[disposition-strip]',
  '        if entry["disposition"] == "not-reproduced":',
  '        if False:'),
 ('receipt decision unreachable',
  'test_050_receipt_value_is_stripped_with_outer_whitespace_preserved',
  '[receipt-strip]',
  '    if report or exclusion:',
  '    if exclusion:'),
 ('exclusion receipt ignored',
  'test_050_receipt_value_is_stripped_with_outer_whitespace_preserved',
  '[exclusion-receipt-strip]',
  '    if report or exclusion:',
  '    if report:'),
 ('receipt trailing whitespace removed',
  'test_050_receipt_value_is_stripped_with_outer_whitespace_preserved',
  '[receipt-remainder]',
  '            _receipt, end = json.JSONDecoder().raw_decode(text, start)',
  '            _receipt, end = json.JSONDecoder().raw_decode(text, start)\n            end = len(text)'),
 ('finding map overstripped',
  'test_050_receipt_value_is_stripped_with_outer_whitespace_preserved',
  '[dispositions-file-acceptance]',
  '    cursor = 0',
  '    if \'"disposition":\' in text:\n        return ""\n    cursor = 0'),
 ('protocol overstripped',
  'test_060_protocol_is_an_admissible_file',
  '[protocol-acceptance]',
  '    cursor = 0',
  '    if "Never tell the panel what a previous round decided" in text:\n'
  '        return ""\n'
  '    cursor = 0'),
 ('readme overstripped',
  'test_070_readme_is_an_admissible_file',
  '[readme-acceptance]',
  '    cursor = 0',
  '    if "Never tell the panel what a previous round decided" in text:\n'
  '        return ""\n'
  '    cursor = 0'),
 ('table overstripped',
  'test_080_documentation_can_quote_state_names',
  '[table-acceptance]',
  '    cursor = 0',
  '    if \'| Round 1 | "REVIEW_PASS" |\' in text:\n        return ""\n    cursor = 0'),
 ('schema overstripped',
  'test_090_schema_keys_are_not_receipt_values',
  '[schema-acceptance]',
  '    cursor = 0',
  '    if \'"verdict":\' in text:\n        return ""\n    cursor = 0'),
 ('CI prose overstripped',
  'test_100_flaky_ci_context_is_admissible',
  '[flaky-ci-acceptance]',
  '    cursor = 0',
  '    if "previous run failed" in text:\n        return ""\n    cursor = 0'),
 ('nested state overstripped',
  'test_110_partial_or_nested_data_is_not_a_receipt',
  '[partial-data-acceptance]',
  '    cursor = 0',
  '    if \'"state": "REVIEW_PASS"\' in text:\n        return ""\n    cursor = 0'),
 ('annotation BOM bypass',
  'test_120_bom_annotation_preserves_unmatched_evidence',
  '[annotation-prefix-strip]',
  '    start = re.compile(r"[\\s\\ufeff]*").match(text, offset).end()',
  '    start = re.compile(r"\\s*").match(text, offset).end()'),
 ('receipt BOM bypass',
  'test_130_bom_receipt_does_not_overstrip_schema',
  '[receipt-prefix-strip]',
  '    start = re.compile(r"[\\s\\ufeff]*").match(text, offset).end()',
  '    start = re.compile(r"\\s*").match(text, offset).end()'),
 ('prefix whitespace lost',
  'test_120_bom_annotation_preserves_unmatched_evidence',
  '[annotation-prefix-remainder]',
  '        parts.append(text[cursor:start])',
  '        parts.append("")'),
 ('accepted BOM context changed',
  'test_120_bom_annotation_preserves_unmatched_evidence',
  '[bom-context-delivery]',
  '        out.append(f"CONTEXT\\n{context}\\n")',
  '        out.append("CONTEXT\\n" + context.lstrip(chr(65279)) + "\\n")'),
 ('annotation read overflow accepted',
  'test_140_annotation_past_read_limit_is_refused',
  '[annotation-read-overflow]',
  '        if len(raw) > MAX_FILE_READ_BYTES:',
  '        if False:'),
 ('receipt read overflow accepted',
  'test_150_receipt_past_read_limit_is_refused',
  '[receipt-read-overflow]',
  '        if len(raw) > MAX_FILE_READ_BYTES:',
  '        if False:'),
 ('exact read boundary overblocked',
  'test_140_annotation_past_read_limit_is_refused',
  '[read-limit-boundary]',
  '        if len(raw) > MAX_FILE_READ_BYTES:',
  '        if len(raw) >= MAX_FILE_READ_BYTES:'),
 ('context clipped to file cap',
  'test_160_context_is_not_subject_to_file_read_limit',
  '[unbounded-context-strip]',
  '    args.context = strip_review_input(args.context, "--context", args.input_redactions)',
  '    args.context = strip_review_input(args.context[:MAX_FILE_READ_BYTES], "--context", '
  'args.input_redactions)'),
 ('panel never told',
  'test_170_removal_is_visible_to_panel_caller_receipt_and_audit',
  '[prompt-notice]',
  '    if input_redactions:\n        out.append(redaction_notice(input_redactions) + "\\n")',
  '    if False:\n        out.append(redaction_notice(input_redactions) + "\\n")'),
 ('caller never told',
  'test_170_removal_is_visible_to_panel_caller_receipt_and_audit',
  '[caller-notice]',
  '        print(redact(redaction_notice(args.input_redactions)), file=sys.stderr)',
  '        pass'),
 ('JSON result loses redactions',
  'test_170_removal_is_visible_to_panel_caller_receipt_and_audit',
  '[result-notice]',
  '\n        "input_redactions": args.input_redactions,\n',
  '\n        "input_redactions": [],\n'),
 ('journal transport loses redactions',
  'test_170_removal_is_visible_to_panel_caller_receipt_and_audit',
  '[audit-notice]',
  '        input_redactions=getattr(args, "input_redactions", ()))',
  '        input_redactions=())'),
 ('human result loses notice',
  'test_180_human_result_records_removal',
  '[human-result-notice]',
  '    else:\n        print()\n        if args.input_redactions:',
  '    else:\n        print()\n        if False:'),
 ('unavailable result loses redactions',
  'test_190_unavailable_result_records_removal',
  '[unavailable-result-notice]',
  '\n                  "input_redactions": args.input_redactions,\n',
  '\n                  "input_redactions": [],\n'),
 ('only first signature stripped',
  'test_200_consecutive_signatures_preserve_original_offsets',
  '[consecutive-strip]',
  '        cursor = end',
  '        return "".join(parts) + text[end:]'),
 ('original offsets lost',
  'test_200_consecutive_signatures_preserve_original_offsets',
  '[consecutive-offsets]',
  '                           "start_char": start, "end_char": end,',
  '                           "start_char": 0, "end_char": end,'),
 ('empty remainder refused',
  'test_210_completely_stripped_input_is_not_a_configuration_error',
  '[empty-after-strip-dispatch]',
  '    if not body.strip() and not args.input_redactions:',
  '    if not body.strip():'),
 ('empty escalation loses redactions',
  'test_220_empty_escalation_records_removal',
  '[escalation-result-notice]',
  '                      "input_redactions": args.input_redactions, "journal": journal}',
  '                      "input_redactions": [], "journal": journal}'))

def mutated_module(source, before, after):
    if source.count(before) != 1:
        raise RuntimeError("ambiguous or absent mutation site: " + before)
    module = types.ModuleType("arc720_mutant")
    module.__file__ = str(cases.SCRIPT)
    exec(compile(source.replace(before, after), str(cases.SCRIPT), "exec"), module.__dict__)
    return module


def binding_probes(source):
    """Prove the selected module changes both an exit constant and delivered text."""
    original = cases.review
    variants = (
        ("before", original, 0, False),
        ("constant", mutated_module(source, '"REVIEW_PASS": 0,', '"REVIEW_PASS": 44,'), 44, False),
        ("function", mutated_module(source, '        disclosure = prior_decision(text, cursor)',
                                    '        disclosure = None'), 0, True),
        ("restored", original, 0, False),
    )
    records = []
    for name, module, expected_exit, expected_disclosure in variants:
        cases.review = module
        case = cases.TestPriorDecisionInputs("test_000_context_signatures_are_stripped")
        try:
            case.setUp()
            status, _diagnostic, prompts = case.invoke(context=cases.OFFENDER)
            disclosed = any(cases.OFFENDER in prompt for prompt in prompts)
            bound = (case.invoke.__func__.__globals__["review"] is module
                     and module.main.__globals__ is module.__dict__
                     and module.strip_review_input.__globals__ is module.__dict__)
            if not bound or (status, len(prompts), disclosed) != (expected_exit, 2, expected_disclosure):
                raise RuntimeError("mutant execution probe failed: " + name)
            if original.STATES["REVIEW_PASS"] != 0:
                raise RuntimeError("mutant contaminated original module")
            records.append({"variant": name, "bound_to_selected_module": bound,
                            "observed_exit": status, "provider_calls": len(prompts),
                            "disclosed_signature": disclosed,
                            "selected_pass_constant": module.STATES["REVIEW_PASS"],
                            "original_pass_constant": original.STATES["REVIEW_PASS"]})
        finally:
            case.doCleanups()
            cases.review = original
    return records


def main():
    original = cases.review
    source = cases.SCRIPT.read_text()
    probes = binding_probes(source)
    baseline_log = io.StringIO()
    baseline = unittest.TextTestRunner(stream=baseline_log).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(cases.TestPriorDecisionInputs))
    if not baseline.wasSuccessful():
        raise RuntimeError("baseline failed; no mutation kill is admissible:\n" + baseline_log.getvalue())
    methods = set(unittest.defaultTestLoader.getTestCaseNames(cases.TestPriorDecisionInputs))
    covered = {mutation[1] for mutation in MUTATIONS}
    if methods != covered:
        raise RuntimeError("test/mutation coverage mismatch: " + repr(methods ^ covered))
    records = []
    failed = False
    for name, method, marker, before, after in MUTATIONS:
        module = mutated_module(source, before, after)
        cases.review = module
        try:
            result = unittest.TextTestRunner(stream=io.StringIO()).run(
                unittest.TestSuite([cases.TestPriorDecisionInputs(method)]))
        finally:
            cases.review = original
        traces = [trace for _test, trace in result.failures]
        targeted = (bool(traces) and not result.errors
                    and all(marker in trace.splitlines()[-1] for trace in traces))
        records.append({"mutation": name, "test": method, "target": marker,
                        "targeted_kill": targeted,
                        "assertions": [trace.splitlines()[-1] for trace in traces],
                        "errors": [trace for _test, trace in result.errors]})
        failed = failed or not targeted
    print(json.dumps({"source_sha256": hashlib.sha256(source.encode()).hexdigest(),
                      "binding_probes": probes,
                      "baseline_tests": baseline.testsRun, "mutations": records,
                      "all_targeted_kills": not failed}, indent=2))
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
