"""Independent durable attachment worker; never execute parsing on API/IM ingress."""
import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from sqlalchemy import select, or_, and_, func
from .db import SessionLocal, engine
from .models import Run, now, uid
from .attachment_models import Attachment, AttachmentJob, AttachmentArtifact
from . import attachments, attachment_storage as storage

log = logging.getLogger(__name__)


def lease(factory=SessionLocal):
    with factory.begin() as db:
        job = db.scalar(select(AttachmentJob).where(or_(
            and_(AttachmentJob.state == 'queued', AttachmentJob.available_at <= now()),
            and_(AttachmentJob.state == 'leased', AttachmentJob.lease_until < now())))
            .order_by(AttachmentJob.available_at).with_for_update(skip_locked=True).limit(1))
        if not job:
            return None
        job.attempts += 1
        if job.attempts > storage.limit('MAX_ATTEMPTS', 3):
            job.state, job.lease_until = 'failed', None
            item = db.get(Attachment, job.attachment_id)
            if item and item.status != 'revoked':
                item.status, item.error = 'failed', '附件处理多次中断，请重新上传'
            return None
        job.lease_id, job.state = uid(), 'leased'
        job.lease_until = now() + timedelta(seconds=storage.limit('LEASE_SECONDS', 180))
        job.updated_at = now()
        return job.attachment_id, job.lease_id, job.attempts


def parse(identifier):
    source, result = storage.path(identifier), storage.path(identifier, 'result.json')
    result.unlink(missing_ok=True)
    command = [sys.executable, '-m', 'app.attachment_parser', str(source), str(result)]
    env = {'PATH': os.defpath, 'LANG': 'C.UTF-8', 'PYTHONPATH': str(Path(__file__).resolve().parents[1])}
    env.update({k: v for k, v in os.environ.items() if k.startswith('ATTACHMENT_') and k != 'ATTACHMENT_ROOT'})
    # macOS enforces deny-network at OS level. Other platforms must fail closed unless explicitly opted into process-only isolation.
    if sys.platform == 'darwin':
        profile = '(version 1)(allow default)(deny network*)(deny file-write*)(allow file-write* (subpath ' + json.dumps(str(result.parent)) + '))'
        command = ['/usr/bin/sandbox-exec', '-p', profile, *command]
    elif os.getenv('ATTACHMENT_ALLOW_PROCESS_ONLY') != '1':
        raise RuntimeError('此平台尚未配置解析进程网络隔离；请部署受限解析环境')
    proc = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        proc.wait(timeout=storage.limit('PARSE_SECONDS', 45))
    except subprocess.TimeoutExpired:
        raise RuntimeError('附件解析超时') from None
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
    if not result.exists() or result.stat().st_size > storage.limit('RESULT_BYTES', 8 * 1024 * 1024):
        raise RuntimeError('附件解析失败或结果超出限制')
    with storage.open_read(identifier, 'result.json') as stream:
        data = json.load(stream)
    if proc.returncode != 0 or data.get('error'):
        raise RuntimeError(data.get('error', '附件格式损坏或不受支持'))
    return data


def process(ticket, factory=SessionLocal):
    identifier, lease_id, attempts = ticket
    try:
        with factory.begin() as db:
            item = db.get(Attachment, identifier)
            attachments.authorize(db, item)
            if item.provider != 'web':
                item.status = 'fetching'
            else:
                item.status = 'parsing'
        if item.provider != 'web':
            from .attachment_download import download
            download(identifier, factory)
        with factory.begin() as db:
            item = db.get(Attachment, identifier)
            attachments.authorize(db, item)
            item.status = 'parsing'
        data = parse(identifier)
        with factory.begin() as db:
            job = db.scalar(select(AttachmentJob).where(AttachmentJob.attachment_id == identifier).with_for_update())
            if job.lease_id != lease_id or job.state != 'leased':
                return
            item = db.get(Attachment, identifier)
            attachments.authorize(db, item)
            db.merge(AttachmentArtifact(attachment_id=identifier, manifest=data))
            item.status, item.error, item.mime = 'ready', None, data['mime']
            job.state, job.lease_until, job.updated_at = 'done', None, now()
    except Exception as exc:
        with factory.begin() as db:
            job = db.scalar(select(AttachmentJob).where(AttachmentJob.attachment_id == identifier).with_for_update())
            if not job or job.lease_id != lease_id:
                return
            item = db.get(Attachment, identifier)
            # Format/permission errors never retry; transient network failure is bounded.
            retry = getattr(exc, 'retryable', False) and attempts < storage.limit('MAX_ATTEMPTS', 3)
            job.state, job.lease_until = ('queued' if retry else 'failed'), None
            job.available_at = now() + timedelta(seconds=min(60, 2 ** attempts))
            job.updated_at = now()
            if item.status != 'revoked':
                item.status = 'received' if retry else 'failed'
                item.error = str(exc)[:280] if isinstance(exc, RuntimeError) else '附件处理失败或授权已失效'
        log.warning('Attachment job failed (%s)', type(exc).__name__)
    finally:
        wake_runs(factory)


def wake_runs(factory=SessionLocal):
    with factory.begin() as db:
        runs = list(db.scalars(select(Run).where(Run.status == 'waiting_attachments').with_for_update(skip_locked=True).limit(100)))
        for run in runs:
            rows = list(db.scalars(select(Attachment).where(Attachment.run_id == run.id)))
            try:
                for item in rows:
                    attachments.authorize(db, item)
                if any(a.status in ('failed', 'revoked') for a in rows):
                    run.status, run.error = 'failed', '附件解析失败或已撤销，请检查附件后重新发送'
                elif rows and all(a.status == 'ready' for a in rows):
                    run.status = 'queued'
            except Exception:
                run.status, run.error = 'failed', '附件授权已失效，任务已停止'
            if run.status == 'failed':
                from .models import IMEvent
                for event in db.scalars(select(IMEvent).where(IMEvent.run_id == run.id)):
                    if event.delivered_at is None:
                        event.delivery_error = 'ATTACHMENT_FAILED'


def cleanup(factory=SessionLocal):
    import shutil
    with factory.begin() as db:
        rows = list(db.scalars(select(Attachment).where(or_(Attachment.status == 'revoked',
            and_(Attachment.message_id.is_(None), Attachment.expires_at < now()))).with_for_update(skip_locked=True).limit(100)))
        for item in rows:
            try:
                attachments.authorize(db, item)
            except Exception:
                item.status, item.error = 'revoked', '附件授权已失效或草稿已过期'
            item.status = 'revoked'
            job = db.get(AttachmentJob, item.id)
            if job and job.state == 'leased' and job.lease_until and job.lease_until > now():
                continue
            directory = storage.path(item.id).parent
            shutil.rmtree(directory)
        known = set(db.scalars(select(Attachment.id)))
    cutoff = time.time() - storage.limit('DRAFT_HOURS', 24) * 3600
    for directory in storage.root().iterdir():
        if directory.is_dir() and not directory.is_symlink() and directory.name not in known and directory.stat().st_mtime < cutoff:
            shutil.rmtree(directory)


def main():
    # One local worker instance; SKIP LOCKED/leases remain safe if deployed with more workers later.
    with engine.connect() as connection:
        if not connection.scalar(select(func.pg_try_advisory_lock(731940282))):
            raise SystemExit('附件worker已运行')
        connection.commit()
        tick = 0
        while True:
            connection.execute(select(1))
            connection.commit()
            ticket = lease()
            if ticket:
                process(ticket)
            else:
                wake_runs()
                time.sleep(0.5)
            tick += 1
            if tick % 120 == 0:
                cleanup()


if __name__ == '__main__':
    main()
