"""Offline census and relational checks; never import or execute inventoried roots."""
import ast
import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/d5/entrypoint-inventory.json"
MANIFEST = ROOT / "tests/fixtures/d5/integration-manifest.json"
VENDORED = ("agent-bus", "paseo", "paseo-advisor", "paseo-committee",
            "paseo-handoff", "paseo-loop", "pi-fleet", "start-a-sprint")


def source_files(root):
    """Tracked-layout inputs, including hidden launch configuration, without Git.

    Fixture strings are data, not launch sites. Exclude this inventory's own
    assertions so the census cannot satisfy itself. No caller's HOME is read.
    """
    files = []
    for directory in ("bin", "lib", "scripts", "skills", "tests", "docs",
                      "verifiers", ".claude", ".github"):
        for path in (root / directory).rglob("*"):
            rel = path.relative_to(root)
            if (path.is_file() and not path.is_symlink()
                    and "__pycache__" not in rel.parts
                    and not str(rel).startswith("tests/fixtures/")
                    and path.name != Path(__file__).name
                    and (path.suffix in (".py", ".sh", ".md", ".json", ".yml", ".yaml", ".plist")
                         or directory == "bin")):
                files.append(path)
    files += [root / p for p in ("install.sh", "README.md", "CLAUDE.md", "verifiers.json")]
    return sorted(p for p in files if p.is_file())


def tree_for(path):
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".py" or text.startswith("#!/usr/bin/env python"):
        return ast.parse(text)
    return None


def discover(root=ROOT):
    """Union of executable census, path launches, D5 APIs and vendored workflows.

    Resolve names used in launch expressions through all same-file assignments;
    retain ambiguous basename matches rather than silently selecting a sibling.
    Generated inline programs and external executables are effects of their
    launcher, not fictional files in this repository.
    """
    files = source_files(root)
    by_name = {}
    for path in files:
        by_name.setdefault(path.name, []).append(path)
    found = {}

    def add(path, source, line, mechanism):
        key = path.relative_to(root).as_posix()
        evidence = [source.relative_to(root).as_posix(), line, mechanism]
        if evidence not in found.setdefault(key, []):
            found[key].append(evidence)

    def referenced(text, source, line, mechanism):
        for name in re.findall(r"[\w./-]+\.(?:py|sh)\b", text):
            for path in by_name.get(Path(name).name, ()):
                add(path, source, line, mechanism)

    for path in files:
        rel = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        tree = tree_for(path)
        mains = [n for n in (tree.body if tree else ()) if isinstance(n, ast.If)
                 and any(isinstance(x, ast.Constant) and x.value == "__main__"
                         for x in ast.walk(n.test))]
        seed = ((rel.startswith(("skills/hanig-", "lib/")) and mains)
                or (rel.startswith("bin/") and path.stat().st_mode & 0o111)
                or rel in ("install.sh", "skills/hanig-swarm/scripts/swarm-cron.sh"))
        if seed:
            add(path, path, mains[0].lineno if mains else 1, "seed")
        elif ((mains or (tree and text.startswith("#!")))
              and not rel.startswith("tests/test_")):
            add(path, path, mains[0].lineno if mains else 1, "program")
        if path.name.startswith("d5_") and path.suffix == ".py":
            add(path, path, 1, "manifest-helper")
        if path.name == "SKILL.md" and path.parent.name in VENDORED:
            add(path, path, 1, "vendored-workflow")
        for line, value in enumerate(text.splitlines(), 1):
            if re.search(r"python\d*(?:\.\d+)*\s+|run_sandbox\(|\bexec\s|launchd|cron", value):
                referenced(value, path, line, "text-launch")
        if tree is None:
            continue
        assignments = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        assignments.setdefault(target.id, []).append(node.value)

        def strings(node, seen):
            result = []
            for child in ast.walk(node):
                if isinstance(child, ast.Constant) and isinstance(child.value, str):
                    result.append(child.value)
                elif isinstance(child, ast.Name) and child.id not in seen:
                    seen.add(child.id)
                    for value in assignments.get(child.id, ()):
                        result.extend(strings(value, seen))
            return result

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if name in ("run", "Popen", "call", "check_call", "check_output", "execv",
                        "execve", "execvp", "execl", "run_sandbox", "protected_spawn",
                        "worker_spawn", "script_path", "with_name"):
                for arg in node.args:
                    for value in strings(arg, set()):
                        referenced(value, path, node.lineno, "argv-path")
    return {key: sorted(value) for key, value in sorted(found.items())}


def literal_strings(node, bindings):
    """Conservative static strings, including conditional lists and flag joins."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.Name):
        return bindings.get(node.id, set())
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return set().union(*(literal_strings(n, bindings) for n in node.elts))
    if isinstance(node, ast.IfExp):
        return literal_strings(node.body, bindings) | literal_strings(node.orelse, bindings)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return {a + b for a in literal_strings(node.left, bindings)
                for b in literal_strings(node.right, bindings)}
    return set()


def parser_tokens(path):
    """Static selector census; follows the operator's delegated parsers too."""
    paths = [path]
    if path.name == "linear_sync.py":
        paths += [path.with_name(name) for name in ("linear_file.py", "linear_issue.py")]
    tokens = set()
    for source in paths:
        tree = tree_for(source)
        if tree is None:
            continue
        bindings = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.For):
                if isinstance(node.target, ast.Name):
                    bindings.setdefault(node.target.id, set()).update(literal_strings(node.iter, {}))
                elif isinstance(node.target, ast.Tuple) and isinstance(node.iter, (ast.Tuple, ast.List)):
                    for row in node.iter.elts:
                        if isinstance(row, (ast.Tuple, ast.List)):
                            for target, value in zip(node.target.elts, row.elts):
                                if isinstance(target, ast.Name):
                                    bindings.setdefault(target.id, set()).update(literal_strings(value, {}))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", "")
            if name not in ("add_parser", "add_argument"):
                continue
            for arg in node.args:
                for value in literal_strings(arg, bindings):
                    if name == "add_parser" or value.startswith("-"):
                        tokens.add(value)
            for keyword in node.keywords:
                if keyword.arg == "choices":
                    tokens.update(literal_strings(keyword.value, bindings))
    return sorted(tokens)


def assert_census(case, data, discovered):
    case.assertEqual(sorted(r["path"] for r in data["roots"]), sorted(discovered))


def load_inventory(path=FIXTURE):
    data = json.loads(path.read_text())
    columns = data["record_columns"]
    if columns != ["id", "root", "mode", "effects", "disposition", "branches",
                   "guard", "proof", "placement", "boundaries", "routes", "new"]:
        raise ValueError("unexpected record columns")
    if any(not isinstance(row, list) or len(row) != len(columns) for row in data["records"]):
        raise ValueError("invalid record row")
    data["records"] = [dict(zip(columns, row)) for row in data["records"]]
    columns = data["root_columns"]
    if columns != ["id", "path", "discovery", "parser_tokens", "environment_names", "api"]:
        raise ValueError("unexpected root columns")
    if any(not isinstance(row, list) or len(row) != len(columns) for row in data["roots"]):
        raise ValueError("invalid root row")
    data["roots"] = [dict(zip(columns, row)) for row in data["roots"]]

    def expand(value):
        prefix, _, suffix = value.partition("/")
        return data["source_roots"][prefix] + suffix if prefix in data["source_roots"] else value

    for root in data["roots"]:
        root["path"] = expand(root["path"])
    paths = {r["id"]: r["path"] for r in data["roots"]}
    for root in data["roots"]:
        for citation in root["discovery"]:
            citation[0] = paths.get(citation[0], expand(citation[0]))
    for effect in data["effects"].values():
        for citation in effect["citations"]:
            citation[0] = paths.get(citation[0], expand(citation[0]))
    for record in data["records"]:
        if record["branches"].startswith("$"):
            record["branches"] = data["text"][record["branches"][1:]]
    return data


def assert_reconciliation(case, data, manifest):
    records = {r["id"]: r for r in data["records"]}
    case.assertEqual(len(records), len(data["records"]))
    case.assertEqual(set(data["boundaries"]), set(manifest["boundaries"]))
    case.assertEqual(set(data["routes"]), {str(n) for n in range(1, 77)})
    case.assertEqual(set(data["parsers"]), set(manifest["tools"]))
    for boundary, refs in data["boundaries"].items():
        case.assertTrue(refs)
        for ref in refs:
            case.assertIn(boundary, records[ref]["boundaries"])
    for route, row in data["routes"].items():
        case.assertTrue(row["records"])
        case.assertTrue(row["site"])
        for ref in row["records"]:
            case.assertIn(route, records[ref]["routes"])
    for tool, mapping in data["parsers"].items():
        expected = set(manifest["tools"][tool]["commands"])
        expected.update(manifest["tools"][tool].get("modes", {}))
        case.assertEqual(set(mapping), expected)
        for refs in mapping.values():
            case.assertTrue(refs)
            case.assertTrue(set(refs) <= set(records))
    for key, record in records.items():
        case.assertTrue(record["boundaries"] or record["routes"] or record["new"])
        for boundary in record["boundaries"]:
            case.assertIn(key, data["boundaries"][boundary])
        for route in record["routes"]:
            case.assertIn(key, data["routes"][route]["records"])


class TestD5Inventory(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = load_inventory()
        cls.manifest = json.loads(MANIFEST.read_text())

    def test_shape_and_dispositions(self):
        data = self.data
        self.assertEqual(data["schema"], 1)
        self.assertEqual(len(data["base"]), 40)
        roots = {r["id"]: r for r in data["roots"]}
        self.assertEqual(len(roots), len(data["roots"]))
        effects = data["effects"]
        represented = set()
        for record in data["records"]:
            self.assertIn(record["root"], roots)
            represented.add(record["root"])
            self.assertIn(record["disposition"], ("admitted", "refused"))
            self.assertIsInstance(record["mode"], list)
            self.assertTrue(record["mode"])
            self.assertTrue(all(isinstance(m, str) and m for m in record["mode"]))
            self.assertIs(type(record["new"]), bool)
            for field in ("mode", "guard", "proof", "placement", "branches"):
                self.assertTrue(record[field], record["id"])
            self.assertIn(record["guard"], data["guards"])
            self.assertIn(record["proof"], data["proofs"])
            self.assertIn(record["placement"], data["placement"])
            self.assertIsInstance(record["effects"], list)
            self.assertTrue(set(record["effects"]) <= set(effects))
        self.assertEqual(represented, set(roots))
        for effect in effects.values():
            self.assertTrue(effect["description"])
            self.assertTrue(effect["citations"])
            for path, line in effect["citations"]:
                prefix, _, suffix = path.partition("/")
                if prefix in data["source_roots"]:
                    path = data["source_roots"][prefix] + suffix
                self.assertGreater(line, 0)
                self.assertLessEqual(line, len((ROOT / path).read_text().splitlines()))

    def test_mechanical_census(self):
        discovered = discover()
        assert_census(self, self.data, discovered)
        for root in self.data["roots"]:
            self.assertTrue(root["discovery"])
            for citation in root["discovery"]:
                self.assertIn(citation, discovered[root["path"]])
            self.assertEqual(root["parser_tokens"], parser_tokens(ROOT / root["path"]))

    def test_bidirectional_reconciliation(self):
        assert_reconciliation(self, self.data, self.manifest)


    def test_delegated_and_constructed_selectors(self):
        project = ROOT / "skills/hanig-project/scripts/linear_sync.py"
        self.assertTrue({"file", "issue", "new", "edit", "replay", "--adopt-checked",
                         "--add-blocked-by", "--remove-blocks", "--out", "--audit"}
                        <= set(parser_tokens(project)))
        committee = ROOT / "skills/hanig-review-gate/scripts/committee.py"
        self.assertTrue({"synthesize", "tiebreak"} <= set(parser_tokens(committee)))
        self.assertEqual(parser_tokens(ROOT / "scripts/docs_index.py"), ["--root", "write"])

    def test_selector_and_finding_references(self):
        data = self.data
        records = {r["id"]: r for r in data["records"]}
        expected = {r["id"] for r in data["roots"] if r["parser_tokens"]}
        self.assertEqual(set(data["selector_records"]), expected)
        for root in data["roots"]:
            if root["id"] not in expected:
                continue
            selectors = data["selector_records"][root["id"]]
            self.assertEqual(selectors["tokens"], root["parser_tokens"])
            self.assertEqual(set(selectors["records"]),
                             {r["id"] for r in data["records"] if r["root"] == root["id"]})
        self.assertEqual(set(data["finding_records"]), {f[0] for f in data["findings"]})
        for refs in data["finding_records"].values():
            self.assertTrue(refs)
            self.assertTrue(set(refs) <= set(records))
        self.assertTrue(data["supported_envelope"].startswith("Supported envelope and accepted limits\n"))
        self.assertEqual(data["discovery"]["root_count"], len(data["roots"]))
        for part in data["partition"]:
            self.assertLessEqual(part["max_diff_bytes"], 55000)

    def test_summary_is_bounded_and_names_findings(self):
        path = FIXTURE.with_suffix(".md")
        self.assertLessEqual(len(path.read_bytes()), 6000)
        summary = path.read_text()
        for finding, refs in self.data["finding_records"].items():
            self.assertIn(finding, summary)
            for ref in refs:
                self.assertIn(ref, summary)

    def test_drop_a_root_mutation(self):
        # Exercise the same equality consumed by the production census test.
        from copy import deepcopy
        mutated = deepcopy(self.data)
        mutated["roots"].pop()
        with self.assertRaises(AssertionError):
            assert_census(self, mutated, discover())

    def test_drop_a_boundary_mutation(self):
        from copy import deepcopy
        mutated = deepcopy(self.data)
        mutated["boundaries"].pop(next(iter(mutated["boundaries"])))
        with self.assertRaises(AssertionError):
            assert_reconciliation(self, mutated, self.manifest)


if __name__ == "__main__":
    unittest.main()
