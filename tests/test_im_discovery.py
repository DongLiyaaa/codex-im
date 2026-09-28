"""Real PG onboarding; no external network or live mappings."""
import asyncio
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from sqlalchemy import select, func
from test_im_postgres import database
from app import im, im_discovery as discovery, im_settings
from app.models import IMDiscovery, IMEvent, Run, Identity, User, Group, Audit, Binding, IMScopeBinding


def root():
    return SimpleNamespace(id='root', role='super_admin', active=True, org_id=None)


def count(db, model):
    return db.scalar(select(func.count()).select_from(model))


@pytest.mark.parametrize('failure', ['sender', 'group', 'membership'])
def test_persist_and_new_event_only(database, failure):
    sender = 'new' if failure == 'sender' else 'sender'
    chat = 'newchat' if failure == 'group' else 'chat'
    with database.begin() as db:
        user = db.scalar(select(User))
        uid = user.id
        group = db.scalar(select(Group))
        gid = group.id
        if failure == 'membership':
            group.member_ids = []
        assert im._enqueue(db, 'feishu', 'before', sender, chat, 'PRIVATE BODY', True)['pending']
    with database.begin() as db:
        row = db.scalar(select(IMDiscovery))
        assert count(db, Run) == 0 and count(db, IMEvent) == 1
        assert 'PRIVATE BODY' not in str(row.__dict__)
        body = discovery.Approve(user_id=uid)
        if failure == 'group':
            body = discovery.Approve(user_id=uid, confirm_member=True, new_group={
                'name': 'new group', 'org_id': 'org', 'team_id': 'team', 'member_ids': [uid],
                'provider': 'feishu', 'external_id': chat})
        elif failure == 'membership':
            # Empty legacy groups are not manageable under existing policy; restore
            # a separately authorized member to exercise explicit membership add.
            other = User(email='other@test.local', name='other', role='member', org_id='org', team_id='team', active=True, password_hash='x')
            db.add(other); db.flush()
            db.get(Group, gid).member_ids = [other.id]
            body = discovery.Approve(user_id=uid, group_id=gid, confirm_member=True)
        discovery.approve(row.id, body, root(), db)
        assert count(db, Binding) == 0
        assert count(db, Audit) == 1
    with database.begin() as db:
        assert im._enqueue(db, 'feishu', 'before', sender, chat, 'PRIVATE BODY', True)['duplicate']
        assert count(db, Run) == 0
        assert im._enqueue(db, 'feishu', 'after', sender, chat, 'new message', True) == {'ok': True}
    with database() as db:
        assert count(db, Run) == 1


def test_concurrent_upsert_and_duplicate(database):
    def send(index):
        with database.begin() as db:
            return im._enqueue(db, 'feishu', str(index), 'unknown', 'newchat', 'secret', True)
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(send, [1, 2, 3, 1, 2, 3]))
    with database() as db:
        assert count(db, IMDiscovery) == 1 and count(db, IMEvent) == 3 and count(db, Run) == 0
        row = db.scalar(select(IMDiscovery))
        assert row.last_seen >= row.first_seen


@pytest.mark.parametrize('role', ['org_admin', 'team_lead', 'member'])
def test_permission_denial(database, role):
    with database.begin() as db:
        actor = root(); actor.role = role
        with pytest.raises(HTTPException) as exc:
            discovery.admin(actor)
        assert exc.value.status_code == 403
        with pytest.raises(HTTPException) as exc:
            discovery.approve('missing', discovery.Approve(user_id='x'), actor, db)
        assert exc.value.status_code == 403


def test_scope_rotation_and_secret_stability(database, monkeypatch):
    initial = discovery.scope('feishu')
    monkeypatch.setenv('FEISHU_APP_SECRET', 'DO_NOT_LEAK')
    assert discovery.scope('feishu') == initial
    with database.begin() as db:
        im._enqueue(db, 'feishu', 'unknown', 'new', 'chat', 'PRIVATE BODY', True)
        rid = db.scalar(select(IMDiscovery.id)); uid = db.scalar(select(User.id))
    monkeypatch.setenv('FEISHU_APP_ID', 'new-app')
    with database.begin() as db:
        with pytest.raises(HTTPException) as exc:
            discovery.approve(rid, discovery.Approve(user_id=uid), root(), db)
        assert exc.value.status_code == 409
        rows = discovery.discoveries(root(), db)
        assert rows[0]['status'] == 'stale_application'
        assert 'DO_NOT_LEAK' not in json.dumps(rows, default=str)
        assert im._enqueue(db, 'feishu', 'new-event', 'sender', 'chat', 'secret', True)['pending']
        assert count(db, Run) == 0


@pytest.mark.parametrize('conflict', ['user', 'tenant', 'platform', 'mapping', 'unconfirmed', 'wrong_app'])
def test_approval_atomic_conflicts(database, conflict):
    with database.begin() as db:
        user = db.scalar(select(User)); group = db.scalar(select(Group))
        uid, gid = user.id, group.id
        discovery.record(db, 'feishu', discovery.scope('feishu'), 'sender', 'chat', True, 'not_member')
        rid = db.scalar(select(IMDiscovery.id))
        if conflict == 'user':
            other = User(email='other@test.local', name='other', role='member', org_id='org', team_id='team', active=True, password_hash='x')
            db.add(other); db.flush(); uid = other.id
        if conflict == 'tenant': group.org_id = 'foreign'
        if conflict == 'platform': group.provider = 'web'
        if conflict == 'mapping': group.external_id = 'other'
        if conflict == 'wrong_app':
            db.scalar(select(IMScopeBinding).where(IMScopeBinding.subject_type == 'identity')).app_scope = 'other-app'
    with pytest.raises(HTTPException) as exc:
        with database.begin() as db:
            discovery.approve(rid, discovery.Approve(user_id=uid, group_id=gid, confirm_member=conflict != 'unconfirmed'), root(), db)
    assert exc.value.status_code in (400, 403, 409)
    with database() as db:
        assert count(db, Audit) == 0 and count(db, Identity) == 1


def test_webhook_signature_and_commit(database, monkeypatch):
    monkeypatch.setenv('FEISHU_TRANSPORT', 'webhook')
    monkeypatch.setenv('FEISHU_ENCRYPT_KEY', 'encrypt-test')
    monkeypatch.setenv('FEISHU_VERIFICATION_TOKEN', 'verify-test')
    from test_im_connections import feishu
    payload = feishu(sender='unknown')
    payload['schema'] = '2.0'; payload['header']['token'] = 'verify-test'
    raw = json.dumps(payload).encode(); timestamp = str(int(time.time())); nonce = 'nonce'
    class Request:
        headers = {'x-lark-request-timestamp': timestamp, 'x-lark-request-nonce': nonce, 'x-lark-signature': 'bad'}
        async def stream(self): yield raw
    with pytest.raises(HTTPException):
        with database.begin() as db:
            asyncio.run(im.feishu_callback(Request(), db))
    with database() as db: assert count(db, IMDiscovery) == 0
    Request.headers['x-lark-signature'] = hashlib.sha256((timestamp + nonce + 'encrypt-test').encode() + raw).hexdigest()
    with database.begin() as db:
        assert asyncio.run(im.feishu_callback(Request(), db))['pending']
    with database() as db:
        assert count(db, IMDiscovery) == 1 and count(db, Run) == 0


def test_nickname_bounds_and_unknown_fields(database):
    with database.begin() as db:
        discovery.record(db, 'dingtalk', discovery.scope('dingtalk'), 's', 'c', False, 'unknown_sender', 'n' * 400)
    with database() as db:
        row = db.scalar(select(IMDiscovery))
        assert len(row.nickname) == 200
        assert set(row.__table__.columns.keys()) == {'id', 'provider', 'app_scope', 'sender_id', 'chat_id', 'chat_type', 'nickname', 'reason', 'first_seen', 'last_seen'}


def test_worker_rechecks_application(database, monkeypatch):
    from app.service import build_payload
    with database.begin() as db:
        im._enqueue(db, 'feishu', 'allowed', 'sender', 'chat', 'hello', True)
    monkeypatch.setenv('FEISHU_APP_ID', 'changed')
    with database() as db:
        with pytest.raises(HTTPException):
            build_payload(db, db.scalar(select(Run)))


def test_http_dingtalk_discovery_commit(database, monkeypatch):
    import base64
    import hmac
    from fastapi.testclient import TestClient
    from app import main, db as database_module
    monkeypatch.setattr(database_module, 'SessionLocal', database)
    monkeypatch.setenv('DINGTALK_TRANSPORT', 'webhook')
    monkeypatch.setenv('DINGTALK_APP_SECRET', 'test-secret')
    monkeypatch.setenv('DINGTALK_ROBOT_CHAT_ID', 'fixed')
    monkeypatch.setenv('DINGTALK_ROBOT_ACCESS_TOKEN', 'test-token')
    timestamp = str(int(time.time() * 1000))
    signature = base64.b64encode(hmac.new(b'test-secret', (timestamp + '\n' + 'test-secret').encode(), hashlib.sha256).digest()).decode()
    client = TestClient(main.app)  # No lifespan: never start worker or runner.
    payload = {'msgtype': 'text', 'msgId': 'http-event', 'senderStaffId': 'unknown',
               'conversationId': 'unregistered', 'conversationType': '2', 'text': {'content': 'PRIVATE BODY'}}
    invalid = client.post('/api/im/dingtalk/callback', json=payload, headers={'timestamp': timestamp, 'sign': 'invalid'})
    assert invalid.status_code == 403
    with database() as db:
        assert count(db, IMDiscovery) == 0
    response = client.post('/api/im/dingtalk/callback', json=payload, headers={'timestamp': timestamp, 'sign': signature})
    assert response.status_code == 200 and response.json()['pending']
    with database() as db:
        assert count(db, IMDiscovery) == 1 and count(db, Run) == 0
        assert db.scalar(select(IMEvent)).reply_target == {}


def test_unknown_legacy_identity_cannot_be_claimed(database):
    from sqlalchemy import delete
    with database.begin() as db:
        db.execute(delete(IMScopeBinding).where(IMScopeBinding.subject_type == 'identity'))
        im._enqueue(db, 'feishu', 'legacy', 'sender', 'chat', 'private', True)
        rid = db.scalar(select(IMDiscovery.id)); uid = db.scalar(select(User.id))
    with pytest.raises(HTTPException) as exc:
        with database.begin() as db:
            discovery.approve(rid, discovery.Approve(user_id=uid), root(), db)
    assert exc.value.status_code == 409
    with database() as db:
        assert count(db, Audit) == 0
        assert not discovery.pinned(db, 'identity', db.scalar(select(Identity.id)), discovery.scope('feishu'))


def test_concurrent_conflicting_approvals(database):
    with database.begin() as db:
        other = User(email='second@test.local', name='second', role='member', org_id='org', team_id='team', active=True, password_hash='x')
        db.add(other); db.flush()
        uids = list(db.scalars(select(User.id)))
        im._enqueue(db, 'feishu', 'race', 'new', 'private-chat', 'private', False)
        rid = db.scalar(select(IMDiscovery.id))
    def approve(uid):
        try:
            with database.begin() as db:
                discovery.approve(rid, discovery.Approve(user_id=uid), root(), db)
            return 200
        except HTTPException as exc:
            return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(approve, uids)) == [200, 409]
    with database() as db:
        assert count(db, Audit) == 1 and count(db, Run) == 0


def test_peer_admin_cannot_be_bound(database):
    with database.begin() as db:
        peer = User(email='peer@test.local', name='peer', role='super_admin', active=True, password_hash='x')
        db.add(peer); db.flush(); uid = peer.id
        im._enqueue(db, 'feishu', 'peer', 'new', 'private', 'private', False)
        rid = db.scalar(select(IMDiscovery.id))
    with pytest.raises(HTTPException) as exc:
        with database.begin() as db:
            discovery.approve(rid, discovery.Approve(user_id=uid), root(), db)
    assert exc.value.status_code == 403


def test_existing_empty_mapping_and_explicit_membership(database):
    with database.begin() as db:
        user = db.scalar(select(User))
        group = db.scalar(select(Group))
        group.external_id = None
        uid, gid = user.id, group.id
        im._enqueue(db, 'feishu', 'new-group', 'sender', 'chat', 'hello', True)
        rid = db.scalar(select(IMDiscovery.id))
    with database.begin() as db:
        discovery.approve(rid, discovery.Approve(user_id=uid, group_id=gid, confirm_member=True), root(), db)
    with database() as db:
        assert db.get(Group, gid).external_id == 'chat'
        assert db.get(Group, gid).member_ids == [uid]
        assert count(db, Run) == 0
