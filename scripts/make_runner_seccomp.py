"""Builds the runner container's seccomp profile: Docker's default profile plus one rule.

The Codex sandbox (bubblewrap) must create user and mount namespaces. Docker's default profile only allows that with
CAP_SYS_ADMIN, which the runner deliberately does not have (cap_drop: ALL), and it does not allow pivot_root at all.
Instead of removing the filter (seccomp=unconfined), this adds the few calls bubblewrap needs and keeps every other
restriction of the default profile.

    gh api repos/moby/profiles/contents/seccomp/default.json --jq .content | base64 -d > /tmp/docker-default.json
    python scripts/make_runner_seccomp.py /tmp/docker-default.json deploy/seccomp/runner.json

The upstream file is pinned by SHA-256: a different upstream version has to be reviewed before it is used.
"""
import hashlib
import json
import sys
from pathlib import Path

# https://github.com/moby/profiles/blob/main/seccomp/default.json as reviewed on 2026-10-05.
UPSTREAM_SHA256 = '6416b47770785a41ac59073cdc77d9fe98517df2799dc83ef207e622de3053f6'

# Needed by bubblewrap to build the sandbox, allowed without a capability. Inside the new user namespace the process
# only holds capabilities over that namespace, never over the container or the host.
EXTRA_RULE = {
    'names': ['clone', 'mount', 'pivot_root', 'sethostname', 'setns', 'umount', 'umount2', 'unshare'],
    'action': 'SCMP_ACT_ALLOW',
    'comment': 'Agent Hub runner: namespaces for the Codex (bubblewrap) sandbox; see scripts/make_runner_seccomp.py',
}


def build(upstream_text):
    if hashlib.sha256(upstream_text.encode()).hexdigest() != UPSTREAM_SHA256:
        raise SystemExit('上游 default.json 与已审核的版本不一致；请先审核差异，再更新 UPSTREAM_SHA256。')
    profile = json.loads(upstream_text)
    if profile.get('defaultAction') != 'SCMP_ACT_ERRNO':
        raise SystemExit('上游默认动作不是 SCMP_ACT_ERRNO，拒绝生成。')
    profile['syscalls'] = [*profile['syscalls'], EXTRA_RULE]
    return profile


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        raise SystemExit('用法：make_runner_seccomp.py <上游 default.json> <输出文件>')
    profile = build(Path(args[0]).read_text())
    Path(args[1]).write_text(json.dumps(profile, indent=2) + '\n')
    print(f'已生成 {args[1]}（{len(profile["syscalls"])} 条规则，较上游多 1 条）。')


if __name__ == '__main__':
    main()
