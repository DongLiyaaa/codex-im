"""Official SDK adapters. Run outside the API; never log payloads or SDK errors."""
import argparse
import asyncio
import json
import logging
import os
import threading
import subprocess
import sys
import time
import signal
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.dialects.postgresql import insert

from . import im, im_settings
from .db import SessionLocal, engine
from .models import IMConnection


def receive_feishu(payload, factory=SessionLocal):
    try:
        if payload['header'].get('event_type') != 'im.message.receive_v1':
            return {'ok': True, 'ignored': True}
        event = payload['event']
        message, sender = event['message'], event['sender']
        if sender.get('sender_type') != 'user':
            return {'ok': True, 'ignored': True}
        if message.get('chat_type') not in ('p2p', 'group'):
            im._reject(400)
        from . import im_inbound
        # Network lookups (bot identity, quoted message) happen before the database transaction opens.
        inbound = im_inbound.feishu(message)
        if inbound is None:
            return {'ok': True, 'ignored': True}
        with factory.begin() as db:
            ensure_current(db, 'feishu')
            return im._enqueue(db, 'feishu', message['message_id'], sender['sender_id']['open_id'],
                               message['chat_id'], inbound.content,
                               message['chat_type'] == 'group', reply_mode='websocket', notice=inbound.notice,
                               sender_internal=im_inbound.feishu_internal(payload['header'], sender),
                               **({'attachment_refs': inbound.refs} if inbound.refs else {}))
    except (KeyError, TypeError, AttributeError, ValueError):
        im._reject(400)


def receive_dingtalk(payload, factory=SessionLocal):
    from dingtalk_stream import ChatbotMessage
    try:
        # Do not parse/store sessionWebhook or arbitrary extension fields.
        data = {key: payload[key] for key in ('msgtype', 'msgId', 'senderStaffId', 'conversationId',
                'conversationType', 'text', 'content', 'robotCode', 'isInAtList') if key in payload}
        message = ChatbotMessage.from_dict(data)
        from . import im_inbound
        inbound = im_inbound.dingtalk(data)
        if inbound is None:
            return {'ok': True, 'ignored': True}
        if str(message.conversation_type) not in ('1', '2'):
            im._reject(400)
        if message.robot_code != im._required('DINGTALK_ROBOT_CODE'):
            im._reject()
        with factory.begin() as db:
            ensure_current(db, 'dingtalk')
            return im._enqueue(db, 'dingtalk', message.message_id, message.sender_staff_id,
                               message.conversation_id, inbound.content,
                               str(message.conversation_type) == '2', reply_mode='stream', nickname=payload.get('senderNick'),
                               chat_name=payload.get('conversationTitle'), notice=inbound.notice,
                               sender_internal=im_inbound.dingtalk_internal(payload), corp_id=payload.get('chatbotCorpId'),
                               **({'attachment_refs': inbound.refs} if inbound.refs else {}))
    except (KeyError, TypeError, AttributeError, ValueError):
        im._reject(400)


def ensure_current(db, provider):
    from .im_discovery import configuration_lock
    configuration_lock(db, provider)
    expected = os.environ.get('IM_CONFIG_FINGERPRINT')
    if expected:
        values, _ = im_settings.effective(db, provider)
        if expected != im_settings.fingerprint(values):
            raise HTTPException(503, 'IM configuration changed')


def safe_receive(receiver, payload):
    try:
        receiver(payload)
        return 200
    except HTTPException as exc:
        # Permanent rejection is acknowledged without authorizing or storing it.
        return 200 if exc.status_code in (400, 403) else 500
    except Exception:
        return 500


def socket_open(connection):
    if connection is None:
        return False
    state = getattr(connection, 'state', None)
    return getattr(state, 'name', None) == 'OPEN'


def record_state(provider, mode, state):
    state = state + ':' + os.environ.get('IM_CONFIG_FINGERPRINT', '')
    with SessionLocal.begin() as db:
        statement = insert(IMConnection).values(provider=provider, transport=mode, state=state,
                                                updated_at=datetime.now(timezone.utc))
        db.execute(statement.on_conflict_do_update(index_elements=['provider'], set_={
            'transport': statement.excluded.transport, 'state': statement.excluded.state,
            'updated_at': statement.excluded.updated_at}))


def run(provider):
    config = im.configuration(provider)
    if not config['configured'] or config['transport'] == 'webhook':
        print(json.dumps({'provider': provider, **config}), flush=True)
        return
    # SDKs log ticket URLs, payloads and remote error bodies even at ERROR level.
    logging.disable(logging.CRITICAL)
    IMConnection.__table__.create(engine, checkfirst=True)
    mode = config['transport']
    record_state(provider, mode, 'connecting')
    if provider == 'feishu':
        import lark_oapi as lark
        def handler(event):
            if safe_receive(receive_feishu, json.loads(lark.JSON.marshal(event))) != 200:
                raise RuntimeError('IM_INGRESS_FAILED')
        dispatcher = lark.EventDispatcherHandler.builder('', '').register_p2_im_message_receive_v1(handler).build()
        client = lark.ws.Client(im._required('FEISHU_APP_ID'), im._required('FEISHU_APP_SECRET'), event_handler=dispatcher)
        connection = lambda: client._conn  # pinned lark-oapi 1.7.3
        start = client.start
    else:
        import dingtalk_stream as ding
        class Handler(ding.ChatbotHandler):
            async def process(self, message):
                # Bounded sequential DB work on this dedicated process's loop.
                code = safe_receive(receive_dingtalk, message.data)
                return code, 'OK' if code == 200 else 'IM_INGRESS_FAILED'
        client = ding.DingTalkStreamClient(ding.Credential(im._required('DINGTALK_CLIENT_ID'), im._required('DINGTALK_CLIENT_SECRET')))
        client.register_callback_handler(ding.ChatbotMessage.TOPIC, Handler())
        connection = lambda: client.websocket
        start = lambda: asyncio.run(client.start())
    stop = threading.Event()
    def heartbeat():
        while not stop.wait(5):
            try:
                record_state(provider, mode, 'connected' if socket_open(connection()) else 'connecting')
            except Exception:
                print('IM_STATUS_WRITE_FAILED', flush=True)
    threading.Thread(target=heartbeat, daemon=True).start()
    try:
        start()
    except KeyboardInterrupt:
        pass
    except Exception:
        print('IM_CONNECTION_FAILED', flush=True)
        raise SystemExit(1)
    finally:
        stop.set()
        record_state(provider, mode, 'stopped')


def stop_child(child):
    if child is not None and child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=8)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=3)


def supervise(provider):
    """One supervisor per provider; restart SDK process on effective config changes."""
    from sqlalchemy import select, func
    child, previous = None, None
    stopping = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopping.set())
    # Session-level advisory lock prevents duplicate supervisors across hosts.
    with engine.connect() as lock:
        key = 81901 if provider == 'feishu' else 81902
        if not lock.scalar(select(func.pg_try_advisory_lock(key))):
            raise SystemExit('IM_SUPERVISOR_ALREADY_RUNNING')
        try:
            while not stopping.is_set():
                try:
                    lock.execute(select(1))
                    with SessionLocal() as db:
                        values, _ = im_settings.effective(db, provider)
                    fingerprint = im_settings.fingerprint(values)
                    token = im_settings._active.set(values)
                    try:
                        config = im.configuration(provider)
                    finally:
                        im_settings._active.reset(token)
                    if fingerprint != previous:
                        stop_child(child)
                        child = None
                        previous = fingerprint
                    if config['configured'] and config['transport'] != 'webhook' and (child is None or child.poll() is not None):
                        env = {**os.environ, **values, 'IM_CONFIG_FINGERPRINT': fingerprint}
                        child = subprocess.Popen([sys.executable, '-m', 'app.im_connections', '--provider', provider, '--child'], env=env)
                except Exception:
                    stop_child(child)
                    child = None
                    print('IM_SUPERVISOR_CONFIGURATION_UNAVAILABLE', flush=True)
                    # Losing the singleton DB session must fail closed.
                    raise
                stopping.wait(5)
        finally:
            stop_child(child)
            lock.execute(select(func.pg_advisory_unlock(key)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--provider', choices=['feishu', 'dingtalk'], required=True)
    parser.add_argument('--child', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    run(args.provider) if args.child else supervise(args.provider)


if __name__ == '__main__':
    main()
