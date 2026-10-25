"""Offline census and relational checks; never import or execute inventoried roots."""
import ast
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/d5/entrypoint-inventory.json"
MANIFEST = ROOT / "tests/fixtures/d5/integration-manifest.json"
VENDORED = ("agent-bus", "paseo", "paseo-advisor", "paseo-committee",
            "paseo-handoff", "paseo-loop", "pi-fleet", "start-a-sprint")


def track_fixture(root, *paths):
    """Put synthetic census inputs in a disposable Git index, without commits."""
    if not (root / ".git").exists():
        subprocess.check_call(["git", "init", "-q", str(root)])
    subprocess.check_call(["git", "-C", str(root), "add", "--"] + list(paths))


def source_files(root):
    """Tracked inputs only, including hidden launch configuration.

    Fixture strings are data, not launch sites. Exclude this inventory's own
    assertions so the census cannot satisfy itself. No caller's HOME is read.
    """
    files = []
    tracked = subprocess.check_output(["git", "ls-files", "-z", "--cached"], cwd=str(root))
    for name in sorted(set(tracked.decode("utf-8").split("\0")) - {""}):
        path = root / name
        rel = path.relative_to(root)
        if ((path.is_symlink() or path.is_file())
                and not {".git", "__pycache__"}.intersection(rel.parts)
                and not rel.as_posix().startswith("tests/fixtures/")
                and path.name != Path(__file__).name):
            files.append(path)
    return sorted(files)


def source_text(path):
    # A tracked alias is a launch root, not permission to read its target.
    if path.is_symlink():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ""
    return "" if "\0" in text else text


def interpreter(text):
    match = re.match(r"^#!\s*(?:\S*/env\s+(?:-S\s+)?)?(?:\S*/)?([^\s/]+)", text)
    return match.group(1) if match else ""


def tree_for(path):
    text = source_text(path)
    if path.suffix == ".py" or re.fullmatch(r"python\d*(?:\.\d+)*", interpreter(text)):
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
        text = source_text(path)
        tree = tree_for(path)
        mains = [n for n in (tree.body if tree else ()) if isinstance(n, ast.If)
                 and any(isinstance(x, ast.Constant) and x.value == "__main__"
                         for x in ast.walk(n.test))]
        seed = ((rel.startswith(("skills/hanig-", "lib/")) and mains)
                or (rel.startswith("bin/") and (path.is_symlink() or path.stat().st_mode & 0o111))
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
            if path.suffix in (".plist", ".cron"):
                referenced(value, path, line, "launch-config")
            for launch in re.finditer(
                    r"\b(?:python\d*(?:\.\d+)*|exec)\s+"
                    r"((?:-[\w-]+\s+)*[\"']?[$\w{}/.~+-]+\.(?:py|sh)\b)", value):
                prefix = value[:launch.start()].rsplit(";", 1)[-1]
                if not re.search(r"\b(?:not|never)\b", prefix, re.I):
                    referenced(launch.group(1), path, line, "text-launch")
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
            text = source_text(source)
            if interpreter(text) in ("sh", "bash", "dash", "ksh", "zsh"):
                for line in text.splitlines():
                    if re.match(r"\s*--?[a-z][^)]*\)", line):
                        tokens.update(re.findall(r"--?[a-z][a-z-]*", line.split(")", 1)[0]))
                    if re.search(r'\[.*\$.*=.*"--[a-z-]+".*\]', line):
                        tokens.update(re.findall(r'"(--[a-z-]+)"', line))
            continue
        if source.name in ("agent-view", "agent-manager"):
            for node in ast.walk(tree):
                if isinstance(node, ast.Compare):
                    tokens.update(c.value for c in ast.walk(node)
                                  if isinstance(c, ast.Constant) and isinstance(c.value, str)
                                  and c.value.startswith("--"))
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
    root_names = {r["id"]: Path(r["path"]).name for r in data["roots"]}
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
        owners = {"S": "swarm.py", "M": "merge_unit.py", "W/unit.py": "unit.py"}
        expected = {owners[site.strip().split(":", 1)[0]] for site in row["site"].split(",")}
        case.assertEqual({root_names[records[ref]["root"]] for ref in row["records"]}, expected)
    for tool, mapping in data["parsers"].items():
        expected = set(manifest["tools"][tool]["commands"])
        expected.update(manifest["tools"][tool].get("modes", {}))
        case.assertEqual(set(mapping), expected)
        for selector, refs in mapping.items():
            case.assertTrue(refs)
            case.assertTrue(set(refs) <= set(records))
            case.assertEqual(set(refs), {key for key, record in records.items()
                                        if root_names[record["root"]] == tool
                                        and selector in record["mode"]})
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
        self.assertRegex(data["base"], r"\A[0-9a-f]{40}\Z")
        self.assertEqual(data["citation_validation"]["base"], data["base"])
        self.assertTrue(data["citation_validation"]["output"])
        roots = {r["id"]: r for r in data["roots"]}
        self.assertEqual(len(roots), len(data["roots"]))
        effects = data["effects"]
        represented = set()
        modes = set()
        for record in data["records"]:
            self.assertIn(record["root"], roots)
            represented.add(record["root"])
            self.assertIn(record["disposition"], ("admitted", "refused"))
            self.assertIsInstance(record["mode"], list)
            self.assertTrue(record["mode"])
            self.assertTrue(all(isinstance(m, str) and m for m in record["mode"]))
            for mode in record["mode"]:
                key = (record["root"], mode)
                self.assertNotIn(key, modes, "overlapping mode dispositions")
                modes.add(key)
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
                self.assertIsInstance(path, str)
                self.assertTrue(path)
                self.assertIs(type(line), int)
                self.assertGreater(line, 0)

    def test_mechanical_census(self):
        discovered = discover()
        assert_census(self, self.data, discovered)
        for root in self.data["roots"]:
            self.assertTrue(root["discovery"])
            for path, line, mechanism in root["discovery"]:
                self.assertIsInstance(path, str)
                self.assertTrue(path)
                self.assertIs(type(line), int)
                self.assertGreater(line, 0)
                self.assertIn(mechanism, ("seed", "program", "manifest-helper",
                                         "vendored-workflow", "launch-config",
                                         "text-launch", "argv-path"))
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

    def test_manual_cli_selectors(self):
        self.assertEqual(parser_tokens(ROOT / "bin/doctor"), ["--help", "--json", "--prefix", "-h"])
        self.assertEqual(parser_tokens(ROOT / "bin/probe.sh"), ["--json"])
        self.assertTrue({"--all", "--json", "--web"} <= set(parser_tokens(ROOT / "bin/agent-view")))
        self.assertEqual(parser_tokens(ROOT / "bin/agent-manager"), ["--once"])

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

    def test_repository_config_and_new_directories_are_discovered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for folder in ("config", "new-launchers"):
                path = root / folder / "launch.py"
                path.parent.mkdir()
                path.write_text("if __name__ == '__main__': pass\n")
                track_fixture(root, path.relative_to(root).as_posix())
                self.assertIn(path.relative_to(root).as_posix(), discover(root))

    def test_negative_cron_prose_is_not_a_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "lib").mkdir()
            (root / "lib/helper.py").write_text("def helper(): return 1\n")
            note = root / "README.md"
            note.write_text("cron does not invoke lib/helper.py\n")
            track_fixture(root, "lib/helper.py", "README.md")
            self.assertNotIn("lib/helper.py", discover(root))
            note.write_text("python3 lib/helper.py\n")
            self.assertIn("lib/helper.py", discover(root))
            note.write_text("")
            for name in ("job.plist", "job.cron"):
                config = root / name
                config.write_text("<string>lib/helper.py</string>\n")
                track_fixture(root, name)
                self.assertIn("lib/helper.py", discover(root))
                config.unlink()

    def test_local_config_and_candidates_do_not_enter_census(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / "skills/hanig-project/scripts/linear_file.py"
            helper.parent.mkdir(parents=True)
            helper.write_text("def helper(): return 1\n")
            (root / "README.md").write_text("Repository fixture\n")
            track_fixture(root, "README.md", helper.relative_to(root).as_posix())
            baseline = discover(root)
            local = root / ".claude/settings.local.json"
            local.parent.mkdir()
            candidate = root / "config/local.py"
            candidate.parent.mkdir()
            for ignored in (False, True):
                with self.subTest(ignored=ignored):
                    if ignored:
                        (root / ".git/info/exclude").write_text(".claude/\nconfig/\n")
                    local.write_text('{"note":"python3 skills/hanig-project/scripts/linear_file.py"}\n')
                    self.assertEqual(discover(root), baseline)
                    candidate.write_text("if __name__ == '__main__': pass\n")
                    self.assertEqual(discover(root), baseline)
                    local.unlink()
                    candidate.unlink()

    def test_honest_line_shift_preserves_snapshot_checks(self):
        read_text = Path.read_text
        cited = ROOT / "skills/hanig-swarm/scripts/swarm.py"

        def shifted(path, *args, **kwargs):
            text = read_text(path, *args, **kwargs)
            return "# Harmless line shift\n" + text if path == cited else text

        with mock.patch.object(Path, "read_text", shifted):
            self.test_mechanical_census()
            self.test_shape_and_dispositions()
        with mock.patch.object(Path, "read_text", side_effect=AssertionError("live citation read")):
            self.test_shape_and_dispositions()

    def test_parser_mapping_rejects_wrong_root_and_mode(self):
        from copy import deepcopy
        for parser, selector, ref in (("swarm.py", "validate", "r01.0"),
                                     ("merge_unit.py", "--verify-integration", "r31.0")):
            with self.subTest(parser=parser):
                data = deepcopy(self.data)
                data["parsers"][parser][selector] = [ref]
                with self.assertRaises(AssertionError):
                    assert_reconciliation(self, data, self.manifest)

    def test_route_75_retains_both_owners(self):
        from copy import deepcopy
        self.assertIn("r31.0", self.data["routes"]["75"]["records"])
        self.assertIn("75", next(r for r in self.data["records"] if r["id"] == "r31.0")["routes"])
        data = deepcopy(self.data)
        data["routes"]["75"]["records"].remove("r31.0")
        next(r for r in data["records"] if r["id"] == "r31.0")["routes"].remove("75")
        with self.assertRaises(AssertionError):
            assert_reconciliation(self, data, self.manifest)

    def test_tracked_config_formats_and_symlink_launchers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            (root / "tools").mkdir()
            (root / "bin").mkdir()
            (root / "tools/worker.py").write_text("def helper(): return 1\n")
            track_fixture(root, "tools/worker.py")
            for name in ("job.service", "job.conf", "job.toml", "job"):
                with self.subTest(config=name):
                    config = root / name
                    config.write_text("ExecStart=/usr/bin/python3 /repo/tools/worker.py\n")
                    track_fixture(root, name)
                    self.assertIn("tools/worker.py", discover(root))
                    config.write_text("")
            target = Path(directory) / "external-tool"
            target.write_text("#!/usr/bin/env python3\n# python3 tools/worker.py\n")
            target.chmod(0o700)
            link = root / "bin/alias"
            link.symlink_to(target)
            track_fixture(root, "bin/alias")
            self.assertIn("bin/alias", discover(root))
            self.assertNotIn("tools/worker.py", discover(root))
            (root / "binary.dat").write_bytes(b"\xff\x00")
            track_fixture(root, "binary.dat")
            target.unlink()
            self.assertIn("bin/alias", discover(root))

    def test_prose_commands_do_not_launch_unrelated_paths(self):
        original = Path.read_text
        for sentence in (
                "The example `python3 --version` does not run `lib/skill_lifecycle.py`.",
                "The python3 interpreter only imports skills/hanig-project/scripts/linear_file.py; it is never executed by path.",
                "Do not python3 lib/skill_lifecycle.py"):
            with self.subTest(sentence=sentence):
                def amended(path, *args, **kwargs):
                    text = original(path, *args, **kwargs)
                    return text + "\n" + sentence + "\n" if path == ROOT / "README.md" else text
                with mock.patch.object(Path, "read_text", amended):
                    self.test_mechanical_census()

    def test_equivalent_shebangs_retain_selectors(self):
        original = Path.read_text
        for name, shebang in (("doctor", "#!/usr/bin/env sh"),
                              ("doctor", "#!/bin/bash"),
                              ("agent-view", "#!/usr/bin/python3"),
                              ("agent-view", "#!/usr/bin/env -S python3")):
            with self.subTest(name=name, shebang=shebang):
                path = ROOT / "bin" / name
                expected = parser_tokens(path)
                def amended(source, *args, **kwargs):
                    text = original(source, *args, **kwargs)
                    return shebang + "\n" + text.split("\n", 1)[1] if source == path else text
                with mock.patch.object(Path, "read_text", amended):
                    self.assertEqual(parser_tokens(path), expected)

    def test_overlapping_mode_dispositions_are_rejected(self):
        from copy import deepcopy
        case = TestD5Inventory()
        case.data = deepcopy(self.data)
        record = deepcopy(next(r for r in case.data["records"] if r["id"] == "r01.0"))
        record.update(id="r01.1", mode=["--once"], disposition="admitted", proof="A", placement="A")
        case.data["records"].append(record)
        case.data["selector_records"]["r01"]["records"].append("r01.1")
        with self.assertRaises(AssertionError):
            case.test_shape_and_dispositions()


if __name__ == "__main__":
    unittest.main()
