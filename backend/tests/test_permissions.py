import os
import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

os.environ.setdefault('SESSION_SECRET', 'integration-test-secret-at-least-32-characters')
from pathlib import Path
from sqlalchemy.engine import URL

os.environ.setdefault('DATABASE_URL', URL.create(
    'postgresql+psycopg', database='agent_hub_test',
    query={'host': str(Path(__file__).resolve().parents[2] / '.runtime' / 'pgsocket'), 'port': '55439'},
).render_as_string(hide_password=False))
from app import main, models as m, service, policy
from app.db import get_db
from app.security import hash_password


MOCK_PASSWORD = '-'.join(['test', 'password', '123'])  # not a real credential; local test fixture only


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv('FEISHU_APP_ID', 'test-app')
    root = create_engine(os.environ['DATABASE_URL'])
    assert root.url.database.endswith('_test'), 'Tests require a dedicated *_test database'
    schema = 'test_' + uuid.uuid4().hex
    from sqlalchemy import text
    from psycopg import sql
    # Identifier is internally generated (fixed 'test_' prefix + uuid4 hex), never
    # supplied by external input; quote() applies the dialect's own escaping.
    quoted_schema = root.dialect.identifier_preparer.quote(schema)
    with root.connect() as conn:
        conn.execute(text('CREATE SCHEMA ' + quoted_schema))
        conn.commit()
    engine = create_engine(os.environ['DATABASE_URL'], connect_args={'options': f'-csearch_path={schema}'})
    factory = sessionmaker(engine, expire_on_commit=False)
    m.Base.metadata.create_all(engine)
    monkeypatch.setattr(service, 'SessionLocal', factory)
    monkeypatch.setattr(service, 'engine', engine)
    people = {}
    with factory.begin() as db:
        for key, role, org, team in [
            ('root','super_admin',None,None), ('admin','org_admin','a',None),
            ('peer','org_admin','a',None), ('other','org_admin','b',None),
            ('lead','team_lead','a','x'), ('lead2','team_lead','a','y'),
            ('member','member','a','x'), ('peer_member','member','a','x'),
            ('different','member','a','y'), ('outsider','member','b','x')]:
            user = m.User(email=key+'@test.local', name=key, role=role, org_id=org, team_id=team, active=True, password_hash=hash_password(MOCK_PASSWORD))
            db.add(user)
            db.flush()
            people[key] = user
    def dependency():
        with factory() as db:
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
    main.app.dependency_overrides[get_db] = dependency
    client = TestClient(main.app)
    def auth(key):
        main.app.dependency_overrides[main.current_user] = lambda: people[key]
        return client
    yield factory, people, auth
    main.app.dependency_overrides.clear()
    engine.dispose()
    with root.connect() as conn:
        # Identifier is internally generated, never supplied by external input.
        conn.connection.driver_connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
        conn.connection.driver_connection.commit()
    root.dispose()


@pytest.mark.parametrize('provider', ['web', 'feishu', 'dingtalk'])
def test_conversation_state_source_permissions_and_terminal(env, provider):
    factory, people, auth = env
    # Deliberately misleading title: provider must come from the run's IMEvent.
    cid = auth('member').post('/api/conversations', json={'title': 'feishu:pretend'}).json()['id']
    assert auth('member').get(f'/api/conversations/{cid}/state').json() == {
        'messages': [], 'active_run': None, 'latest_run': None}
    created = auth('member').post(f'/api/conversations/{cid}/messages', json={'content': 'work'}).json()
    rid, mid = created['run']['id'], created['user_message']['id']
    if provider != 'web':
        with factory.begin() as db:
            db.add(m.IMEvent(provider=provider, event_id=uuid.uuid4().hex, run_id=rid,
                             reply_target={'secret': 'must-not-leak'}))
    for status in ['queued', 'running', 'succeeded', 'failed', 'cancelled', 'interrupted']:
        with factory.begin() as db:
            run = db.get(m.Run, rid)
            run.status = status
            run.error = 'Worker interrupted; manual retry required' if status == 'failed' else None
        for actor in ['member', 'lead']:
            response = auth(actor).get(f'/api/conversations/{cid}/state')
            assert response.status_code == 200
            state = response.json()
            assert state['latest_run']['provider'] == provider
            assert state['latest_run']['message_id'] == mid
            assert (state['active_run'] is not None) == (status in ['queued', 'running'])
            assert 'must-not-leak' not in response.text
            assert state['messages'][0]['id'] == mid
        for actor in ['peer_member', 'other', 'outsider']:
            assert auth(actor).get(f'/api/conversations/{cid}/state').status_code == 403
    assert auth('lead').post(f'/api/conversations/{cid}/messages', json={'content': 'forbidden'}).status_code == 403
    with factory() as db:
        assert not db.scalar(select(m.Audit).where(m.Audit.action == 'conversation.supervised_read'))
    assert auth('lead').get(f'/api/conversations/{cid}/messages').status_code == 200
    with factory() as db:
        assert db.scalar(select(m.Audit).where(m.Audit.action == 'conversation.supervised_read'))
    # A user-only historical conversation must not be inferred to be working.
    with factory.begin() as db:
        if provider != 'web':
            db.delete(db.scalar(select(m.IMEvent).where(m.IMEvent.run_id == rid)))
            db.flush()
        db.delete(db.get(m.Run, rid))
    assert auth('member').get(f'/api/conversations/{cid}/state').json()['active_run'] is None


def test_user_hierarchy_and_cross_tenant(env):
    db, people, auth = env
    for actor, role, org, team, expected in [
        ('admin','org_admin','a',None,403), ('admin','super_admin',None,None,403),
        ('admin','member','b','x',403), ('lead','team_lead','a','x',403),
        ('lead','member','a','y',403), ('member','member','a','x',403),
        ('lead','member','a','x',201), ('root','org_admin','c',None,201)]:
        r = auth(actor).post('/api/users', json={'email':uuid.uuid4().hex+'@test.local','password':MOCK_PASSWORD,'name':'new','role':role,'org_id':org,'team_id':team})
        assert r.status_code == expected, r.text
    visible = auth('admin').get('/api/users').json()
    assert people['peer'].id not in {u['id'] for u in visible}
    assert people['outsider'].id not in {u['id'] for u in visible}


def test_group_scope_and_supervision(env):
    factory, p, auth = env
    for members, team, expected in [(['member','outsider'],'x',403),(['member','different'],None,403),(['member','peer_member'],'x',201)]:
        r = auth('lead').post('/api/groups', json={'name':'test','org_id':'a','team_id':team,'member_ids':[p[x].id for x in members]})
        assert r.status_code == expected, r.text
    with factory.begin() as db:
        g=m.Group(name='mixed',org_id='a',team_id=None,member_ids=[p['member'].id,p['different'].id],provider='web')
        db.add(g); db.flush()
        c=m.Conversation(title='mixed',owner_id=p['member'].id,group_id=g.id)
        db.add(c); db.flush(); cid=c.id
    assert auth('lead').get(f'/api/conversations/{cid}/messages').status_code == 403
    assert auth('admin').get(f'/api/conversations/{cid}/messages').status_code == 200
    assert auth('admin').post(f'/api/conversations/{cid}/messages',json={'content':'impersonate'}).status_code == 403


def test_private_read_audit_no_impersonation(env):
    factory,p,auth=env
    cid=auth('member').post('/api/conversations',json={'title':'private'}).json()['id']
    assert auth('peer_member').get(f'/api/conversations/{cid}/messages').status_code == 403
    assert auth('other').get(f'/api/conversations/{cid}/messages').status_code == 403
    assert auth('lead').get(f'/api/conversations/{cid}/messages').status_code == 200
    assert auth('lead').post(f'/api/conversations/{cid}/messages',json={'content':'bad'}).status_code == 403
    assert auth('member').post(f'/api/conversations/{cid}/messages',json={'content':'ok','user_id':p['outsider'].id}).status_code == 422
    with factory() as db:
        assert db.scalar(select(m.Audit).where(m.Audit.action=='conversation.supervised_read'))


def test_resource_binding_scope_intersection_and_masking(env):
    factory,p,auth=env
    with factory.begin() as db:
        g=m.Group(name='g',org_id='a',team_id='x',member_ids=[p['member'].id],provider='web'); db.add(g); db.flush()
        c=m.Conversation(title='g',owner_id=p['member'].id,group_id=g.id); db.add(c); db.flush()
        resources=[]
        for name,org,enabled in [('both','a',True),('user','a',True),('group','a',True),('disabled','a',False),('foreign','b',True)]:
            r=m.Resource(name=name,org_id=org,kind='mcp',enabled=enabled,config={'url':'https://example.com/mcp','headers':{'Authorization':'secret'}})
            db.add(r); db.flush(); resources.append(r)
            if name != 'group': db.add(m.Binding(subject_type='user',subject_id=p['member'].id,resource_id=r.id))
            if name != 'user': db.add(m.Binding(subject_type='group',subject_id=g.id,resource_id=r.id))
        cid,gid=c.id,g.id
    result=auth('member').get(f'/api/conversations/{cid}/capabilities').json()
    assert [r['name'] for r in result['mcps']] == ['both']
    assert result['mcps'][0]['config']=={}
    admin=auth('admin').get('/api/resources').json()
    assert all(r['config']['headers']['Authorization']=='***' for r in admin)
    for actor,subject,rid in [('admin',p['outsider'].id,resources[1].id),('admin',p['peer'].id,resources[1].id),('admin',p['peer_member'].id,resources[-1].id),('lead',p['peer_member'].id,resources[1].id)]:
        assert auth(actor).post('/api/bindings',json={'subject_type':'user','subject_id':subject,'resource_id':rid}).status_code==403
    assert auth('admin').post('/api/resources',json={'name':'cross','org_id':'b','kind':'skill','config':{'content':'---\nname: x\ndescription: x\n---\nhello'}}).status_code==403


def test_identity_unique_and_scope(env):
    factory,p,auth=env
    body={'provider':'feishu','external_user_id':'external','user_id':p['member'].id}
    assert auth('member').post('/api/identities',json=body).status_code==403
    assert auth('other').post('/api/identities',json=body).status_code==403
    assert auth('admin').post('/api/identities',json=body).status_code==403
    assert auth('root').post('/api/identities',json=body).status_code==201
    body['user_id']=p['peer_member'].id
    assert auth('root').post('/api/identities',json=body).status_code==409


def test_queue_failure_and_recheck(env,monkeypatch):
    factory,p,auth=env
    cid=auth('member').post('/api/conversations',json={'title':'queue'}).json()['id']
    r=auth('member').post(f'/api/conversations/{cid}/messages',json={'content':'hello'})
    assert r.status_code==202,r.text
    rid=r.json()['run']['id']
    assert auth('member').post(f'/api/conversations/{cid}/messages',json={'content':'duplicate'}).status_code==409
    with factory.begin() as db: db.get(m.Run,rid).status='running'
    monkeypatch.delenv('RUNNER_TOKEN',raising=False)
    service.execute_run(rid)
    assert auth('member').get('/api/runs/'+rid).json()['status']=='failed'
    assert [x['role'] for x in auth('member').get(f'/api/conversations/{cid}/messages').json()]==['user']
    second=auth('member').post(f'/api/conversations/{cid}/messages',json={'content':'next'}).json()['run']['id']
    with factory.begin() as db:
        db.get(m.Run,second).status='running'
        db.get(m.User,p['member'].id).active=False
    service.execute_run(second)
    with factory() as db:
        assert db.get(m.Run,second).status=='failed'
        assert db.get(m.Run,second).error=='Execution permission or resource validation failed'


def test_admin_self_bindings(env):
    factory,p,auth=env
    resource=auth('root').post('/api/resources',json={'name':'全局技能','kind':'skill','config':{'content':'---\nname: test\ndescription: test\n---\nhello'}}).json()
    assert auth('root').post('/api/bindings',json={'subject_type':'user','subject_id':p['root'].id,'resource_id':resource['id']}).status_code==201
    assert auth('root').post('/api/identities',json={'provider':'feishu','external_user_id':'root','user_id':p['root'].id}).status_code==201
    assert auth('member').post('/api/bindings',json={'subject_type':'user','subject_id':p['member'].id,'resource_id':resource['id']}).status_code==403


def test_actual_worker_recovery_and_failure(env,monkeypatch):
    import time
    factory,p,auth=env
    monkeypatch.setattr(service,'WORKER_LOCK', 123456789)
    monkeypatch.delenv('RUNNER_TOKEN',raising=False)
    ids=[]
    for title in ['interrupted','queued']:
        cid=auth('member').post('/api/conversations',json={'title':title}).json()['id']
        ids.append(auth('member').post(f'/api/conversations/{cid}/messages',json={'content':'hello'}).json()['run']['id'])
    with factory.begin() as db: db.get(m.Run,ids[0]).status='running'
    worker=service.Worker(); worker.start()
    try:
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            with factory() as db:
                if all(db.get(m.Run,rid).status=='failed' for rid in ids): break
            time.sleep(0.05)
        with factory() as db:
            assert db.get(m.Run,ids[0]).error=='Worker interrupted; manual retry required'
            assert db.get(m.Run,ids[1]).status=='failed'
    finally: worker.stop()


def test_payload_limits_roll_back_enqueue(env):
    factory,p,auth=env
    cid=auth('member').post('/api/conversations',json={'title':'limits'}).json()['id']
    with factory.begin() as db:
        for i in range(33):
            r=m.Resource(name=str(i),org_id='a',kind='skill',enabled=True,config={'content':'---\nname: test\ndescription: test\n---\nhello'})
            db.add(r);db.flush();db.add(m.Binding(subject_type='user',subject_id=p['member'].id,resource_id=r.id))
    response=auth('member').post(f'/api/conversations/{cid}/messages',json={'content':'hello'})
    assert response.status_code==422,response.text
    with factory() as db:
        assert not db.scalar(select(m.Run.id).where(m.Run.conversation_id==cid))
        assert not db.scalar(select(m.Message.id).where(m.Message.conversation_id==cid))


def test_origin_and_config_validation(env):
    _,_,auth=env
    assert auth('admin').post('/api/conversations',json={'title':'x'},headers={'Origin':'https://evil.example'}).status_code==403
    for config in [{'url':'http://example.com'}, {'url':'https://127.0.0.1'}, {'command':'sh'}]:
        assert auth('admin').post('/api/resources',json={'name':'bad','kind':'mcp','org_id':'a','config':config}).status_code==422
    assert auth('admin').post('/api/resources',json={'name':'bad','kind':'skill','org_id':'a','config':{'content':'no frontmatter'}}).status_code==422
    assert auth('member').get('/api/unknown').status_code==404


def test_skill_body_create_without_overwriting_existing(env):
    from test_skill_content import prepare
    factory, _, auth = env
    generated = prepare([['test1', '111', '111', 'body']])[0]['content']
    payload = {'name': 'test1', 'description': '111', 'kind': 'skill', 'org_id': 'a', 'config': {'content': generated}}
    first = auth('admin').post('/api/resources', json=payload)
    second = auth('admin').post('/api/resources', json=payload)
    assert first.status_code in (200, 201), first.text
    assert second.status_code in (200, 201), second.text
    assert first.json()['id'] != second.json()['id']
    with factory() as db:
        assert db.get(m.Resource, first.json()['id']).config['content'] == generated
    payload['config']['content'] = '---\nname: test1\ndescription: 111\n---\n111'
    rejected = auth('admin').post('/api/resources', json=payload)
    assert rejected.status_code == 422
    assert '纯数字请加双引号' in rejected.json()['detail']



@pytest.mark.parametrize('actor,allowed', [('member',True),('root',False),('admin',False),('lead',False),('peer_member',False)])
def test_archive_private_permissions(env, actor, allowed):
    factory, p, auth = env
    cid = auth('member').post('/api/conversations', json={'title':'private'}).json()['id']
    listed = auth(actor).get('/api/conversations').json()
    entry = next((c for c in listed if c['id'] == cid), None)
    if entry: assert entry['can_delete'] == allowed
    assert auth(actor).delete('/api/conversations/' + cid).status_code == (200 if allowed else 403)


@pytest.mark.parametrize('actor,allowed', [('member',False),('root',True),('admin',True),('lead',True),('lead2',False),('other',False)])
def test_archive_group_permissions(env, actor, allowed):
    factory, p, auth = env
    with factory.begin() as db:
        group = m.Group(name='g',org_id='a',team_id='x',member_ids=[p['member'].id],provider='web')
        db.add(group); db.flush()
        conversation = m.Conversation(title='g',owner_id=p['member'].id,group_id=group.id)
        db.add(conversation); db.flush(); cid = conversation.id
    assert auth(actor).delete('/api/conversations/' + cid).status_code == (200 if allowed else 403)


def test_archive_blocks_all_access_retains_audit_and_history(env):
    factory, p, auth = env
    client = auth('member')
    cid = client.post('/api/conversations', json={'title':'retained'}).json()['id']
    result = client.post(f'/api/conversations/{cid}/messages', json={'content':'retained message'}).json()
    rid = result['run']['id']
    for status in ['queued','running']:
        with factory.begin() as db: db.get(m.Run,rid).status=status
        assert client.delete('/api/conversations/'+cid).status_code == 409
    with factory.begin() as db: db.get(m.Run,rid).status='succeeded'
    assert client.delete('/api/conversations/'+cid).status_code == 200
    for actor in ['member','root']:
        client=auth(actor)
        assert cid not in [c['id'] for c in client.get('/api/conversations').json()]
        for suffix in ['messages','state','capabilities']:
            assert client.get(f'/api/conversations/{cid}/{suffix}').status_code == 404
        assert client.get('/api/runs/'+rid).status_code == 404
        assert client.post(f'/api/conversations/{cid}/messages',json={'content':'blocked'}).status_code == 404
        assert client.delete('/api/conversations/'+cid).status_code == 404
    with factory() as db:
        assert db.get(m.Conversation,cid).archived_by == p['member'].id
        assert db.get(m.Message,result['user_message']['id']).content == 'retained message'
        assert db.get(m.Run,rid)
        assert db.scalar(select(m.Audit).where(m.Audit.action=='conversation.archive',m.Audit.target_id==cid))


def test_archive_im_reaction_cleanup_and_new_session(env):
    from app.im import _enqueue
    factory,p,auth=env
    assert auth('root').post('/api/identities',json={'provider':'feishu','external_user_id':'sender','user_id':p['member'].id}).status_code==201
    with factory.begin() as db: _enqueue(db,'feishu','old','sender','private','hello',False)
    with factory.begin() as db:
        run=db.scalar(select(m.Run)); cid,rid=run.conversation_id,run.id; run.status='succeeded'
    assert auth('member').delete('/api/conversations/'+cid).status_code==409
    with factory.begin() as db:
        row=db.scalar(select(m.IMReaction)); row.state='cleared'; row.message_id=None
    assert auth('member').delete('/api/conversations/'+cid).status_code==200
    with factory.begin() as db:
        assert _enqueue(db,'feishu','old','sender','private','hello',False)['duplicate']
        assert _enqueue(db,'feishu','new','sender','private','hello again',False)=={'ok':True}
        runs=list(db.scalars(select(m.Run)))
        assert len(runs)==2
        assert next(r for r in runs if r.id!=rid).conversation_id!=cid
        assert len(list(db.scalars(select(m.IMEvent))))==2


@pytest.mark.parametrize('winner', ['archive','enqueue'])
def test_archive_enqueue_lock_race(env,winner):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from fastapi import HTTPException
    factory,p,auth=env
    cid=auth('member').post('/api/conversations',json={'title':'race'}).json()['id']
    ready=Event()
    def contender():
        with factory.begin() as db:
            c=db.get(m.Conversation,cid)  # Cache before waiting: must refresh after lock.
            ready.set()
            try:
                if winner=='archive': service.enqueue_message(db,p['member'],c,'late')
                else: service.archive_conversation(db,p['member'],cid)
            except HTTPException as exc: return exc.status_code
    with ThreadPoolExecutor(max_workers=1) as pool:
        with factory.begin() as db:
            service.lock_conversation(db,cid)
            future=pool.submit(contender)
            assert ready.wait(5)
            if winner=='archive': service.archive_conversation(db,p['member'],cid)
            else: service.enqueue_message(db,p['member'],db.get(m.Conversation,cid),'first')
        assert future.result(timeout=5)==(404 if winner=='archive' else 409)


def test_archive_migration_existing_and_new_schema(env):
    from app.im_migrations import migrate
    from sqlalchemy import text, inspect
    factory,_,_=env
    engine=factory.kw['bind']
    with engine.begin() as conn:
        conn.execute(text('ALTER TABLE conversations DROP COLUMN archived_at'))
        conn.execute(text('ALTER TABLE conversations DROP COLUMN archived_by'))
    migrate(engine)
    migrate(engine)
    assert {'archived_at','archived_by'} <= {c['name'] for c in inspect(engine).get_columns('conversations')}
