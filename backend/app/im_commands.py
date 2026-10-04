"""IM slash commands for already-authorized senders, plus the durable reply outbox.

Commands never create a Run or reach the model. Replies are queued in im_outbox inside
the ingress transaction and sent afterwards by a dedicated API thread, so no network call
happens inside ingress, and a long-running Codex task cannot delay /stop. A reply whose
send was interrupted is marked ambiguous and never replayed.
"""
import logging
import re
import threading

from fastapi import HTTPException
from sqlalchemy import select

from . import policy
from .db import SessionLocal
from .models import Conversation, IMEvent, IMOutbox, PlatformApproval, PlatformConnection, Run, now

log = logging.getLogger(__name__)
ACTIVE = ('queued', 'running', 'waiting_attachments')
ALIASES = {'/help': 'help', '/帮助': 'help', '/new': 'new', '/新会话': 'new', '/reset': 'new',
           '/stop': 'stop', '/停止': 'stop', '/status': 'status', '/状态': 'status'}
ROLES = {'super_admin': '超级管理员', 'org_admin': '组织管理员', 'team_lead': '团队负责人', 'member': '成员'}
STATES = {'connected': '已连接', 'pending': '待你确认', 'starting': '发起中'}
RUN_STATES = {'queued': '排队中', 'running': '执行中', 'waiting_attachments': '等待附件解析'}
HELP = ('可用指令：\n'
        '/help —— 查看本说明\n'
        '/new —— 开启新会话，之前的上下文不再带入（历史保留在 Hub）\n'
        '/stop —— 停止你排队中或执行中的任务\n'
        '/status —— 查看身份、当前任务、可用能力、本人授权和待确认操作\n'
        '/approve 审批码 —— 批准高风险操作（删除、覆盖整篇文档等）；/deny 审批码 拒绝\n\n'
        '也可以直接用自然语言：\n'
        '· 「新建一个飞书文档，标题××，内容××」\n'
        '· 「建一个飞书表格 / 飞书多维表格」\n'
        '· 「建一个钉钉文档 / 钉钉表格」（需先完成钉钉本人授权）\n'
        '· 「发起飞书授权」「查看我的授权状态」')


def parse(content):
    text = re.sub(r'^(?:@\S+\s+)+', '', (content or '').strip()).strip()
    if not text.startswith('/') or len(text) > 64:
        return None
    return ALIASES.get(text.split()[0].lower())


def _status(db, user, group, conversation):
    active = db.scalar(select(Run).where(Run.conversation_id == conversation.id, Run.status.in_(ACTIVE))
                       .order_by(Run.created_at.desc()).limit(1))
    resources = policy.effective_resources(db, user, conversation)
    skills = [r.name for r in resources if r.kind == 'skill']
    mcps = [r.name for r in resources if r.kind == 'mcp']
    states = {}
    for provider in ('feishu', 'dingtalk'):
        row = db.get(PlatformConnection, (user.id, provider))
        expired = bool(row and row.state == 'connected' and (not row.expires_at or row.expires_at <= now()))
        states[provider] = '未连接' if not row or expired else STATES.get(row.state, '未连接')
    waiting = list(db.scalars(select(PlatformApproval).where(
        PlatformApproval.user_id == user.id, PlatformApproval.conversation_id == conversation.id,
        PlatformApproval.state == 'pending', PlatformApproval.expires_at > now()).order_by(PlatformApproval.created_at)))
    return (f'身份：{user.name}（{ROLES.get(user.role, user.role)}）\n'
            f'会话：{"群聊「" + group.name + "」" if group else "私聊"}\n'
            f'当前任务：{RUN_STATES[active.status] if active else "无"}\n'
            + ''.join(f'待确认：{row.code} —— {row.summary[:60]}（/approve {row.code} 批准，/deny {row.code} 拒绝）\n' for row in waiting) +
            f'可用 Skill：{"、".join(skills) or "无"}\n'
            f'可用 MCP：{"、".join(mcps) or "无"}\n'
            f'本人授权：飞书 {states["feishu"]}，钉钉 {states["dingtalk"]}\n'
            '内置能力：创建、读取、修改飞书/钉钉的云文档与表格（飞书含多维表格）；删除、覆盖等高风险操作需你用 /approve 批准')


def _stop(db, user, group, conversation, provider):
    from .service import audit
    runs = list(db.scalars(select(Run).where(Run.conversation_id == conversation.id, Run.status.in_(ACTIVE)).with_for_update()))
    manager = bool(group and policy.can_manage_group(db, user, group))
    mine = [run for run in runs if run.user_id == user.id or manager]
    for run in mine:
        run.status, run.error = 'cancelled', 'Cancelled by IM command'
        audit(db, user, 'run.cancelled', run.id, {'source': provider})
    if mine:
        return f'已停止 {len(mine)} 个任务；执行中的任务不会再回复，也不能继续调用工具。'
    return '没有你可以停止的任务（群里他人的任务需群管理员停止）。' if runs else '当前没有排队或执行中的任务。'


def _new(db, user, conversation):
    from .service import archive_conversation
    try:
        archive_conversation(db, user, conversation.id)
    except HTTPException as exc:
        if exc.status_code == 409:
            return '当前会话还有排队或执行中的任务（或回复状态未清理完），请先发送 /stop 或稍后再试。', conversation
        return '群会话重置需要群管理员（或更高权限）操作。', conversation
    fresh = Conversation(title=conversation.title, owner_id=user.id, group_id=conversation.group_id)
    db.add(fresh)
    db.flush()
    return '已开启新会话，之前的上下文不会再带入；历史记录仍保留在 Hub。', fresh


def handle(db, command, provider, event, user, group, conversation):
    """Runs inside the ingress transaction; queues the reply for the outbox thread."""
    if command == 'help':
        text = HELP
    elif command == 'status':
        text = _status(db, user, group, conversation)
    elif command == 'stop':
        text = _stop(db, user, group, conversation, provider)
    else:
        text, conversation = _new(db, user, conversation)
        event.reply_target = {**event.reply_target, 'conversation_id': conversation.id}
    db.add(IMOutbox(event_id=event.id, text=text))
    db.flush()


def flush(limit=10):
    from .im import deliver_event
    with SessionLocal.begin() as db:
        rows = list(db.scalars(select(IMOutbox).where(IMOutbox.state == 'pending').order_by(IMOutbox.created_at)
                               .with_for_update(skip_locked=True).limit(limit)))
        for row in rows:
            row.state = 'sending'
        identifiers = [row.event_id for row in rows]
    for identifier in identifiers:
        with SessionLocal.begin() as db:
            row = db.get(IMOutbox, identifier, with_for_update=True)
            event = db.get(IMEvent, identifier, with_for_update=True)
            if not row or row.state != 'sending' or not event:
                continue
            deliver_event(db, event, row.text)
            row.state = 'sent' if event.delivered_at else 'failed'
    return len(identifiers)


def recover():
    # A send interrupted by a crash may or may not have reached the platform: never replay it.
    with SessionLocal.begin() as db:
        for row in db.scalars(select(IMOutbox).where(IMOutbox.state == 'sending').with_for_update()):
            row.state = 'ambiguous'


class OutboxWorker:
    def __init__(self):
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.loop, name='im-outbox-worker', daemon=True)

    def start(self):
        try:
            recover()
        except Exception:
            # Safe to continue: flush() only sends 'pending' rows, so 'sending' rows are never replayed.
            log.warning('IM outbox recovery failed')
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=30)

    def loop(self):
        while not self.stop_event.is_set():
            try:
                if flush():
                    continue
            except Exception:
                log.warning('IM outbox flush failed')
            self.stop_event.wait(0.5)
