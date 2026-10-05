"""Conversation-scoped attachment admission and atomic message claiming."""
from datetime import timedelta
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select, or_
from .db import get_db
from .security import current_user
from .models import Conversation, Run, User, now, uid
from .attachment_models import Attachment, AttachmentJob, AttachmentArtifact
from . import policy, attachment_storage as storage

router = APIRouter()
ACTIVE_RUNS = ('queued', 'running', 'waiting_attachments')


def public(item):
    return {key: getattr(item, key) for key in ('id', 'filename', 'size', 'mime', 'status', 'error')}


def authorize(db, item, *, read=False, actor=None):
    owner = db.get(User, item.uploader_id, populate_existing=True)
    conversation = db.get(Conversation, item.conversation_id, populate_existing=True)
    if conversation and conversation.group_id:
        from .models import Group
        db.get(Group, conversation.group_id, populate_existing=True)
    policy.require(owner and conversation and policy.can_send_conversation(db, owner, conversation), '附件授权已失效')
    policy.require(item.status != 'revoked', '附件已撤销')
    if item.expires_at and item.expires_at <= now():
        raise HTTPException(410, '附件草稿已过期')
    if item.run_id:
        from .service import build_payload
        run = db.get(Run, item.run_id)
        policy.require(run is not None)
        # This validates IM application/mapping without invoking attachment payload construction.
        build_payload(db, run, check_attachments=False)
    if actor:
        if read and item.message_id:
            policy.require(policy.can_read_conversation(db, actor, conversation))
        else:
            policy.require(actor.id == item.uploader_id and policy.can_send_conversation(db, actor, conversation))
    return conversation


def for_message(db, message_id):
    return [public(a) for a in db.scalars(select(Attachment).where(Attachment.message_id == message_id).order_by(Attachment.created_at))]


def claim(db, actor, conversation, message, run, identifiers):
    if not identifiers:
        return
    if len(identifiers) > storage.limit('MAX_FILES', 5) or len(set(identifiers)) != len(identifiers):
        raise HTTPException(422, '每条消息最多5个不重复附件')
    rows = list(db.scalars(select(Attachment).where(Attachment.id.in_(identifiers)).order_by(Attachment.id).with_for_update()))
    if len(rows) != len(identifiers):
        raise HTTPException(404, '附件不存在')
    for item in rows:
        authorize(db, item, actor=actor)
        if item.conversation_id != conversation.id or item.message_id or item.run_id:
            raise HTTPException(409, '附件已经发送或不属于当前会话')
        if item.status in ('failed', 'revoked'):
            raise HTTPException(422, '附件解析失败或已撤销，请移除后重试')
    for item in rows:
        item.message_id, item.run_id, item.expires_at = message.id, run.id, None
    if any(item.status != 'ready' for item in rows):
        run.status = 'waiting_attachments'


def run_attachments(db, run):
    rows = list(db.scalars(select(Attachment).where(Attachment.run_id == run.id, Attachment.message_id == run.message_id).order_by(Attachment.created_at)))
    for item in rows:
        authorize(db, item)
        if item.status != 'ready':
            raise HTTPException(409, '附件未完成解析或解析失败，任务不能继续')
    return rows


@router.post('/api/conversations/{identifier}/attachments', status_code=201)
async def upload(identifier: str, request: Request, actor=Depends(current_user), db=Depends(get_db, scope='function')):
    conversation = db.get(Conversation, identifier)
    policy.require(conversation and policy.can_send_conversation(db, actor, conversation))
    # Bound the entire multipart body before the multipart parser can spool arbitrary input.
    maximum = storage.limit('MAX_BYTES', 20 * 1024 * 1024)
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > maximum + 65536:
            raise HTTPException(413, '附件超过单文件大小限制（默认20MiB）')
    request._body = bytes(raw)
    async def replay():
        yield request._body
    request.stream = replay
    form = await request.form(max_files=1, max_fields=0, max_part_size=maximum)
    try:
        parts = form.getlist('file')
        if len(parts) != 1 or not hasattr(parts[0], 'file') or set(form) != {'file'}:
            raise HTTPException(422, '请选择一个附件上传')
        part = parts[0]
        item = Attachment(id=uid(), conversation_id=identifier, uploader_id=actor.id,
                          filename=storage.filename(part.filename), status='received',
                          expires_at=now() + timedelta(hours=storage.limit('DRAFT_HOURS', 24)))
        item.size, item.checksum = storage.save_stream(item.id, iter(lambda: part.file.read(65536), b''))
        # Recheck after receiving bytes; attachment parsing runs solely in the independent worker.
        actor = db.get(User, actor.id, populate_existing=True)
        db.refresh(conversation)
        policy.require(policy.can_send_conversation(db, actor, conversation))
        db.add(item)
        db.flush()
        db.add(AttachmentJob(attachment_id=item.id))
        from .service import audit
        audit(db, actor, 'attachment.upload', item.id, {'size': item.size})
        return public(item)
    finally:
        await form.close()


@router.get('/api/conversations/{identifier}/attachments')
def listing(identifier: str, actor=Depends(current_user), db=Depends(get_db, scope='function')):
    conversation = db.get(Conversation, identifier)
    policy.require(conversation and policy.can_read_conversation(db, actor, conversation))
    rows = db.scalars(select(Attachment).where(Attachment.conversation_id == identifier,
        or_(Attachment.message_id.is_not(None), Attachment.uploader_id == actor.id)).order_by(Attachment.created_at).limit(500))
    from .service import audit
    if not policy.can_send_conversation(db, actor, conversation):
        audit(db, actor, 'attachment.supervised_read', identifier)
    return [public(item) for item in rows if item.status != 'revoked']


@router.delete('/api/conversations/{identifier}/attachments/{attachment_id}')
def remove(identifier: str, attachment_id: str, actor=Depends(current_user), db=Depends(get_db, scope='function')):
    item = db.scalar(select(Attachment).where(Attachment.id == attachment_id).with_for_update())
    if not item or item.conversation_id != identifier:
        raise HTTPException(404, '附件不存在')
    authorize(db, item, actor=actor)
    if item.message_id:
        raise HTTPException(409, '已发送附件不能作为草稿移除')
    item.status, item.error = 'revoked', '上传者已移除附件'
    from .service import audit
    audit(db, actor, 'attachment.revoke', item.id)
    return {'ok': True}
