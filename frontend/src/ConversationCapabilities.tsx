import { useState } from 'react';
import { api, post, sameId } from './api';
import type { Binding, Conversation, ID, Resource, User } from './api';
import { Empty, Feedback, Field, Form, Loading, Modal, useData } from './ui';
import { SearchableSelect } from './SearchableSelect';

export function ConversationCapabilities({ conversation, user, subjectUserId, kind, close, changed }: {
  conversation: Conversation; user: User; subjectUserId?: ID; kind: 'skill' | 'mcp'; close: () => void; changed: () => void;
}) {
  const resources = useData<Resource[]>('/resources', 0, true);
  const bindings = useData<Binding[]>('/bindings', 0, true);
  const [target, setTarget] = useState<'user' | 'group'>('user');
  const [resource, setResource] = useState('');
  const [busy, setBusy] = useState(false);
  const [remove, setRemove] = useState<Binding | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const subjectId = target === 'group' ? conversation.group_id! : subjectUserId ?? (conversation.group_id != null ? user.id : conversation.owner_id);
  const direct = (bindings.data ?? []).filter(b => b.subject_type === target && sameId(b.subject_id, subjectId) && resources.data?.some(r => sameId(r.id, b.resource_id) && r.kind === kind));
  const options = (resources.data ?? []).filter(r => r.kind === kind && r.enabled && r.can_manage && !direct.some(b => sameId(b.resource_id, r.id)));
  const loading = resources.loading || bindings.loading;
  const loadError = resources.error || bindings.error;
  const title = kind === 'skill' ? '管理 Skills 授权' : '管理 MCP 授权';
  function refresh() { bindings.reload(); changed(); }
  return <Modal title={title} dirty={!!resource} busy={busy} close={close}>
    <div className="conversation-capability-settings">
      <p>为「{conversation.title}」配置授权。此处修改的是用户或群组授权，会影响该对象的其他会话，并非仅本次会话。</p>
      {conversation.group_id != null && <p className="notice">群聊只使用当前用户与群组共同获授权的能力；需要分别核对两侧。组织或部门继承的授权在「绑定与授权」管理。</p>}
      <Field label="授权对象"><select value={target} disabled={busy} onChange={e => { setTarget(e.target.value as 'user' | 'group'); setResource(''); setRemove(null); setError(''); setNotice(''); }}><option value="user">{conversation.group_id != null ? sameId(subjectId, user.id) ? '当前用户' : '会话所属用户' : '会话所属用户'}</option>{conversation.group_id != null && <option value="group">当前群组</option>}</select></Field>
      <Feedback error={loadError || error} success={notice}/>
      {loadError && <button className="secondary" onClick={() => { resources.reload(); bindings.reload(); }}>重试加载授权</button>}
      {loading && <Loading/>}
      <Form label="添加授权" submitDisabled={loading || !!loadError || !options.some(r => sameId(r.id, resource)) || busy} submit={async () => {
        setBusy(true); setError('');
        try { return await post('/bindings', { subject_type: target, subject_id: subjectId, resource_id: resource }); }
        finally { setBusy(false); }
      }} onSuccess={() => { setResource(''); setNotice('授权已添加，正在刷新当前会话的有效能力。'); refresh(); }}>
        <SearchableSelect label={kind === 'skill' ? '选择 Skill' : '选择 MCP'} value={resource} onChange={setResource} options={options.map(r => ({ id: r.id, name: r.name }))} required disabled={loading || busy}/>
      </Form>
      {!loading && !loadError && !options.length && <p className="muted">没有可添加的能力。请先在「Skill和MCP管理」创建并启用，或检查管理范围。</p>}
      <h3>此对象的直接授权</h3>
      {!loading && !loadError && !direct.length && <Empty text="暂无直接授权" description="仍可能通过组织或部门获得能力，以会话右侧的有效能力为准。"/>}
      {direct.map(b => <div className="capability-direct" key={b.id}><span>{resources.data?.find(r => sameId(r.id, b.resource_id))?.name}</span>{b.can_manage && <button className="danger-button" disabled={busy || loading} onClick={() => { setRemove(b); setError(''); }}>撤销</button>}</div>)}
      {remove && <div className="notice" role="alert"><p>确认撤销该对象的直接授权？其他会话也会受影响；组织或部门继承的授权不受此次撤销影响。</p><div className="actions"><button className="secondary" disabled={busy} onClick={() => setRemove(null)}>取消撤销</button><button className="danger-button" disabled={busy} onClick={async () => { setBusy(true); setError(''); try { await api(`/bindings/${encodeURIComponent(remove.id)}`, { method: 'DELETE' }); setRemove(null); setNotice('直接授权已撤销，正在刷新有效能力。'); refresh(); } catch (e) { setError(e instanceof Error ? e.message : '撤销失败，请重试'); } finally { setBusy(false); } }}>{busy ? '正在撤销…' : '确认撤销'}</button></div></div>}
    </div>
  </Modal>;
}
