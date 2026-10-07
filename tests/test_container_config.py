"""Container deployment guards: what the runner's relaxed seccomp profile may and may not contain, amd64 by default,
and no model credential anywhere in files that get committed."""
import importlib.util
import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('make_runner_seccomp', ROOT / 'scripts' / 'make_runner_seccomp.py')
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)
PROFILE = json.loads((ROOT / 'deploy' / 'seccomp' / 'runner.json').read_text())
BASE = yaml.safe_load((ROOT / 'compose.yaml').read_text())
ARM64 = yaml.safe_load((ROOT / 'compose.runner-arm64.yaml').read_text())

# Calls that give real power over the host or other processes; the relaxed profile must never grant them without a capability.
NEVER_UNCONDITIONAL = {'ptrace', 'bpf', 'perf_event_open', 'kexec_load', 'kexec_file_load', 'reboot', 'init_module', 'finit_module',
                       'delete_module', 'keyctl', 'add_key', 'request_key', 'swapon', 'swapoff', 'open_by_handle_at',
                       'process_vm_readv', 'process_vm_writev', 'userfaultfd', 'chroot', 'acct', 'settimeofday', 'clock_settime'}


def unconditional_allows(profile):
    return [rule for rule in profile['syscalls'] if rule['action'] == 'SCMP_ACT_ALLOW'
            and not rule.get('includes') and not rule.get('excludes') and not rule.get('args')]


def test_the_profile_keeps_the_deny_by_default_stance_of_dockers_profile():
    assert PROFILE['defaultAction'] == 'SCMP_ACT_ERRNO'
    assert PROFILE['archMap'] and PROFILE['syscalls']


def test_the_profile_is_the_official_one_plus_exactly_the_documented_rule():
    assert PROFILE['syscalls'][-1] == generator.EXTRA_RULE
    assert len(PROFILE['syscalls']) == 61  # 60 upstream rules + 1


def test_the_added_rule_is_limited_to_what_the_sandbox_needs():
    extra = generator.EXTRA_RULE
    assert extra['action'] == 'SCMP_ACT_ALLOW' and not extra.get('args') and not extra.get('includes') and not extra.get('excludes')
    assert sorted(extra['names']) == ['clone', 'mount', 'pivot_root', 'sethostname', 'setns', 'umount', 'umount2', 'unshare']
    assert not NEVER_UNCONDITIONAL & set(extra['names'])


def test_nothing_dangerous_is_allowed_unconditionally_anywhere_in_the_profile():
    granted = {name for rule in unconditional_allows(PROFILE) for name in rule.get('names', [])}
    assert not NEVER_UNCONDITIONAL & granted, sorted(NEVER_UNCONDITIONAL & granted)


def test_only_the_new_rule_adds_unconditional_allows_compared_with_upstream_shape():
    # Upstream's unconditional allow-list is rule 0; the only other unconditional ALLOW is ours, the last one.
    allows = unconditional_allows(PROFILE)
    assert allows[0] is PROFILE['syscalls'][0] or allows[0] == PROFILE['syscalls'][0]
    assert allows[-1] == generator.EXTRA_RULE
    assert len(allows) == 2


def test_the_generator_refuses_an_upstream_that_was_not_reviewed():
    with pytest.raises(SystemExit, match='审核'):
        generator.build('{"defaultAction": "SCMP_ACT_ALLOW", "syscalls": []}')
    with pytest.raises(SystemExit):
        generator.build('{}')


def test_the_pinned_upstream_hash_is_a_full_sha256():
    assert re.fullmatch(r'[0-9a-f]{64}', generator.UPSTREAM_SHA256)


def test_every_service_is_amd64_by_default_and_none_use_a_custom_seccomp_profile():
    assert BASE['name'] == 'codex-hub-v1'
    for name, service in BASE['services'].items():
        assert service['platform'] == 'linux/amd64', name
        assert not any(str(option).startswith(('seccomp', 'apparmor')) for option in service.get('security_opt', [])), name
        assert service.get('privileged') is not True and 'cap_add' not in service and 'pid' not in service and 'network_mode' not in service, name


def test_the_default_runner_stays_fully_hardened():
    runner = BASE['services']['runner']
    assert runner['cap_drop'] == ['ALL'] and runner['read_only'] is True and 'no-new-privileges:true' in runner['security_opt']
    assert 'hub_data' not in runner['networks']  # No route to the database.
    assert runner['environment']['CODEX_BASE_URL'].startswith('${CODEX_BASE_URL')


def test_the_arm64_override_changes_only_the_runner_and_only_what_it_must():
    assert set(ARM64['services']) == {'runner'}
    runner = ARM64['services']['runner']
    assert runner['platform'] == 'linux/arm64' and runner['build']['args'] == {'RUNNER_PLATFORM': 'linux/arm64'}
    assert runner['security_opt'] == ['seccomp=./deploy/seccomp/runner.json']
    assert (ROOT / 'deploy' / 'seccomp' / 'runner.json').is_file()
    assert not {'privileged', 'cap_add', 'networks', 'volumes', 'ports', 'environment', 'read_only', 'cap_drop'} & set(runner)
    assert runner['image'] != BASE['services']['runner']['image']  # Never overwrites the amd64 image tag.


def test_the_runner_image_is_amd64_unless_told_otherwise():
    dockerfile = (ROOT / 'runner' / 'Dockerfile').read_text()
    assert re.search(r'^ARG RUNNER_PLATFORM=linux/amd64$', dockerfile, re.M)
    assert dockerfile.count('--platform=${RUNNER_PLATFORM}') == 2 and 'COPY main.py sandbox_probe.py' in dockerfile


COMMITTED = [path for path in [*ROOT.glob('*.md'), *ROOT.glob('*.yaml'), *ROOT.glob('*.yml'), ROOT / '.env.example', ROOT / 'Dockerfile',
                               *ROOT.glob('docs/*'), *ROOT.glob('scripts/*'), *ROOT.glob('deploy/**/*'), *ROOT.glob('runner/*'),
                               *ROOT.glob('backend/app/*.py'), *ROOT.glob('tests/*.py'), *ROOT.glob('frontend/src/*')] if path.is_file()]


def test_the_committed_files_contain_no_model_api_key():
    pattern = re.compile(r'sk-[A-Za-z0-9_-]{20,}')
    assert COMMITTED, 'no files scanned'
    offenders = [str(path.relative_to(ROOT)) for path in COMMITTED if pattern.search(path.read_text(errors='ignore'))]
    assert offenders == []


def test_local_env_files_are_excluded_from_git_and_from_images():
    ignored = (ROOT / '.gitignore').read_text().splitlines()
    assert '.env' in ignored and '.env.*' in ignored and '!.env.example' in ignored
    docker_ignored = (ROOT / '.dockerignore').read_text().splitlines()
    assert '.env' in docker_ignored and '.env.*' in docker_ignored
