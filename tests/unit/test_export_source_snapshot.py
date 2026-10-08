import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('snapshot_tested', ROOT / 'scripts/export_source_snapshot.py')
snapshot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(snapshot)


def source(tmp_path):
    root = tmp_path / 'source'
    for name in ('src/app.py', 'MANIFEST.in', 'src/fmd/fixtures/public.bin', 'pyproject.toml'):
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('public source\n')
    return root


@pytest.mark.parametrize('name', ['generation/private/recipe.json', 'generation/private-recipe/input.json',
    'generation/private-generation.json', 'scripts/.env.local', 'generation/control_receipt.json',
    'generation/ground_truth.json', 'tests/.fmd/cache.json'])
def test_private_names_rejected_before_read(tmp_path, name):
    files, skipped = snapshot.select_files(source(tmp_path), [name])
    assert files == [] and skipped
    with pytest.raises(snapshot.SnapshotError):
        snapshot.safe_member_path(name)


def test_determinism_closure_and_extract(tmp_path):
    root = source(tmp_path)
    first = snapshot.create_snapshot(root, tmp_path / 'one', use_git=False)
    second = snapshot.create_snapshot(root, tmp_path / 'two', use_git=False)
    assert first['tarball_sha256'] == second['tarball_sha256']
    assert {'MANIFEST.in', 'src/fmd/fixtures/public.bin'} <= set(first['files'])
    output = tmp_path / 'extracted'
    assert snapshot.main(['--verify', first['tarball'], '--extract', str(output)]) == 0
    assert (output / 'src/app.py').read_text() == 'public source\n'


def test_failed_seals_prevent_extraction(tmp_path):
    item = snapshot.create_snapshot(source(tmp_path), tmp_path / 'one', use_git=False)
    declaration = tmp_path / 'declaration.json'
    declaration.write_text(json.dumps({'source_sha256': {'src/app.py': '0' * 64}}))
    output = tmp_path / 'extracted'
    assert snapshot.main(['--verify', item['tarball'], '--verify-declaration', str(declaration), '--extract', str(output)]) != 0
    assert not output.exists()
    Path(item['tarball']).write_bytes(b'changed')
    assert snapshot.main(['--verify', item['tarball'], '--extract', str(output)]) != 0
    assert not output.exists()


def test_parent_symlink_and_traversal_refused(tmp_path):
    root = source(tmp_path)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'data.py').write_text('outside')
    (root / 'src/link').symlink_to(outside, target_is_directory=True)
    assert snapshot.select_files(root, ['src/link/data.py'])[0] == []
    with pytest.raises(snapshot.SnapshotError):
        snapshot.select_files(root, ['src/../../outside/data.py'])
    with pytest.raises(snapshot.SnapshotError):
        snapshot.create_snapshot(root, tmp_path / 'output', name='../escape.tar.gz', use_git=False)


def test_duplicate_or_noncanonical_manifest_refused():
    with pytest.raises(snapshot.SnapshotError):
        snapshot.manifest_files({'files': [{'path': 'src/x.py', 'sha256': 'a' * 64}] * 2})
    for name in ('src//x.py', 'src/./x.py', 'src/../x.py'):
        with pytest.raises(snapshot.SnapshotError):
            snapshot.safe_member_path(name)
