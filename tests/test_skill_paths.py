"""Pin bundled resolver bytes to the canonical, independently shipped source."""
import unittest
from pathlib import Path


SKILLS = Path(__file__).resolve().parents[1] / "skills"
CANONICAL = SKILLS / "hanig-orchestrate" / "scripts" / "skill_paths.py"


class BundledSkillPaths(unittest.TestCase):
    def test_bundled_resolvers_match_canonical_bytes(self):
        copies = sorted(SKILLS.glob("hanig-*/scripts/skill_paths.py"))
        # Discovery includes future consumers; these current consumers must
        # not disappear and turn the byte comparison into a vacuous pass.
        required = {CANONICAL, SKILLS / "hanig-project/scripts/skill_paths.py"}
        self.assertTrue(required.issubset(copies), "missing bundled resolver")
        canonical = CANONICAL.read_bytes()
        for copy in copies:
            with self.subTest(copy=str(copy.relative_to(SKILLS))):
                self.assertEqual(copy.read_bytes(), canonical,
                                 "copy the canonical resolver byte-for-byte")


if __name__ == "__main__":
    unittest.main()
