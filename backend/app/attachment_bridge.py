"""Run-bound internal MCP, independently scoped from personal OAuth capabilities."""
import base64
import hashlib
import hmac
import json
import time
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from .db import get_db
from .models import Run, User
from .attachment_models import Attachment, AttachmentArtifact
from . import attachments, attachment_storage as storage
from .platform_bridge import key

router = APIRouter()
AUDIENCE = 'hub-run-attachments'
IMAGE_AUDIENCE = 'hub-run-image-fetch'
TOOLS = {
    'list_attachments': {},
    'get_attachment_status': {'attachment_id': {'type': 'string'}},
    'read_document': {'attachment_id': {'type': 'string'}, 'page': {'type': 'integer', 'minimum': 1}, 'offset': {'type': 'integer', 'minimum': 0}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 12000}},
    'list_sheets': {'attachment_id': {'type': 'string'}},
    'read_sheet_range': {'attachment_id': {'type': 'string'}, 'sheet': {'type': 'string'}, 'start_row': {'type': 'integer', 'minimum': 1}, 'end_row': {'type': 'integer', 'minimum': 1}, 'start_column': {'type': 'integer', 'minimum': 1}, 'end_column': {'type': 'integer', 'minimum': 1}},
    'search': {'attachment_id': {'type': 'string'}, 'query': {'type': 'string', 'maxLength': 200}, 'offset': {'type': 'integer', 'minimum': 0}},
}


def issue(run, audience=AUDIENCE):
    claims = {'aud': audience, 'run': run.id, 'user': run.user_id, 'conversation': run.conversation_id, 'exp': int(time.time()) + 240}
    encoded = base64.urlsafe_b64encode(json.dumps(claims, separators=(',', ':')).encode()).decode().rstrip('=')
    return encoded + '.' + hmac.new(key(), encoded.encode(), hashlib.sha256).hexdigest()


def verify(token, db, audience=AUDIENCE):
    try:
        encoded, signature = token.split('.')
        if not hmac.compare_digest(signature, hmac.new(key(), encoded.encode(), hashlib.sha256).hexdigest()):
            raise ValueError()
        claims = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
        if claims['aud'] != audience or not time.time() < claims['exp'] <= time.time() + 250:
            raise ValueError()
        run = db.get(Run, claims['run'])
        if not run or run.status != 'running' or run.user_id != claims['user'] or run.conversation_id != claims['conversation']:
            raise ValueError()
        from .service import build_payload
        build_payload(db, run, check_attachments=False)
        return run
    except Exception:
        raise HTTPException(403, '附件能力已失效或无权限') from None


def bearer(request):
    value = request.headers.get('authorization', '')
    if not value.startswith('Bearer ') or len(value) > 2048:
        raise HTTPException(403, '附件能力无效')
    return value[7:]


def bound(db, run, identifier):
    item = db.get(Attachment, identifier)
    if not item or item.run_id != run.id or item.message_id != run.message_id or item.conversation_id != run.conversation_id:
        raise HTTPException(403, '附件不属于本次任务')
    attachments.authorize(db, item)
    return item


def manifest(db, item):
    if item.status != 'ready':
        raise HTTPException(409, '附件未就绪，不能继续分析')
    artifact = db.get(AttachmentArtifact, item.id)
    if not artifact:
        raise HTTPException(409, '附件解析结果不存在')
    return artifact.manifest


def integer(args, name, default, low, high):
    value = args.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise HTTPException(422, '附件读取范围超出限制')
    return value


def operate(db, run, name, args):
    if not isinstance(args, dict) or set(args) - set(TOOLS[name]):
        raise HTTPException(422, '附件工具参数无效')
    if name == 'list_attachments':
        return {'attachments': [attachments.public(a) for a in attachments.run_attachments(db, run)]}
    identifier = args.get('attachment_id')
    if not isinstance(identifier, str):
        raise HTTPException(422, '需要 attachment_id')
    item = bound(db, run, identifier)
    result = {'source': {'attachment_id': item.id, 'filename': item.filename}, 'truncated': False, 'continuation': None}
    if name == 'get_attachment_status':
        return result | attachments.public(item)
    data = manifest(db, item)
    result['warnings'] = data.get('warnings', [])
    if name == 'read_document':
        pages = data.get('pages', [])
        page = integer(args, 'page', 1, 1, max(1, len(pages)))
        offset = integer(args, 'offset', 0, 0, 10_000_000)
        limit = integer(args, 'limit', 6000, 1, 12000)
        if not pages:
            raise HTTPException(422, '该附件没有文字页；图片由本次任务图像输入分析，表格请用工作表工具')
        text = pages[page - 1]['text']
        end = min(offset + limit, len(text))
        continuation = {'page': page, 'offset': end} if end < len(text) else ({'page': page + 1, 'offset': 0} if page < len(pages) else None)
        return result | {'page': page, 'page_count': len(pages), 'text': text[offset:end], 'truncated': continuation is not None, 'continuation': continuation}
    sheets = data.get('sheets', [])
    if name == 'list_sheets':
        return result | {'sheets': [{'name': s['name'], 'rows': len(s['rows']), 'columns': max((len(r) for r in s['rows']), default=0)} for s in sheets]}
    if name == 'read_sheet_range':
        sheet = next((s for s in sheets if s['name'] == args.get('sheet')), None)
        if not sheet:
            raise HTTPException(404, '工作表不存在')
        start = integer(args, 'start_row', 1, 1, 1_000_000)
        end = integer(args, 'end_row', start + 19, start, start + 99)
        col = integer(args, 'start_column', 1, 1, 16384)
        end_col = integer(args, 'end_column', col + 9, col, min(col + 49, 16384))
        if (end - start + 1) * (end_col - col + 1) > 1000:
            raise HTTPException(422, '单次最多读取1000个单元格')
        rows, length = [], 0
        for row in sheet['rows'][start - 1:end]:
            cells = row[col - 1:end_col]
            length += len(json.dumps(cells, ensure_ascii=False))
            if length > 18000:
                break
            rows.append(cells)
        next_row = start + len(rows)
        continuation = {'sheet': sheet['name'], 'start_row': next_row, 'start_column': col} if next_row <= len(sheet['rows']) else None
        return result | {'sheet': sheet['name'], 'range': {'start_row': start, 'end_row': next_row - 1, 'start_column': col, 'end_column': end_col}, 'rows': rows, 'truncated': continuation is not None, 'continuation': continuation, 'formula_policy': '公式从不执行；formula为表达式，cached为文件保存时的缓存值，可能缺失或过期'}
    query = args.get('query')
    if not isinstance(query, str) or not query.strip() or len(query) > 200:
        raise HTTPException(422, '请输入1–200字搜索文本')
    offset = integer(args, 'offset', 0, 0, 1_000_000)
    hits = []
    needle = query.casefold()
    for page in data.get('pages', []):
        for line in page['text'].splitlines():
            if needle in line.casefold():
                hits.append({'page': page['page'], 'text': line[:400]})
    for sheet in sheets:
        for i, row in enumerate(sheet['rows'], 1):
            for j, cell in enumerate(row, 1):
                value = str(cell)
                if needle in value.casefold():
                    hits.append({'sheet': sheet['name'], 'row': i, 'column': j, 'text': value[:400]})
    more = len(hits) > offset + 20
    return result | {'matches': hits[offset:offset + 20], 'truncated': more, 'continuation': {'offset': offset + 20} if more else None}


@router.post('/internal/attachment-mcp')
async def mcp(request: Request, db=Depends(get_db)):
    run = verify(bearer(request), db)
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 8192:
            raise HTTPException(413, '请求过大')
    try:
        body = json.loads(raw)
        if not isinstance(body, dict) or body.get('jsonrpc') != '2.0':
            raise ValueError()
    except (ValueError, UnicodeError):
        raise HTTPException(400, 'MCP请求无效') from None
    method, params = body.get('method'), body.get('params', {})
    if method == 'notifications/initialized':
        return Response(status_code=202)
    if method == 'initialize':
        result = {'protocolVersion': '2025-03-26', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'hub-attachments', 'version': '1.0.0'}}
    elif method == 'ping':
        result = {}
    elif method == 'tools/list':
        result = {'tools': [{'name': name, 'description': '读取本次消息已授权附件，按页/范围分块，遵循返回的来源、截断和续读信息。',
            'annotations': {'readOnlyHint': True, 'openWorldHint': False},
            'inputSchema': {'type': 'object', 'properties': props, 'additionalProperties': False,
                'required': [] if name == 'list_attachments' else ['attachment_id']}} for name, props in TOOLS.items()]}
    elif method == 'tools/call':
        if not isinstance(params, dict) or params.get('name') not in TOOLS:
            raise HTTPException(400, '未知工具')
        try:
            value = operate(db, run, params['name'], params.get('arguments', {}))
            from .service import audit
            audit(db, db.get(User, run.user_id), 'attachment.tool_read', run.id, {'tool': params['name']})
            result = {'content': [{'type': 'text', 'text': json.dumps(value, ensure_ascii=False)}], 'isError': False}
        except HTTPException as exc:
            result = {'content': [{'type': 'text', 'text': str(exc.detail)}], 'isError': True}
    else:
        return {'jsonrpc': '2.0', 'id': body.get('id'), 'error': {'code': -32601, 'message': 'Method not found'}}
    return {'jsonrpc': '2.0', 'id': body.get('id'), 'result': result}


@router.get('/internal/attachment-images/{identifier}')
def image(identifier: str, request: Request, db=Depends(get_db)):
    run = verify(bearer(request), db, IMAGE_AUDIENCE)
    item = bound(db, run, identifier)
    data = manifest(db, item)
    images = data.get('images', [])
    if len(images) != 1 or images[0].get('name') != 'sanitized.png':
        raise HTTPException(404, '附件没有可用图片')
    entry = images[0]
    stream = storage.open_read(item.id, 'sanitized.png')
    raw = stream.read(storage.limit('MAX_BYTES', 20 * 1024 * 1024) + 1)
    stream.close()
    if len(raw) != entry['size'] or hashlib.sha256(raw).hexdigest() != entry['sha256']:
        raise HTTPException(409, '图片校验失败')
    from .service import audit
    audit(db, db.get(User, run.user_id), 'attachment.image_fetch', item.id)
    return Response(raw, media_type='image/png', headers={'Cache-Control': 'no-store'})
