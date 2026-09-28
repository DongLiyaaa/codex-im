"""Best-effort native Feishu reaction outbox. Never log remote bodies or IDs.

Typing is the documented keyboard/work emoji, not an invented WORKING enum.
Ambiguous creates are reconciled by listing only this app's Typing reaction.
"""
from datetime import timedelta
from urllib.parse import quote
import hashlib
import httpx
from sqlalchemy import select, func
from .db import SessionLocal
from .models import IMReaction, IMEvent, Run, now
from . import im, im_settings, im_discovery

EMOJI = 'Typing'
BASE = 'https://open.feishu.cn/open-apis/im/v1/messages/'


class ReactionError(Exception):
    def __init__(self, code):
        self.code = code


def request(client, method, url, token, **kwargs):
    response = client.request(method, url, headers={'Authorization': 'Bearer ' + token}, **kwargs)
    data = response.json()
    code = data.get('code') if isinstance(data, dict) else None
    if code != 0 or response.is_error:
        raise ReactionError('PERMISSION_REQUIRED' if code in (99991672, 99991679, 231002, 231008, 231018, 231020, 231021) else 'REACTION_API_FAILED')
    return data.get('data', {})


def process(run_id, create=False, factory=None):
    """Serialize across processes; commit intent before any HTTP side effect.

    Configuration session shared lock blocks config replacement until HTTP ends.
    Cleanup remains permitted after user grant revocation, only for our own mark.
    """
    factory = factory or SessionLocal
    try:
        with factory.kw['bind'].connect() as conn:
            key = int.from_bytes(hashlib.sha256(('reaction:' + run_id).encode()).digest()[:8], 'big', signed=True)
            if not conn.scalar(select(func.pg_try_advisory_lock(key))):
                conn.commit()
                return
            conn.commit()
            try:
                if not conn.scalar(select(func.pg_try_advisory_lock_shared(71901))):
                    conn.commit()
                    return
                conn.commit()
                try:
                    _process(conn, factory, run_id, create)
                finally:
                    conn.rollback()
                    conn.execute(select(func.pg_advisory_unlock_shared(71901)))
                    conn.commit()
            finally:
                conn.rollback()
                conn.execute(select(func.pg_advisory_unlock(key)))
                conn.commit()
    except Exception:
        # A status indicator must never block Codex or normal reply delivery.
        pass


def _process(conn, factory, run_id, create):
    with factory(bind=conn) as db:
        event = db.scalar(select(IMEvent).where(IMEvent.run_id == run_id))
        row = db.get(IMReaction, event.id) if event else None
        if not row or row.state == 'cleared':
            return
        values, _ = im_settings.effective(db, 'feishu')
        context = im_settings._active.set(values)
        try:
            if row.app_scope != im_discovery.scope('feishu') or row.app_scope != event.reply_target.get('app_scope'):
                row.error = 'APPLICATION_CHANGED'
                row.updated_at = now()
                db.commit()
                return
            run = db.get(Run, run_id)
            if create:
                if row.state != 'pending' or run.status != 'running':
                    return
                from .service import build_payload
                build_payload(db, run)
                row.state = 'creating'
            elif row.state == 'pending':
                row.state, row.message_id = 'cleared', None
                db.commit()
                return
            identifier, message_id, reaction_id = row.event_id, row.message_id, row.reaction_id
            row.updated_at = now()
            db.commit()
            url = BASE + quote(message_id, safe='') + '/reactions'
            try:
                with httpx.Client(timeout=3, follow_redirects=False, trust_env=False) as client:
                    token = im.access_token(client, 'feishu')
                    if create:
                        data = request(client, 'POST', url, token, json={'reaction_type': {'emoji_type': EMOJI}})
                        reaction_id = data.get('reaction_id')
                        if not isinstance(reaction_id, str) or not reaction_id or len(reaction_id) > 256:
                            raise ReactionError('REACTION_API_FAILED')
                    else:
                        ids = [reaction_id] if reaction_id else []
                        if not ids:
                            # A crash/timeout may occur after remote creation but before saving its ID.
                            cursor = None
                            for _ in range(3):
                                params = {'reaction_type': EMOJI, 'page_size': 50}
                                if cursor:
                                    params['page_token'] = cursor
                                data = request(client, 'GET', url, token, params=params)
                                ids.extend(item['reaction_id'] for item in data.get('items', [])
                                           if item.get('operator', {}).get('operator_type') == 'app'
                                           and item['operator'].get('operator_id') == values['FEISHU_APP_ID']
                                           and item.get('reaction_type', {}).get('emoji_type') == EMOJI)
                                if not data.get('has_more'):
                                    break
                                cursor = data.get('page_token')
                                if not cursor:
                                    raise ReactionError('RECONCILIATION_INCOMPLETE')
                            else:
                                raise ReactionError('RECONCILIATION_INCOMPLETE')
                        for rid in ids:
                            request(client, 'DELETE', url + '/' + quote(rid, safe=''), token)
                row = db.get(IMReaction, identifier)
                row.state, row.error = ('active' if create else 'cleared'), None
                row.reaction_id = reaction_id if create else None
                if not create:
                    row.message_id = None
            except Exception as exc:
                row = db.get(IMReaction, identifier)
                row.state = 'uncertain' if create else 'cleanup_pending'
                row.error = exc.code if isinstance(exc, ReactionError) else 'REACTION_UNAVAILABLE'
                # Reconcile a possibly already deleted reaction on the next retry.
                if not create:
                    row.reaction_id = None
            row.updated_at = now()
            db.commit()
        finally:
            im_settings._active.reset(context)


def recover(factory=None):
    factory = factory or SessionLocal
    try:
        with factory() as db:
            ids = list(db.scalars(select(IMEvent.run_id).join(IMReaction, IMReaction.event_id == IMEvent.id)
                .join(Run, Run.id == IMEvent.run_id).where(IMReaction.state != 'cleared',
                    Run.status.notin_(['queued', 'running']), IMReaction.updated_at < now() - timedelta(seconds=30)).limit(20)))
        for run_id in ids:
            process(run_id, factory=factory)
    except Exception:
        pass


def status(db):
    app_scope = im_discovery.scope('feishu')
    row = db.scalar(select(IMReaction).where(IMReaction.app_scope == app_scope).order_by(IMReaction.updated_at.desc()).limit(1))
    return {'supported': True, 'emoji_type': EMOJI, 'state': row.state if row else 'unverified',
            'error': row.error if row else None,
            'required_permissions': ['im:message.reactions:write_only', 'im:message.reactions:read'],
            'message': '飞书原生工作表情：请在开发者后台开通表情回复写入权限；读取权限用于超时或重启后的清理。创建并发布应用版本，由管理员审批。尚无新消息时未验收。'}
