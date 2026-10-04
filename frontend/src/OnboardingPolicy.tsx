import { useState } from 'react';
import { toast } from 'sonner';
import { api } from './api';
import { ScopeFields } from './ScopeFields';
import { Feedback, Field, Form, Loading, useData } from './ui';

interface Policy { provider: 'feishu' | 'dingtalk'; enabled: boolean; org_id: string | null; team_id: string | null; daily_cap: number; used_24h: number; valid: boolean | null }
const platform = (provider: string) => provider === 'feishu' ? '飞书' : '钉钉';

// Off by default: a verified first private message from the application's own organization may then create a member.
export function OnboardingPolicy() {
  const list = useData<Policy[]>('/im/onboarding-policy');
  return <section className="panel"><div className="section-heading"><h2>自动接入策略</h2><button className="text-button" onClick={list.reload}>刷新</button></div>
    <p>默认关闭。开启后，本组织员工首次私聊机器人即自动成为成员，不再逐个审批；群聊不触发，新成员默认没有任何 Skill / MCP。</p>
    <Feedback error={list.error}/>{list.loading ? <Loading/> : <div className="integration-config-grid">{(list.data ?? []).map(p => <PolicyForm key={`${p.provider}-${p.enabled}-${p.org_id}-${p.team_id}-${p.daily_cap}`} policy={p} reload={list.reload}/>)}</div>}</section>;
}

function PolicyForm({ policy, reload }: { policy: Policy; reload: () => void }) {
  const [enabled, setEnabled] = useState(policy.enabled);
  const [scope, setScope] = useState({ org: policy.org_id ?? '', team: policy.team_id ?? '' });
  const [cap, setCap] = useState(String(policy.daily_cap));
  const name = platform(policy.provider);
  return <Form label="保存" submit={() => api(`/im/onboarding-policy/${policy.provider}`, { method: 'PUT', body: JSON.stringify({ enabled, org_id: scope.org || null, team_id: scope.team || null, daily_cap: Number(cap) }) })} onSuccess={() => { toast.success(`${name}自动接入策略已保存。`); reload(); }}>
    <h3>{name}</h3>
    <label className="checkbox"><input type="checkbox" checked={enabled} onChange={e => setEnabled(e.target.checked)}/>启用{name}自动接入</label>
    {enabled && <>
      <ScopeFields scope={scope} change={setScope} requireTeam/>
      <Field label="每日上限（人）" hint="达到上限后，其余发送者退回人工接入。"><input aria-label="每日上限（人）" type="number" min={1} max={200} required value={cap} onChange={e => setCap(e.target.value)}/></Field>
      {!policy.enabled && <label className="checkbox"><input type="checkbox" required/>我确认已在{name}后台限制了应用的可用范围</label>}
    </>}
    {policy.enabled && <p className="im-help">近 24 小时已自动接入 {policy.used_24h} / {policy.daily_cap} 人。</p>}
    {policy.enabled && policy.valid === false && <p className="notice error">已选组织或部门已不存在，新发送者会退回人工接入；请重新选择后保存。</p>}
  </Form>;
}
