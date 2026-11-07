import { useEffect, useState } from 'react';
import { SearchableSelect } from './SearchableSelect';
import { api, errorText } from './api';
import type { Group, User } from './api';
import { Badge, Empty, Feedback, Loading, Modal } from './ui';
import './BindingPreview.css';

type Source = { side: 'user' | 'group'; subject_type: string; subject_id: string };
type Resource = { id: string; name: string; kind: string; enabled: boolean; effective: boolean; reasons: string[]; sources: Source[] };
type Result = { user_id: string; group_id: string | null; can_send: boolean; context_reasons: string[]; resources: Resource[] };
const reasons: Record<string, string> = { cannot_send: '该用户不能在此会话发送任务', disabled: '资源已停用', user_not_granted: '用户侧未授权', group_not_granted: '群侧未授权', outside_scope: '不在资源适用范围', inactive_user: '用户已停用', archived_group: '群组已归档', not_group_member: '用户不是该群成员', outside_group_scope: '用户归属不符合群组范围' };
const sourceNames: Record<string, string> = { user: '直接授权', group: '群组授权', team: '部门授权', org: '组织授权' };
export function BindingPreview({ users, groups, loading, error, close, initialUser = '', initialGroup = '', onSelection, notice }: { onSelection?: (selection: { user: string; group: string }) => void; notice?: string; initialGroup?: string; users: User[]; groups: Group[]; loading: boolean; error: string; close: () => void; initialUser?: string }) {
  const [user, setUser] = useState(initialUser);
  const [group, setGroup] = useState(initialGroup);
  const [result, setResult] = useState<Result | null>(null);
  const [pending, setPending] = useState(false);
  const [problem, setProblem] = useState('');
  const [revision, setRevision] = useState(0);
  const available = users.some(item => String(item.id) === user) && (!group || groups.some(item => String(item.id) === group));
  useEffect(() => {
    const controller = new AbortController();
    setResult(null); setProblem(''); setPending(false);
    if (!user || !available || loading || error) return () => controller.abort();
    setPending(true);
    const query = new URLSearchParams({ user_id: user, ...(group ? { group_id: group } : {}) });
    void api<Result>(`/bindings/preview?${query}`, { signal: controller.signal })
      .then(data => { if (!controller.signal.aborted) setResult(data); })
      .catch(err => { if (!controller.signal.aborted) setProblem(errorText(err)); })
      .finally(() => { if (!controller.signal.aborted) setPending(false); });
    return () => controller.abort();
  }, [user, group, available, loading, error, revision]);
  const current = available && !loading && !error && result?.user_id === user && (result.group_id ?? '') === group ? result : null;
  return <Modal title="授权生效预览" drawer close={close}>
    <div className="binding-preview">
      <p className="muted">基于当前已保存授权，不会发起任务。仅展示你有资源管理权限的资源；授权可用不代表模型或远端工具连接正常。</p>
      {notice && <p className="notice">{notice}</p>}
      <SearchableSelect label="预览用户" value={user} options={users.map(item => ({ id: item.id, name: `${item.name}${item.active ? '' : '（已停用）'}` }))} disabled={loading} placeholder="请选择用户" onChange={value => { setUser(value); setResult(null); onSelection?.({ user: value, group }); }}/>
      <SearchableSelect label="会话范围" value={group} options={groups.map(item => ({ id: item.id, name: `群聊 · ${item.name}` }))} disabled={loading} placeholder="本人私聊" onChange={value => { setGroup(value); setResult(null); onSelection?.({ user, group: value }); }}/>
      <button type="button" className="secondary" disabled={!user || !available || loading || pending} onClick={() => setRevision(value => value + 1)}>重新预览</button>
      <Feedback error={error || problem}/>
      {loading || pending ? <Loading/> : !user ? <Empty text="选择用户查看生效权限" description="私聊看用户授权；群聊同时检查用户与群两侧授权。"/> : current && <>
        {!current.can_send && <div className="notice" role="status">{current.context_reasons.map(code => reasons[code] ?? code).join('；') || '该用户不能在此会话发送任务'}</div>}
        {([true, false] as const).map(effective => {
          const rows = current.resources.filter(item => item.effective === effective);
          return <section key={String(effective)} className="preview-section"><h3>{effective ? '可使用' : '未生效'} <Badge tone={effective ? 'green' : ''}>{rows.length}</Badge></h3>
            {rows.length ? <ul>{rows.map(item => <li key={item.id}><div className="preview-resource"><strong>{item.name}</strong><Badge>{item.kind === 'skill' ? 'Skill' : 'MCP'}</Badge></div>
              {!effective && <p>{item.reasons.map(code => reasons[code] ?? code).join('；')}</p>}
              <small>{item.sources.length ? [...new Set(item.sources.map(source => `${source.side === 'user' ? '用户侧' : '群侧'} · ${sourceNames[source.subject_type] ?? source.subject_type}`))].join('；') : '没有匹配的授权来源'}</small>
            </li>)}</ul> : <p className="muted">{effective ? '当前没有可使用的资源。' : '没有未生效的资源。'}</p>}
          </section>;
        })}
      </>}
    </div>
  </Modal>;
}
