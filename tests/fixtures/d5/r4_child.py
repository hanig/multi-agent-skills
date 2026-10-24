"""Synthetic newly admitted child; receipt transported through stdin, never argv."""
import json
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'skills/hanig-orchestrate/scripts'))
import d5_completion as completion
import d5_effect_log as ledger
import d5_journal_completion as journal
from d5_submission import Operation

m = json.load(sys.stdin)
r, op = m['receipt'], m['operation']
receipt = ledger.Receipt(r['effect_id'], r['directory'], bytes.fromhex(r['binding']), bytes.fromhex(r['secret']))
operation = Operation(op['request_id'], op['kind'], bytes.fromhex(op['scope']), bytes.fromhex(op['intent']))
with patch.object(ledger.registry, 'd5_active', return_value=True):
    spec = journal.prepare(m['args'][2], m['record'])
    bound = journal.bind(spec, operation, tuple(m['args']), r['directory'])
    print(json.dumps(completion.bind(receipt, journal=bound).complete(m['outcome'])))
