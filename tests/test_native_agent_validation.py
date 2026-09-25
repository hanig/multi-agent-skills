"""Hermetic tests for the explicitly invoked native-agent host harness."""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
HARNESS_PATH = ROOT / "tests" / "native_agent_validation.py"
SPEC = importlib.util.spec_from_file_location("native_agent_validation_unit", HARNESS_PATH)
validation = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = validation
SPEC.loader.exec_module(validation)

EXPECTED_PI_PACKAGE_NAMES = (
    "@mariozechner/pi-coding-agent",
    "@earendil-works/pi-coding-agent",
)


def package_fixture(root, package_name, version="0.86.2"):
    package_root = root / "node_modules" / package_name
    entry = package_root / "dist" / "bundle" / "cli.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("// fixture\n", encoding="utf-8")
    (package_root / "package.json").write_text(
        json.dumps(
            {
                "name": package_name,
                "version": version,
                "main": "./dist/index.js",
            }
        ),
        encoding="utf-8",
    )
    return package_root, entry


class TestPiPackageResolution(unittest.TestCase):
    def test_current_and_legacy_package_scopes_resolve(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for package_name in EXPECTED_PI_PACKAGE_NAMES:
                with self.subTest(package_name=package_name):
                    package_root, executable = package_fixture(root, package_name)
                    resolved = validation._pi_package_root(
                        str(executable), cwd=root, env={"PATH": ""}
                    )
                    self.assertEqual(resolved, package_root.resolve())

    def test_unrelated_package_name_does_not_resolve(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _, executable = package_fixture(root, "@unrelated/pi-coding-agent")
            resolved = validation._pi_package_root(
                str(executable), cwd=root, env={"PATH": ""}
            )
        self.assertIsNone(resolved)

    def test_global_package_roots_try_both_supported_scopes(self):
        for package_name in EXPECTED_PI_PACKAGE_NAMES:
            with self.subTest(package_name=package_name), \
                    tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                package_root, _ = package_fixture(root, package_name)
                executable = root / "bin" / "pi"
                executable.parent.mkdir()
                executable.write_text("fixture\n", encoding="utf-8")

                def which(command, path=None):
                    return "/fixture/npm" if command == "npm" else None

                npm_result = {
                    "returncode": 0,
                    "stdout": str(root / "node_modules"),
                }
                with mock.patch.object(validation.shutil, "which", side_effect=which), \
                        mock.patch.object(validation, "_run", return_value=npm_result):
                    resolved = validation._pi_package_root(
                        str(executable), cwd=root, env={"PATH": ""}
                    )
                self.assertEqual(resolved, package_root)

    def test_renamed_global_package_does_not_shadow_legacy_in_later_root(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            first = root / "first"
            second = root / "second"
            package_fixture(first, "@earendil-works/pi-coding-agent")
            legacy_root, _ = package_fixture(
                second, "@mariozechner/pi-coding-agent", version="0.73.1"
            )
            executable = root / "bin" / "pi"
            executable.parent.mkdir()
            executable.write_text("fixture\n", encoding="utf-8")

            def which(command, path=None):
                return "/fixture/" + command if command in ("npm", "pnpm") else None

            def package_root(command, **kwargs):
                return {
                    "returncode": 0,
                    "stdout": str(
                        (first if command[0] == "npm" else second) / "node_modules"
                    ),
                }

            with mock.patch.object(validation.shutil, "which", side_effect=which), \
                    mock.patch.object(validation, "_run", side_effect=package_root):
                resolved = validation._pi_package_root(
                    str(executable), cwd=root, env={"PATH": ""}
                )
        self.assertEqual(resolved.resolve(), legacy_root.resolve())

    def test_unbuilt_renamed_ancestor_does_not_shadow_global_legacy(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            renamed_root, executable = package_fixture(
                root / "checkout", "@earendil-works/pi-coding-agent"
            )
            self.assertFalse(renamed_root.joinpath("dist", "index.js").exists())
            legacy_root, _ = package_fixture(
                root / "global", "@mariozechner/pi-coding-agent", version="0.73.1"
            )
            legacy_root.joinpath("dist", "index.js").write_text(
                "// fixture\n", encoding="utf-8"
            )

            def which(command, path=None):
                return "/fixture/npm" if command == "npm" else None

            npm_result = {
                "returncode": 0,
                "stdout": str(root / "global" / "node_modules"),
            }
            with mock.patch.object(validation.shutil, "which", side_effect=which), \
                    mock.patch.object(validation, "_run", return_value=npm_result):
                resolved = validation._pi_package_root(
                    str(executable), cwd=root, env={"PATH": ""}
                )
        self.assertEqual(resolved.resolve(), legacy_root.resolve())

    def test_path_executable_owner_precedes_global_legacy_scope(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            renamed_root, executable = package_fixture(
                root / "opt" / "pi", "@earendil-works/pi-coding-agent"
            )
            renamed_root.joinpath("dist", "index.js").write_text(
                "// fixture\n", encoding="utf-8"
            )
            legacy_root, _ = package_fixture(
                root / "global", "@mariozechner/pi-coding-agent", version="0.73.1"
            )
            legacy_root.joinpath("dist", "index.js").write_text(
                "// fixture\n", encoding="utf-8"
            )

            def which(command, path=None):
                return "/fixture/npm" if command == "npm" else None

            npm_result = {
                "returncode": 0,
                "stdout": str(root / "global" / "node_modules"),
            }
            with mock.patch.object(validation.shutil, "which", side_effect=which), \
                    mock.patch.object(validation, "_run", return_value=npm_result):
                resolved = validation._pi_package_root(
                    str(executable), cwd=root, env={"PATH": ""}
                )
        self.assertEqual(resolved.resolve(), renamed_root.resolve())

    def test_discovery_validates_path_owner_before_global_legacy(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            paths = {
                name: root / name for name in ("workspace", "tmp", "home", "pi")
            }
            for path in paths.values():
                path.mkdir()
            renamed_root, executable = package_fixture(
                root / "opt" / "pi", "@earendil-works/pi-coding-agent"
            )
            renamed_entry = renamed_root / "dist" / "index.js"
            renamed_entry.write_text("// fixture\n", encoding="utf-8")
            legacy_root, _ = package_fixture(
                root / "global", "@mariozechner/pi-coding-agent", version="0.73.1"
            )
            legacy_root.joinpath("dist", "index.js").write_text(
                "// fixture\n", encoding="utf-8"
            )
            installed = (
                paths["home"]
                / ".agents"
                / "skills"
                / validation.SKILL
                / "SKILL.md"
            )
            installed.parent.mkdir(parents=True)
            installed.write_text("fixture\n", encoding="utf-8")
            loaded_entries = []

            def which(command, path=None):
                if command == "pi":
                    return str(executable)
                if command == "npm":
                    return "/fixture/npm"
                return None

            def run(command, **kwargs):
                if command[:3] == ("npm", "root", "-g"):
                    return {
                        "returncode": 0,
                        "stdout": str(root / "global" / "node_modules"),
                    }
                self.assertEqual(command[0], "node")
                loaded_entries.append(Path(command[2]))
                return {
                    "returncode": 0,
                    "stdout": json.dumps(
                        {
                            "skills": [
                                {
                                    "name": validation.SKILL,
                                    "filePath": str(installed),
                                }
                            ]
                        }
                    ),
                    "stderr": "",
                }

            with mock.patch.object(
                validation.shutil, "which", side_effect=which
            ), mock.patch.object(validation, "_run", side_effect=run):
                result = validation._pi_discovery(paths, {"PATH": ""})

        self.assertEqual(result["status"], "unverified")
        self.assertEqual(
            result["package_version_gate"]["observed_package_name"],
            "@earendil-works/pi-coding-agent",
        )
        self.assertEqual(
            [entry.resolve() for entry in loaded_entries], [renamed_entry.resolve()]
        )

    def test_inaccessible_renamed_fallback_does_not_break_legacy_resolution(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            legacy_root, executable = package_fixture(
                root / "legacy", "@mariozechner/pi-coding-agent", version="0.73.1"
            )
            legacy_root.joinpath("dist", "index.js").write_text(
                "// fixture\n", encoding="utf-8"
            )
            global_root = root / "global" / "node_modules"
            blocked_scope = global_root / "@earendil-works"
            blocked_scope.mkdir(parents=True)
            blocked_manifest = blocked_scope / "pi-coding-agent" / "package.json"

            def which(command, path=None):
                return "/fixture/npm" if command == "npm" else None

            original_is_file = Path.is_file

            def is_file(path):
                if path == blocked_manifest:
                    raise PermissionError("fixture denies manifest access")
                return original_is_file(path)

            npm_result = {"returncode": 0, "stdout": str(global_root)}
            with mock.patch.object(
                validation.shutil, "which", side_effect=which
            ), mock.patch.object(
                validation, "_run", return_value=npm_result
            ), mock.patch.object(Path, "is_file", new=is_file):
                resolved = validation._pi_package_root(
                    str(executable), cwd=root, env={"PATH": ""}
                )
        self.assertEqual(resolved.resolve(), legacy_root.resolve())

    def test_deep_fallback_manifest_does_not_break_legacy_resolution(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            legacy_root, executable = package_fixture(
                root / "legacy", "@mariozechner/pi-coding-agent", version="0.73.1"
            )
            legacy_root.joinpath("dist", "index.js").write_text(
                "// fixture\n", encoding="utf-8"
            )
            global_root = root / "global" / "node_modules"
            fallback = global_root / "@earendil-works" / "pi-coding-agent"
            fallback.mkdir(parents=True)
            nested = "[" * 2000 + "0" + "]" * 2000
            fallback.joinpath("package.json").write_text(
                '{"name":"@earendil-works/pi-coding-agent","metadata":'
                + nested
                + "}",
                encoding="utf-8",
            )

            def which(command, path=None):
                return "/fixture/npm" if command == "npm" else None

            npm_result = {"returncode": 0, "stdout": str(global_root)}
            with mock.patch.object(
                validation.shutil, "which", side_effect=which
            ), mock.patch.object(validation, "_run", return_value=npm_result):
                resolved = validation._pi_package_root(
                    str(executable), cwd=root, env={"PATH": ""}
                )
        self.assertEqual(resolved.resolve(), legacy_root.resolve())

    def test_long_integer_fallback_does_not_break_legacy_resolution(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            legacy_root, executable = package_fixture(
                root / "legacy", "@mariozechner/pi-coding-agent", version="0.73.1"
            )
            legacy_root.joinpath("dist", "index.js").write_text(
                "// fixture\n", encoding="utf-8"
            )
            global_root = root / "global" / "node_modules"
            fallback = global_root / "@earendil-works" / "pi-coding-agent"
            fallback.mkdir(parents=True)
            long_integer = "1" * 5000
            fallback.joinpath("package.json").write_text(
                long_integer, encoding="utf-8"
            )

            def which(command, path=None):
                return "/fixture/npm" if command == "npm" else None

            original_loads = json.loads

            def loads(value):
                if value == long_integer:
                    raise ValueError("integer string conversion limit")
                return original_loads(value)

            npm_result = {"returncode": 0, "stdout": str(global_root)}
            with mock.patch.object(
                validation.shutil, "which", side_effect=which
            ), mock.patch.object(
                validation, "_run", return_value=npm_result
            ), mock.patch.object(validation.json, "loads", side_effect=loads):
                resolved = validation._pi_package_root(
                    str(executable), cwd=root, env={"PATH": ""}
                )
        self.assertEqual(resolved.resolve(), legacy_root.resolve())

    def test_fifo_fallback_manifest_is_not_read(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            legacy_root, executable = package_fixture(
                root / "legacy", "@mariozechner/pi-coding-agent", version="0.73.1"
            )
            legacy_root.joinpath("dist", "index.js").write_text(
                "// fixture\n", encoding="utf-8"
            )
            global_root = root / "global" / "node_modules"
            fallback = global_root / "@earendil-works" / "pi-coding-agent"
            fallback.mkdir(parents=True)
            fifo = fallback / "package.json"
            os.mkfifo(fifo)

            def which(command, path=None):
                return "/fixture/npm" if command == "npm" else None

            original_read_text = Path.read_text

            def read_text(path, *args, **kwargs):
                if path == fifo:
                    raise AssertionError("resolver attempted to read a FIFO manifest")
                return original_read_text(path, *args, **kwargs)

            npm_result = {"returncode": 0, "stdout": str(global_root)}
            with mock.patch.object(
                validation.shutil, "which", side_effect=which
            ), mock.patch.object(
                validation, "_run", return_value=npm_result
            ), mock.patch.object(Path, "read_text", new=read_text):
                resolved = validation._pi_package_root(
                    str(executable), cwd=root, env={"PATH": ""}
                )
        self.assertEqual(resolved.resolve(), legacy_root.resolve())

    def test_unreadable_renamed_entry_does_not_shadow_readable_later_root(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            first = root / "first"
            second = root / "second"
            first_root, _ = package_fixture(
                first, "@earendil-works/pi-coding-agent"
            )
            unreadable = first_root / "dist" / "index.js"
            unreadable.write_text("// fixture\n", encoding="utf-8")
            second_root, _ = package_fixture(
                second, "@earendil-works/pi-coding-agent"
            )
            second_root.joinpath("dist", "index.js").write_text(
                "// fixture\n", encoding="utf-8"
            )
            executable = root / "bin" / "pi"
            executable.parent.mkdir()
            executable.write_text("fixture\n", encoding="utf-8")

            def which(command, path=None):
                return "/fixture/" + command if command in ("npm", "pnpm") else None

            def package_root(command, **kwargs):
                return {
                    "returncode": 0,
                    "stdout": str(
                        (first if command[0] == "npm" else second) / "node_modules"
                    ),
                }

            original_open = Path.open

            def open_path(path, *args, **kwargs):
                if path == unreadable:
                    raise PermissionError("fixture denies entry access")
                return original_open(path, *args, **kwargs)

            with mock.patch.object(
                validation.shutil, "which", side_effect=which
            ), mock.patch.object(
                validation, "_run", side_effect=package_root
            ), mock.patch.object(Path, "open", new=open_path):
                resolved = validation._pi_package_root(
                    str(executable), cwd=root, env={"PATH": ""}
                )
        self.assertEqual(resolved.resolve(), second_root.resolve())

    def test_non_string_main_does_not_shadow_valid_later_package(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            first = root / "first"
            second = root / "second"
            first_root, _ = package_fixture(
                first, "@mariozechner/pi-coding-agent", version="0.73.1"
            )
            invalid_main = ["dist", "index.js"]
            first_root.joinpath("package.json").write_text(
                json.dumps(
                    {
                        "name": "@mariozechner/pi-coding-agent",
                        "version": "0.73.1",
                        "main": invalid_main,
                    }
                ),
                encoding="utf-8",
            )
            first_root.joinpath(str(invalid_main)).write_text(
                "// misleading fixture\n", encoding="utf-8"
            )
            second_root, _ = package_fixture(
                second, "@earendil-works/pi-coding-agent"
            )
            second_root.joinpath("dist", "index.js").write_text(
                "// fixture\n", encoding="utf-8"
            )
            executable = root / "bin" / "pi"
            executable.parent.mkdir()
            executable.write_text("fixture\n", encoding="utf-8")

            def which(command, path=None):
                return "/fixture/" + command if command in ("npm", "pnpm") else None

            def package_root(command, **kwargs):
                return {
                    "returncode": 0,
                    "stdout": str(
                        (first if command[0] == "npm" else second) / "node_modules"
                    ),
                }

            with mock.patch.object(
                validation.shutil, "which", side_effect=which
            ), mock.patch.object(validation, "_run", side_effect=package_root):
                resolved = validation._pi_package_root(
                    str(executable), cwd=root, env={"PATH": ""}
                )
        self.assertEqual(resolved.resolve(), second_root.resolve())

    def test_embedded_nul_entry_does_not_abort_later_package(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            first = root / "first"
            second = root / "second"
            first_root, _ = package_fixture(
                first, "@mariozechner/pi-coding-agent", version="0.73.1"
            )
            first_root.joinpath("package.json").write_text(
                json.dumps(
                    {
                        "name": "@mariozechner/pi-coding-agent",
                        "version": "0.73.1",
                        "main": "\x00",
                    }
                ),
                encoding="utf-8",
            )
            second_root, _ = package_fixture(
                second, "@earendil-works/pi-coding-agent"
            )
            second_root.joinpath("dist", "index.js").write_text(
                "// fixture\n", encoding="utf-8"
            )
            executable = root / "bin" / "pi"
            executable.parent.mkdir()
            executable.write_text("fixture\n", encoding="utf-8")

            def which(command, path=None):
                return "/fixture/" + command if command in ("npm", "pnpm") else None

            def package_root(command, **kwargs):
                return {
                    "returncode": 0,
                    "stdout": str(
                        (first if command[0] == "npm" else second) / "node_modules"
                    ),
                }

            with mock.patch.object(
                validation.shutil, "which", side_effect=which
            ), mock.patch.object(validation, "_run", side_effect=package_root):
                resolved = validation._pi_package_root(
                    str(executable), cwd=root, env={"PATH": ""}
                )
        self.assertEqual(resolved.resolve(), second_root.resolve())

    def test_non_object_unrelated_ancestor_manifest_is_ignored(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            legacy_root, executable = package_fixture(
                root, "@mariozechner/pi-coding-agent", version="0.73.1"
            )
            legacy_root.joinpath("dist", "index.js").write_text(
                "// fixture\n", encoding="utf-8"
            )
            (root / "package.json").write_text("[]\n", encoding="utf-8")
            resolved = validation._pi_package_root(
                str(executable), cwd=root, env={"PATH": ""}
            )
        self.assertEqual(resolved.resolve(), legacy_root.resolve())

    def test_unresolved_reason_names_both_supported_scopes(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            paths = {name: root / name for name in ("workspace", "tmp", "home", "pi")}
            for path in paths.values():
                path.mkdir()
            with mock.patch.object(validation.shutil, "which", return_value="/fixture/pi"), \
                    mock.patch.object(validation, "_pi_package_roots", return_value=[]):
                result = validation._pi_discovery(paths, {"PATH": ""})
        self.assertEqual(result["status"], "unavailable")
        for package_name in EXPECTED_PI_PACKAGE_NAMES:
            self.assertIn(package_name, result["reason"])
            self.assertIn(package_name, result["minimal_requirement"])

    def test_incomplete_package_reason_names_both_supported_scopes(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            paths = {name: root / name for name in ("workspace", "tmp", "home", "pi")}
            for path in paths.values():
                path.mkdir()
            package_root, _ = package_fixture(
                root, "@earendil-works/pi-coding-agent"
            )
            with mock.patch.object(
                validation.shutil, "which", return_value="/fixture/pi"
            ), mock.patch.object(
                validation, "_pi_package_roots", return_value=[package_root]
            ):
                result = validation._pi_discovery(paths, {"PATH": ""})
        self.assertEqual(result["status"], "unavailable")
        for package_name in EXPECTED_PI_PACKAGE_NAMES:
            self.assertIn(package_name, result["reason"])
            self.assertIn(package_name, result["minimal_requirement"])

    def test_newer_package_can_pass_discovery_without_changing_version_gate(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            paths = {name: root / name for name in ("workspace", "tmp", "home", "pi")}
            for path in paths.values():
                path.mkdir()
            package_root, _ = package_fixture(
                root, "@earendil-works/pi-coding-agent", version="0.86.2"
            )
            entry = package_root / "dist" / "index.js"
            entry.write_text("// fixture\n", encoding="utf-8")
            installed = paths["home"] / ".agents" / "skills" / validation.SKILL / "SKILL.md"
            installed.parent.mkdir(parents=True)
            installed.write_text("fixture\n", encoding="utf-8")
            loader_result = {
                "status": "passed",
                "returncode": 0,
                "stdout": json.dumps(
                    {
                        "skills": [
                            {"name": validation.SKILL, "filePath": str(installed)}
                        ],
                        "diagnostics": [],
                    }
                ),
                "stderr": "",
            }
            with mock.patch.object(validation.shutil, "which", return_value="/fixture/pi"), \
                    mock.patch.object(
                        validation, "_pi_package_roots", return_value=[package_root]
                    ), mock.patch.object(validation, "_run", return_value=loader_result):
                result = validation._pi_discovery(paths, {"PATH": ""})

        self.assertEqual(result["status"], "unverified")
        self.assertEqual(result["package_version_gate"]["status"], "failed")
        self.assertEqual(result["package_version_gate"]["expected_version"], "0.86.1")
        self.assertEqual(result["package_version_gate"]["observed_version"], "0.86.2")

    def test_validated_current_package_passes_without_a_version_gap(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            paths = {name: root / name for name in ("workspace", "tmp", "home", "pi")}
            for path in paths.values():
                path.mkdir()
            package_root, _ = package_fixture(
                root, "@earendil-works/pi-coding-agent", version="0.86.1"
            )
            package_root.joinpath("dist", "index.js").write_text(
                "// fixture\n", encoding="utf-8"
            )
            installed = paths["home"] / ".agents" / "skills" / validation.SKILL / "SKILL.md"
            installed.parent.mkdir(parents=True)
            installed.write_text("fixture\n", encoding="utf-8")
            loader_result = {
                "returncode": 0,
                "stdout": json.dumps(
                    {
                        "skills": [
                            {"name": validation.SKILL, "filePath": str(installed)}
                        ],
                        "diagnostics": [],
                    }
                ),
                "stderr": "",
            }
            with mock.patch.object(validation, "_run", return_value=loader_result):
                native = validation._pi_candidate_discovery(
                    package_root,
                    paths=paths,
                    env={"PATH": ""},
                    script=paths["tmp"] / "loader.mjs",
                )

        self.assertEqual(native["status"], "passed")
        self.assertEqual(native["package_version_gate"]["status"], "passed")
        versions = {"pi": {"version_gate": "passed"}}
        self.assertEqual(validation._version_gaps(("pi",), versions, {"pi": native}), [])

    def test_absent_diagnostics_passes_validated_current_package(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            paths = {name: root / name for name in ("workspace", "tmp", "home", "pi")}
            for path in paths.values():
                path.mkdir()
            package_root, _ = package_fixture(
                root, "@earendil-works/pi-coding-agent", version="0.86.1"
            )
            package_root.joinpath("dist", "index.js").write_text(
                "// fixture\n", encoding="utf-8"
            )
            installed = paths["home"] / ".agents" / "skills" / validation.SKILL / "SKILL.md"
            installed.parent.mkdir(parents=True)
            installed.write_text("fixture\n", encoding="utf-8")
            loader_result = {
                "returncode": 0,
                "stdout": json.dumps(
                    {
                        "skills": [
                            {"name": validation.SKILL, "filePath": str(installed)}
                        ]
                    }
                ),
                "stderr": "",
            }
            with mock.patch.object(validation, "_run", return_value=loader_result):
                native = validation._pi_candidate_discovery(
                    package_root,
                    paths=paths,
                    env={"PATH": ""},
                    script=paths["tmp"] / "loader.mjs",
                )

        self.assertEqual(native["status"], "passed")
        self.assertTrue(native["checks"]["loader_diagnostics_empty"])
        self.assertIsNone(native["loader_diagnostics"])

    def test_renamed_package_with_old_version_remains_unverified(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            paths = {name: root / name for name in ("workspace", "tmp", "home", "pi")}
            for path in paths.values():
                path.mkdir()
            package_root, _ = package_fixture(
                root, "@earendil-works/pi-coding-agent", version="0.73.1"
            )
            entry = package_root / "dist" / "index.js"
            entry.write_text("// fixture\n", encoding="utf-8")
            installed = paths["home"] / ".agents" / "skills" / validation.SKILL / "SKILL.md"
            installed.parent.mkdir(parents=True)
            installed.write_text("fixture\n", encoding="utf-8")
            loader_result = {
                "returncode": 0,
                "stdout": json.dumps(
                    {
                        "skills": [
                            {"name": validation.SKILL, "filePath": str(installed)}
                        ],
                        "diagnostics": [],
                    }
                ),
                "stderr": "",
            }
            with mock.patch.object(validation.shutil, "which", return_value="/fixture/pi"), \
                    mock.patch.object(
                        validation, "_pi_package_roots", return_value=[package_root]
                    ), mock.patch.object(validation, "_run", return_value=loader_result):
                result = validation._pi_discovery(paths, {"PATH": ""})

        self.assertEqual(result["status"], "unverified")
        self.assertEqual(result["package_version_gate"]["status"], "failed")

    def test_loader_failure_falls_back_to_later_supported_package(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            paths = {name: root / name for name in ("workspace", "tmp", "home", "pi")}
            for path in paths.values():
                path.mkdir()
            first_root, _ = package_fixture(
                root / "first", "@earendil-works/pi-coding-agent"
            )
            second_root, _ = package_fixture(
                root / "second", "@earendil-works/pi-coding-agent"
            )
            for package_root in (first_root, second_root):
                package_root.joinpath("dist", "index.js").write_text(
                    "// fixture\n", encoding="utf-8"
                )
            installed = paths["home"] / ".agents" / "skills" / validation.SKILL / "SKILL.md"
            installed.parent.mkdir(parents=True)
            installed.write_text("fixture\n", encoding="utf-8")
            failed_loader = {
                "returncode": 1,
                "stdout": "null",
                "stderr": "invalid JavaScript",
            }
            passed_loader = {
                "returncode": 0,
                "stdout": json.dumps(
                    {
                        "skills": [
                            {"name": validation.SKILL, "filePath": str(installed)}
                        ],
                        "diagnostics": [],
                    }
                ),
                "stderr": "",
            }
            with mock.patch.object(
                validation.shutil, "which", return_value="/fixture/pi"
            ), mock.patch.object(
                validation,
                "_pi_package_roots",
                return_value=[first_root, second_root],
            ), mock.patch.object(
                validation, "_run", side_effect=[failed_loader, passed_loader]
            ) as run:
                result = validation._pi_discovery(paths, {"PATH": ""})

        self.assertEqual(result["status"], "unverified")
        self.assertEqual(run.call_count, 2)

    def test_invalid_loader_evidence_falls_back_to_later_supported_package(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            paths = {name: root / name for name in ("workspace", "tmp", "home", "pi")}
            for path in paths.values():
                path.mkdir()
            first_root, _ = package_fixture(
                root / "first", "@earendil-works/pi-coding-agent"
            )
            second_root, _ = package_fixture(
                root / "second", "@earendil-works/pi-coding-agent"
            )
            for package_root in (first_root, second_root):
                package_root.joinpath("dist", "index.js").write_text(
                    "// fixture\n", encoding="utf-8"
                )
            installed = paths["home"] / ".agents" / "skills" / validation.SKILL / "SKILL.md"
            installed.parent.mkdir(parents=True)
            installed.write_text("fixture\n", encoding="utf-8")
            passed_loader = {
                "returncode": 0,
                "stdout": json.dumps(
                    {
                        "skills": [
                            {"name": validation.SKILL, "filePath": str(installed)}
                        ],
                        "diagnostics": [],
                    }
                ),
                "stderr": "",
            }
            malformed_payloads = (
                {
                    "skills": [
                        {"name": validation.SKILL, "filePath": str(installed)}
                    ],
                    "diagnostics": [{"message": "fixture loader warning"}],
                },
                {
                    "skills": [{"name": validation.SKILL, "filePath": "\x00"}],
                    "diagnostics": [],
                },
            )
            for payload in malformed_payloads:
                with self.subTest(payload=payload), mock.patch.object(
                    validation.shutil, "which", return_value="/fixture/pi"
                ), mock.patch.object(
                    validation,
                    "_pi_package_roots",
                    return_value=[first_root, second_root],
                ), mock.patch.object(
                    validation,
                    "_run",
                    side_effect=[
                        {
                            "returncode": 0,
                            "stdout": json.dumps(payload),
                            "stderr": "",
                        },
                        passed_loader,
                    ],
                ) as run:
                    result = validation._pi_discovery(paths, {"PATH": ""})
                self.assertEqual(result["status"], "unverified")
                self.assertEqual(run.call_count, 2)

    def test_live_pi_certification_keeps_package_identity_exact(self):
        for package_name, version, status in (
                ("@earendil-works/pi-coding-agent", "0.86.1", "passed"),
                ("@earendil-works/pi-coding-agent", "0.86.2", "unverified"),
                ("@mariozechner/pi-coding-agent", "0.86.1", "unverified"),
                ("@mariozechner/pi-coding-agent", "0.73.1", "unverified")):
            with self.subTest(package=package_name, version=version), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                paths = {name: root / name for name in ("workspace", "tmp", "home", "pi")}
                for path in paths.values():
                    path.mkdir()
                package_root, _ = package_fixture(root, package_name, version=version)
                (package_root / "dist" / "index.js").write_text("// fixture\n")
                installed = paths["home"] / ".agents" / "skills" / validation.SKILL / "SKILL.md"
                installed.parent.mkdir(parents=True)
                installed.write_text("fixture\n")
                response = {"returncode": 0, "stderr": "", "stdout": json.dumps({
                    "skills": [{"name": validation.SKILL, "filePath": str(installed)}],
                    "diagnostics": []})}
                with mock.patch.object(validation, "_run", return_value=response):
                    native = validation._pi_candidate_discovery(
                        package_root, paths=paths, env={"PATH": ""},
                        script=paths["tmp"] / "loader.mjs")
                self.assertEqual(native["status"], status)
                self.assertTrue(all(native["checks"].values()))
                self.assertEqual(native["package_version_gate"]["status"],
                                 "passed" if status == "passed" else "failed")

    def test_unverified_sdk_is_a_version_gap_even_when_cli_pin_matches(self):
        versions = {"pi": {"version_gate": "passed"}}
        native = {"pi": {"status": "unverified"}}
        self.assertEqual(validation._version_gaps(("pi",), versions, native), ["pi"])


class TestCurrentNativeVersions(unittest.TestCase):
    def test_expected_versions_match_the_live_run_and_reject_newer_patches(self):
        expected = {"claude": "2.1.282", "codex": "0.154.0",
                    "opencode": "1.18.29", "pi": "0.86.1"}
        self.assertEqual(validation.EXPECTED_VERSIONS, expected)
        for agent, version in expected.items():
            major, minor, patch = version.split(".")
            newer = ".".join((major, minor, str(int(patch) + 1)))
            for observed, gate in ((version, "passed"), (newer, "failed")):
                with self.subTest(agent=agent, version=observed), mock.patch.object(
                    validation.shutil, "which", return_value="/fixture/" + agent
                ), mock.patch.object(validation, "_run", return_value={
                    "status": "passed", "stdout": observed, "stderr": "",
                    "elapsed_seconds": 0.01
                }):
                    result = validation._version(agent, cwd=ROOT, env={"PATH": ""})
                    self.assertEqual(result["version_gate"], gate)
                    self.assertEqual(result["expected_version"], version)


if __name__ == "__main__":
    unittest.main()
