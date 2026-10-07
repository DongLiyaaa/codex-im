"""Server-verified approval of risky platform actions.

A model-supplied "confirmed" flag proves nothing: a steered model (prompt injection in a document, a group
message) can set it. Instead the server stores the exact request, issues a short code bound to the
requester and conversation, and only that user's own chat message ("/approve CODE") releases it. The model
then triggers execution with the code alone and cannot change what was approved. Approvals are single use
and expire; the true summary is sent to the user by the server, not by the model.
"""
import hashlib
import json
import re
import secrets
from datetime import timedelta
from typing import NamedTuple

from sqlalchemy import select

from . import policy
from .models import IMEvent, IMOutbox, Message, PlatformApproval, now

TTL = timedelta(minutes=10)
MAX_PENDING = 5
CODE_LENGTH = 6
ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'  # No 0/O/1/I/L look-alikes: codes are typed by people.
VERBS = {'approve': 'approve', '批准': 'approve', '确认': 'approve', '同意': 'approve', 'deny': 'deny', '拒绝': 'deny'}
COMMAND = re.compile(r'(approve|deny|批准|确认|同意|拒绝)(?:\s+([A-Za-z0-9]{4,12}))?', re.IGNORECASE)
BUSY = '上一个任务还在处理中，暂时无法继续。请等它完成后再发送一次，审批码仍然有效。'
SHOWN = 80


class Decision(NamedTuple):
    verb: str  # 'approve' | 'deny'
    code: str | None


class Handled(NamedTuple):
    reply: str | None         # Text for the user when nothing else will answer.
    continuation: str | None  # A message to run as a normal turn so the model carries on.


def parse(content):
    text = re.sub(r'^(?:@\S+\s+)+', '', (content or '').strip()).strip()
    if not text.startswith('/') or len(text) > 40:
        return None
    match = COMMAND.fullmatch(text[1:])
    if not match:
        return None
    return Decision(VERBS[match.group(1).lower()], match.group(2).upper() if match.group(2) else None)


def _short(value):
    text = ', '.join(map(str, value)) if isinstance(value, list) else str(value)
    return text if len(text) <= SHOWN else text[:SHOWN] + '…'


def summarize(operation, provider, identity, args):
    """Built from the real request, never from model text."""
    platform = {'feishu': '飞书', 'dingtalk': '钉钉'}.get(provider, provider)
    who = '你本人身份' if identity == 'user' else '机器人身份'
    if operation == 'write':
        return f'{platform}：以{who}覆盖整篇文档 {_short(args.get("url", ""))}（新内容 {len(args.get("content") or "")} 字）'
    shown = '，'.join(f'{name}={_short(value)}' for name, value in sorted((args.get('flags') or {}).items()))
    body = f'，正文 {len(args["stdin"])} 字' if args.get('stdin') else ''
    target = f'，目标 {_short(args["target_url"])}' if args.get('target_url') else ''
    return f'{platform}：以{who}执行高风险命令 {" ".join(args.get("command") or [])}（{shown}{body}{target}）'


def notice(row):
    return (f'⚠️ 需要你确认的操作（审批码 {row.code}）\n{row.summary}\n\n'
            f'回复「/approve {row.code}」批准，或「/deny {row.code}」拒绝；{int(TTL.total_seconds() // 60)} 分钟内有效，只有你本人的回复有效。')


def public(row):
    """What the model may see: the code and the server-built summary, never the stored payload."""
    return {'approval_code': row.code, 'summary': row.summary, 'valid_minutes': int(TTL.total_seconds() // 60)}


def _error(code, next_action='ask_user'):
    from .platform_workspace import WorkspaceError
    return WorkspaceError(code, next_action)


def _close(row, state):
    row.state, row.payload, row.decided_at = state, {}, now()


def expire(db, user):
    for row in db.scalars(select(PlatformApproval).where(PlatformApproval.user_id == user.id,
            PlatformApproval.state.in_(('pending', 'approved')), PlatformApproval.expires_at <= now()).with_for_update()):
        _close(row, 'expired')


def cancel_all(db, user):
    """Closes every open approval of a user who lost access; the stored requests are discarded with them."""
    rows = list(db.scalars(select(PlatformApproval).where(PlatformApproval.user_id == user.id,
            PlatformApproval.state.in_(('pending', 'approved'))).with_for_update()))
    for row in rows:
        _close(row, 'denied')
    return len(rows)


def _code(db, user):
    while True:
        code = ''.join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
        if not db.scalar(select(PlatformApproval.id).where(PlatformApproval.user_id == user.id, PlatformApproval.code == code)):
            return code


def notify(db, run, row):
    """Tells the requester what is about to happen, from the server, so it cannot be misdescribed."""
    text = notice(row)
    db.add(Message(conversation_id=run.conversation_id, role='assistant', content=text))
    event = db.scalar(select(IMEvent).where(IMEvent.run_id == run.id))
    if event is not None:
        # A synthetic event with the same reply target reuses the outbox path and its delivery checks.
        clone = IMEvent(provider=event.provider, event_id=hashlib.sha256(('approval-notice:' + row.id).encode()).hexdigest(),
                        reply_target=dict(event.reply_target))
        db.add(clone)
        db.flush()
        db.add(IMOutbox(event_id=clone.id, text=text))
    db.flush()


def request(db, user, run, provider, operation, identity, args):
    """Stores a risky request and asks the user to approve it. Returns the pending approval."""
    from .service import audit
    expire(db, user)
    payload = {'operation': operation, 'provider': provider, 'identity': identity, 'args': args}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    pending = list(db.scalars(select(PlatformApproval).where(PlatformApproval.user_id == user.id,
                                                             PlatformApproval.state == 'pending').with_for_update()))
    for row in pending:
        if row.digest == digest and row.conversation_id == run.conversation_id:
            return row  # The model repeated itself; do not spam the user with a second code.
    if len(pending) >= MAX_PENDING:
        raise _error('too_many_approvals')
    row = PlatformApproval(code=_code(db, user), user_id=user.id, conversation_id=run.conversation_id, provider=provider,
                           operation=operation, digest=digest, summary=summarize(operation, provider, identity, args),
                           payload=payload, expires_at=now() + TTL)
    db.add(row)
    db.flush()
    audit(db, user, 'platform.approval.requested', provider, {'code': row.code, 'operation': operation, 'run_id': run.id,
          'command': ' '.join(args['command']) if isinstance(args.get('command'), list) else None})
    notify(db, run, row)
    return row


def handle(db, user, conversation, decision, busy=False):
    """The user's own /approve or /deny. Runs inside the ingress or web transaction."""
    from .service import audit
    if not policy.can_send_conversation(db, user, conversation):
        return Handled('没有权限处理这个会话里的操作。', None)
    expire(db, user)
    rows = list(db.scalars(select(PlatformApproval).where(PlatformApproval.user_id == user.id,
            PlatformApproval.conversation_id == conversation.id, PlatformApproval.state == 'pending')
            .order_by(PlatformApproval.created_at).with_for_update()))
    row = next((r for r in rows if r.code == decision.code), None) if decision.code else (rows[0] if len(rows) == 1 else None)
    if row is None:
        if decision.code:
            known = db.scalar(select(PlatformApproval).where(PlatformApproval.user_id == user.id,
                    PlatformApproval.conversation_id == conversation.id, PlatformApproval.code == decision.code))
            states = {'approved': '已经批准过了，正在执行或等待执行。', 'denied': '已经被拒绝。', 'consumed': '已经执行过了。', 'expired': '已过期，请让我重新发起。'}
            return Handled(f'审批码 {decision.code} ' + states.get(known.state, '无效。') if known else f'没有找到审批码 {decision.code}。', None)
        if rows:
            return Handled('有多个待确认的操作，请带上审批码：' + '、'.join(f'{r.code}（{r.summary[:40]}）' for r in rows), None)
        return Handled('当前没有待确认的操作。', None)
    if decision.verb == 'deny':
        _close(row, 'denied')
        audit(db, user, 'platform.approval.denied', row.provider, {'code': row.code})
        return Handled(f'已拒绝（{row.code}），该操作不会执行。', None)
    if busy:
        return Handled(BUSY, None)
    row.state, row.decided_at, row.expires_at = 'approved', now(), now() + TTL  # A fresh window to carry it out.
    audit(db, user, 'platform.approval.approved', row.provider, {'code': row.code})
    return Handled(None, f'我批准了操作 {row.code}：{row.summary}。请调用 run_approved_platform_action(approval_id="{row.code}") 执行，并简短汇报结果。')


def record_exchange(db, user, conversation, content, reply):
    """Web chat: show the decision and its outcome in the conversation when no model turn follows."""
    from . import schemas
    message = Message(conversation_id=conversation.id, role='user', content=content)
    db.add(message)
    db.flush()
    db.add(Message(conversation_id=conversation.id, role='assistant', content=reply))
    db.flush()
    return {'user_message': schemas.MessageOut.model_validate(message).model_dump(mode='json') | {'attachments': []}, 'run': None}


def load(db, user, run, code):
    """The approved request to execute for this run. Raises WorkspaceError with a precise reason otherwise."""
    expire(db, user)
    row = db.scalar(select(PlatformApproval).where(PlatformApproval.user_id == user.id, PlatformApproval.code == str(code).upper())
                    .with_for_update())
    if row is None or row.conversation_id != run.conversation_id:
        raise _error('approval_not_found')
    if row.state != 'approved':
        raise _error({'pending': 'approval_pending', 'denied': 'approval_denied', 'consumed': 'approval_used',
                      'expired': 'approval_expired'}.get(row.state, 'approval_not_found'),
                     'wait_for_user_approval' if row.state == 'pending' else 'ask_user')
    return row


def consume(db, user, row, run):
    from .service import audit
    command = row.payload.get('args', {}).get('command')
    audit(db, user, 'platform.approval.executed', row.provider, {'code': row.code, 'run_id': run.id,
          'command': ' '.join(command) if isinstance(command, list) else None})
    _close(row, 'consumed')
