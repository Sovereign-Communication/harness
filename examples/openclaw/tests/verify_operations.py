"""Acceptance gate for the first useful governed documentation update."""
from pathlib import Path
base=Path(__file__).resolve().parents[1]
text=base.joinpath('OPERATIONS.md').read_text(encoding='utf-8') + base.joinpath('EXPERIMENTAL.md').read_text(encoding='utf-8')
required='Native Jev completion requirement: pending live verification.'
assert required in text, 'Native completion contract is missing'
assert 'not been live-tested' in text, 'Experimental status is missing'
assert 'Interrupted edits and ambiguous message sends require review rather than automatic replay.' in text
assert 'The separate SCM cloud node must remain untouched' in text
print('Operations completion contract verified')


