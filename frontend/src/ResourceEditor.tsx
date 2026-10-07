import { useState } from 'react';
import { api } from './api';
import type { Resource, User } from './api';
import { Field, Form, Modal, nullable, value } from './ui';
import { ScopeFields } from './ScopeFields';
import { resourceScope, scopeHints, skillBody } from './ResourceCard';
import type { DirectoryNames } from './ResourceCard';
import { prepareSkillContent } from './skillContent';
import { buildMcpConfig, HEADER_LIMIT } from './mcpHeaders';
import type { HeaderRow } from './mcpHeaders';

const MASK = '***';
type Config = { content?: string; url?: string; headers?: Record<string, string> };

export function ResourceEditor({ resource, user, close, saved, directory }: { resource: Resource; user: User; close: () => void; saved: () => void; directory?: DirectoryNames | null }) {
  const config = resource.config as Config;
  const isMcp = resource.kind === 'mcp';
  const [scope, setScope] = useState({ org: String(resource.org_id ?? ''), team: String(resource.team_id ?? '') });
  const [kept, setKept] = useState<string[]>(Object.keys(config.headers ?? {}));
  const [extra, setExtra] = useState<number[]>([]);
  const [next, setNext] = useState(1);

  async function submit(f: FormData) {
    let body: Config;
    if (isMcp) {
      const rows: HeaderRow[] = kept.map(name => ({ name, value: String(f.get(`keep_${name}`) ?? '').trim() || MASK }));
      extra.forEach(key => rows.push({ name: String(f.get(`new_name_${key}`) ?? ''), value: String(f.get(`new_value_${key}`) ?? '') }));
      body = buildMcpConfig(String(f.get('url') ?? ''), rows.some(row => row.name.trim() || row.value.trim()) ? 'headers' : 'none', rows);
    } else {
      body = { content: await prepareSkillContent(value(f, 'name'), String(f.get('description') ?? ''), String(f.get('content') ?? ''), 'body') };
    }
    return api(`/resources/${encodeURIComponent(resource.id)}`, {
      method: 'PATCH',
      body: JSON.stringify({ name: value(f, 'name'), description: String(f.get('description') ?? '').trim(), org_id: nullable(f, 'org_id'), team_id: nullable(f, 'team_id'), enabled: f.has('enabled'), config: body, grant_scope: !!scope.org && f.has('grant_scope') }),
    });
  }

  return <Modal title={`编辑${isMcp ? ' MCP 服务' : ' Skill 技能'}`} close={close}>
    <Form label="保存" submit={submit} onSuccess={saved}>
      <Field label="名称"><input name="name" required maxLength={120} defaultValue={resource.name}/></Field>
      <ScopeFields scope={scope} change={setScope} optionalOrg departmentWithOrg fixedOrg={user.role !== 'super_admin'}
        orgHint={scopeHints.org} teamHint={`${scopeHints.team}缩小范围后，范围之外已有的授权会被自动撤销。`}
        teamEmptyLabel="整个组织（不限部门）"/>
      {scope.org && <label className="checkbox"><input type="checkbox" name="grant_scope" defaultChecked={!resource.grants?.length}/>同时授权给「{resourceScope({ org_id: scope.org, team_id: scope.team || null }, directory)}」的所有成员和群</label>}
      {isMcp ? <>
        <Field label="MCP 服务地址" hint="必须以 https:// 开头。"><input type="url" name="url" required defaultValue={config.url ?? ''}/></Field>
        <Field label="说明（选填）"><textarea name="description" maxLength={5000} rows={2} defaultValue={resource.description}/></Field>
        <fieldset className="header-mode"><legend>请求头</legend>
          {kept.length === 0 && extra.length === 0 && <p className="im-help">当前不需要请求头。</p>}
          {kept.map(name => <div className="header-row" key={name}>
            <label>请求头名称<input value={name} disabled readOnly/></label>
            <label>密钥（留空表示不修改）<input name={`keep_${name}`} type="password" autoComplete="off" placeholder="已保存，不会显示" data-1p-ignore/></label>
            <button type="button" className="secondary" onClick={() => setKept(current => current.filter(item => item !== name))}>移除</button>
          </div>)}
          {extra.map(key => <div className="header-row" key={key}>
            <label>请求头名称<input name={`new_name_${key}`} placeholder="例如 Authorization" maxLength={128} autoComplete="off"/></label>
            <label>密钥<input name={`new_value_${key}`} type="password" autoComplete="off" data-1p-ignore/></label>
            <button type="button" className="secondary" onClick={() => setExtra(current => current.filter(item => item !== key))}>移除</button>
          </div>)}
          <button type="button" className="secondary" disabled={kept.length + extra.length >= HEADER_LIMIT} onClick={() => { setExtra(current => [...current, next]); setNext(n => n + 1); }}>添加请求头</button>
        </fieldset>
      </> : <>
        <Field label="这个技能是做什么的？（必填）"><textarea name="description" required maxLength={5000} rows={2} defaultValue={resource.description}/></Field>
        <Field label="技能内容（必填）"><textarea name="content" required rows={10} className="skill-text-area" defaultValue={skillBody(String(config.content ?? ''))}/></Field>
      </>}
      <label className="checkbox"><input type="checkbox" name="enabled" defaultChecked={resource.enabled}/>启用</label>
    </Form>
  </Modal>;
}
