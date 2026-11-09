import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import type { User, Identity } from './api';
import { post } from './api';
import type { Page } from './Management';
import { Modal, Form, Field, Empty, Feedback } from './ui';
import { SearchableSelect } from './SearchableSelect';
import { HelpSections } from './HelpSections';
import { getPageHelp } from './pageHelpContent';
import './IntegrationWorkspace.css';

type Provider = 'feishu' | 'dingtalk';
type Section = 'overview' | 'connection' | 'members' | 'oauth';
const sections: { id: Section; title: string; description: string }[] = [
  { id: 'overview', title: '接入概况', description: '查看连接状态，选择下一步操作。' },
  { id: 'connection', title: '机器人连接', description: '填写当前平台应用配置，再核对实际连接状态。' },
  { id: 'members', title: '员工接入', description: '员工先私聊机器人，再处理发现记录并核对身份。' },
  { id: 'oauth', title: '个人文档授权', description: '需要读写本人文档时，再配置授权应用和可用人员。' },
];
export function IntegrationWorkspace({ user, identities, users, loading, error, reload, navigate, renderPanel }: {
  user: User; identities: Identity[]; users: User[]; loading: boolean; error: string; reload: () => void;
  navigate: (page: Page) => void; renderPanel: (section: Section, provider: Provider) => ReactNode;
}) {
  const [provider, setProvider] = useState<Provider>('feishu');
  const [section, setSection] = useState<Section>('overview');
  const [visited, setVisited] = useState(['feishu:overview']);
  const [help, setHelp] = useState(false);
  const [search, setSearch] = useState('');
  const [binding, setBinding] = useState(false);
  const [target, setTarget] = useState('');
  const [externalId, setExternalId] = useState('');
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState('');
  const admin = user.role === 'super_admin';
  const platform = provider === 'feishu' ? '飞书' : '钉钉';
  function select(nextProvider: Provider, nextSection: Section) {
    setProvider(nextProvider); setSection(nextSection); setHelp(false); setSearch(''); setNotice('');
    setVisited(previous => [...new Set([...previous, `${nextProvider}:${nextSection}`])]);
  }
  const rows = identities.filter(row => row.provider === provider);
  const name = (id: string) => users.find(item => String(item.id) === String(id))?.name ?? id;
  const matches = rows.filter(row => `${row.external_user_id} ${name(String(row.user_id))}`.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()));
  const topics = getPageHelp('integrations', user.role).sections ?? [];
  const helpTopics = topics.filter(item => section === 'connection' ? item.title.startsWith(`${platform}：`) || item.title === '接入模式怎么选'
    : section === 'oauth' ? /个人文档授权|OAuth|群聊授权/.test(item.title)
    : section === 'members' ? /群聊如何调用|权限自查|分给个人与群|卡住/.test(item.title)
    : /四种权限|私聊里调用|常用指令/.test(item.title));
  const current = sections.find(item => item.id === section)!;
  return <div className="integration-workspace">
    <div className="integration-platforms" role="group" aria-label="选择平台">{(['feishu', 'dingtalk'] as const).map(item => <button type="button" key={item} aria-pressed={item === provider} onClick={() => select(item, section)}>{item === 'feishu' ? '飞书' : '钉钉'}</button>)}</div>
    <nav className="integration-sections" aria-label="集成操作分区">{sections.map(item => <button type="button" key={item.id} aria-current={section === item.id ? 'page' : undefined} onClick={() => select(provider, item.id)}>{item.title}</button>)}</nav>
    <div className="integration-heading"><div><h2>{platform} · {current.title}</h2><p>{current.description}</p></div><button className="secondary" type="button" aria-expanded={help} onClick={() => setHelp(!help)}>{help ? '收起操作说明' : '查看操作说明'}</button></div>
    <div className={`integration-layout ${help ? 'with-help' : ''}`}>
      <div className="integration-main">
        {notice && <p role="status" className="notice">{notice}</p>}
        {/* Keep visited forms mounted so platform/section switching never discards a draft. */}
        {visited.map(key => { const [p, s] = key.split(':') as [Provider, Section]; return <div key={key} hidden={provider !== p || section !== s}>{admin && renderPanel(s, p)}</div>; })}
        {admin && section === 'connection' && <div className="actions integration-next"><button type="button" className="primary" onClick={() => select(provider, 'members')}>下一步：接入员工</button><button type="button" className="text-button" onClick={() => select(provider, 'oauth')}>需要本人文档权限？配置个人授权</button></div>}
        {section === 'overview' && <>
          <p className="notice">配置已保存、连接已建立、消息收发成功是不同状态。请按下面步骤逐项验证。</p>
          <div className="integration-steps">{[
            ['1', '连接机器人', '保存应用配置，查看实际连接状态。', () => select(provider, 'connection')],
            ['2', '接入员工', '员工先发消息，再处理身份绑定。', () => select(provider, 'members')],
            ['3', '分配能力', '分别核对用户与群的 Skill / MCP 授权。', () => navigate('bindings')],
          ].map(([number, title, description, action]) => <button type="button" key={String(number)} className="quick-card" onClick={action as () => void}><small>步骤 {String(number)}</small><h3>{String(title)}</h3><p>{String(description)}</p><span>前往处理 →</span></button>)}</div>
          <div className="panel"><h3>最后，验证一次真实调用</h3><p>接入后私聊发送 <code>/help</code>、<code>/status</code>；群里需 @ 机器人。确认身份与能力清单，再发送一个简单任务。这里不会自动发送测试消息。</p><div className="actions"><button className="secondary" onClick={() => navigate('groups')}>到协作群组处理群聊</button><button className="text-button" onClick={() => navigate('manual')}>阅读完整说明书</button></div></div>
        </>}
        {!admin && section !== 'members' && <p className="notice">平台配置与全局连接状态仅超级管理员可查看。你可以查阅操作说明，或联系管理员完成配置。</p>}
        {section === 'members' && <section className="panel">
          <div className="section-heading"><h3>已绑定身份</h3>{admin && <button className="secondary" disabled={loading} onClick={() => { setTarget(''); setExternalId(''); setBinding(true); }}>手动绑定身份</button>}</div>
          <p>优先从待接入发现处理；手动绑定适用于已经核对外部 ID 的员工。群登记与成员确认在协作群组完成。</p>
          <label className="field"><span>搜索已绑定身份</span><input type="search" value={search} onChange={event => setSearch(event.target.value)} placeholder="搜索成员姓名或外部用户 ID"/></label>
          <Feedback error={error}/>{error && <button className="secondary" onClick={reload}>重试身份列表</button>}
          {loading && <p role="status">正在刷新身份，上次结果暂时保留…</p>}
          <p className="muted" role="status">{platform} · 显示 {matches.length} / {rows.length} 条身份</p>
          {matches.length ? <div className="table-wrap"><table><thead><tr><th>内部成员</th><th>外部用户 ID</th><th>平台</th></tr></thead><tbody>{matches.map(row => <tr key={`${row.provider}:${row.external_user_id}`}><td>{name(String(row.user_id))}</td><td>{row.external_user_id}</td><td>{platform}</td></tr>)}</tbody></table></div> : <Empty text={search ? '没有匹配的身份' : '暂无已绑定身份'} description={search ? '试试成员姓名或完整外部 ID。' : '让员工私聊机器人，再由超级管理员处理接入。'}/>}
          {search && <button className="text-button" onClick={() => setSearch('')}>清空搜索</button>}
          <button className="secondary" onClick={() => navigate('groups')}>到协作群组处理群聊</button>
        </section>}
      </div>
      {help && <ContextHelp title={`${platform}${current.title}操作说明`} close={() => setHelp(false)}><div className="section-heading"><h3>操作说明</h3><button type="button" className="text-button" onClick={() => setHelp(false)}>关闭说明</button></div><p>按需展开；关闭说明不会丢失表单输入。</p><HelpSections sections={helpTopics}/><button className="secondary" onClick={() => navigate('manual')}>打开完整说明书</button></ContextHelp>}
    </div>
    {binding && <Modal title={`绑定${platform}身份`} dirty={!!(externalId || target)} busy={saving} close={() => setBinding(false)}>
      <p>只绑定当前{platform}账号；不会创建网页账号或自动授予 Skill/MCP。</p>
      <Form label="确认绑定" submit={async () => { setSaving(true); try { return await post('/identities', { provider, external_user_id: externalId.trim(), user_id: target }); } finally { setSaving(false); } }} onSuccess={() => { setBinding(false); setNotice('身份已绑定，请让员工重新发送消息；能力仍需单独授权。'); reload(); }}>
        <Field label={provider === 'feishu' ? '飞书 open_id' : '钉钉 senderStaffId / userid'}><input required value={externalId} onChange={event => setExternalId(event.target.value)} autoComplete="off"/></Field>
        <SearchableSelect label="内部成员" value={target} options={users.filter(item => item.active).map(item => ({ id: String(item.id), name: `${item.name} · ${item.email}` }))} onChange={setTarget} required/>
        <p>请核对所选成员，不能仅凭昵称相同判断为同一人。</p>
      </Form>
    </Modal>}
  </div>;
}

function ContextHelp({ title, close, children }: { title: string; close: () => void; children: ReactNode }) {
  const [compact, setCompact] = useState(() => window.matchMedia('(max-width: 1100px)').matches);
  useEffect(() => {
    const query = window.matchMedia('(max-width: 1100px)');
    const update = () => setCompact(query.matches);
    query.addEventListener('change', update);
    return () => query.removeEventListener('change', update);
  }, []);
  return compact ? <Modal title={title} close={close}><div className="integration-help-mobile">{children}</div></Modal> : <aside className="integration-help" aria-label={title}>{children}</aside>;
}
