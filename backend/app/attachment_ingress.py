"""Resource references only; invoked after verified transport, admitted only after identity checks."""
from . import im


def feishu(message):
    kind = message.get('message_type')
    content = im._object(message['content'])
    if kind == 'text':
        return content['text'], []
    refs, texts = [], []
    def add(key, resource_type, filename):
        refs.append({'filename': filename, 'reference': {'message_id': message['message_id'], 'key': im._text(key, 1024), 'type': resource_type}})
    if kind == 'image':
        add(content['image_key'], 'image', '飞书图片')
    elif kind == 'file':
        add(content['file_key'], 'file', content.get('file_name', '飞书附件'))
    elif kind == 'post':
        post = content if 'content' in content else next(iter(content.values()), {})
        texts.append(post.get('title', ''))
        for line in post.get('content', []):
            for block in line:
                if block.get('tag') in ('text', 'a'):
                    texts.append(block.get('text', ''))
                elif block.get('tag') == 'img':
                    add(block['image_key'], 'image', '飞书图片')
    else:
        return None, []
    if len(refs) > 5:
        im._reject(400)
    return '\n'.join(texts), refs


def dingtalk(payload):
    kind = payload.get('msgtype')
    if kind == 'text':
        return payload['text']['content'], []
    content = payload.get('content', {})
    if isinstance(content, str):
        content = im._object(content)
    refs, texts = [], []
    def add(block, default):
        refs.append({'filename': block.get('fileName', default), 'reference': {'downloadCode': im._text(block['downloadCode'], 4096)}})
    if kind in ('picture', 'image', 'file'):
        add(content, '钉钉图片' if kind != 'file' else '钉钉附件')
    elif kind == 'richText':
        for block in content.get('richText', []):
            if 'text' in block:
                texts.append(block['text'])
            if 'downloadCode' in block:
                add(block, '钉钉图片')
    else:
        return None, []
    if len(refs) > 5:
        im._reject(400)
    return '\n'.join(texts), refs


def register(db, user, conversation, provider, app_scope, refs):
    import json
    from .models import uid
    from .attachment_models import Attachment, AttachmentJob
    from .attachment_storage import filename
    from .im_settings import cipher
    identifiers = []
    for ref in refs:
        item = Attachment(id=uid(), conversation_id=conversation.id, uploader_id=user.id,
            filename=filename(ref['filename']), provider=provider, app_scope=app_scope, status='received',
            encrypted_reference=cipher().encrypt(json.dumps(ref['reference']).encode()).decode())
        db.add(item)
        db.flush()
        db.add(AttachmentJob(attachment_id=item.id))
        identifiers.append(item.id)
    return identifiers
