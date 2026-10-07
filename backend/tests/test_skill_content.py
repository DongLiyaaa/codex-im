"""Exercise the actual TypeScript serializer against the backend YAML validator; no DB writes."""
import json
import os
from pathlib import Path
import re
import subprocess

import pytest
import yaml
from fastapi import HTTPException

os.environ.setdefault('SESSION_SECRET', 'integration-test-secret-at-least-32-characters')
from app.service import validate_resource

ROOT = Path(__file__).resolve().parents[2]


def prepare(cases):
    script = """
import ts from 'typescript';
import fs from 'node:fs';
import { webcrypto } from 'node:crypto';
if (!globalThis.crypto) Object.defineProperty(globalThis, 'crypto', {value: webcrypto});
const source = fs.readFileSync('src/skillContent.ts', 'utf8');
const js = ts.transpileModule(source, {compilerOptions: {target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022}}).outputText;
const {prepareSkillContent} = await import('data:text/javascript;base64,' + Buffer.from(js).toString('base64'));
const cases = JSON.parse(fs.readFileSync(0, 'utf8'));
const results = [];
for (const args of cases) {
  try { results.push({content: await prepareSkillContent(...args)}); }
  catch (e) { results.push({error: e.message}); }
}
console.log(JSON.stringify(results));
"""
    result = subprocess.run(['node', '--input-type=module', '-e', script], cwd=ROOT / 'frontend', input=json.dumps(cases), text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


def test_body_roundtrip_and_safe_yaml():
    cases = [
        ['test1', '111', '111', 'body'],
        ['中文技能', '中文触发描述', '\n  正文\n---\n保留空白\n', 'body'],
        ['中文技能', '说明', '另一个正文', 'body'],
        ['111', '111', '111', 'body'],
        ['Quoted Name', '引号 " : \\ \n换行\r\n next: true\u0085\u2028\u2029', '原文', 'body'],
        ['a' * 120, '说明', '正文', 'body'],
    ]
    results = prepare(cases)
    names = []
    for case, result in zip(cases, results):
        content = result['content']
        validate_resource('skill', {'content': content})
        header, body = content[4:].split('\n---\n', 1)
        front = yaml.safe_load(header)
        assert front['description'] == case[1]
        assert body == case[2]
        assert len(front['name']) <= 64
        assert re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', front['name'])
        names.append(front['name'])
    assert names[0] == 'test1'
    assert names[1] == names[2]
    assert names[3] == '111'


def test_document_passthrough_and_malformed_rejection():
    documents = [
        '---\nname: "test1"\ndescription: "111"\n---\n111',
        '---\nname: test1\ndescription: [\n---\n111',
        '---\nname: test1\ndescription: 111\n---\n111',
        '---\nname: test1\ndescription: "111"',
        '111',
        '---\nname: 中文\ndescription: "111"\n---\n111',
        '---\nname: bad--name\ndescription: "111"\n---\n111',
        '---\nname: test1\ndescription: " "\n---\n111',
    ]
    results = prepare([['显示名', '资源描述', doc, 'document'] for doc in documents])
    for index, result in enumerate(results):
        assert result['content'] == documents[index]
        if index == 0:
            validate_resource('skill', result)
        else:
            with pytest.raises(HTTPException) as error:
                validate_resource('skill', result)
            assert error.value.status_code == 422
            assert 'Skill' in error.value.detail and ('头部' in error.value.detail)


def test_body_validation_and_generated_length():
    header = '---\nname: "test1"\ndescription: "111"\n---\n'
    size = 64000 - len(header)
    results = prepare([
        ['test1', '111', 'x' * size, 'body'],
        ['test1', '111', 'x' * (size + 1), 'body'],
        ['test1', '111', '😀' * size, 'body'],
        ['test1', ' ', '111', 'body'],
        ['test1', '111', ' ', 'body'],
        ['test1', '111', '---\nname: broken', 'body'],
        ['test1', '111', ' \n---\nname: broken', 'body'],
        ['test1', '111', header + 'x' * (size + 1), 'document'],
    ])
    for index in (0, 2):
        assert len(results[index]['content']) == 64000
        validate_resource('skill', results[index])
    for index in (1, 3, 4, 7):
        assert 'error' in results[index]
    # Plain text that merely looks like a file header is still just the body the user typed.
    for index, body in ((5, '---\nname: broken'), (6, ' \n---\nname: broken')):
        content = results[index]['content']
        assert content.startswith(header) and content[len(header):] == body
        validate_resource('skill', results[index])
    with pytest.raises(HTTPException) as error:
        validate_resource('skill', {'content': header + 'x' * (size + 1)})
    assert '64000' in error.value.detail
