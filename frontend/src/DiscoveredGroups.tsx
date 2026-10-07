import { useState } from 'react';
import { ScopeFields } from './ScopeFields';
import { dateText, post } from './api';
import type { Group } from './api';
import { useData, Empty, Loading, Feedback, Modal, Form, Field, value } from './ui';
interface Member { role?: string; id: string; name: string; org_id: string | null; team_id: string | null }
interface FoundGroup { id: string; provider: string; external_id: string; name: string | null; first_seen: string; last_seen: string; members: Member[] }
const platform = (provider: string) => provider === 'feishu' ? '飞书' : '钉钉';
export function DiscoveredGroups({ groups, reloadGroups }: { groups: Group[]; reloadGroups: () => void }) {
  const list = useData<FoundGroup[]>('/im/discoveries/groups');
  const [selected, setSelected] = useState<FoundGroup | null>(null); const [success, setSuccess] = useState('');
  return <section className="panel"><div className="section-heading"><h2>已发现群 / 待绑定</h2><button className="secondary" disabled={list.loading} onClick={() => { list.reload(); reloadGroups(); }}>同步已发现数据</button></div><p>仅同步系统已发现的当前应用群聊，已关联群显示在上方。这里不代表平台全部群或全租户通讯录。群名未采集时显示真实外部 ID；发现数据不会自动授予成员或 Skill / MCP 权限。</p><Feedback error={list.error} success={success}/>{list.error && <button className="secondary" onClick={list.reload}>重试同步</button>}{list.loading ? <Loading/> : !list.data?.length ? <Empty text="暂无待绑定的已发现群" description="已绑定群见上方；系统尚未发现的群需先让机器人收到该群消息。"/> : <div className="table-wrap"><table><thead><tr>{['群名 / 外部 ID', '来源', '首次 / 最近发现', '状态', '操作'].map(h => <th key={h}>{h}</th>)}</tr></thead><tbody>{list.data.map(row => <tr key={row.id}><td>{row.name || row.external_id}{row.name && <small>{row.external_id}</small>}</td><td>{platform(row.provider)} · 系统发现</td><td>{dateText(row.first_seen)}<br/>{dateText(row.last_seen)}</td><td>待绑定</td><td><button className="secondary" onClick={() => setSelected(row)}>绑定 / 一键带入</button></td></tr>)}</tbody></table></div>}{selected && <Modal title="绑定已发现群" close={() => setSelected(null)}><BindFoundGroup key={selected.id} found={selected} groups={groups} saved={() => { setSelected(null); list.reload(); reloadGroups(); setSuccess('群数据已关联，已确认成员关系；未新增任何 Skill / MCP 授权。'); }}/></Modal>}</section>;
}
function BindFoundGroup({ found, groups, saved }: { found: FoundGroup; groups: Group[]; saved: () => void }) {
  const orgs = [...new Set(found.members.map(m => m.org_id).filter(Boolean))];
  const initialOrg = orgs.length === 1 ? orgs[0]! : '';
  const teams = [...new Set(found.members.filter(m => m.org_id === initialOrg).map(m => m.team_id))];
  const [scope, setScope] = useState({org: initialOrg, team: teams.length === 1 ? teams[0] || '' : ''});
  const [target, setTarget] = useState('new');
  const {org, team} = scope;
  const available = found.members.filter(m => org && (m.role === 'super_admin' || (m.org_id === org && (!team || m.team_id === team))));
  const eligible = new Set(found.members.map(m => m.id));
  const existing = groups.filter(g => !g.external_id && (g.provider === 'web' || g.provider === found.provider) && g.member_ids.length > 0 && g.member_ids.every(id => eligible.has(String(id))));
  const chosen = existing.find(g => g.id === target);
  return <Form label="确认绑定" submit={f => post(`/im/discoveries/groups/${found.id}/bind`, target === 'new' ? { name: value(f, 'name'), org_id: org, team_id: team || null, member_ids: f.getAll('members').map(String), confirm_members: f.has('confirm_members') } : { group_id: target, confirm_members: f.has('confirm_members') })} onSuccess={saved}><p>来源：{platform(found.provider)}<br/>外部群 ID：{found.external_id}（自动带入）</p><Field label="绑定方式"><select value={target} onChange={e => setTarget(e.target.value)}><option value="new">一键带入创建协作群</option>{existing.map(g => <option key={g.id} value={g.id}>关联已有群：{g.name}</option>)}</select></Field>{target === 'new' ? <><Field label="群名称"><input name="name" required maxLength={200} defaultValue={found.name || found.external_id}/></Field><ScopeFields scope={scope} change={setScope}/><p>超级管理员只有明确加入本群后才能参与，身份保持全局；群能力限于本群组织和全局资源，且仍须双重授权。</p><p>请人工勾选成员；候选仅包含当前应用已明确绑定身份的用户。</p><div className="check-list" key={`${org}/${team}`}>{available.map(m => <label key={m.id}><input name="members" type="checkbox" value={m.id}/>{m.name}</label>)}</div>{!available.length && <p>暂无可选成员。请先在下方「群聊发送者待接入」接入群里的发送者，再同步已发现数据。</p>}</> : <p>组织 / 团队：{chosen?.org_name ?? chosen?.org_id} / {chosen?.team_name ?? chosen?.team_id ?? '组织级'}<br/>保留成员：{chosen?.member_ids.map(id => found.members.find(m => m.id === id)?.name || id).join('、')}</p>}<label className="checkbox"><input name="confirm_members" type="checkbox" required/>我确认群映射及上述成员关系，不新增 Skill / MCP 授权</label></Form>;
}
