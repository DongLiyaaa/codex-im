"""Opaque server-owned file names; never expose arbitrary filesystem access."""
import hashlib
import os
from pathlib import Path
import re
import stat
from fastapi import HTTPException


def limit(name, default):
    return int(os.getenv('ATTACHMENT_' + name, str(default)))


def root():
    base = Path(os.getenv('ATTACHMENT_ROOT', str(Path(__file__).resolve().parents[2] / '.runtime' / 'attachments'))).absolute()
    # Reject symlinks in every existing ancestor, including the configured root.
    for item in (base, *base.parents):
        if item.is_symlink():
            raise RuntimeError('附件存储目录不能包含符号链接')
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(base, 0o700)
    return base


def path(identifier, name='source'):
    if not re.fullmatch(r'[a-f0-9-]{36}', identifier) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,80}', name) or name in ('.', '..'):
        raise ValueError('Invalid attachment storage identifier')
    directory = root() / identifier
    if directory.is_symlink():
        raise RuntimeError('附件存储路径无效')
    directory.mkdir(mode=0o700, exist_ok=True)
    target = directory / name
    if target.is_symlink():
        raise RuntimeError('附件文件路径无效')
    return target


def open_read(identifier, name='source'):
    fd = os.open(path(identifier, name), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(fd)
        raise RuntimeError('附件文件类型无效')
    return os.fdopen(fd, 'rb')


def filename(value):
    value = str(value or 'attachment').replace('\\', '/').split('/')[-1]
    value = ''.join(c for c in value if ord(c) >= 32 and ord(c) != 127).strip()[:240]
    return value or 'attachment'


def save_stream(identifier, chunks):
    target = path(identifier)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o660)
    size, digest = 0, hashlib.sha256()
    try:
        with os.fdopen(fd, 'wb') as stream:
            for chunk in chunks:
                size += len(chunk)
                if size > limit('MAX_BYTES', 20 * 1024 * 1024):
                    raise HTTPException(413, '附件超过单文件大小限制（默认20MiB）')
                stream.write(chunk)
                digest.update(chunk)
        if not size:
            raise HTTPException(422, '不能上传空文件')
        return size, digest.hexdigest()
    except BaseException:
        target.unlink(missing_ok=True)
        raise
