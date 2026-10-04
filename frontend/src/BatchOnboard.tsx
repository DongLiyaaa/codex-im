import { useState } from 'react';
import { post, roles, translateError } from './api';
import type { Role } from './api';
import { ScopeFields } from './ScopeFields';
import type { Scope } from './ScopeFields';
import { Field, Form } from './ui';

export interface Picked { id: string; provider: string; sender_id: string; nickname: string | null }
interface Result { id: string; ok: boolean; error?: string }
interface Outcome { results: Result[]; created: number; failed: number }
const memberRoles: Role[] = ['member', 'team_lead'];
const platform = (provider: string) => provider === 'feishu' ? '飞书' : '钉钉';

// Onboards several discovered senders of one platform as IM-only members that share a role, organization and department.
export function BatchOnboard({ rows, initialScope, progress, done }: { rows: Picked[]; initialScope: Scope; progress: (created: number, scope: Scope) => void; done: (scope: Scope) => void }) {
  const [scope, setScope] = useState(initialScope);
  const [provider] = useState(rows[0]?.provider ?? '');  // Fixed at open: the list behind it reloads as senders get onboarded.
  const [role, setRole] = useState<Role>('member');
  const [names, setNames] = useState<Record<string, string>>(() => Object.fromEntries(rows.map(r => [r.id, r.nickname ?? ''])));
  const [remaining, setRemaining] = useState(rows);
  const [failures, setFailures] = useState<Record<string, string>>({});
  async function submit() {
    const out = await post<Outcome>('/im/discoveries/onboard-batch', { items: remaining.map(r => ({ discovery_id: r.id, name: names[r.id] ?? '' })), role, org_id: scope.org, team_id: scope.team });
    const failed = Object.fromEntries(out.results.filter(r => !r.ok).map(r => [r.id, translateError(r.error ?? '')]));
    if (out.created) progress(out.created, scope);
    setRemaining(remaining.filter(r => r.id in failed));
    setFailures(failed);
    if (out.failed) throw new Error(`${out.created} 人已接入，${out.failed} 人未成功，原因见下表；修正后可再次提交剩余的人。`);
    return out;
  }
  return <Form label={`确认接入 ${remaining.length} 人`} submit={submit} onSuccess={() => done(scope)}>
    <p>平台：{platform(provider)}。以下成员使用相同的角色、组织和部门。</p>
    <Field label="角色"><select value={role} onChange={e => setRole(e.target.value as Role)}>{memberRoles.map(r => <option key={r} value={r}>{roles[r]}</option>)}</select></Field>
    <ScopeFields scope={scope} change={setScope} requireTeam/>
    <div className="table-wrap"><table><thead><tr><th>发送者外部 ID</th><th>姓名</th><th>结果</th></tr></thead><tbody>{remaining.map(r => <tr key={r.id}><td>{r.sender_id}</td><td><input aria-label={`姓名 ${r.sender_id}`} required maxLength={200} value={names[r.id] ?? ''} placeholder="成员姓名" onChange={e => setNames({ ...names, [r.id]: e.target.value })}/></td><td>{failures[r.id] ?? '待提交'}</td></tr>)}</tbody></table></div>
    <p className="im-help">不创建邮箱和密码；Skill / MCP 仍需单独授权。</p>
  </Form>;
}
