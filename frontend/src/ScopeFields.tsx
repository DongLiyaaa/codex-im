import { useState } from 'react';
import { post, errorText } from './api';
import { useData, Field, Feedback, Loading } from './ui';
export interface Scope { org: string; team: string }
interface Org { id: string; name: string; legacy: boolean }
interface Dept extends Org { org_id: string }
interface Directory { organizations: Org[]; departments: Dept[]; can_create_org: boolean; can_create_department: boolean }
export function ScopeFields({ scope, change, requireTeam = false, optionalOrg = false, fixedOrg = false, fixedTeam = false }: {
  scope: Scope; change: (scope: Scope) => void; requireTeam?: boolean; optionalOrg?: boolean; fixedOrg?: boolean; fixedTeam?: boolean;
}) {
  const directory = useData<Directory>('/directory');
  const [addedOrgs, setAddedOrgs] = useState<Org[]>([]); const [addedTeams, setAddedTeams] = useState<Dept[]>([]);
  const [creating, setCreating] = useState<'org' | 'team' | null>(null); const [name, setName] = useState('');
  const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  const orgs = [...(directory.data?.organizations ?? []), ...addedOrgs].filter((o,i,a) => a.findIndex(x=>x.id===o.id)===i);
  const teams = [...(directory.data?.departments ?? []), ...addedTeams].filter((d,i,a) => d.org_id === scope.org && a.findIndex(x=>x.id===d.id && x.org_id===d.org_id)===i);
  async function create() {
    if (!name.trim() || busy) return;
    setBusy(true); setError('');
    try {
      if (creating === 'org') { const row = await post<Org>('/directory/organizations', {name: name.trim()}); setAddedOrgs(v=>[...v,row]); change({org:row.id,team:''}); }
      else { const row = await post<Dept>('/directory/departments', {name:name.trim(),org_id:scope.org}); setAddedTeams(v=>[...v,row]); change({...scope,team:row.id}); }
      setCreating(null); setName('');
    } catch(e) { setError(errorText(e)); } finally { setBusy(false); }
  }
  return <><div className="form-grid"><Field label="组织"><select name="org_id" required={!optionalOrg} value={scope.org} disabled={busy} onChange={e=>{change({org:e.target.value,team:''});setCreating(null);setName('');}}>
    <option value="">{optionalOrg ? '全局资源' : '请选择组织'}</option>{scope.org && !orgs.some(o=>o.id===scope.org) && <option value={scope.org}>{scope.org}（已有归属）</option>}{orgs.filter(o=>!fixedOrg || o.id===scope.org).map(o=><option key={o.id} value={o.id}>{o.name}{o.legacy?'（历史 ID）':''}</option>)}
  </select></Field>{!optionalOrg && <Field label="部门" hint="每位用户仅有一个主部门；组织级群可容纳同组织多个部门成员。"><select aria-label="部门" name="team_id" required={requireTeam} value={scope.team} disabled={busy} onChange={e=>change({...scope,team:e.target.value})}>
    <option value="">{requireTeam?'请选择部门':'组织级 / 跨部门'}</option>{scope.team && !teams.some(t=>t.id===scope.team) && <option value={scope.team}>{scope.team}（已有归属）</option>}{teams.filter(t=>!fixedTeam || t.id===scope.team).map(t=><option key={t.id} value={t.id}>{t.name}{t.legacy?'（历史 ID）':''}</option>)}
  </select></Field>}</div>{directory.loading && <Loading/>}<Feedback error={error || directory.error}/>{directory.error && <button type="button" className="secondary" onClick={directory.reload}>重新加载目录</button>}
  <div className="actions">{directory.data?.can_create_org && <button type="button" className="secondary" disabled={busy} onClick={()=>{setCreating('org');setName('');setError('');}}>新增组织</button>}{!optionalOrg && directory.data?.can_create_department && scope.org && <button type="button" className="secondary" disabled={busy} onClick={()=>{setCreating('team');setName('');setError('');}}>新增部门</button>}</div>
  {creating && <div className="notice"><Field label={creating==='org'?'新组织名称':'新部门名称'}><input value={name} maxLength={200} disabled={busy} onChange={e=>setName(e.target.value)} onKeyDown={e=>{if(e.key==='Enter'){e.preventDefault();void create();}}}/></Field><p>输入中文名称即可，系统自动生成 ID。只创建目录，不修改已有用户归属或授权。</p><button type="button" className="secondary" disabled={busy || !name.trim()} onClick={()=>void create()}>{busy?'创建中…':'创建并选择'}</button> <button type="button" className="secondary" disabled={busy} onClick={()=>setCreating(null)}>取消</button></div>}</>;
}
