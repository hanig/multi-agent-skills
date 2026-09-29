#!/usr/bin/env bash
set -euo pipefail

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
cd "$root"
[[ "$(git rev-parse --show-toplevel)" == "$root" ]]
[[ "$(git branch --show-current)" == arc1163-claim-scope ]]
base=a0ae042b874a4822dce26a28c1c56600675b311e
git merge-base --is-ancestor "$base" HEAD
export HANIG_REVIEW_GATE_DIR="$root/skills/hanig-review-gate"

paths=(skills/hanig-review-gate/SKILL.md skills/hanig-review-gate/PROTOCOL.md
  tests/test_arc1163_claim_scope.py tests/mutate_arc1163.py
  docs/research/arc1163-review-implementation.sh)
for path in "${paths[@]}"; do
  git ls-files --error-unmatch -- "$path" > /dev/null
done
git diff --exit-code "$base" -- skills/hanig-review-gate/scripts/review.py
mkdir -p "$root/.swarm/arc1163/implementation"
output="$(mktemp -d "$root/.swarm/arc1163/implementation/gate-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")"
git diff --no-ext-diff --no-textconv --binary "$base" -- "${paths[@]}" > "$output/change.diff"
[[ -s "$output/change.diff" ]]
python3 - "$output" <<'PY'
import hashlib
import json
import subprocess
import sys
from pathlib import Path

output = Path(sys.argv[1])
mandate = Path('docs/orchestrator-mandate.md').read_text()
start = mandate.index('## Adjudication policy\n')
end = mandate.index('## How the orchestrator runs\n', start)
(output / 'authority-context.txt').write_text(
    'SOURCE AUTHORITY CONTEXT: docs/orchestrator-mandate.md\n' + mandate[start:end])
paths = [output / 'change.diff', Path('skills/hanig-review-gate/scripts/review.py'),
         output / 'authority-context.txt']
assert sum(len(path.read_text()) for path in paths) + 2000 < 180000
(output / 'inputs.json').write_text(json.dumps({
    'head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
    'inputs': [{'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
               for path in paths],
}, indent=2))
PY
printf 'Implementation review output: %s\n' "$output"
status=0
python3 "$HANIG_REVIEW_GATE_DIR/scripts/review.py" \
  --kind implementation --profile fast --escalate --round 1 \
  --quorum 2 --author codex/gpt-6-astra \
  --file "$output/change.diff" \
  --file "$HANIG_REVIEW_GATE_DIR/scripts/review.py" \
  --context "$(cat "$output/authority-context.txt")" \
  --threat-model 'The operator does not edit or replace the gate implementation or its routing while using it; such operator modification is outside the defended boundary. Reviewer replies and caller scope prose may be mistaken or misleading. The deliberate in-memory mutation is a test of the regression, not an authorized operational override. Local historical evidence paths are not installed dependencies. No new adjudication authority or training settings are granted.' \
  --claim 'The delivered change documents the required-refusal limit as an orchestrator decision reversible by the owner, not an owner decision or a fix for false rejections; it routes disputes to the existing outside-gate adjudication matrix without relabeling non-passing verdicts.' \
  --claim 'The portable regression first executes a wrongful UTC refusal, then exercises real gate parsing and adjudication with one false-scoped explicit counter-claim refutation, a misleading caller boundary, and one supportive reply; it requires exit 7 and retains the refutation in output and serialized audit evidence.' \
  --claim 'The mutation runner adds only a claim-scope filter in memory and requires the positive control to fail specifically at the exit-code assertion with 0 instead of 7; baseline failures, unrelated assertions, errors, or changed production bytes do not count as a mutation kill.' \
  --claim 'No gate decision logic or mandatory-claim validation is changed, and no deployed skill installation is modified by this change.' \
  --claim 'This change cannot make an honest run fail.' \
  --json > "$output/gate.json" 2> "$output/gate.stderr" || status=$?
printf '%s\n' "$status" > "$output/exit-status.txt"
cat "$output/gate.json"
cat "$output/gate.stderr" >&2
exit "$status"
