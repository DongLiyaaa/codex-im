import { useState } from 'react';
import { Copy, Settings2, Users } from 'lucide-react';
import { toast } from 'sonner';
import { api, roles, type Role } from './api';
import { authorizationLabels } from './AuthorizationCard';
import { useData, Loading, Feedback, Modal, Form, Field, value } from './ui';
type Person = { id: string; name: string; role: Role; active: boolean; email: string | null; organization: string | null; department: string | null; account: string | null; authorization: string | null };
type Access = { provider: string; revision: number; user_scope: 'all' | 'specified'; user_ids: string[]; people: Person[] };
type Config = { provider: string; revision: number; configured: boolean; message: string; fields: Record<string,string>; secrets_set: Record<string,boolean>; credential_source?: string; bot_revision?: number; bot_copy_stale?: boolean; bot_app?: { available: boolean; revision: number; snapshot: string; client_id: string; message: string } };
export function OAuthConfig({ provider }: { provider: 'feishu' | 'dingtalk' }) {
  const config = useData<Config>(`/integrations/oauth/${provider}`);
  const [open, setOpen] = useState(false); const [clear, setClear] = useState<string[]>([]);
  const [reuse, setReuse] = useState<Config | null>(null);
  function saved() {
    setOpen(false); setReuse(null); config.reload();
    toast.success('个人授权配置已保存'); window.dispatchEvent(new Event('hub-integration-config-saved'));
  }
  return <section className="panel">
    <div className="section-heading"><h2>{provider === 'feishu' ? '飞书' : '钉钉'}个人 OAuth / CLI 授权配置</h2>
      <div className="actions"><button className="secondary" disabled={!config.data} onClick={() => { setClear([]); setOpen(true); }}><Settings2 size={16}/>配置个人授权</button>
        <button className="secondary" disabled={!config.data?.bot_app?.available} onClick={() => setReuse(config.data)}><Copy size={16}/>使用当前机器人应用（管理员确认）</button></div>
    </div>
    <Feedback error={config.error}/>{config.loading ? <Loading/> : config.data && <>
      <p>{config.data.configured ? '应用字段已配置；设备授权与本人 Identity 仍须验证' : '需要管理员配置：缺少个人 OAuth 应用配置'}</p><p>{config.data.message}</p>
      {config.data.bot_app && <p>{config.data.bot_app.message}</p>}
      {config.data.credential_source === 'bot_app_copy' && <p>来源：机器人应用复制快照，版本 {config.data.bot_revision}；机器人变更不自动同步，需重新确认。</p>}
      {config.data.bot_copy_stale && <Feedback error="机器人应用已变化，复制快照已停用。请重新确认或填写独立应用配置。"/>}
    </>}
    <AccessList provider={provider}/>
    {reuse?.bot_app && <Modal title="确认使用当前机器人应用" close={() => setReuse(null)}>
      <p>将当前应用 {reuse.bot_app.client_id}（机器人版本 {reuse.bot_app.revision}）复制为独立加密 OAuth 配置，替换已有应用字段。密钥仅在服务器内复制，不回显。</p>
      <p>机器人配置变更不会自动同步；旧快照将停用并要求重新确认。复制不代表平台已开通设备授权，也不会自动发起本人授权或修改组织权限。</p>
      <Form label="确认复制机器人应用" submit={() => api(`/integrations/oauth/${provider}/use-bot-app`, {method:'POST', body:JSON.stringify({revision:reuse.revision, bot_revision:reuse.bot_app!.revision, bot_snapshot:reuse.bot_app!.snapshot, confirm:true})})} onSuccess={saved}>
        <button type="button" className="secondary" onClick={() => setReuse(null)}>取消</button>
      </Form>
    </Modal>}
    {open && config.data && <Modal title="个人 OAuth / CLI 授权配置" close={() => setOpen(false)}>
      <Form label="保存个人授权配置" submit={f => api(`/integrations/oauth/${provider}`, {method:'PUT', body:JSON.stringify({revision:config.data!.revision, fields:Object.fromEntries(['CLIENT_ID','CLIENT_SECRET','SCOPES'].map(name => [name,clear.includes(name) ? '' : value(f,name)])),clear})})} onSuccess={saved}>
        {['CLIENT_ID','CLIENT_SECRET','SCOPES'].map(name => <div key={name}><Field label={`PLATFORM_${provider.toUpperCase()}_${name}`} hint={name === 'CLIENT_SECRET' ? config.data!.secrets_set[name] ? '已设置；留空保留，永不回显' : '未设置；不会自动复用机器人密钥' : undefined}><input name={name} type={name === 'CLIENT_SECRET' ? 'password' : 'text'} defaultValue={config.data!.fields[name] ?? ''} readOnly={name === 'SCOPES'} disabled={clear.includes(name)} autoComplete="off" maxLength={4096}/></Field>{name !== 'SCOPES' && <label className="checkbox"><input type="checkbox" checked={clear.includes(name)} onChange={e=>setClear(e.target.checked ? [...clear,name] : clear.filter(x=>x!==name))}/>明确清除 {name}</label>}</div>)}
        <p>仅允许已批准的最小权限。自建应用不保证支持设备授权，请在官方后台确认开通。员工仍需绑定同一应用下的本人平台 Identity。</p>
      </Form>
    </Modal>}
  </section>;
}

// Who may authorize in person. DingTalk's own CLI 可用人员 list has no API, so the platform is opened to everyone there
// and the list kept here decides. People are shown as an administrator knows them: organization, department and the
// platform account bound to them.
const orgOf = (p: Person) => p.organization ?? (p.role === 'super_admin' ? '全局（不属于组织）' : '未分配组织');
const placement = (p: Person) => p.organization ? `${p.organization} / ${p.department ?? '组织级'}` : orgOf(p);
function AccessList({ provider }: { provider: 'feishu' | 'dingtalk' }) {
  const platform = provider === 'feishu' ? '飞书' : '钉钉';
  const access = useData<Access>(`/integrations/oauth/${provider}/access`);
  const [open, setOpen] = useState(false);
  const [scope, setScope] = useState<'all' | 'specified'>('all'); const [chosen, setChosen] = useState<string[]>([]);
  const [filter, setFilter] = useState(''); const [org, setOrg] = useState(''); const [boundOnly, setBoundOnly] = useState(false);
  const people = access.data?.people ?? [];
  const listed = access.data?.user_scope === 'specified' ? access.data.user_ids ?? [] : null;
  const allowedPeople = listed ? listed.map(id => people.find(p => p.id === id) ?? { id, name: '已删除的用户', role: 'member' as Role, active: false, email: null, organization: null, department: null, account: null, authorization: null }) : people.filter(p => p.active && p.account);
  const orgs = [...new Set(people.map(orgOf))];
  const shown = people.filter(p => (p.active || chosen.includes(p.id)) && (!org || orgOf(p) === org) && (!boundOnly || p.account)
    && (!filter || [p.name, p.email, p.organization, p.department, p.account].join(' ').toLowerCase().includes(filter.toLowerCase())));
  const account = (p: Person) => p.account ? `已绑定${platform}账号 ${p.account}` : `未绑定${platform}账号，绑定后才能发起本人授权`;
  const state = (p: Person) => p.authorization ? authorizationLabels[p.authorization] ?? p.authorization : '未发起';
  function edit() { setScope(access.data?.user_scope === 'specified' ? 'specified' : 'all'); setChosen((access.data?.user_ids ?? []).map(String)); setFilter(''); setOrg(''); setBoundOnly(false); setOpen(true); }
  return <>
    <div className="section-heading"><h3>个人授权可用人员</h3><button className="secondary" disabled={!access.data} onClick={edit}><Users size={16}/>设置可用人员</button></div>
    <Feedback error={access.error}/>{access.loading ? <Loading/> : access.data && <>
      <p>{listed ? listed.length ? `指定人员范围可用：以下 ${listed.length} 人可以发起本人${platform}授权。` : '指定人员范围可用：未选择任何人，所有人都不能发起本人授权。'
        : `全员可用：已绑定本人${platform}账号的成员都可以发起本人授权，当前 ${allowedPeople.length} 人。`}</p>
      {allowedPeople.length ? <div className="table-wrap"><table><thead><tr><th>姓名</th><th>组织 / 部门</th><th>角色</th><th>{platform}账号</th><th>本人授权</th></tr></thead>
        <tbody>{allowedPeople.map(p => <tr key={p.id}><td><strong>{p.name}</strong>{!p.active && <> <span className="badge">已停用</span></>}{p.email && <small className="muted"><br/>{p.email}</small>}</td><td>{placement(p)}</td><td>{roles[p.role] ?? p.role}</td><td>{p.account ?? <span className="muted">未绑定，需先在「待接入发现」绑定</span>}</td><td>{state(p)}</td></tr>)}</tbody></table></div>
        : !listed && <p className="im-help">暂无成员绑定{platform}账号。先让员工私聊机器人，再在「待接入发现」为其建立或绑定成员。</p>}
    </>}
    {provider === 'dingtalk' && <p className="im-help">钉钉开发者后台 → CLI设置 → 可用人员设置请选「全员可用」，由这里决定谁能用；钉钉后台的指定人员名单没有接口，Hub 无法替你修改。</p>}
    {open && access.data && <Modal title={`${platform}个人授权可用人员`} close={() => setOpen(false)}>
      <Form label="保存可用人员" submit={() => { if (scope === 'specified' && !chosen.length) throw new Error('请至少选择一位成员，或改为全员可用。'); return api(`/integrations/oauth/${provider}/access`, { method: 'PUT', body: JSON.stringify({ revision: access.data!.revision, user_scope: scope, user_ids: scope === 'specified' ? chosen : [] }) }); }} onSuccess={() => { setOpen(false); access.reload(); toast.success('个人授权可用人员已保存'); }}>
        <label className="checkbox"><input type="radio" name="user_scope" checked={scope === 'all'} onChange={() => setScope('all')}/>全员可用</label>
        <label className="checkbox"><input type="radio" name="user_scope" checked={scope === 'specified'} onChange={() => setScope('specified')}/>指定人员范围可用</label>
        {scope === 'specified' && <>
          <Field label={`已选 ${chosen.length} 人`}><input aria-label="搜索成员" placeholder={`按姓名、组织、部门或${platform}账号搜索`} value={filter} onChange={e => setFilter(e.target.value)}/></Field>
          <div className="form-grid"><Field label="组织"><select aria-label="按组织筛选" value={org} onChange={e => setOrg(e.target.value)}><option value="">全部组织</option>{orgs.map(o => <option key={o} value={o}>{o}</option>)}</select></Field>
            <label className="checkbox"><input type="checkbox" checked={boundOnly} onChange={e => setBoundOnly(e.target.checked)}/>只看已绑定{platform}账号</label></div>
          <div className="actions"><button type="button" className="secondary" disabled={!shown.length} onClick={() => setChosen([...new Set([...chosen, ...shown.map(p => p.id)])])}>选中筛选结果（{shown.length}）</button>
            <button type="button" className="secondary" disabled={!chosen.length} onClick={() => setChosen([])}>清空已选</button></div>
          <div className="check-list people-list">{shown.length ? shown.map(p => <label key={p.id}><input type="checkbox" aria-label={`选择 ${p.name}（${placement(p)}）`} checked={chosen.includes(p.id)} onChange={e => setChosen(e.target.checked ? [...chosen, p.id] : chosen.filter(id => id !== p.id))}/>
            <span><strong>{p.name}</strong> <span className="badge">{roles[p.role] ?? p.role}</span>{!p.active && <> <span className="badge">已停用</span></>}
              <small>{placement(p)}{p.email ? ` · ${p.email}` : ''}</small><small>{account(p)} · 本人授权：{state(p)}</small></span></label>)
            : <p>没有符合筛选条件的成员。</p>}</div>
          <p>不在名单中的成员不能发起本人授权；已保存的本人授权会立即作废。只有已绑定{platform}账号的成员才能真正完成授权。</p>
        </>}
      </Form>
    </Modal>}
  </>;
}
