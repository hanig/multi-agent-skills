#!/usr/bin/env python3
"""Render hanig-swarm declarations and reject unregistered reference rules."""

import argparse
import difflib
import json
import os
from pathlib import Path
import re
import stat
import string
import sys
import tempfile


BEGIN_MARKER = "<!-- BEGIN GENERATED DECLARATIONS: declarations.json -->"
END_MARKER = "<!-- END GENERATED DECLARATIONS -->"
ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*$")
REFERENCE_PATTERN = re.compile(
    r"^references/[a-z0-9]+(?:-[a-z0-9]+)*\.md$"
)
DECLARATION_SUFFIX = re.compile(
    r"[ ]+<!--\s*declaration:\s*([^>]+?)\s*-->[ ]*$", re.IGNORECASE
)
STANDALONE_COMMENT = re.compile(r"^<!--(?:[^-]|-(?!-))*-->$")
IMPERATIVE_MODAL = re.compile(
    r"\b(?:must(?:n't)?|required|requires?|requirement|cannot|can\s+not|can't|"
    r"only|limits?|limited|"
    r"never|should(?:n't)?|shall|may\s+not|need(?:s|ed)?|ought|mandatory|"
    r"allowed|disallowed|permitted|"
    r"do(?:es)?\s+not|don't|doesn't|refus(?:e|es|ed|al)|"
    r"forbid(?:s|den|ding)?|prohibit(?:s|ed|ion)?|always)\b",
    re.IGNORECASE,
)
FENCE_OPENER = re.compile(
    r"^(?P<fence>`{3,}|~{3,})(?: ?(?P<info>[A-Za-z0-9][A-Za-z0-9_.+-]*))?$"
)
FENCE_CLOSER = re.compile(r"^(?P<indent> {0,3})(?P<fence>`{3,}|~{3,})[ ]*$")
FENCE_RUN = re.compile(r"`{3,}|~{3,}")
THEMATIC_OR_SETEXT = re.compile(
    r"^ {0,3}(?:(?:\*[ ]*){3,}|(?:_[ ]*){3,}|[-=]+[ ]*)$"
)
ORDERED_LIST = re.compile(r"^[0-9]{1,9}[.)] ")
UNORDERED_LIST = re.compile(r"^[-+*] ")
PROSE_PREFIX = re.compile(
    r"^(?:(?P<heading>#{1,6}) |(?P<unordered>[-+*]) |"
    r"(?P<ordered>[0-9]{1,9}[.)]) |(?P<continuation>  ))(?P<payload>.*)$"
)
PROSE_START = frozenset(string.ascii_letters + string.digits + "`|\"'(")
ORDERED_AT_CONTENT = re.compile(r"^[0-9]{1,9}[.)](?: |$)")


def _closed_ids(value):
    return tuple(value.split())


CLOSED_DECLARATIONS = {
    "hanig-swarm": _closed_ids("""
        placement.behavior-deciding placement.reference-elaboration
        placement.reference-dialect capability.shell-filesystem
        capability.python-git capability.slurm capability.paseo-bus
        capability.review capability.tracker capability.worker-backend
        paths.skill-directory code.default-agent code.provider-mode
        runtime.declaration runtime.verification runtime.canary retry.boundary
        retry.checkpoint retry.exposure retry.concurrency
        isolation.exclusive-root isolation.done-predicate
        isolation.artifact-basis isolation.container-profile
        isolation.container-attestation authority.coordinator-state
        closure.by-kind code.remote-ref compatibility.judgment-generation
        verifier.corpus verifier.integration code.write-scopes
        code.worktree-identity code.adoption usage.outputs
        scheduler.queued-job cluster.plan-specific cluster.access
        python.host-floor kind.pipeline-boundary drift.coordinator-size
        drift.lifted-module convergence.verdict convergence.plan
        unattended.scheduler unattended.lock unattended.orphan
        unattended.incomplete unattended.plan-digest unattended.output-claims
        unattended.stash credential.boundary credential.worker
        limit.runtime-canary-scope limit.trusted-writer-isolation
        limit.container-isolation-scope limit.pre-dispatch-artifact-basis
        limit.same-uid-authority limit.process-tree-quiescence
        limit.remote-ref-durability limit.verifier-corpus-boundary
        limit.integration-topology limit.write-scopes limit.worktree-inode
        limit.child-credentials limit.worktree-adoption limit.workspace-id
        limit.pipeline-interior limit.convergence-plateau
        limit.coordinator-lock-topology limit.output-claim-registry
        limit.base-branch-comparison compatibility.python
    """),
    "hanig-orchestrate": _closed_ids("""
        placement.behavior-deciding placement.reference-elaboration
        placement.reference-dialect capability.host-policy
        capability.dependencies paths.skill-directory authority.source
        authority.confirmation authority.narrow-mode authority.revocation
        role.supervision delegation.whole-loop delegation.prompt
        delegation.configuration delegation.continuation retry.boundary
        evidence.checkable evidence.authority review.panel-source
        review.author-exclusion review.claims review.honesty review.cost
        review.effort review.rounds adjudication.matrix
        adjudication.concurrence adjudication.record
        adjudication.nonoverridable adjudication.honesty watch.facts
        watch.source watch.proof loop.quiescence loop.advance loop.yield
        preservation.before-cleanup dispatch.mechanics merge.requirements
        tracker.authority tracker.reconcile tracker.dag report.three-parts
        handoff.contents handoff.transfer takeover.verify
        limit.session-liveness
    """),
    "hanig-project": _closed_ids("""
        placement.behavior-deciding placement.reference-elaboration
        placement.reference-dialect capability.host-policy capability.tracker
        capability.install-boundary paths.skill-directory workflow.order
        survey.read-before-ask survey.incomplete-walk survey.partition-state
        adoption.context repository.destination repository.source-data
        repository.creation-approval interview.judgment-only
        interview.retry-boundary interview.dispatch-complete
        interview.reporting-cadence plan.inputs plan.scheduler-route
        plan.promotion code.configuration code.target-branch runtime.contract
        retry.contract cluster.memory-flag cluster.account-allowance
        cluster.memory-charging cluster.qos-scope findings.interview
        unit.retry-size judgment.by-kind slurm.command-boundary
        pipeline.command-boundary code.prompt-boundary
        outputs.attempt-relative code.default-agent slurm.array-outputs
        plan.required-fields code.write-scopes code.worktree-isolation
        plan.docs-protection plan.human-document plan.validate tracker.team
        tracker.credential-boundary tracker.approval tracker.autopilot
        tracker.apply tracker.edges tracker.readback-shape tracker.attestation
        tracker.check dispatch.sequence drain.authority closure.evidence
        closure.by-kind drain.block-intent outbox.receipt outbox.idempotency
        report.required report.evidence-source report.contents
        findings.contract findings.bound adoption.remaining-work
    """),
}


class RegistryError(ValueError):
    """The registry or generated block violates its data contract."""


def _skill_dir_from_script():
    return Path(__file__).resolve().parents[1]


def _one_line(value):
    return (isinstance(value, str) and bool(value.strip())
            and "\n" not in value and "\r" not in value)


def _lifecycle_ids(data, known_ids, skill_name):
    """Return retired/replaced ids after validating durable reasons."""
    retired = data.get("retired_declarations")
    replacements = data.get("replacement_declarations")
    if not isinstance(retired, list):
        raise RegistryError("{} retired_declarations must be a JSON list".format(
            skill_name
        ))
    if not isinstance(replacements, list):
        raise RegistryError(
            "{} replacement_declarations must be a JSON list".format(skill_name)
        )

    lifecycle = {}
    for index, item in enumerate(retired):
        where = "retired_declarations[{}]".format(index)
        if not isinstance(item, dict) or set(item) != {"id", "reason"}:
            raise RegistryError(
                "{} {} requires exactly id and reason".format(skill_name, where)
            )
        declaration_id = item.get("id")
        if (not isinstance(declaration_id, str)
                or not ID_PATTERN.fullmatch(declaration_id)):
            raise RegistryError("{} {} has an invalid id".format(
                skill_name, where
            ))
        if declaration_id not in known_ids:
            raise RegistryError("{} {} names an unknown id: {}".format(
                skill_name, where, declaration_id
            ))
        if not _one_line(item.get("reason")):
            raise RegistryError("{} {} reason must be one non-empty line".format(
                skill_name, where
            ))
        if declaration_id in lifecycle:
            raise RegistryError("{} repeats lifecycle id: {}".format(
                skill_name, declaration_id
            ))
        lifecycle[declaration_id] = ("retired", None)

    for index, item in enumerate(replacements):
        where = "replacement_declarations[{}]".format(index)
        if (not isinstance(item, dict)
                or set(item) != {"id", "replacement", "reason"}):
            raise RegistryError(
                "{} {} requires exactly id, replacement, and reason".format(
                    skill_name, where
                )
            )
        declaration_id = item.get("id")
        replacement = item.get("replacement")
        if (not isinstance(declaration_id, str)
                or not ID_PATTERN.fullmatch(declaration_id)):
            raise RegistryError("{} {} has an invalid id".format(
                skill_name, where
            ))
        if (not isinstance(replacement, str)
                or not ID_PATTERN.fullmatch(replacement)):
            raise RegistryError("{} {} has an invalid replacement".format(
                skill_name, where
            ))
        if declaration_id not in known_ids:
            raise RegistryError("{} {} names an unknown id: {}".format(
                skill_name, where, declaration_id
            ))
        if replacement not in known_ids or replacement == declaration_id:
            raise RegistryError("{} {} has an invalid replacement: {}".format(
                skill_name, where, replacement
            ))
        if not _one_line(item.get("reason")):
            raise RegistryError("{} {} reason must be one non-empty line".format(
                skill_name, where
            ))
        if declaration_id in lifecycle:
            raise RegistryError("{} repeats lifecycle id: {}".format(
                skill_name, declaration_id
            ))
        lifecycle[declaration_id] = ("replaced", replacement)
    return lifecycle


def load_registry(skill_dir):
    """Return validated declarations in source order."""
    skill_dir = Path(skill_dir)
    skill_name = skill_dir.name
    path = skill_dir / "declarations.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RegistryError("{} cannot read declarations.json: {}".format(
            skill_name, exc
        ))
    required_fields = {
        "schema_version", "known_declarations", "retired_declarations",
        "replacement_declarations", "declarations",
    }
    if not isinstance(data, dict) or data.get("schema_version") != 2:
        raise RegistryError(
            "{} declarations.json requires schema_version 2".format(skill_name)
        )
    if set(data) != required_fields:
        raise RegistryError(
            "{} declarations.json requires exactly {}".format(
                skill_name, ", ".join(sorted(required_fields))
            )
        )
    known = data.get("known_declarations")
    if (not isinstance(known, list) or not known
            or any(not isinstance(value, str) for value in known)):
        raise RegistryError(
            "{} known_declarations must be a non-empty JSON list of strings".format(
                skill_name
            )
        )
    invalid_known = [value for value in known if not ID_PATTERN.fullmatch(value)]
    if invalid_known:
        raise RegistryError("{} known_declarations has an invalid id: {}".format(
            skill_name, invalid_known[0]
        ))
    if len(set(known)) != len(known):
        raise RegistryError("{} known_declarations repeats an id".format(
            skill_name
        ))
    closed = CLOSED_DECLARATIONS.get(skill_name)
    if closed is None:
        raise RegistryError("{} has no closed declaration inventory".format(
            skill_name
        ))
    for declaration_id in closed:
        if declaration_id not in known:
            raise RegistryError("{} missing known declaration: {}".format(
                skill_name, declaration_id
            ))
    for declaration_id in known:
        if declaration_id not in closed:
            raise RegistryError("{} has an unexpected known declaration: {}".format(
                skill_name, declaration_id
            ))
    if tuple(known) != closed:
        raise RegistryError("{} known declaration order differs".format(skill_name))
    lifecycle = _lifecycle_ids(data, set(known), skill_name)
    declarations = data.get("declarations")
    if not isinstance(declarations, list) or not declarations:
        raise RegistryError(
            "{} declarations must be a non-empty JSON list".format(skill_name)
        )
    references_dir = skill_dir / "references"
    try:
        reference_names = set(os.listdir(str(references_dir)))
    except OSError as exc:
        raise RegistryError("cannot read references directory: {}".format(exc))

    seen = set()
    result = []
    for index, item in enumerate(declarations):
        where = "declarations[{}]".format(index)
        if not isinstance(item, dict):
            raise RegistryError("{} must be an object".format(where))
        if set(item) - {"id", "normative_text", "references"}:
            raise RegistryError("{} has an unknown field".format(where))
        declaration_id = item.get("id")
        text = item.get("normative_text")
        references = item.get("references", [])
        if not isinstance(declaration_id, str) or not ID_PATTERN.fullmatch(declaration_id):
            raise RegistryError("{} has an invalid id".format(where))
        if declaration_id in seen:
            raise RegistryError("duplicate declaration id: {}".format(declaration_id))
        if (not isinstance(text, str) or not text.strip()
                or "\n" in text or "\r" in text):
            raise RegistryError("{} normative_text must be one non-empty line".format(where))
        if (not isinstance(references, list)
                or any(not isinstance(value, str) for value in references)):
            raise RegistryError("{} references must be a JSON list of strings".format(where))
        normalized = []
        for reference in references:
            if not REFERENCE_PATTERN.fullmatch(reference):
                raise RegistryError("{} has an invalid reference: {}".format(
                    where, reference
                ))
            candidate = Path(reference)
            if (candidate.is_absolute() or ".." in candidate.parts
                    or len(candidate.parts) != 2
                    or candidate.parts[0] != "references"
                    or candidate.suffix != ".md"):
                raise RegistryError("{} has an invalid reference: {}".format(
                    where, reference
                ))
            if (candidate.name not in reference_names
                    or (references_dir / candidate.name).is_symlink()
                    or not (references_dir / candidate.name).is_file()):
                raise RegistryError("{} reference is absent: {}".format(
                    where, reference
                ))
            if reference in normalized:
                raise RegistryError("{} repeats reference: {}".format(
                    where, reference
                ))
            normalized.append(reference)
        seen.add(declaration_id)
        result.append({
            "id": declaration_id,
            "normative_text": text,
            "references": tuple(normalized),
        })
    actual_ids = [item["id"] for item in result]
    expected_ids = [value for value in known if value not in lifecycle]
    for declaration_id in expected_ids:
        if declaration_id not in seen:
            raise RegistryError(
                "{} declaration {} left the registry without a retired or "
                "replacement record".format(skill_name, declaration_id)
            )
    for declaration_id in actual_ids:
        if declaration_id not in expected_ids:
            raise RegistryError(
                "{} declaration {} is active despite its lifecycle record".format(
                    skill_name, declaration_id
                )
            )
    if actual_ids != expected_ids:
        raise RegistryError("{} active declaration order differs from the "
                            "known_declarations ledger".format(skill_name))
    active = set(actual_ids)
    for declaration_id, (status, replacement) in lifecycle.items():
        if status == "replaced" and replacement not in active:
            raise RegistryError(
                "{} replacement for {} is not active: {}".format(
                    skill_name, declaration_id, replacement
                )
            )
    return tuple(result)


def render_block(declarations):
    """Render the complete generated Markdown block."""
    lines = [
        BEGIN_MARKER,
        "",
        "## Canonical behavior declarations",
        "",
        "This section is generated by `scripts/declaration_registry.py` from",
        "`declarations.json`; edit the registry, then run `write-body`.",
        "",
    ]
    for declaration in declarations:
        pointers = declaration["references"]
        suffix = ""
        if pointers:
            links = ["[{}]({})".format(Path(value).name, value)
                     for value in pointers]
            suffix = " (Elaboration: {}.)".format(", ".join(links))
        lines.append("- **`{}`:** {}{}".format(
            declaration["id"], declaration["normative_text"], suffix
        ))
    lines.extend(["", END_MARKER])
    return "\n".join(lines)


def expected_skill_text(skill_dir):
    """Return SKILL.md with only the marked block regenerated."""
    path = Path(skill_dir) / "SKILL.md"
    text = path.read_bytes().decode("utf-8")
    if text.count(BEGIN_MARKER) != 1 or text.count(END_MARKER) != 1:
        raise RegistryError("SKILL.md must contain exactly one generated block")
    start = text.index(BEGIN_MARKER)
    end = text.index(END_MARKER, start) + len(END_MARKER)
    return text[:start] + render_block(load_registry(skill_dir)) + text[end:]


def body_diff(skill_dir):
    """Return a unified diff when the committed block has drifted."""
    path = Path(skill_dir) / "SKILL.md"
    actual = path.read_bytes().decode("utf-8")
    expected = expected_skill_text(skill_dir)
    if actual == expected:
        return ""
    return "".join(difflib.unified_diff(
        actual.splitlines(True),
        expected.splitlines(True),
        fromfile="SKILL.md (committed)",
        tofile="SKILL.md (generated)",
    ))


def write_body(skill_dir):
    """Atomically replace SKILL.md with the registry rendering."""
    path = Path(skill_dir) / "SKILL.md"
    expected = expected_skill_text(skill_dir)
    mode = stat.S_IMODE(path.stat().st_mode)
    descriptor, temporary = tempfile.mkstemp(prefix=".SKILL.md.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(expected)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), mode)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _reference_lines(path):
    """Read physical LF-delimited lines without Unicode newline folding."""
    text = path.read_bytes().decode("utf-8")
    if text.startswith("\ufeff"):
        raise RegistryError("{} starts with a UTF-8 BOM".format(path))
    forbidden = ("\r", "\v", "\f", "\x85", "\u2028", "\u2029")
    if any(value in text for value in forbidden):
        raise RegistryError("{} contains a non-LF line terminator".format(path))
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _split_declaration_suffix(line):
    marker = DECLARATION_SUFFIX.search(line)
    if marker is None:
        return line, (), None
    prefix = line[:marker.start()]
    if "<!--" in prefix or "-->" in prefix:
        return line, (), "unsupported reference syntax: multiple HTML comments"
    ids = tuple(value.strip() for value in marker.group(1).split(",")
                if value.strip())
    if not ids:
        return prefix, (), "unsupported reference syntax: empty declaration marker"
    return prefix, ids, None


def _outside_fence_syntax_problem(line):
    if "\t" in line:
        return "unsupported reference syntax: tab outside a fenced code block"
    indentation = len(line) - len(line.lstrip(" "))
    if indentation >= 4:
        return "unsupported reference syntax: indentation of four or more spaces"
    if line.startswith(">") or line.startswith(" >") or line.startswith("  >") or line.startswith("   >"):
        return "unsupported reference syntax: blockquote"
    if FENCE_RUN.search(line):
        return "unsupported reference syntax: noncanonical fence"
    if THEMATIC_OR_SETEXT.fullmatch(line):
        return "unsupported reference syntax: thematic break or Setext heading"
    if "<" in line:
        return "unsupported reference syntax: raw HTML"
    if re.match(r"^\[[^]]+\]:", line.lstrip(" ")):
        return "unsupported reference syntax: link definition"
    if indentation:
        if indentation != 2:
            return "unsupported reference syntax: prose indentation must be two spaces"
        if line[2:].startswith(("- ", "+ ", "* ")) or ORDERED_LIST.match(line[2:]):
            return "unsupported reference syntax: nested list"
    elif ((line.startswith(("-", "+", "*")) and not UNORDERED_LIST.match(line))
          or (re.match(r"^[0-9]{1,9}[.)]", line) and not ORDERED_LIST.match(line))):
        return "unsupported reference syntax: list marker must use one space"
    prefix = PROSE_PREFIX.match(line)
    payload = prefix.group("payload") if prefix else line
    if not payload or payload[0] not in PROSE_START:
        return "unsupported reference syntax: ambiguous or empty prose content"
    if ((prefix is None or prefix.group("heading") is None)
            and ORDERED_AT_CONTENT.match(payload)):
        return "unsupported reference syntax: ordered marker at prose content position"
    return None


def _scan_references(skill_dir):
    """Return dialect problems and valid elaboration-marker occurrences."""
    declarations = load_registry(skill_dir)
    by_id = {item["id"]: item for item in declarations}
    root = Path(skill_dir) / "references"
    problems = []
    elaborations = {}
    for path in sorted(root.rglob("*.md")):
        relative = path.relative_to(Path(skill_dir)).as_posix()
        fence = None
        opener_line = None
        for number, line in enumerate(_reference_lines(path), 1):
            if fence is not None:
                closer = FENCE_CLOSER.fullmatch(line)
                if (closer and closer.group("fence")[0] == fence[0]
                        and len(closer.group("fence")) >= len(fence)):
                    if closer.group("indent"):
                        problems.append(
                            "{}:{}: unsupported reference syntax: "
                            "fence closer must start at column zero".format(
                                relative, number
                            )
                        )
                    fence = None
                    opener_line = None
                continue

            if "\t" in line:
                problems.append(
                    "{}:{}: unsupported reference syntax: tab outside a "
                    "fenced code block".format(relative, number)
                )
                continue
            opener = FENCE_OPENER.fullmatch(line)
            if opener:
                fence = opener.group("fence")
                opener_line = number
                continue
            if not line:
                continue
            if re.match(r"^<!--\s*declaration:", line, re.IGNORECASE):
                problems.append(
                    "{}:{}: unsupported reference syntax: declaration marker "
                    "must follow prose on the same line".format(relative, number)
                )
                continue
            if STANDALONE_COMMENT.fullmatch(line):
                continue

            payload, marker_ids, marker_problem = _split_declaration_suffix(line)
            if marker_problem:
                problems.append("{}:{}: {}".format(
                    relative, number, marker_problem
                ))
                continue
            if "<!--" in payload or "-->" in payload:
                problems.append(
                    "{}:{}: unsupported reference syntax: malformed or mixed "
                    "HTML comment".format(relative, number)
                )
                continue
            syntax_problem = _outside_fence_syntax_problem(payload)
            if syntax_problem:
                problems.append("{}:{}: {}".format(
                    relative, number, syntax_problem
                ))
                continue
            for declaration_id in marker_ids:
                declaration = by_id.get(declaration_id)
                if declaration is None:
                    problems.append("{}:{}: unknown declaration id {}".format(
                        relative, number, declaration_id
                    ))
                elif relative not in declaration["references"]:
                    problems.append(
                        "{}:{}: declaration {} does not point to this reference".format(
                            relative, number, declaration_id
                        )
                    )
                else:
                    key = (relative, declaration_id)
                    elaborations[key] = elaborations.get(key, 0) + 1
            normalized_payload = payload.replace("\u2019", "'")
            if IMPERATIVE_MODAL.search(normalized_payload) and not marker_ids:
                problems.append(
                    "{}:{}: imperative modal lacks a declaration marker".format(
                        relative, number
                    )
                )
        if fence is not None:
            problems.append(
                "{}:{}: unsupported reference syntax: unclosed fenced code block".format(
                    relative, opener_line
                )
            )
    return problems, elaborations


def reference_problems(skill_dir):
    """Validate the strict reference dialect and modal-to-declaration ties."""
    return _scan_references(skill_dir)[0]


def reference_elaborations(skill_dir):
    """Return the parsed occurrence count for each valid elaboration tie."""
    elaborations = _scan_references(skill_dir)[1]
    return tuple(
        (reference, declaration_id, count)
        for (reference, declaration_id), count in sorted(elaborations.items())
    )


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--skill-dir", type=Path, default=_skill_dir_from_script())
    parser.add_argument("action", choices=(
        "check-body", "write-body", "check-references",
    ))
    args = parser.parse_args(argv)
    try:
        if args.action == "write-body":
            write_body(args.skill_dir)
            return 0
        if args.action == "check-body":
            difference = body_diff(args.skill_dir)
            if difference:
                sys.stderr.write(difference)
                return 1
            return 0
        problems = reference_problems(args.skill_dir)
        if problems:
            sys.stderr.write("\n".join(problems) + "\n")
            return 1
        return 0
    except (OSError, UnicodeError, RegistryError) as exc:
        sys.stderr.write("declaration registry error: {}\n".format(exc))
        return 2


if __name__ == "__main__":
    sys.exit(main())
