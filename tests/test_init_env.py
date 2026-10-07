"""The deployment configuration generator: random secrets, port substitution, no overwrite, no path tricks."""
import importlib.util
import stat
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'init_env.py'
spec = importlib.util.spec_from_file_location('init_env', SCRIPT)
init_env = importlib.util.module_from_spec(spec)
spec.loader.exec_module(init_env)
EXAMPLE = (SCRIPT.parents[1] / '.env.example').read_text()


@pytest.fixture
def root(tmp_path):
    (tmp_path / '.env.example').write_text(EXAMPLE)
    return tmp_path


def values(path):
    return dict(line.split('=', 1) for line in path.read_text().splitlines() if '=' in line and not line.startswith('#'))


def test_default_target_gets_independent_random_secrets_and_private_permissions(root):
    init_env.main([], root)
    data = values(root / '.env')
    secrets_ = [data[key] for key in init_env.RANDOM_KEYS]
    assert all(len(value) == 48 and value.isalnum() for value in secrets_) and len(set(secrets_)) == len(secrets_)
    assert stat.S_IMODE((root / '.env').stat().st_mode) == 0o600
    assert data['HUB_PORT'] == '18200' and data['APP_ORIGIN'] == 'http://127.0.0.1:18200'
    assert not any('replace_with' in value for value in data.values())


def test_two_generated_files_never_share_a_secret(root):
    init_env.main(['.env'], root)
    init_env.main(['.env.docker'], root)
    first, second = values(root / '.env'), values(root / '.env.docker')
    assert all(first[key] != second[key] for key in init_env.RANDOM_KEYS)


def test_a_port_moves_every_address_that_has_to_agree_with_it(root):
    init_env.main(['.env.docker', '18210'], root)
    data = values(root / '.env.docker')
    assert data['HUB_PORT'] == '18210'
    assert data['APP_ORIGIN'] == 'http://127.0.0.1:18210'
    assert data['PLATFORM_BRIDGE_URL'] == 'http://127.0.0.1:18210/internal/platform-mcp'
    assert data['ATTACHMENT_BRIDGE_URL'] == 'http://127.0.0.1:18210/internal/attachment-mcp'
    assert data['HUB_BIND'] == '127.0.0.1'


def test_an_existing_file_is_never_overwritten(root):
    (root / '.env.docker').write_text('KEEP=1\n')
    with pytest.raises(SystemExit, match='已存在'):
        init_env.main(['.env.docker', '18210'], root)
    assert (root / '.env.docker').read_text() == 'KEEP=1\n'


@pytest.mark.parametrize('args', [['../.env'], ['/tmp/.env.x'], ['.env.example'], ['.envrc'], ['.env.'], ['env'], ['.env.a/b'],
                                  ['.env.x', '18210', 'extra'], ['.env.x', 'abc'], ['.env.x', '80'], ['.env.x', '70000'], ['.env.x', '-1']])
def test_unsafe_names_and_ports_are_refused(root, args):
    with pytest.raises(SystemExit):
        init_env.main(args, root)
    assert sorted(p.name for p in root.iterdir()) == ['.env.example']
