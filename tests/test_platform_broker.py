"""Authorization outbox tests: real isolated PostgreSQL, no external IM calls."""
import json
from datetime import timedelta
import pytest
from sqlalchemy import select, func
from test_im_postgres import database
from test_platform_auth import configure, device
from app import platform_broker as broker, platform_auth as pa, platform_worker as worker, platform_settings as settings
from app.models import *
from app.im import _enqueue
from app.im_discovery import pin, scope


def request(database, provider='feishu', group=True):
    with database.begin() as db:
        _enqueue(db, 'feishu', uid(), 'sender', 'chat' if group else 'private-chat', '发起本人授权', group)
        run = db.scalar(select(Run).order_by(Run.created_at.desc()))
        run.status = 'running'
        user = db.get(User, run.user_id)
        result = broker.operate(db, user, provider, 'start', run=run)
        return user.id, run.id, result


@pytest.mark.parametrize('group', [False, True])
def test_private_delivery_poll_completion(database, monkeypatch, group):
    configure(monkeypatch)
    sent = []
    monkeypatch.setattr(pa, 'begin', device)
    monkeypatch.setattr(broker, 'dispatch', lambda *args: sent.append(args))
    monkeypatch.setattr(pa, 'poll', lambda *args: ('connected', {'access_token':'PERSONAL-TOKEN', 'expires_in':600}))
    monkeypatch.setattr(worker, 'identity_matches', lambda *args: True)
    user_id, run_id, output = request(database, group=group)
    assert 'secret-code' not in json.dumps(output)
    worker.tick(database); worker.tick(database)
    assert len(sent) == 1 and sent[0][1] == 'sender' and 'secret-code' in sent[0][3]
    with database.begin() as db:
        run = db.get(Run, run_id)
        assert broker.operate(db, db.get(User,user_id), 'feishu','start',run=run)['state'] == 'pending'
        assert db.scalar(select(func.count()).select_from(PlatformAuthRequest)) == 1
        assert all('secret' not in m.content for m in db.scalars(select(Message)))
        connection = db.get(PlatformConnection,(user_id,'feishu')); connection.next_poll_at = now()-timedelta(seconds=1)
    worker.tick(database); worker.tick(database); worker.tick(database)
    assert len(sent) == 2 and 'PERSONAL-TOKEN' not in sent[1][3] and '重新发送' in sent[1][3]
    with database.begin() as db:
        assert db.get(PlatformConnection,(user_id,'feishu')).state == 'connected'
        entries = [dict(id=m.id) for m in db.scalars(select(Message))]
        owner = broker.markers(db,db.get(User,user_id),entries)
        assert next(m for m in owner if 'platform_authorization' in m)['platform_authorization']['can_open']
        other = User(email='other@example.invalid',name='other',role='super_admin',active=True,password_hash='unused');db.add(other);db.flush()
        other_entries = broker.markers(db,other,[dict(id=m.id) for m in db.scalars(select(Message))])
        assert not next(m for m in other_entries if 'platform_authorization' in m)['platform_authorization']['can_open']


def test_cross_provider_never_uses_source_identity(database, monkeypatch):
    configure(monkeypatch); monkeypatch.setattr(pa,'begin',device)
    sent=[];monkeypatch.setattr(broker,'dispatch',lambda *args:sent.append(args))
    user_id, _, result = request(database,'dingtalk')
    assert result['delivery_status'] == 'binding_required'
    worker.tick(database);worker.tick(database)
    assert not sent


def test_cross_provider_bound_target_and_legacy_fallback(database, monkeypatch):
    configure(monkeypatch);monkeypatch.setattr(pa,'begin',lambda p: device(p) | {'url':'https://login.dingtalk.com/device'})
    monkeypatch.setenv('DINGTALK_TRANSPORT','stream')
    with database.begin() as db:
        u=db.scalar(select(User));i=Identity(user_id=u.id,provider='dingtalk',external_user_id='staff-target');db.add(i);db.flush();pin(db,'identity',i.id,scope('dingtalk'))
    sent=[];monkeypatch.setattr(broker,'dispatch',lambda *args:sent.append(args))
    request(database,'dingtalk');worker.tick(database);worker.tick(database)
    assert sent[0][1] == 'staff-target'
    monkeypatch.setenv('DINGTALK_TRANSPORT','webhook')
    with database.begin() as db:
        identity,_,_=broker.target(db,db.scalar(select(User)),'dingtalk')
        assert identity is None


@pytest.mark.parametrize('stop', ['cancel','expiry','mapping','config','inactive'])
def test_stops_before_private_send(database, monkeypatch, stop):
    configure(monkeypatch);monkeypatch.setattr(pa,'begin',device)
    sent=[];monkeypatch.setattr(broker,'dispatch',lambda *args:sent.append(args))
    user_id,_,_=request(database);worker.tick(database)
    with database.begin() as db:
        if stop=='cancel':broker.operate(db,db.get(User,user_id),'feishu','cancel')
        if stop=='expiry':db.get(PlatformConnection,(user_id,'feishu')).expires_at=now()-timedelta(seconds=1)
        if stop=='mapping':db.delete(db.scalar(select(Identity)))
        if stop=='inactive':db.get(User,user_id).active=False
    if stop=='config':monkeypatch.setenv('PLATFORM_FEISHU_CLIENT_SECRET','changed')
    worker.tick(database);assert not sent


def test_ambiguous_send_never_replayed(database, monkeypatch):
    configure(monkeypatch);monkeypatch.setattr(pa,'begin',device)
    user_id,_,_=request(database);worker.tick(database)
    with database.begin() as db:
        j=db.get(PlatformAuthJob,(user_id,'feishu'));j.delivery='sending';j.lease_until=now()-timedelta(seconds=1)
    monkeypatch.setattr(broker,'dispatch',lambda *args:pytest.fail('ambiguous send replayed'))
    worker.tick(database);worker.tick(database)
    with database.begin() as db:
        assert db.get(PlatformAuthJob,(user_id,'feishu')).delivery=='ambiguous'


@pytest.mark.parametrize('provider', ['feishu','dingtalk'])
def test_dispatch_official_payload(monkeypatch, provider):
    from app import im
    monkeypatch.setattr(im,'access_token',lambda *args:'BOT-TOKEN')
    calls=[]
    monkeypatch.setattr(pa,'request',lambda method,url,**kwargs:calls.append((url,kwargs)) or {'code':0,'processQueryKey':'Q'})
    values={'DINGTALK_TRANSPORT':'stream','DINGTALK_ROBOT_CODE':'robot'}
    broker.dispatch(provider,'recipient',values,'PRIVATE-CODE','dedup')
    url,kwargs=calls[0]
    if provider=='feishu':
        assert kwargs['params']=={'receive_id_type':'open_id'} and kwargs['json']['receive_id']=='recipient'
    else:
        assert url.endswith('/oToMessages/batchSend') and kwargs['json']['userIds']==['recipient']
    assert 'chat_id' not in json.dumps(kwargs)


def test_config_revision_mask_clear_allowlist(database,monkeypatch):
    configure(monkeypatch)
    with database.begin() as db:
        out=settings.save(db,'feishu',settings.Update(revision=0,fields={'CLIENT_ID':'new','CLIENT_SECRET':'sensitive'}))
        assert out['revision']==1 and 'sensitive' not in json.dumps(out)
        assert 'sensitive' not in db.get(PlatformSettings,'feishu').encrypted
        with pytest.raises(Exception):settings.save(db,'feishu',settings.Update(revision=0))
        with pytest.raises(Exception):settings.save(db,'feishu',settings.Update(revision=1,fields={'SCOPES':'im:message:send_as_user'}))
        out=settings.save(db,'feishu',settings.Update(revision=1,fields={'CLIENT_SECRET':''}));assert out['secrets_set']['CLIENT_SECRET']
        out=settings.save(db,'feishu',settings.Update(revision=2,clear=['CLIENT_SECRET']));assert not out['configured']
        assert settings.effective(db,'feishu')[0]['CLIENT_SECRET']==''


def test_poll_identity_mismatch_discards_tokens(database,monkeypatch):
    configure(monkeypatch);monkeypatch.setattr(pa,'begin',device);monkeypatch.setattr(broker,'dispatch',lambda *args:None)
    user_id,_,_=request(database);worker.tick(database);worker.tick(database)
    with database.begin() as db:db.get(PlatformConnection,(user_id,'feishu')).next_poll_at=now()-timedelta(seconds=1)
    monkeypatch.setattr(pa,'poll',lambda *args:('connected',{'access_token':'WRONG-TOKEN','expires_in':600}))
    monkeypatch.setattr(worker,'identity_matches',lambda *args:False)
    worker.tick(database)
    with database.begin() as db:
        row=db.get(PlatformConnection,(user_id,'feishu'));assert row.state=='identity_mismatch' and row.encrypted==''


def test_poll_interval_slowdown_and_no_refresh_exchange(database,monkeypatch):
    configure(monkeypatch);monkeypatch.setattr(pa,'begin',lambda p:device(p)|{'interval':120})
    user_id,_,_=request(database);worker.tick(database)
    calls=[];monkeypatch.setattr(pa,'poll',lambda *args:calls.append(1) or ('slow_down',{}))
    worker.tick(database);assert not calls
    with database.begin() as db:
        row=db.get(PlatformConnection,(user_id,'feishu'));row.next_poll_at=now()-timedelta(seconds=1)
        broker.operate(db,db.get(User,user_id),'feishu','refresh')
    assert not calls
    worker.tick(database);assert len(calls)==1
    with database.begin() as db:
        row=db.get(PlatformConnection,(user_id,'feishu'));assert pa.unseal(row)['interval']==125
        assert (row.next_poll_at-now()).total_seconds()>120


def test_send_failure_does_not_recreate_or_retry(database,monkeypatch):
    configure(monkeypatch);calls=[];monkeypatch.setattr(pa,'begin',lambda p:calls.append(p) or device(p))
    monkeypatch.setattr(broker,'dispatch',lambda *args:(_ for _ in ()).throw(ValueError('PRIVATE-CODE-LEAK')))
    user_id,run_id,_=request(database);worker.tick(database);worker.tick(database)
    with database.begin() as db:
        out=broker.operate(db,db.get(User,user_id),'feishu','start',run=db.get(Run,run_id))
        assert out['delivery_status']=='failed' and out['error_code']=='PRIVATE_DELIVERY_FAILED'
        assert 'PRIVATE-CODE-LEAK' not in json.dumps(out)
    worker.tick(database);assert len(calls)==1


@pytest.mark.parametrize('values', [{'expires_in':0},{'expires_in':'900'},{'interval':False},{'url':'https://evil.test/'},{'user_code':'\nBAD'}])
def test_malformed_device_response(monkeypatch,values):
    configure(monkeypatch)
    response={'device_code':'D','user_code':'U','verification_uri':'https://accounts.feishu.cn/device','expires_in':240,'interval':5}
    response.update({'verification_uri':values['url']} if 'url' in values else values)
    monkeypatch.setattr(pa,'request',lambda *args,**kwargs:response)
    with pytest.raises(ValueError):pa.begin('feishu')


def test_private_dns_and_arbitrary_endpoint_rejected(monkeypatch):
    from app import attachment_download as download
    import socket
    monkeypatch.setattr(socket,'getaddrinfo',lambda *args,**kwargs:[(None,None,None,None,('127.0.0.1',443))])
    with pytest.raises(RuntimeError):pa.request('POST','https://accounts.feishu.cn/oauth/v1/device_authorization')
    with pytest.raises(ValueError):pa.request('POST','https://evil.test/oauth')


def test_oauth_http_admin_and_validation_redaction(database,monkeypatch):
    from app import main,security
    from fastapi.testclient import TestClient
    configure(monkeypatch)
    with database.begin() as db:u=db.scalar(select(User));user_id=u.id
    def session():
        with database.begin() as db:yield db
    def actor():
        with database() as db:return db.get(User,user_id)
    main.app.dependency_overrides[main.get_db]=session;main.app.dependency_overrides[security.current_user]=actor
    try:
        client=TestClient(main.app)
        assert client.get('/api/integrations/oauth/feishu').status_code==403
        with database.begin() as db:db.get(User,user_id).role='super_admin'
        result=client.put('/api/integrations/oauth/feishu',json={'revision':0,'fields':{'CLIENT_SECRET':'https://secret.invalid/?code=LEAK'},'unexpected':'LEAK'})
        assert result.status_code==422 and 'LEAK' not in result.text
        result=client.put('/api/integrations/oauth/feishu',json={'revision':0,'fields':{'CLIENT_SECRET':'PRIVATE-SECRET'}})
        assert result.status_code==200 and 'PRIVATE-SECRET' not in result.text
    finally:main.app.dependency_overrides.clear()
