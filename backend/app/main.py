import importlib
import os
import secrets
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from fastapi import Depends, FastAPI, HTTPException, Request, Response, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from sqlalchemy import select, func, or_, and_
from sqlalchemy.exc import IntegrityError
from .db import engine, SessionLocal, get_db
from .models import Base, User, Group, Resource, Binding, Conversation, Message, Run, Audit, Identity, SessionToken, now
from .security import current_user, hash_password, verify_password, token_hash, session_secret, rate_limit
from . import policy, schemas, service, im_settings


@asynccontextmanager
async def lifespan(app):
    session_secret()
    Base.metadata.create_all(engine, tables=[table for table in Base.metadata.sorted_tables if table.name not in ('im_reactions', 'organizations', 'departments', 'platform_settings', 'platform_auth_jobs', 'platform_auth_requests', 'im_chat_names', 'im_outbox', 'platform_approvals', 'im_onboarding_policies')])
    from .im_migrations import migrate
    migrate(engine)
    with SessionLocal.begin() as db:
        from .setup import initialized, create_admin
        if not initialized(db):
            email = os.getenv('BOOTSTRAP_ADMIN_EMAIL', '').strip().lower()
            password = os.getenv('BOOTSTRAP_ADMIN_PASSWORD', '')
            if email or password:
                try:
                    body = schemas.SetupAdmin(email=email, password=password, name='Super Admin')
                except ValueError:
                    raise RuntimeError('Bootstrap configuration invalid (valid email and password minimum 12 required)') from None
                create_admin(db, body)
    worker = service.Worker()
    worker.start()
    app.state.worker = worker
    from .platform_worker import AuthWorker
    auth_worker = AuthWorker()
    auth_worker.start()
    from .im_commands import OutboxWorker
    outbox_worker = OutboxWorker()
    outbox_worker.start()
    yield
    outbox_worker.stop()
    auth_worker.stop()
    worker.stop()


app = FastAPI(title='Agent Hub API', lifespan=lifespan)
from .platform_bridge import router as platform_router
from .attachments import router as attachment_router
from .attachment_bridge import router as attachment_bridge_router
app.include_router(attachment_bridge_router)
app.include_router(attachment_router)
app.include_router(platform_router)


@app.middleware('http')
async def guard_origin(request: Request, call_next):
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        origin = request.headers.get('origin')
        expected = os.getenv('APP_ORIGIN', 'http://127.0.0.1:18200').rstrip('/')
        if origin is not None and origin != expected:
            return JSONResponse({'detail': 'Origin rejected'}, status_code=403)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'same-origin'
    if request.url.path.startswith(('/api/', '/internal/platform-mcp')):
        response.headers['Cache-Control'] = 'no-store'
    return response


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    if request.url.path.startswith('/api/setup') or request.url.path == '/api/auth/login':
        return JSONResponse({'detail': 'Invalid authentication request'}, status_code=422)
    if request.url.path.startswith(('/api/integrations/config/', '/api/integrations/oauth/')):
        return JSONResponse({'detail': 'Invalid IM configuration request'}, status_code=422)
    return await request_validation_exception_handler(request, exc)


@app.exception_handler(IntegrityError)
async def conflict(request, exc):
    return JSONResponse({'detail': 'Conflicting or invalid record'}, status_code=409)


def get_or_404(db, model, identifier):
    obj = db.get(model, identifier)
    if obj is None or (isinstance(obj, Conversation) and obj.archived_at is not None):
        raise HTTPException(404, 'Not found')
    return obj


def read_conversation(db, actor, identifier):
    conversation = get_or_404(db, Conversation, identifier)
    policy.require(policy.can_read_conversation(db, actor, conversation))
    if not policy.can_send_conversation(db, actor, conversation):
        service.audit(db, actor, 'conversation.supervised_read', conversation.id)
    return conversation


@app.get('/api/health')
def health(db=Depends(get_db)):
    from sqlalchemy import text
    db.execute(text('SELECT 1'))
    return {'status': 'ok'}


@app.get('/api/setup/status', response_model=bool)
def setup_status(db=Depends(get_db)):
    from .setup import initialized
    return initialized(db)


@app.post('/api/setup/bootstrap', status_code=201)
def setup_bootstrap(body: schemas.SetupAdmin, request: Request, db=Depends(get_db)):
    from .setup import create_admin
    rate_limit('setup:' + (request.client.host if request.client else 'unknown'))
    create_admin(db, body)
    db.commit()
    return {'ok': True}


@app.post('/api/auth/login', response_model=schemas.UserOut)
def login(body: schemas.Login, request: Request, response: Response, db=Depends(get_db)):
    rate_limit(request.client.host if request.client else 'unknown')
    user = db.scalar(select(User).where(User.email == body.email))
    encoded = user.password_hash if user else hash_password('dummy-password')
    if not verify_password(body.password, encoded) or not user or not user.active:
        raise HTTPException(401, 'Invalid credentials')
    old = request.cookies.get('hub_session')
    if old:
        previous = db.get(SessionToken, token_hash(old))
        if previous:
            db.delete(previous)
    token = secrets.token_urlsafe(48)
    db.add(SessionToken(token_hash=token_hash(token), user_id=user.id, expires_at=now() + timedelta(hours=12)))
    service.audit(db, user, 'auth.login', user.id)
    response.set_cookie('hub_session', token, httponly=True, samesite='strict', secure=os.getenv('APP_ORIGIN', '').startswith('https://'), max_age=43200, path='/')
    return user


@app.post('/api/auth/logout')
def logout(request: Request, response: Response, db=Depends(get_db)):
    token = request.cookies.get('hub_session')
    session = db.get(SessionToken, token_hash(token)) if token else None
    if session:
        db.delete(session)
    response.delete_cookie('hub_session', path='/', httponly=True, samesite='strict')
    return {'ok': True}


@app.get('/api/auth/me', response_model=schemas.UserOut)
def me(actor=Depends(current_user)):
    return actor


def user_out(actor, user):
    result = schemas.UserOut.model_validate(user)
    result.can_manage = policy.can_manage_user(actor, user)
    return result


@app.get('/api/users', response_model=list[schemas.UserOut])
def users(actor=Depends(current_user), db=Depends(get_db)):
    return [user_out(actor, u) for u in db.scalars(select(User)) if u.id == actor.id or policy.can_manage_user(actor, u)]


@app.post('/api/users', response_model=schemas.UserOut, status_code=201)
def create_user(body: schemas.UserCreate, actor=Depends(current_user), db=Depends(get_db)):
    from .directory import validate_scope
    validate_scope(db, body.org_id, body.team_id)
    target = User(**body.model_dump(exclude={'password'}), password_hash=hash_password(body.password))
    policy.require(policy.can_manage_user(actor, target), 'Can only create lower-ranked users in your scope')
    db.add(target)
    db.flush()
    service.audit(db, actor, 'user.create', target.id)
    return user_out(actor, target)


@app.patch('/api/users/{identifier}', response_model=schemas.UserOut)
def update_user(identifier: str, body: schemas.UserUpdate, actor=Depends(current_user), db=Depends(get_db)):
    from . import user_lifecycle
    target = user_lifecycle.manageable(db, actor, identifier)
    user_lifecycle.apply(db, actor, target, body)
    return user_out(actor, target)


def group_out(db, actor, group):
    from .models import Organization, Department
    result = schemas.GroupOut.model_validate(group)
    result.can_edit = result.can_delete = policy.can_manage_group(db, actor, group)
    org, team = db.get(Organization, group.org_id), db.get(Department, group.team_id) if group.team_id else None
    result.org_name = org.name if org else group.org_id
    result.team_name = team.name if team and team.org_id == group.org_id else group.team_id
    return result


@app.get('/api/groups', response_model=list[schemas.GroupOut])
def groups(actor=Depends(current_user), db=Depends(get_db)):
    return [group_out(db, actor, g) for g in db.scalars(select(Group)) if policy.can_read_group(db, actor, g)]


@app.patch('/api/groups/{identifier}', response_model=schemas.GroupOut)
def edit_group(identifier: str, body: schemas.GroupUpdate, actor=Depends(current_user), db=Depends(get_db)):
    group = service.lock_group(db, identifier)
    policy.require(policy.can_manage_group(db, actor, group))
    service.require_idle_group(db, identifier)
    for member_id in body.member_ids:
        member = get_or_404(db, User, member_id)
        policy.require(policy.in_group_scope(member, group), 'Member outside group scope')
        policy.require(member.id == actor.id or policy.can_manage_user(actor, member))
    previous = list(group.member_ids)
    group.name, group.member_ids = body.name, body.member_ids
    service.audit(db, actor, 'group.update', identifier, {'previous_member_ids': previous, 'member_ids': group.member_ids})
    db.flush()
    return group_out(db, actor, group)


@app.delete('/api/groups/{identifier}')
def delete_group(identifier: str, actor=Depends(current_user), db=Depends(get_db)):
    group = service.lock_group(db, identifier)
    policy.require(policy.can_manage_group(db, actor, group))
    service.require_idle_group(db, identifier)
    group.archived_at, group.archived_by = now(), actor.id
    service.audit(db, actor, 'group.archive', identifier, {'history_retained': True, 'external_mapping_disabled': True})
    return {'ok': True}


@app.post('/api/groups', response_model=schemas.GroupOut, status_code=201)
def create_group(body: schemas.GroupCreate, actor=Depends(current_user), db=Depends(get_db)):
    from .directory import validate_scope
    group = Group(**body.model_dump())
    if group.provider != 'web':
        policy.require(actor.role == 'super_admin', 'Global IM mappings require super_admin')
        from .im_discovery import configuration_lock
        configuration_lock(db, group.provider)
    validate_scope(db, body.org_id, body.team_id)
    for identifier in group.member_ids:
        member = get_or_404(db, User, identifier)
        policy.require(policy.in_group_scope(member, group), 'Member outside group scope')
    policy.require(policy.can_manage_group(db, actor, group))
    db.add(group)
    db.flush()
    if group.provider != 'web':
        from .im_discovery import pin, scope
        with im_settings.snapshot(db, group.provider):
            pin(db, 'group', group.id, scope(group.provider))
    service.audit(db, actor, 'group.create', group.id)
    return group


def visible_resources(db, actor):
    granted = set(db.scalars(select(Binding.resource_id).where(Binding.subject_type == 'user', Binding.subject_id == actor.id)))
    return [r for r in db.scalars(select(Resource)) if policy.can_manage_resource(actor, r) or
        (r.enabled and r.id in granted and (r.org_id is None or r.org_id == actor.org_id))]


@app.get('/api/resources')
def resources(actor=Depends(current_user), db=Depends(get_db)):
    return [service.resource_out(r, actor) for r in visible_resources(db, actor)]


@app.post('/api/resources', status_code=201)
def create_resource(body: schemas.ResourceCreate, actor=Depends(current_user), db=Depends(get_db)):
    from .directory import validate_scope
    validate_scope(db, body.org_id, None)
    resource = Resource(**body.model_dump())
    policy.require(policy.can_manage_resource(actor, resource))
    service.validate_resource(body.kind, body.config)
    db.add(resource)
    db.flush()
    service.audit(db, actor, 'resource.create', resource.id)
    return service.resource_out(resource, actor)


@app.get('/api/bindings', response_model=list[schemas.BindingOut])
def bindings(actor=Depends(current_user), db=Depends(get_db)):
    return [b for b in db.scalars(select(Binding)) if policy.can_manage_binding(db, actor, b) or (b.subject_type == 'user' and b.subject_id == actor.id)]


@app.post('/api/bindings', response_model=schemas.BindingOut, status_code=201)
def create_binding(body: schemas.BindingCreate, actor=Depends(current_user), db=Depends(get_db)):
    binding = Binding(**body.model_dump())
    if binding.subject_type == 'group':
        service.lock_group(db, binding.subject_id)
    policy.require(policy.can_manage_binding(db, actor, binding))
    db.add(binding)
    db.flush()
    service.audit(db, actor, 'binding.create', binding.id)
    return binding


@app.delete('/api/bindings/{identifier}')
def delete_binding(identifier: str, actor=Depends(current_user), db=Depends(get_db)):
    binding = get_or_404(db, Binding, identifier)
    policy.require(policy.can_manage_binding(db, actor, binding))
    db.delete(binding)
    service.audit(db, actor, 'binding.delete', identifier)
    return {'ok': True}


@app.get('/api/identities', response_model=list[schemas.IdentityOut])
def identities(actor=Depends(current_user), db=Depends(get_db)):
    return [i for i in db.scalars(select(Identity)) if (target := db.get(User, i.user_id)) and
            (i.user_id == actor.id or policy.can_manage_user(actor, target))]


@app.post('/api/identities', response_model=schemas.IdentityOut, status_code=201)
def create_identity(body: schemas.IdentityCreate, actor=Depends(current_user), db=Depends(get_db)):
    policy.require(actor.role == 'super_admin', 'Global IM mappings require super_admin')
    target = get_or_404(db, User, body.user_id)
    policy.require(target.active and (policy.can_manage_user(actor, target) or
        (target.id == actor.id and actor.role in ('super_admin', 'org_admin'))))
    from .im_discovery import configuration_lock, pin, scope
    configuration_lock(db, body.provider)
    identity = Identity(**body.model_dump())
    db.add(identity)
    db.flush()
    with im_settings.snapshot(db, body.provider):
        pin(db, 'identity', identity.id, scope(body.provider))
    service.audit(db, actor, 'identity.create', identity.id)
    return identity


def conversation_out(db, actor, conversation):
    result = schemas.ConversationOut.model_validate(conversation)
    result.can_delete = policy.can_delete_conversation(db, actor, conversation)
    return result


@app.delete('/api/conversations/{identifier}')
def delete_conversation(identifier: str, actor=Depends(current_user), db=Depends(get_db)):
    return service.archive_conversation(db, actor, identifier)


@app.get('/api/conversations', response_model=list[schemas.ConversationOut])
def conversations(actor=Depends(current_user), db=Depends(get_db)):
    return [conversation_out(db, actor, c) for c in db.scalars(select(Conversation).where(Conversation.archived_at.is_(None)).order_by(Conversation.created_at.desc())) if policy.can_read_conversation(db, actor, c)]


@app.post('/api/conversations', response_model=schemas.ConversationOut, status_code=201)
def create_conversation(body: schemas.ConversationCreate, actor=Depends(current_user), db=Depends(get_db)):
    if body.group_id:
        group = service.lock_group(db, body.group_id)
        policy.require(policy.is_group_member(actor, group))
    conversation = Conversation(**body.model_dump(), owner_id=actor.id)
    db.add(conversation)
    db.flush()
    service.audit(db, actor, 'conversation.create', conversation.id)
    return conversation_out(db, actor, conversation)


@app.get('/api/conversations/{identifier}/messages', response_model=list[schemas.MessageOut])
def messages(identifier: str, actor=Depends(current_user), db=Depends(get_db)):
    read_conversation(db, actor, identifier)
    from .attachments import for_message
    entries = list(db.scalars(select(Message).where(Message.conversation_id == identifier).order_by(Message.created_at, Message.id)))
    from .platform_broker import markers
    return markers(db, actor, [schemas.MessageOut.model_validate(m).model_dump() | {'attachments': for_message(db, m.id)} for m in entries])


@app.get('/api/conversations/{identifier}/state', response_model=schemas.ConversationStateOut)
def conversation_state(identifier: str, actor=Depends(current_user), db=Depends(get_db)):
    # Status polling is read-only; message-open auditing remains on /messages.
    conversation = get_or_404(db, Conversation, identifier)
    policy.require(policy.can_read_conversation(db, actor, conversation))
    from .models import IMEvent
    row = db.execute(select(Run, IMEvent.provider).outerjoin(IMEvent, IMEvent.run_id == Run.id)
        .where(Run.conversation_id == identifier).order_by(Run.created_at.desc(), Run.id.desc()).limit(1)).first()
    latest = None
    if row:
        run, provider = row
        latest = schemas.ConversationRunOut(**schemas.RunOut.model_validate(run).model_dump(),
                                           provider=provider if provider in ('feishu', 'dingtalk') else 'web')
    entries = list(db.scalars(select(Message).where(Message.conversation_id == identifier)
                             .order_by(Message.created_at, Message.id)))
    from .attachments import for_message
    from .platform_broker import markers
    entries = markers(db, actor, [schemas.MessageOut.model_validate(m).model_dump() | {'attachments': for_message(db, m.id)} for m in entries])
    return {'messages': entries, 'latest_run': latest,
            'active_run': latest if latest and latest.status in ('queued', 'running', 'waiting_attachments') else None}


@app.post('/api/conversations/{identifier}/messages', status_code=202)
def send_message(identifier: str, body: schemas.MessageCreate, actor=Depends(current_user), db=Depends(get_db)):
    conversation = get_or_404(db, Conversation, identifier)
    from . import approvals
    decision = None if body.attachment_ids else approvals.parse(body.content)
    if decision:
        # "/approve CODE" released by the signed-in user themselves; same rules as in IM.
        busy = bool(db.scalar(select(Run.id).where(Run.conversation_id == conversation.id,
                                                   Run.status.in_(['queued', 'running', 'waiting_attachments'])).limit(1)))
        handled = approvals.handle(db, actor, conversation, decision, busy)
        if handled.continuation is None:
            return approvals.record_exchange(db, actor, conversation, body.content, handled.reply)
        return service.enqueue_message(db, actor, conversation, handled.continuation)
    return service.enqueue_message(db, actor, conversation, body.content, body.attachment_ids)


@app.get('/api/runs/{identifier}', response_model=schemas.RunOut)
def run_status(identifier: str, actor=Depends(current_user), db=Depends(get_db)):
    run = get_or_404(db, Run, identifier)
    read_conversation(db, actor, run.conversation_id)
    return run


@app.get('/api/conversations/{identifier}/capabilities')
def capabilities(identifier: str, actor=Depends(current_user), db=Depends(get_db)):
    conversation = read_conversation(db, actor, identifier)
    result = {'skills': [], 'mcps': []}
    for resource in policy.effective_resources(db, actor, conversation):
        result['skills' if resource.kind == 'skill' else 'mcps'].append(service.resource_out(resource, actor))
    return result


@app.get('/api/overview')
def overview(actor=Depends(current_user), db=Depends(get_db)):
    visible = conversations(actor, db)
    ids = [c.id for c in visible]
    return {'users': len(users(actor, db)), 'groups': len(groups(actor, db)), 'conversations': len(ids),
            'runs': len(list(db.scalars(select(Run.id).where(Run.conversation_id.in_(ids))))) if ids else 0}


@app.get('/api/audit')
def audit_events(actor=Depends(current_user), db=Depends(get_db)):
    policy.require(actor.role != 'member')
    allowed = [u.id for u in users(actor, db)]
    query = select(Audit).order_by(Audit.created_at.desc()).limit(500)
    if actor.role != 'super_admin':
        query = query.where(Audit.actor_id.in_(allowed))
    return [{key: getattr(a, key) for key in ('id', 'actor_id', 'action', 'target_id', 'details', 'created_at')} for a in db.scalars(query)]


@app.get('/api/audit/page')
def audit_page(page: int = Query(1, ge=1, le=2147483647), page_size: int = Query(50),
               actor=Depends(current_user), db=Depends(get_db)):
    policy.require(actor.active and actor.role != 'member')
    if page_size not in (50, 100):
        raise HTTPException(422, 'page_size must be 50 or 100')
    query = select(Audit)
    if actor.role != 'super_admin':
        lower_roles = [role for role, rank in policy.RANK.items() if rank < policy.RANK[actor.role]]
        managed = and_(User.org_id == actor.org_id, User.role.in_(lower_roles)) if actor.org_id else False
        if actor.role == 'team_lead':
            managed = and_(managed, User.team_id == actor.team_id) if actor.team_id else False
        allowed = select(User.id).where(or_(User.id == actor.id, managed))
        query = query.where(Audit.actor_id.in_(allowed))
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, pages)
    rows = db.scalars(query.order_by(Audit.created_at.desc(), Audit.id.desc())
                      .offset((page - 1) * page_size).limit(page_size))
    return {'items': [{key: getattr(row, key) for key in
            ('id', 'actor_id', 'action', 'target_id', 'details', 'created_at')} for row in rows],
            'total': total, 'page': page, 'page_size': page_size, 'pages': pages}


def platform_admin(actor=Depends(current_user)):
    policy.require(actor.role == 'super_admin')
    return actor


@app.get('/api/integrations/config/{provider}')
def integration_config(provider: str, actor=Depends(platform_admin), db=Depends(get_db)):
    return im_settings.view(db, provider)


@app.put('/api/integrations/config/{provider}')
def save_integration_config(provider: str, body: im_settings.Update, actor=Depends(platform_admin), db=Depends(get_db)):
    result = im_settings.save(db, provider, body)
    service.audit(db, actor, 'im.configuration.update', provider, {'revision': result['revision']})
    return result


@app.get('/api/integrations/status')
def integrations(actor=Depends(platform_admin), db=Depends(get_db)):
    from .models import IMConnection
    result = {'runner': {'configured': bool(os.getenv('RUNNER_TOKEN'))}}
    for provider in ('feishu', 'dingtalk'):
        with im_settings.snapshot(db, provider) as values:
            config = im.configuration(provider)
            if provider == 'feishu' and config['configured']:
                from .im_reactions import status
                config['native_work_status'] = status(db)
            elif provider == 'dingtalk':
                config['native_work_status'] = {'supported': False, 'state': 'unsupported',
                    'message': '当前机器人官方接口未证实支持用户消息下的工作表情。卡片替代需单独配置与产品确认。'}
        if config['configured'] and config['transport'] != 'webhook':
            row = db.get(IMConnection, provider)
            if row and row.transport == config['transport']:
                # Heartbeat state is bound to the full effective configuration.
                state, _, fingerprint = row.state.partition(':')
                if fingerprint == im_settings.fingerprint(values):
                    config['state'] = state if (now() - row.updated_at).total_seconds() < 20 else 'stale'
                elif row.state == 'configuration_changed':
                    config['state'] = 'configuration_changed'
        result[provider] = config
    return result


try:
    im = importlib.import_module('.im', __package__)
except ModuleNotFoundError as exc:
    if exc.name != f'{__package__}.im':
        raise
else:
    app.include_router(im.router)
    from .im_discovery import router as discovery_router
    from .im_onboarding import router as onboarding_router
    app.include_router(discovery_router)
    app.include_router(onboarding_router)

from .directory import router as directory_router
app.include_router(directory_router)

# API routes and IM callbacks are registered before the SPA fallback.
STATIC_DIR = Path(os.getenv('STATIC_DIR', str(Path(__file__).resolve().parents[2] / 'frontend' / 'dist'))).resolve()


@app.get('/{path:path}', include_in_schema=False)
def frontend(path: str):
    if path == 'api' or path.startswith('api/'):
        raise HTTPException(404, 'Not found')
    target = (STATIC_DIR / path).resolve()
    if not target.is_relative_to(STATIC_DIR):
        raise HTTPException(404, 'Not found')
    if target.is_file():
        return FileResponse(target)
    index = STATIC_DIR / 'index.html'
    if index.is_file() and not Path(path).suffix:
        return FileResponse(index)
    raise HTTPException(404, 'Frontend has not been built or file not found')
