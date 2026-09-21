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


class RegistryError(ValueError):
    """The registry or generated block violates its data contract."""


def _skill_dir_from_script():
    return Path(__file__).resolve().parents[1]


def load_registry(skill_dir):
    """Return validated declarations in source order."""
    path = Path(skill_dir) / "declarations.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RegistryError("cannot read declarations.json: {}".format(exc))
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise RegistryError("declarations.json requires schema_version 1")
    declarations = data.get("declarations")
    if not isinstance(declarations, list) or not declarations:
        raise RegistryError("declarations must be a non-empty JSON list")
    references_dir = Path(skill_dir) / "references"
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


def reference_problems(skill_dir):
    """Validate the strict reference dialect and modal-to-declaration ties."""
    declarations = load_registry(skill_dir)
    by_id = {item["id"]: item for item in declarations}
    root = Path(skill_dir) / "references"
    problems = []
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
    return problems


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
