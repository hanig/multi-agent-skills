"""Execute documented review invocations offline with real Git identities."""

import ast
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'skills/hanig-review-gate/scripts/review.py'
SPEC = importlib.util.spec_from_file_location('documented_review', SCRIPT)
review = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(review)


def documented_commands(text):
    """Read shell fences and inline invocations, including the documented R alias.

    Parse, never execute, shell text. Every matching invocation is returned;
    unknown flags stay intact for the production parser to reject.
    """
    text = text.replace('\\\n', ' ')
    fenced = False
    aliases = set()
    for line in text.splitlines():
        if line.lstrip().startswith('```'):
            fenced = not fenced
            continue
        snippets = [line] if fenced else re.findall(r'`([^`\n]+)`', line)
        for snippet in snippets:
            words = shlex.split(snippet, comments=True)
            if len(words) == 1 and '=' in words[0]:
                name, value = words[0].split('=', 1)
                if value.endswith('/review.py'):
                    aliases.add('$' + name)
            for index, word in enumerate(words):
                if (word == 'review.py' or word.endswith('/review.py') or word in aliases):
                    if index + 1 < len(words) and words[index + 1].startswith('--'):
                        yield words[index + 1:]
                    break


class TestDocumentedReviewCommands(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        previous = Path.cwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, previous)
        self.git('init', '-q')
        self.git('config', 'user.name', 'Documentation Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('config', 'commit.gpgsign', 'false')
        self.git('remote', 'add', 'origin', 'https://example.invalid/project.git')
        Path('design.md').write_text('Declare acceptance criteria before implementation.\n')
        Path('code.py').write_text('value = 1\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'base')
        self.base = self.git('rev-parse', 'HEAD')
        self.git('update-ref', 'refs/remotes/origin/main', self.base)
        self.git('checkout', '-qb', 'change')
        Path('code.py').write_text('value = 2\n')
        self.git('commit', '-qam', 'implementation')
        Path('code.py').write_text('value = 3\n')
        self.git('add', 'code.py')
        self.sequence = 0

    def git(self, *args):
        return subprocess.check_output(['git', *args], stderr=subprocess.STDOUT).decode().strip()

    def invoke(self, argv):
        self.sequence += 1
        state = self.root / ('state-' + str(self.sequence))
        calls = []
        roster = [dict(name=name, model=name, provider=provider, enabled=True,
                       profiles=['plan', 'fast', 'standard', 'deep'])
                  for name, provider in [('a', 'openai'), ('b', 'openrouter')]]

        def answer(reviewer, *args):
            calls.append(reviewer['name'])
            return dict(ok=True, name=reviewer['name'], verdict='upheld',
                        findings=[], claims=[], elapsed_s=0)

        out, err = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {'XDG_STATE_HOME': str(state)}))
            stack.enter_context(patch.object(sys, 'argv', [str(SCRIPT)] + argv))
            for name, replacement in [('load_effective_reviewers', lambda: roster),
                                      ('availability', lambda _r: None),
                                      ('run_one', answer),
                                      ('arm_watchdog', lambda *_a: None),
                                      ('disarm_watchdog', lambda: None),
                                      ('cmd_list', lambda *_a, **_k: 0),
                                      ('cmd_ledger', lambda _a: 0),
                                      ('_run_journal_append', lambda *_a: dict(
                                          written=True, path='offline sink', error=None))]:
                stack.enter_context(patch.object(review, name, replacement))
            # Any overlooked transport must fail rather than reach the network.
            stack.enter_context(patch.object(review.urllib.request, 'urlopen',
                                              side_effect=AssertionError('network in offline test')))
            stack.enter_context(redirect_stdout(out))
            stack.enter_context(redirect_stderr(err))
            with self.assertRaises(SystemExit) as stopped:
                review.main()
        markers = [json.loads(p.read_text()) for p in state.glob(
            'hanig-review-gate/rounds/*/*.json')]
        return stopped.exception.code, err.getvalue(), calls, markers

    def test_every_documented_command_parses_and_derives_identity(self):
        documents = [ROOT / 'README.md', ROOT / 'CLAUDE.md'] + sorted(
            (ROOT / 'skills').glob('*/SKILL.md'))
        covered = set()
        sources = set()
        for doc in documents:
            for argv in documented_commands(doc.read_text()):
                covered.add(doc.relative_to(ROOT).as_posix())
                # Substitute only named value placeholders, never missing flags.
                argv = [self.base + '..HEAD' if v == 'BASE..HEAD' else
                        self.base if v == 'SHA' else 'a' * 64 if v == 'FINDING_DIGEST'
                        else v for v in argv]
                with self.subTest(document=str(doc.relative_to(ROOT)), argv=argv):
                    args = review.build_parser().parse_args(argv)
                    identity = review.change_identity(args.target or 'origin/main',
                                                       args.plan_name, args.unit)
                    self.assertRegex(identity, r'^[0-9a-f]{64}$')
                    code, err, calls, markers = self.invoke(argv)
                    self.assertEqual(code, 0, err)
                    if args.kind == 'implementation' and not args.size:
                        self.assertEqual(len(markers), 1)
                        self.assertEqual(markers[0]['identity'], identity)
                        self.assertEqual(markers[0]['round'], args.round)
                        self.assertEqual(len(calls), 2)
                        sources.add('staged' if args.staged else 'range' if args.range else 'other')
                    else:
                        self.assertEqual(markers, [])
        self.assertTrue({'README.md', 'CLAUDE.md', 'skills/hanig-review-gate/SKILL.md',
                         'skills/hanig-project/SKILL.md'} <= covered, covered)
        self.assertTrue({'staged', 'range'} <= sources, sources)

    def test_command_extraction_keeps_inline_alias_and_invalid_flags(self):
        text = ('```sh\nR="$DIR/scripts/review.py"\n'
                'python3 "$R" --kind implementation \\\n --broken\n```\n'
                '`review.py --size --range BASE..HEAD`\n')
        self.assertEqual(list(documented_commands(text)), [
            ['--kind', 'implementation', '--broken'], ['--size', '--range', 'BASE..HEAD']])

    def implementation(self, *flags):
        return ['--kind', 'implementation', '--round', '1', '--staged',
                '--claim', review.HONEST_RUN_CLAIM] + list(flags)

    def test_identity_and_round_refusals_name_repairs_before_dispatch(self):
        argv = self.implementation()
        del argv[2:4]
        code, err, calls, markers = self.invoke(argv)
        self.assertEqual(code, 4)
        self.assertIn('--round', err)
        self.assertEqual((calls, markers), ([], []))
        for flags in [('--target', ''), ('--target', 'missing'), ('--plan-name', '')]:
            code, err, calls, markers = self.invoke(self.implementation(*flags))
            self.assertEqual(code, 4, err)
            self.assertIn(flags[0], err)
            self.assertEqual((calls, markers), ([], []))
        self.git('checkout', '--detach', '-q')
        code, err, calls, markers = self.invoke(self.implementation())
        self.assertEqual(code, 4)
        self.assertIn('--plan-name', err)
        self.assertEqual((calls, markers), ([], []))
        for flags in [('--plan-name', 'plan'), ('--unit', 'unit'),
                      ('--plan-name', 'plan', '--unit', 'unit')]:
            code, err, calls, markers = self.invoke(self.implementation(*flags))
            self.assertEqual(code, 0, err)
            self.assertEqual((len(calls), len(markers)), (2, 1))
        self.git('remote', 'remove', 'origin')
        code, err, calls, markers = self.invoke(self.implementation('--unit', 'unit'))
        self.assertEqual(code, 4)
        self.assertIn('configure its origin', err)
        self.assertEqual((calls, markers), ([], []))

    def test_plan_profile_alias_cannot_escalate(self):
        code, err, calls, markers = self.invoke(
            ['--profile', 'plan', '--escalate', '--file', 'design.md'])
        self.assertEqual(code, 4)
        self.assertIn('Drop --escalate', err)
        self.assertEqual((calls, markers), ([], []))


class TestAuditCitations(unittest.TestCase):
    def test_closed_findings_name_existing_tests(self):
        text = (ROOT / 'docs/audit-protocol-enforcement.md').read_text()
        rows = [line for line in text.splitlines() if line.startswith('|')
                and 'closed' in line.lower()]
        self.assertGreaterEqual(len(rows), 7)
        for row in rows:
            citations = re.findall(r'`(tests/[^`]+)`', row)
            self.assertTrue(citations, row)
            for citation in citations:
                path, _, selector = citation.partition('::')
                tree = ast.parse((ROOT / path).read_text())
                if selector:
                    for name in selector.split('.'):
                        tree = next((node for node in tree.body
                                     if isinstance(node, (ast.ClassDef, ast.FunctionDef))
                                     and node.name == name), None)
                        self.assertIsNotNone(tree, citation)
                else:
                    self.assertTrue(any(isinstance(node, ast.FunctionDef)
                                        and node.name.startswith('test_')
                                        for node in ast.walk(tree)), citation)


if __name__ == '__main__':
    unittest.main()
