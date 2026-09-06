"""Sync public schema/entityAPI references from the sibling shared engine.

Run with --check in development to detect reference drift. Runtime serving uses
the checked-in copies and does not require an engine source checkout.
"""
import argparse
from pathlib import Path

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--check', action='store_true')
parser.add_argument('--engine', type=Path, default=root.parent / 'entropydrop_space_engine')
args = parser.parse_args()
references = {
    'proto/inventory.proto': 'references/inventory.proto',
    'docs/generated/api-v2.md': 'entityAPI.md',
}
for source, filename in references.items():
    original = args.engine / source
    destination = root / 'space/agent' / filename
    expected = original.read_bytes()
    if filename == 'entityAPI.md':
        # The API content remains generated from the shared contract. Only the
        # navigation changes from sibling-repository paths to served siblings.
        navigation = b'[spaceAPI](<../spaceAPI.md>) \xc2\xb7 [entityAPI](<api-v2.md>)'
        assert expected.count(navigation) == 1, 'Generated entityAPI navigation changed'
        expected = expected.replace(navigation, b'[spaceAPI](spaceAPI.md) \xc2\xb7 [entityAPI](entityAPI.md)')
    if args.check:
        if not destination.exists() or destination.read_bytes() != expected:
            raise SystemExit(f'Outdated public reference: {destination}; run space/sync_agent_docs.py')
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(expected)
print('Public Space Agent references are current.')
