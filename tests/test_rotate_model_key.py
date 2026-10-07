"""Model key rotation: the new key is proven before it is written, the old one is proven dead, and no key is ever printed."""
import importlib.util
import os
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'rotate_model_key.py'
spec = importlib.util.spec_from_file_location('rotate_model_key', SCRIPT)
rotate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rotate)

OLD = 'model-key-old-' + 'a1' * 8
NEW = 'model-key-new-' + 'b2' * 8
ENDPOINT = 'https://models.example.com/v1'


def env_text(old=OLD, **extra):
    lines = ['# deployment settings', 'HUB_PORT=18210', f'{rotate.KEY_NAME}={old}', 'CODEX_MODEL=demo-model',
             f'CODEX_BASE_URL={ENDPOINT}', '', 'COMPOSE_FILE=compose.yaml:compose.runner-arm64.yaml']
    lines += [f'{name}={value}' for name, value in extra.items()]
    return '\n'.join(lines) + '\n'


@pytest.fixture
def root(tmp_path):
    (tmp_path / '.env.docker').write_text(env_text())
    return tmp_path


class Run:
    """One rotation with a scripted provider: `answers` maps a key to (status, model listed)."""

    def __init__(self, root, answers, typed=NEW):
        self.root, self.answers, self.typed, self.calls, self.lines, self.proxies = root, answers, typed, [], [], []

    def check(self, endpoint, key, model, proxy=None):
        self.calls.append((endpoint, key, model))
        self.proxies.append(proxy)
        return self.answers.get(key, (None, None))

    def __call__(self, name='.env.docker', *extra):
        def refuse_prompt(_):
            raise AssertionError('a key file must never fall back to the prompt')
        ask = refuse_prompt if '--key-file' in extra else (lambda _: self.typed)
        code = rotate.main([name, *extra], self.root, ask=ask, check=self.check, out=self.lines.append)
        return code, '\n'.join(self.lines)


def test_a_good_rotation_writes_only_the_key_line_and_proves_the_old_key_dead(root):
    code, output = Run(root, {NEW: (200, True), OLD: (401, None)})()
    assert code == 0
    written = (root / '.env.docker').read_text()
    assert written == env_text(old=NEW)  # Every other line, comments and order included, is untouched.
    assert stat.S_IMODE((root / '.env.docker').stat().st_mode) == 0o600
    assert '吊销已生效' in output and 'up -d --no-deps runner' in output
    assert OLD not in output and NEW not in output


def test_the_new_key_is_checked_against_the_configured_endpoint_and_model_before_anything_is_written(root):
    run = Run(root, {NEW: (200, True), OLD: (403, None)})
    run()
    assert run.calls[0] == (ENDPOINT, NEW, 'demo-model') and run.calls[1] == (ENDPOINT, OLD, 'demo-model')


@pytest.mark.parametrize('answer,reason', [((401, None), '服务商拒绝了新 Key'), ((403, None), '服务商拒绝了新 Key'),
                                            ((200, False), '端点上没有配置的模型'), ((500, None), '无法连接端点或端点异常'),
                                            ((None, None), '无法连接端点或端点异常')])
def test_a_new_key_that_does_not_work_changes_nothing(root, answer, reason):
    run = Run(root, {NEW: answer, OLD: (401, None)})
    code, output = run()
    assert code == 2 and reason in output and '未修改任何文件' in output
    assert (root / '.env.docker').read_text() == env_text()
    assert [call[1] for call in run.calls] == [NEW]  # The old key is not even tried: it is still the only working one.
    assert OLD not in output and NEW not in output


@pytest.mark.parametrize('typed', ['', 'short', OLD, NEW + ' extra', NEW + '"', "'" + NEW, NEW + '\nX=1', 'x' * 201, '=' + NEW])
def test_an_unusable_new_key_is_refused_before_any_request(root, typed):
    run = Run(root, {})
    run.typed = typed
    code, output = run()
    assert code == 2 and run.calls == [] and (root / '.env.docker').read_text() == env_text()
    assert OLD not in output


def test_surrounding_whitespace_from_pasting_is_trimmed(root):
    code, _ = Run(root, {NEW: (200, True), OLD: (401, None)}, typed=f'  {NEW}\n')()
    assert code == 0 and (root / '.env.docker').read_text() == env_text(old=NEW)


@pytest.mark.parametrize('old_answer,fragment', [((200, True), '旧 Key 仍然有效'), ((None, None), '无法确认旧 Key'), ((500, None), '无法确认旧 Key')])
def test_an_old_key_that_may_still_work_is_reported_loudly(root, old_answer, fragment):
    code, output = Run(root, {NEW: (200, True), OLD: old_answer})()
    assert code == 3 and fragment in output and '吊销' in output
    assert (root / '.env.docker').read_text() == env_text(old=NEW)  # The new key is in place either way.
    assert OLD not in output and NEW not in output


def test_a_model_is_optional_and_the_default_endpoint_is_used_when_none_is_configured(root):
    (root / '.env.docker').write_text(f'{rotate.KEY_NAME}={OLD}\n')
    run = Run(root, {NEW: (200, True), OLD: (401, None)})
    assert run()[0] == 0 and run.calls[0] == (rotate.DEFAULT_ENDPOINT, NEW, None)


@pytest.mark.parametrize('base', ['http://models.example.com/v1', 'https://user:x@models.example.com/v1',
                                  'https://models.example.com/v1?token=1', 'models.example.com'])
def test_the_key_is_never_sent_to_an_endpoint_that_is_not_plain_https(root, base):
    (root / '.env.docker').write_text(env_text().replace(ENDPOINT, base))
    run = Run(root, {NEW: (200, True)})
    with pytest.raises(SystemExit):
        run()
    assert run.calls == [] and OLD in (root / '.env.docker').read_text()


@pytest.mark.parametrize('content', ['HUB_PORT=1\n', f'{rotate.KEY_NAME}={OLD}\n{rotate.KEY_NAME}={OLD}\n', f'#{rotate.KEY_NAME}={OLD}\n'])
def test_the_file_must_hold_exactly_one_key_line(root, content):
    (root / '.env.docker').write_text(content)
    run = Run(root, {NEW: (200, True)})
    with pytest.raises(SystemExit):
        run()
    assert run.calls == [] and (root / '.env.docker').read_text() == content


@pytest.mark.parametrize('name', ['.env.example', '../.env.docker', 'env', '.env.docker/../x', '.env.'])
def test_only_env_files_in_the_project_can_be_named(root, name):
    with pytest.raises(SystemExit):
        Run(root, {})(name)


def test_a_missing_file_is_refused(tmp_path):
    with pytest.raises(SystemExit):
        Run(tmp_path, {})()


# ---- the real probe, against a local provider -----------------------------------------------------------------

def provider(respond):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append({'path': self.path, 'authorization': self.headers.get('Authorization')})
            status, payload, headers = respond(self.path)
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f'http://127.0.0.1:{httpd.server_port}', seen


def listing(*ids):
    import json
    return 200, json.dumps({'data': [{'id': name} for name in ids]}).encode(), {}


def test_the_probe_reads_status_and_model_presence_and_sends_the_key_as_a_bearer_token():
    httpd, url, seen = provider(lambda _: listing('other', 'demo-model'))
    try:
        assert rotate.probe(url + '/v1', NEW, 'demo-model') == (200, True)
        assert rotate.probe(url + '/v1', NEW, 'missing-model') == (200, False)
        assert rotate.probe(url + '/v1', NEW) == (200, True)
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert seen[0] == {'path': '/v1/models', 'authorization': f'Bearer {NEW}'}


def test_the_probe_reports_refusals_and_malformed_listings():
    for status, payload, expected in [(401, b'{"error": "no"}', (401, None)), (403, b'', (403, None)), (500, b'x', (500, None)),
                                      (200, b'not json', (200, None)), (200, b'{"data": 5}', (200, None)), (200, b'[]', (200, None))]:
        httpd, url, _ = provider(lambda _, s=status, p=payload: (s, p, {}))
        try:
            assert rotate.probe(url, NEW, 'demo-model') == expected
        finally:
            httpd.shutdown()
            httpd.server_close()


def test_the_probe_never_follows_a_redirect_with_the_key():
    elsewhere, elsewhere_url, hits = provider(lambda _: listing('demo-model'))
    httpd, url, _ = provider(lambda _: (302, b'', {'Location': elsewhere_url + '/steal'}))
    try:
        assert rotate.probe(url, NEW, 'demo-model') == (302, None)
    finally:
        for server in (httpd, elsewhere):
            server.shutdown()
            server.server_close()
    assert hits == []


def free_port():
    import socket
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def test_the_probe_reports_an_unreachable_endpoint():
    assert rotate.probe(f'http://127.0.0.1:{free_port()}', NEW) == (None, None)


def test_the_probe_ignores_ambient_proxy_settings_like_the_runner(monkeypatch):
    dead = f'http://127.0.0.1:{free_port()}'
    for name in ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY'):
        monkeypatch.setenv(name, dead)
    monkeypatch.delenv('NO_PROXY', raising=False)
    monkeypatch.delenv('no_proxy', raising=False)
    httpd, url, seen = provider(lambda _: listing('demo-model'))
    try:
        assert rotate.probe(url, NEW, 'demo-model') == (200, True)
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert seen and seen[0]['path'] == '/models'


def test_a_proxy_named_by_the_deployment_is_used():
    proxy, proxy_url, seen = provider(lambda _: listing('demo-model'))
    try:
        assert rotate.probe('http://models.invalid/v1', NEW, 'demo-model', proxy=proxy_url) == (200, True)
    finally:
        proxy.shutdown()
        proxy.server_close()
    assert seen[0]['path'] == 'http://models.invalid/v1/models'  # An absolute-form request line: it went through the proxy.


def test_the_configured_proxy_is_passed_to_every_check_and_a_socks_proxy_is_refused(root):
    (root / '.env.docker').write_text(env_text(CODEX_PROXY_URL='http://127.0.0.1:7890'))
    run = Run(root, {NEW: (200, True), OLD: (401, None)})
    assert run()[0] == 0 and run.proxies == ['http://127.0.0.1:7890'] * 2
    (root / '.env.docker').write_text(env_text(CODEX_PROXY_URL='socks5://127.0.0.1:7890'))
    refused = Run(root, {NEW: (200, True)})
    with pytest.raises(SystemExit):
        refused()
    assert refused.calls == [] and OLD in (root / '.env.docker').read_text()


# ---- the key from a private file -------------------------------------------------------------------------------

def key_file(root, content=NEW + '\n', mode=0o600, name='new-key'):
    path = root / name
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    path.chmod(mode)
    return path


def test_a_key_file_is_read_used_and_removed_without_the_key_being_printed(root):
    path = key_file(root)
    run = Run(root, {NEW: (200, True), OLD: (401, None)})
    code, output = run('.env.docker', '--key-file', str(path))
    assert code == 0 and (root / '.env.docker').read_text() == env_text(old=NEW)
    assert not path.exists() and '已删除密钥文件' in output
    assert OLD not in output and NEW not in output and str(path) not in output
    assert [call[1] for call in run.calls] == [NEW, OLD]


def test_the_file_is_removed_even_when_the_old_key_is_still_valid(root):
    path = key_file(root)
    code, output = Run(root, {NEW: (200, True), OLD: (200, True)})('.env.docker', '--key-file', str(path))
    assert code == 3 and not path.exists() and '旧 Key 仍然有效' in output


def test_a_key_that_does_not_work_leaves_the_file_for_a_retry(root):
    path = key_file(root)
    code, _ = Run(root, {NEW: (401, None)})('.env.docker', '--key-file', str(path))
    assert code == 2 and path.exists() and (root / '.env.docker').read_text() == env_text()


def test_the_option_may_come_before_the_file_name_and_the_default_target_is_env_docker(root):
    path = key_file(root)
    run = Run(root, {NEW: (200, True), OLD: (401, None)})
    code = rotate.main(['--key-file', str(path)], root, ask=None, check=run.check, out=run.lines.append)
    assert code == 0 and (root / '.env.docker').read_text() == env_text(old=NEW)


@pytest.mark.parametrize('mode', [0o644, 0o640, 0o604, 0o666, 0o660])
def test_a_key_file_other_people_can_read_is_refused_and_kept(root, mode):
    path = key_file(root, mode=mode)
    run = Run(root, {NEW: (200, True)})
    with pytest.raises(SystemExit) as refused:
        run('.env.docker', '--key-file', str(path))
    assert run.calls == [] and path.exists() and (root / '.env.docker').read_text() == env_text()
    assert NEW not in str(refused.value)


def test_a_symlink_a_directory_a_missing_file_and_a_fifo_are_refused(root):
    real = key_file(root)
    link = root / 'link'
    link.symlink_to(real)
    directory = root / 'folder'
    directory.mkdir(mode=0o700)
    fifo = root / 'pipe'
    os.mkfifo(fifo, 0o600)
    for target in (link, directory, root / 'absent', fifo):
        run = Run(root, {NEW: (200, True)})
        with pytest.raises(SystemExit):
            run('.env.docker', '--key-file', str(target))
        assert run.calls == [] and (root / '.env.docker').read_text() == env_text()
    assert real.exists()


@pytest.mark.parametrize('content', [b'', b'\n', b'x' * 513, (NEW + '\n' + NEW + '\n').encode(), (NEW + ' ' + NEW).encode(),
                                     b'\xff\xfe', ("'" + NEW + "'").encode()])
def test_a_key_file_with_the_wrong_content_changes_nothing(root, content):
    path = key_file(root, content)
    run = Run(root, {NEW: (200, True)})
    try:
        code, _ = run('.env.docker', '--key-file', str(path))
        assert code == 2
    except SystemExit:
        pass
    assert run.calls == [] and path.exists() and (root / '.env.docker').read_text() == env_text()


@pytest.mark.parametrize('args', [('--key-file',), ('--key-file', 'a', '--key-file', 'b'), ('--unknown',), ('.env.docker', '.env.other'),
                                  ('--key-file', '--key-file'), ('-k', 'x')])
def test_malformed_command_lines_are_refused(root, args):
    with pytest.raises(SystemExit):
        rotate.main(list(args), root, ask=lambda _: NEW, check=lambda *a: (200, True), out=lambda _: None)
    assert (root / '.env.docker').read_text() == env_text()
