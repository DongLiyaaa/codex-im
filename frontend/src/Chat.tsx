import { useEffect, useRef, useState } from 'react';
import { Bot, Send, Plus, Search, LockKeyhole, MessageSquare, Sparkles, Plug, RefreshCw, LoaderCircle, CircleAlert, Trash2 } from 'lucide-react';
import { api, post, sameId, dateText, errorText, ApiError } from './api';
import type { User, Group, Conversation, Message, Resource, Run, ID, ConversationState } from './api';
import { MessageWorkStatus } from './MessageWorkStatus';
import { useData, Loading, Empty, Feedback, Modal, Form, Field, value, Badge } from './ui';

export function Chat({ user, conversationId, onSelect }: { user: User; conversationId?: string | null; onSelect?: (id: string | null, replace?: boolean) => void }) {
  const [deleting, setDeleting] = useState<Conversation | null>(null); const [deleteBusy, setDeleteBusy] = useState(false); const [deleteError, setDeleteError] = useState('');
  const deleteLock = useRef(false);
  const freshSelection = useRef<string | null>(null);
  function selectConversation(c: Conversation | null, replace = false) { setSelected(c); onSelect?.(c ? String(c.id) : null, replace); }

  const conversations = useData<Conversation[]>('/conversations'); const groups = useData<Group[]>('/groups');
  const [selected, setSelected] = useState<Conversation | null>(null); const [search, setSearch] = useState(''); const [open, setOpen] = useState(false);

  const activeId = useRef<ID | null>(null); const bottom = useRef<HTMLDivElement>(null); const sendLock = useRef(false);
  const requestVersion = useRef(0); const selectionEpoch = useRef(0);
  const group = groups.data?.find(g => sameId(g.id, selected?.group_id));
  const writable = !!selected && (selected.group_id != null ? !!group?.member_ids.some(id => sameId(id, user.id)) && (user.role === 'super_admin' || (sameId(group.org_id, user.org_id) && (!group.team_id || sameId(group.team_id, user.team_id)))) : sameId(selected.owner_id, user.id));

  const [draft, setDraft] = useState(''); const [sending, setSending] = useState(false); const [denied, setDenied] = useState(false); const [sendError, setSendError] = useState('');

  const [stateData, setStateData] = useState<ConversationState | null>(null);
  const [stateError, setStateError] = useState('');
  const [stateRevision, setStateRevision] = useState(0);
  const [capData, setCapData] = useState<{ skills: Resource[]; mcps: Resource[] } | null>(null);
  const [capError, setCapError] = useState('');
  const [capLoading, setCapLoading] = useState(false);
  const [capRevision, setCapRevision] = useState(0);
  const reloadCap = () => setCapRevision(v => v + 1);
  useEffect(() => {
    const controller = new AbortController(); let timer: ReturnType<typeof setTimeout> | undefined;
    setStateData(null); setStateError('');
    const id = selected?.id;
    async function load() {
      if (!id) return;
      try { const data = await api<ConversationState>(`/conversations/${encodeURIComponent(id)}/state`, { signal: controller.signal }); if (!controller.signal.aborted) { setStateData(data); setStateError(''); } }
      catch (e) { if (!controller.signal.aborted) { setStateData(null); setStateError(errorText(e)); } }
      finally { if (!controller.signal.aborted) timer = setTimeout(load, 3000); }
    }
    if (id) { void api(`/conversations/${encodeURIComponent(id)}/messages`, { signal: controller.signal }).catch(() => {}); void load(); }
    return () => { controller.abort(); if (timer) clearTimeout(timer); };
  }, [selected?.id, stateRevision]);
  useEffect(() => {
    const controller = new AbortController(); setCapData(null); setCapError(''); setCapLoading(!!selected);
    if (selected) void api<{ skills: Resource[]; mcps: Resource[] }>(`/conversations/${encodeURIComponent(selected.id)}/capabilities`, { signal: controller.signal }).then(data => { if (!controller.signal.aborted) setCapData(data); }).catch(e => { if (!controller.signal.aborted) setCapError(errorText(e)); }).finally(() => { if (!controller.signal.aborted) setCapLoading(false); });
    return () => controller.abort();
  }, [selected?.id, capRevision]);

  const messages = stateData?.messages ?? [];
  const run = stateData?.active_run ?? stateData?.latest_run ?? null;
  const loading = !stateData && !stateError && !!selected;
  const error = stateError ? errorText(stateError) : sendError;

  const busy = sending || run?.status === 'queued' || run?.status === 'running';

  useEffect(() => {
    if (!conversations.data || conversations.loading) return;
    const available = conversations.data;
    if (conversationId) {
      const target = available.find(c => sameId(c.id, conversationId));
      if (target) { freshSelection.current = null; setSelected(target); }
      else if (freshSelection.current !== conversationId) selectConversation(null, true);
    } else if (conversationId === undefined && !selected && available.length) setSelected(available[0]);
    else if (conversationId === null) setSelected(null);
    else if (selected && !available.some(c => sameId(c.id, selected.id))) selectConversation(null, true);
  }, [conversations.data, conversations.loading, conversationId]);

  async function removeConversation() {
    if (!deleting || deleteLock.current) return;
    const target = deleting; deleteLock.current = true; setDeleteBusy(true); setDeleteError('');
    try {
      await api(`/conversations/${encodeURIComponent(target.id)}`, { method: 'DELETE' });
      if (sameId(activeId.current, target.id)) selectConversation(null, true);
      setDeleting(null); conversations.reload();
    } catch (e) { setDeleteError(errorText(e)); conversations.reload(); }
    finally { deleteLock.current = false; setDeleteBusy(false); }
  }

  useEffect(() => {
    activeId.current = selected?.id ?? null; setDraft(''); setDenied(false); setSendError(''); requestVersion.current++; selectionEpoch.current++;
  }, [selected?.id]);

  useEffect(() => { bottom.current?.scrollIntoView({ behavior: 'smooth', block: 'end' }); }, [messages.length, selected?.id]);

  async function send() {
    if (!selected || !draft.trim() || !writable || denied || busy || sendLock.current) return;
    const id = selected.id; const epoch = selectionEpoch.current; const content = draft.trim(); sendLock.current = true; requestVersion.current++; setSending(true); setSendError('');
    try {
      await post<{ user_message: Message; run: Run }>(`/conversations/${encodeURIComponent(id)}/messages`, { content });
      requestVersion.current++;
      if (sameId(activeId.current, id) && selectionEpoch.current === epoch) {
        setStateRevision(v => v + 1);
        setDraft('');
      }
    } catch (e) { if (sameId(activeId.current, id) && selectionEpoch.current === epoch) { setSendError(errorText(e)); if (e instanceof ApiError && e.status === 403) setDenied(true); } }
    finally { sendLock.current = false; setSending(false); }
  }

  const filtered = conversations.data?.filter(c => c.title.toLowerCase().includes(search.toLowerCase())) ?? [];
  return <div className="chat-layout"><aside className="conversation-panel"><div className="conversation-heading"><h2>会话</h2><button className="icon-button" aria-label="创建会话" onClick={() => setOpen(true)}><Plus size={19}/></button></div><label className="search"><Search size={16}/><input aria-label="搜索会话" placeholder="搜索会话…" value={search} onChange={e => setSearch(e.target.value)}/></label><Feedback error={conversations.error}/><div className="conversation-list">{conversations.loading ? <Loading/> : filtered.length ? filtered.map(c => <div className="conversation-row" key={c.id}><button className={`conversation-item ${sameId(selected?.id, c.id) ? 'selected' : ''}`} key={c.id} onClick={() => selectConversation(c)}><MessageSquare size={18}/><div><strong>{c.title}</strong><span>{c.group_id ? '群组会话' : sameId(c.owner_id, user.id) ? '我的会话' : '监管会话 · 只读'}</span></div></button>{c.can_delete && <button className="icon-button" aria-label={`移除会话 ${c.title}`} title="从工作台移除，保留历史与审计" onClick={() => { setDeleteError(''); setDeleting(c); }}><Trash2 size={16}/></button>}</div>) : <Empty text={search ? '没有匹配会话' : '暂无会话'} description="点击上方加号创建会话。"/>}</div><button className="text-button list-refresh" onClick={conversations.reload}><RefreshCw size={14}/>刷新会话列表</button></aside>
  <section className="chat-main">{selected ? <><div className="chat-header"><div><h2>{selected.title}</h2><span>{selected.group_id ? group?.name ?? '群组会话' : '私聊会话'} · {writable && !denied ? '协作空间' : '只读监管'}</span></div><button className="icon-button" aria-label="刷新消息与能力" onClick={() => { setStateRevision(v => v + 1); reloadCap(); }} disabled={loading}><RefreshCw size={18}/></button></div><div className="message-list" aria-live="polite">{loading ? <Loading/> : messages.length ? messages.map(m => <article key={m.id} className={`message ${m.role === 'user' ? 'from-user' : ''}`}><span className="message-avatar">{m.role === 'assistant' ? <Bot size={20}/> : m.role === 'user' ? <MessageSquare size={18}/> : <CircleAlert size={18}/>}</span><div className="message-body"><div className="message-meta"><strong>{m.role === 'assistant' ? 'Agent' : m.role === 'user' ? '用户' : '系统'}</strong><time>{dateText(m.created_at)}</time></div><div className="bubble">{m.content}</div><MessageWorkStatus message={m} messages={messages} run={run}/></div></article>) : <div className="chat-welcome"><span className="agent-symbol"><Bot size={34}/></span><h2>今天，我们一起完成什么？</h2><p>描述你的目标，Agent 将使用当前授权的能力处理任务。</p><span>所有消息与运行结果均来自真实服务</span></div>}<div ref={bottom}/></div><div className="composer-area"><Feedback error={error}/>{run && ['failed', 'cancelled', 'interrupted'].includes(run.status) && <div className="run-state failed" role="alert"><CircleAlert size={16}/><span>任务{run.status === 'failed' ? '执行失败' : '已中断'}{run.error ? `：${run.error}` : ''}</span></div>}{(!writable || denied) && <div className="notice"><LockKeyhole size={16}/>{selected.group_id && groups.loading ? '正在核验群成员权限…' : '当前会话仅供监管查看，不能代替用户发送消息。'}</div>}{selected.group_id && <Feedback error={groups.error}/>}<form className="composer" onSubmit={e => { e.preventDefault(); void send(); }}><textarea aria-label="消息内容" maxLength={16000} value={draft} onChange={e => setDraft(e.target.value)} disabled={!writable || denied || busy || loading} rows={3} placeholder={writable && !denied ? '输入任务或问题，开始与 Agent 协作…' : '只读会话，无法发送消息'} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void send(); } }}/><div className="composer-bottom"><span>Enter 发送 · Shift + Enter 换行</span><button className="primary" type="submit" disabled={!draft.trim() || !writable || denied || busy || loading}>{sending ? <LoaderCircle size={16} className="spin"/> : <Send size={16}/>}发送</button></div></form><small className="composer-hint">Agent 输出请结合业务事实核实。资源权限将在执行前再次校验。</small></div></> : <Empty text="选择或创建会话" description="在左侧开启你的第一段智能协作。"/>}</section>
  <aside className="capability-panel"><div className="conversation-heading"><h2>当前能力</h2><ShieldIcon/></div><p className="muted">由当前会话的有效授权决定</p>{!selected ? <Empty text="尚未选择会话" description="选择后查看可用能力。"/> : capLoading ? <Loading/> : <><Feedback error={capError ? errorText(capError) : ''}/>{(['skills', 'mcps'] as const).map(type => <div className="capability-section" key={type}><h3>{type === 'skills' ? <Sparkles size={16}/> : <Plug size={16}/>} {type === 'skills' ? 'Skills 技能' : 'MCP 服务'}<Badge>{capData?.[type]?.length ?? 0}</Badge></h3>{capData?.[type]?.length ? capData[type].map((r, index) => <div className="capability" key={r.id ?? index}><strong>{r.name}</strong><p>{r.description || (type === 'skills' ? '已授权技能' : '已授权远程服务')}</p></div>) : <p className="small-empty">{capError ? '未能获取能力数据' : '暂无可用授权'}</p>}</div>)}<div className="capability-note"><LockKeyhole size={18}/><p>群聊能力取用户授权与群组授权的交集。已禁用资源不会参与执行。</p></div></>}</aside>
  {deleting && <Modal title="从工作台移除会话" close={() => { if (!deleteLock.current) setDeleting(null); }}><p>确认移除“{deleting.title}”（{deleting.group_id ? '群组会话，对所有可见成员生效' : '个人私聊'}）？</p><p>此操作将会话归档并从工作台移除，保留历史消息、运行记录及审计，不清除飞书或钉钉平台聊天。IM 后续新消息可开启新会话；旧消息不会重放。</p><Feedback error={deleteError}/><div className="form-actions"><button className="secondary" disabled={deleteBusy} onClick={() => setDeleting(null)}>取消</button><button className="primary" disabled={deleteBusy} onClick={() => void removeConversation()}>{deleteBusy ? '正在移除…' : '确认移除'}</button></div></Modal>}
  {open && <Modal title="创建智能会话" close={() => setOpen(false)}><Form submit={async f => { const groupId = value(f, 'group_id'); const result = await post<Conversation>('/conversations', { title: value(f, 'title'), ...(groupId ? { group_id: groups.data?.find(g => String(g.id) === groupId)?.id ?? groupId } : {}) }); freshSelection.current = String(result.id); selectConversation(result); }} onSuccess={() => { setOpen(false); conversations.reload(); }}><Field label="会话名称"><input name="title" required maxLength={160} placeholder="例如：本周运营分析"/></Field><Field label="会话类型"><select name="group_id" defaultValue=""><option value="">个人私聊</option>{groups.data?.filter(g => g.member_ids.some(id => sameId(id, user.id))).map(g => <option key={g.id} value={g.id}>{g.name}</option>)}</select></Field><Feedback error={groups.error}/>{groups.loading && <Loading/>}<p className="muted">群聊仅显示你已加入的群组。</p></Form></Modal>}</div>;
}
function ShieldIcon() { return <LockKeyhole size={16} className="muted"/>; }
