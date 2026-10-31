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


@pytest.mark.parametrize('name', ['test1', '广告诊断 / Ads Diagnosis', '中文技能', 'a' * 120])
def test_plain_markdown_normalized_with_stable_name(name):
    from app.service import normalize_skill_config
    raw = '# 标题\n  正文\n---\n保留空白\n'
    transmitted = prepare([[raw]])[0]
    assert transmitted == {'content': raw}
    result = normalize_skill_config(name, transmitted)
    validate_resource('skill', result)
    header, body = result['content'][4:].split('\n---\n', 1)
    front = yaml.safe_load(header)
    assert body == raw
    assert re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', front['name'])
    assert len(front['name']) <= 64
    assert front['description'] == '执行技能：' + name
    assert normalize_skill_config(name, result) == result


@pytest.mark.parametrize('newline', ['\n', '\r\n'])
def test_valid_document_preserved_exactly(newline):
    from app.service import normalize_skill_config
    raw = newline.join(['---', 'name: test', 'description: "PRIVATE"', 'license: MIT', 'metadata:', '  owner: test', '---', '# Body', ''])
    assert normalize_skill_config('显示名', {'content': raw}) == {'content': raw}
    assert normalize_skill_config('显示名', {'content': '\ufeff' + raw}) == {'content': raw}


def test_incomplete_header_preserves_extra_metadata_and_private_description():
    from app.service import normalize_skill_config
    raw = '---\nname: 中文\ndescription: PRIVATE\nmetadata:\n  mode: strict\n---\n# Original body\n'
    result = normalize_skill_config('广告诊断 / Ads Diagnosis', {'content': raw})
    validate_resource('skill', result)
    front = yaml.safe_load(result['content'][4:].split('\n---\n')[0])
    assert front['description'] == 'PRIVATE'
    assert front['metadata'] == {'mode': 'strict'}
    assert result['content'].endswith('# Original body\n')
    assert normalize_skill_config('another name', result) == result


@pytest.mark.parametrize('raw', ['', '  ', '---\nname: x', '---\nname: [\n---\nbody', '---\n- item\n---\nbody', '---\nname: x\ndescription: y\n---\n', 'x' * 64000])
def test_invalid_or_oversized_normalized_document_rejected(raw):
    from app.service import normalize_skill_config
    with pytest.raises(HTTPException) as exc:
        normalize_skill_config('test', {'content': raw})
    assert exc.value.status_code == 422


def test_frontend_blank_and_size_checks():
    results = prepare([[' '], ['x' * 64001], ['😀' * 64000]])
    assert 'error' in results[0] and 'error' in results[1]
    assert results[2]['content'] == '😀' * 64000
