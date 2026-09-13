"""Sync public schema/entityAPI references from the sibling shared engine.

Run with --check in development to detect reference drift, or --protobuf to also
verify that the checked-in Python bindings match the shared engine schemas byte
for byte. Runtime serving uses the checked-in copies and does not require an
engine source checkout.
"""
import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--check', action='store_true')
parser.add_argument(
    '--protobuf',
    action='store_true',
    help='Require protoc and verify the checked-in Python bindings are current.',
)
parser.add_argument('--engine', type=Path, default=root.parent / 'entropydrop_space_engine')
args = parser.parse_args()
references = {
    'proto/inventory.proto': 'references/inventory.proto',
    'proto/space_api.proto': 'references/space_api.proto',
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


def check_python_bindings(required: bool) -> None:
    """Compare checked-in Python bindings with protoc output from the engine schemas."""
    protoc = shutil.which('protoc')
    if protoc is None:
        if required:
            raise SystemExit('protoc is required for --protobuf but was not found on PATH')
        print('protoc not found; skipped Python binding freshness check.')
        return
    with tempfile.TemporaryDirectory() as temporary:
        for name in ('inventory', 'space_api'):
            subprocess.run(
                [
                    protoc,
                    '--proto_path=space/contracts=../entropydrop_space_engine/proto',
                    f'--python_out={temporary}',
                    f'space/contracts/{name}.proto',
                ],
                cwd=root,
                check=True,
            )
            generated = Path(temporary) / 'space/contracts' / f'{name}_pb2.py'
            checked_in = root / 'space/contracts' / f'{name}_pb2.py'
            if not checked_in.exists() or generated.read_bytes() != checked_in.read_bytes():
                raise SystemExit(
                    f'Stale Python binding: {checked_in}; regenerate it from the engine proto'
                )
    print('Python Protobuf bindings are current.')


if args.check or args.protobuf:
    check_python_bindings(required=args.protobuf)
