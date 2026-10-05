"""A write must be committed before its response is sent.

FastAPI 0.118+ runs the exit code of a plain `Depends(get_db)` only after the response has gone out, so a client that
reacts to the response at once (the web app loads data right after logging in) could query before the commit landed
and be refused. Every database dependency therefore has to be function-scoped.
"""
import asyncio
import json
import re
import secrets
from pathlib import Path

from sqlalchemy import func, select

from test_im_postgres import database
from app import main, security
from app.models import SessionToken, User

APP = Path(__file__).resolve().parents[1] / 'backend' / 'app'


async def call(method, path, body=None, on_start=None):
    """Drives the ASGI app directly and runs `on_start` at the moment the response begins to be sent."""
    raw = json.dumps(body).encode() if body is not None else b''
    scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1', 'method': method, 'path': path, 'raw_path': path.encode(),
             'query_string': b'', 'root_path': '', 'scheme': 'http', 'server': ('testserver', 80), 'client': ('127.0.0.1', 50000),
             'headers': [(b'host', b'testserver'), (b'content-type', b'application/json'), (b'content-length', str(len(raw)).encode())]}
    sent, status = [], None

    async def receive():
        return {'type': 'http.request', 'body': raw, 'more_body': False}

    async def send(message):
        nonlocal status
        if message['type'] == 'http.response.start':
            status = message['status']
            if on_start:
                on_start()
        sent.append(message['type'])

    await main.app(scope, receive, send)
    return status


def test_the_login_session_is_visible_to_other_connections_when_the_response_starts(database):
    password = secrets.token_urlsafe(18)
    with database.begin() as db:
        db.add(User(email='signin@example.invalid', name='登录', role='member', org_id='org', team_id='team', active=True,
                    password_hash=security.hash_password(password)))
        db.flush()

    def tracked():
        with database() as db:
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    seen = []

    def probe():
        with database() as other:  # A different connection, exactly like the browser's next request.
            seen.append(other.scalar(select(func.count()).select_from(SessionToken)))

    security._attempts.clear()
    main.app.dependency_overrides[main.get_db] = tracked
    try:
        status = asyncio.run(call('POST', '/api/auth/login', {'email': 'signin@example.invalid', 'password': password}, probe))
    finally:
        main.app.dependency_overrides.clear()
    assert status == 200
    assert seen == [1], 'the session was not committed when the response began'


def test_a_commit_that_fails_is_reported_instead_of_a_success_that_never_happened(database):
    """Before: the commit ran after the 200 had been sent, so a constraint violation silently lost the data."""
    password = secrets.token_urlsafe(18)
    with database.begin() as db:
        db.add(User(email='admin@example.invalid', name='管理员', role='super_admin', active=True, password_hash=security.hash_password(password)))

    def failing_commit():
        with database() as db:
            try:
                yield db
                raise_on_commit = db.new or db.dirty
                if raise_on_commit:
                    db.flush()
                    raise main.IntegrityError('statement', {}, Exception('simulated unique violation'))
                db.commit()
            except BaseException:
                db.rollback()
                raise

    security._attempts.clear()
    main.app.dependency_overrides[main.get_db] = failing_commit
    try:
        status = asyncio.run(call('POST', '/api/auth/login', {'email': 'admin@example.invalid', 'password': password}))
    finally:
        main.app.dependency_overrides.clear()
    assert status == 409  # The existing IntegrityError handler can now answer, because nothing has been sent yet.
    with database() as db:
        assert db.scalar(select(func.count()).select_from(SessionToken)) == 0


def test_no_endpoint_can_slip_back_to_a_request_scoped_session():
    """Guards the convention: a bare `Depends(get_db)` would silently bring the race back."""
    offenders = [path.name for path in sorted(APP.glob('*.py')) if re.search(r'Depends\(get_db\)', path.read_text())]
    assert offenders == []
    used = sum(len(re.findall(r"Depends\(get_db, scope='function'\)", path.read_text())) for path in APP.glob('*.py'))
    assert used >= 61
