import json
import asyncio
import importlib
import inspect
import ipaddress
import logging
import os
import re
import socket
import threading
from urllib.parse import urlsplit
import httpx
import yaml
from fastapi import HTTPException
from sqlalchemy import select, text, update
from .db import engine, SessionLocal
from .models import Audit, Conversation, Message, Run, User
from . import policy, schemas

log = logging.getLogger(__name__)
WORKER_LOCK = 731940281


def audit(db, actor, action, target_id=None, details=None):
    db.add(Audit(actor_id=actor.id if actor else None, action=action, target_id=target_id, details=details or {}))


def validate_mcp_url(url):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment
                or parsed.port not in (None, 443) or len(url) > 4096 or any(c.isspace() for c in url)):
            raise ValueError()
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise ValueError()
    except (ValueError, OSError):
        raise HTTPException(422, 'MCP URL must resolve exclusively to public HTTPS addresses')


def validate_resource(kind, config):
    if kind == 'skill':
        if set(config) != {'content'} or not isinstance(config.get('content'), str):
            raise HTTPException(422, 'Skill 配置只能包含字符串字段 content，请提交完整技能内容。')
        content = config['content']
        if len(content) > 64000:
            raise HTTPException(422, 'Skill 完整内容（含 YAML 头部）不能超过 64000 个字符，请缩短正文或描述。')
        match = re.match(r'\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)', content, re.S)
        try:
            front = yaml.safe_load(match.group(1)) if match else None
        except yaml.YAMLError:
            front = None
        if not isinstance(front, dict) or not all(isinstance(front.get(k), str) and front[k].strip() for k in ('name', 'description')):
            raise HTTPException(422, 'Skill 需要有效的 YAML 头部：首行和结束行使用 ---，并包含非空字符串 name 和 description；纯数字请加双引号。只有正文时，请选择“正文”模式并填写名称和描述。')
        if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', front['name']) or len(front['name']) > 64:
            raise HTTPException(422, 'Skill 头部 name 须为 1–64 个小写字母、数字或单连字符，不能以连字符开头或结尾；中文资源名称请使用“正文”模式自动生成技术标识。')
    else:
        if not set(config) <= {'url', 'headers'} or not isinstance(config.get('url'), str):
            raise HTTPException(422, 'MCP supports only url and headers; stdio is forbidden')
        headers = config.get('headers', {})
        if not isinstance(headers, dict) or len(headers) > 30 or any(
            not isinstance(k, str) or not re.fullmatch(r'[A-Za-z0-9-]{1,128}', k) or
            not isinstance(v, str) or len(v) > 8192 or any(ord(c) < 32 or ord(c) == 127 for c in v) or
            k.lower() in {'host', 'content-length', 'transfer-encoding', 'connection'} for k, v in headers.items()
        ):
            raise HTTPException(422, 'Invalid MCP headers')
        validate_mcp_url(config['url'])


def resource_out(resource, actor):
    config = {}
    if policy.can_manage_resource(actor, resource):
        config = dict(resource.config)
        if resource.kind == 'mcp':
            config['headers'] = {key: '***' for key in config.get('headers', {})}
    elif resource.kind == 'skill':
        config = {'content': resource.config.get('content', '')}
    return {key: getattr(resource, key) for key in ('id', 'name', 'kind', 'description', 'org_id', 'enabled')} | {'config': config}


def lock_group(db, identifier):
    from .models import Group
    group = db.scalar(select(Group).where(Group.id == identifier).with_for_update()
                      .execution_options(populate_existing=True))
    if not group or group.archived_at is not None:
        raise HTTPException(404, 'Not found')
    return group


def require_idle_group(db, identifier):
    from .models import IMEvent, IMReaction
    runs = select(Run.id).join(Conversation, Conversation.id == Run.conversation_id).where(Conversation.group_id == identifier)
    if db.scalar(select(Run.id).where(Run.id.in_(runs), Run.status.in_(['queued', 'running'])).limit(1)):
        raise HTTPException(409, '群组仍有排队或运行中的任务，请完成后再修改或删除。')
    if db.scalar(select(IMEvent.id).where(IMEvent.run_id.in_(runs), IMEvent.delivered_at.is_(None), IMEvent.delivery_error.is_(None)).limit(1)):
        raise HTTPException(409, '群组仍有待发送的回复，请完成后再修改或删除。')
    if db.scalar(select(IMReaction.event_id).join(IMEvent, IMEvent.id == IMReaction.event_id).where(
            IMEvent.run_id.in_(runs), IMReaction.state != 'cleared').limit(1)):
        raise HTTPException(409, '群组飞书工作表情尚未清理，请完成后再修改或删除。')


def lock_conversation(db, identifier):
    group_id = db.scalar(select(Conversation.group_id).where(Conversation.id == identifier))
    if group_id:
        lock_group(db, group_id)
    conversation = db.scalar(select(Conversation).where(Conversation.id == identifier)
        .with_for_update().execution_options(populate_existing=True))
    if conversation is None or conversation.archived_at is not None:
        raise HTTPException(404, 'Not found')
    return conversation


def archive_conversation(db, actor, identifier):
    from .models import IMEvent, IMReaction, now
    conversation = lock_conversation(db, identifier)
    policy.require(policy.can_delete_conversation(db, actor, conversation))
    if db.scalar(select(Run.id).where(Run.conversation_id == identifier,
            Run.status.in_(['queued', 'running'])).limit(1)):
        raise HTTPException(409, '会话仍有排队或运行中的任务，请完成后再移除。')
    if db.scalar(select(IMReaction.event_id).join(IMEvent, IMEvent.id == IMReaction.event_id)
            .join(Run, Run.id == IMEvent.run_id).where(Run.conversation_id == identifier,
                IMReaction.state != 'cleared').limit(1)):
        raise HTTPException(409, '飞书工作表情尚未清理，请等待清理完成后再移除。')
    conversation.archived_at, conversation.archived_by = now(), actor.id
    audit(db, actor, 'conversation.archive', identifier,
          {'group_id': conversation.group_id, 'owner_id': conversation.owner_id, 'history_retained': True})
    return {'ok': True}


def enqueue_message(db, user, conversation, content):
    # Caller owns the transaction; message, run, IM dedup and audit commit atomically.
    content = schemas.MessageCreate(content=content).content
    conversation = lock_conversation(db, conversation.id)
    policy.require(policy.can_send_conversation(db, user, conversation), 'Cannot send as another user')
    pending = db.scalar(select(Run.id).where(Run.conversation_id == conversation.id, Run.status.in_(['queued', 'running'])).limit(1))
    if pending:
        raise HTTPException(409, 'A run is already pending in this conversation')
    message = Message(conversation_id=conversation.id, role='user', content=content)
    db.add(message)
    db.flush()
    run = Run(conversation_id=conversation.id, user_id=user.id, message_id=message.id)
    db.add(run)
    db.flush()
    build_payload(db, run)
    audit(db, user, 'message.enqueue', conversation.id, {'run_id': run.id})
    return {'user_message': schemas.MessageOut.model_validate(message).model_dump(mode='json'),
            'run': schemas.RunOut.model_validate(run).model_dump(mode='json')}


def build_payload(db, run):
    user = db.get(User, run.user_id)
    conversation = db.get(Conversation, run.conversation_id)
    policy.require(user and conversation and policy.can_send_conversation(db, user, conversation))
    from .models import IMEvent
    from . import im_discovery, im_settings
    event = db.scalar(select(IMEvent).where(IMEvent.run_id == run.id))
    if event:
        target = event.reply_target
        with im_settings.snapshot(db, event.provider):
            app_scope = im_discovery.scope(event.provider)
        policy.require(target.get('app_scope') == app_scope)
        rejection, mapped_user, mapped_group = im_discovery.reason(db, event.provider, app_scope,
            target['sender_id'], target['chat_id'], bool(target.get('group_id')))
        policy.require(not rejection and mapped_user.id == user.id and
            (mapped_group.id if mapped_group else None) == conversation.group_id)
    resources = policy.effective_resources(db, user, conversation)
    skills, mcps = [], []
    for resource in resources:
        validate_resource(resource.kind, resource.config)
        if resource.kind == 'skill':
            skills.append({'name': resource.id, 'content': resource.config['content']})
        else:
            mcps.append({'name': resource.id, 'url': resource.config['url'], 'headers': resource.config.get('headers', {})})
    message = db.get(Message, run.message_id)
    history = list(db.scalars(select(Message).where(Message.conversation_id == conversation.id,
        Message.created_at <= message.created_at).order_by(Message.created_at.desc(), Message.id.desc()).limit(40)))
    parts, remaining = [], 48000
    for entry in history:
        piece = f'{entry.role}: {entry.content}'
        if len(piece) > remaining:
            break
        parts.append(piece)
        remaining -= len(piece)
    payload = {'run_id': run.id, 'conversation_id': conversation.id, 'prompt': '\n\n'.join(reversed(parts)), 'skills': skills, 'mcps': mcps}
    if len(skills) > 32 or len(mcps) > 16:
        raise HTTPException(422, 'Maximum 32 skills and 16 MCP resources per run')
    if len(json.dumps(payload, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')) > 512000:
        raise HTTPException(422, 'Combined runner payload exceeds 512000 bytes')
    return payload


def execute_run(run_id):
    try:
        with SessionLocal.begin() as db:
            run = db.get(Run, run_id)
            payload = build_payload(db, run)
        from .im_reactions import process
        process(run_id, create=True)
        runner_url = os.getenv('RUNNER_URL', 'http://127.0.0.1:18201').rstrip('/')
        token = os.getenv('RUNNER_TOKEN', '')
        if not token:
            raise RuntimeError('RUNNER_TOKEN is not configured')
        with httpx.Client(timeout=190, follow_redirects=False, trust_env=False) as client:
            with client.stream('POST', runner_url + '/execute', json=payload, headers={'Authorization': f'Bearer {token}'}) as response:
                response.raise_for_status()
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > 1024 * 1024:
                        raise RuntimeError('Runner response exceeded limit')
                import json
                result = json.loads(data)
        if not isinstance(result.get('text'), str) or not result['text'].strip():
            raise RuntimeError('Runner returned no text')
        with SessionLocal.begin() as db:
            run = db.get(Run, run_id)
            # Recheck state and grants before persisting an answer generated with those resources.
            current = build_payload(db, run)
            if current['skills'] != payload['skills'] or current['mcps'] != payload['mcps']:
                raise RuntimeError('Resource grants changed during execution')
            db.add(Message(conversation_id=run.conversation_id, role='assistant', content=result['text']))
            run.status = 'succeeded'
            audit(db, db.get(User, run.user_id), 'run.succeeded', run.id)
        deliver(run_id, result['text'])
    except Exception as exc:
        # Upstream bodies and URLs can contain credentials: expose only controlled errors.
        error = 'Runner execution failed; check runner configuration and logs'
        if isinstance(exc, HTTPException):
            error = 'Execution permission or resource validation failed'
        elif isinstance(exc, RuntimeError):
            error = str(exc)
        log.warning('Run %s failed (%s)', run_id, type(exc).__name__)
        with SessionLocal.begin() as db:
            run = db.get(Run, run_id)
            if run and run.status == 'running':
                run.status, run.error = 'failed', error
                audit(db, db.get(User, run.user_id), 'run.failed', run.id, {'error': error})
    finally:
        from .im_reactions import process
        process(run_id)


def deliver(run_id, reply):
    from .im_reactions import process
    process(run_id)
    try:
        module = importlib.import_module('.im', __package__)
    except ModuleNotFoundError as exc:
        if exc.name == f'{__package__}.im':
            return
        raise
    handler = getattr(module, 'deliver_reply', None)
    if not handler:
        return
    try:
        with SessionLocal.begin() as db:
            run = db.get(Run, run_id)
            result = handler(db, run, reply)
            if inspect.isawaitable(result):
                asyncio.run(result)
    except Exception:
        log.exception('IM delivery failed for run %s', run_id)
        with SessionLocal.begin() as db:
            audit(db, None, 'im.delivery_failed', run_id)


class Worker:
    def __init__(self):
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.loop, name='pg-run-worker', daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=200)

    def loop(self):
        while not self.stop_event.is_set():
            try:
                with engine.connect() as lock:
                    acquired = lock.scalar(text('SELECT pg_try_advisory_lock(:key)'), {'key': WORKER_LOCK})
                    lock.commit()
                    if not acquired:
                        self.stop_event.wait(2)
                        continue
                    try:
                        with SessionLocal.begin() as db:
                            db.execute(update(Run).where(Run.status == 'running').values(status='failed', error='Worker interrupted; manual retry required'))
                        while not self.stop_event.is_set():
                            lock.execute(text('SELECT 1'))
                            lock.commit()
                            from .im_reactions import recover
                            recover()
                            with SessionLocal.begin() as db:
                                run = db.scalar(select(Run).where(Run.status == 'queued').order_by(Run.created_at).with_for_update(skip_locked=True).limit(1))
                                run_id = run.id if run else None
                                if run:
                                    run.status = 'running'
                            if run_id:
                                execute_run(run_id)
                            else:
                                self.stop_event.wait(0.5)
                    finally:
                        lock.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': WORKER_LOCK})
                        lock.commit()
            except Exception:
                log.exception('Queue worker connection failure')
                self.stop_event.wait(2)
