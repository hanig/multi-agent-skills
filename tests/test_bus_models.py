#!/usr/bin/env python3
"""Regression coverage for ARC-270's model-registry failure modes."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
BUS = ROOT / "bin" / "bus"


def run_models(home: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, AGENT_BUS_HOME=str(home))
    return subprocess.run(
        [str(BUS), "models", "--no-live", "--json"],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )


class TestBusModelsRegistry(unittest.TestCase):
    def test_missing_registry_is_absent_and_does_not_create_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "bus-state"
            result = run_models(home)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn(f"models registry is absent at {home / 'models.json'}",
                          result.stderr)
            self.assertFalse((home / "models.json").exists())
            for dirname in ("sessions", "inbox", "cursors", "cache"):
                self.assertTrue((home / dirname).is_dir(), dirname)

    def test_unreadable_registry_is_not_reported_as_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "bus-state"
            (home / "models.json").mkdir(parents=True)
            result = run_models(home)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("cannot read models registry", result.stderr)
            self.assertNotIn("is absent", result.stderr)

    def test_malformed_registry_is_reported_as_malformed(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "bus-state"
            home.mkdir()
            (home / "models.json").write_text("{oops")
            result = run_models(home)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("malformed models registry", result.stderr)
            self.assertIn("line 1 column 2", result.stderr)
            self.assertNotIn("is absent", result.stderr)

    def test_invalid_registry_shape_is_reported_as_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "bus-state"
            home.mkdir()
            (home / "models.json").write_text(json.dumps({"models": {}}))
            result = run_models(home)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("invalid models registry", result.stderr)
            self.assertIn("'models' list", result.stderr)

    def test_invalid_model_entry_is_reported_before_routing(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "bus-state"
            home.mkdir()
            document = {"models": [{"id": "test/model", "modalities": "text"}]}
            (home / "models.json").write_text(json.dumps(document))
            result = run_models(home)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("invalid models registry", result.stderr)
            self.assertIn("models[0].modalities must be a list of strings",
                          result.stderr)
            self.assertNotIn("Traceback", result.stderr)

    def test_empty_registry_is_distinct_from_a_missing_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "bus-state"
            home.mkdir()
            (home / "models.json").write_text(json.dumps({"models": []}))
            result = run_models(home)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("contains no models", result.stderr)
            self.assertNotIn("is absent", result.stderr)

    def test_valid_registry_still_prints_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "bus-state"
            home.mkdir()
            model = {"id": "test/model", "modalities": ["text"]}
            (home / "models.json").write_text(json.dumps({"models": [model]}))
            result = run_models(home)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)[0]["id"], "test/model")
            self.assertTrue((home / "cache").is_dir())


if __name__ == "__main__":
    unittest.main(verbosity=2)
