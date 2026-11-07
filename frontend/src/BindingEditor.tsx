import { useRef, useState } from 'react';
import { SearchableSelect } from './SearchableSelect';
import { api } from './api';
import type { Binding, Group, Resource, User } from './api';
import { resourceScope } from './ResourceCard';
import type { DirectoryNames } from './ResourceCard';
import { Feedback, Field, Form, Modal } from './ui';

const labels = { user: '用户', group: '群组', team: '部门', org: '组织' };
export function BindingEditor({ binding, users, groups, resources, directory, loading, error, close, saved }: {
  binding: Binding; users: User[]; groups: Group[]; resources: Resource[]; directory: DirectoryNames | null;
  loading: boolean; error: string; close: () => void; saved: (selection: Pick<Binding, 'subject_type' | 'subject_id' | 'resource_id'>) => void;
}) {
  const [type, setType] = useState(binding.subject_type);
  const [subject, setSubject] = useState(String(binding.subject_id));
  const [resource, setResource] = useState(String(binding.resource_id));
  const [busy, setBusy] = useState(false);
  const lock = useRef(false);
  const content = useRef<HTMLDivElement>(null);
  const cancel = () => content.current?.closest('dialog')?.dispatchEvent(new Event('cancel', { cancelable: true }));
  const rawOptions = type === 'user' ? users.filter(u => u.active) : type === 'group' ? groups
    : type === 'org' ? (directory?.organizations ?? []).filter(o => !o.legacy)
      : (directory?.departments ?? []).filter(d => !d.legacy).map(d => ({ id: d.id, name: resourceScope({ org_id: d.org_id, team_id: d.id }, directory) }));
  const options = rawOptions.map(option => {
    const located = type === 'user' ? users.find(u => String(u.id) === String(option.id)) : type === 'group' ? groups.find(g => String(g.id) === String(option.id)) : null;
    return { ...option, name: located ? `${option.name} · ${resourceScope(located, directory)}` : option.name };
  });
  const availableResources = resources.filter(r => r.enabled && r.can_manage !== false);
  const missingSubject = !!subject && !options.some(o => String(o.id) === subject);
  const missingResource = !!resource && !availableResources.some(r => String(r.id) === resource);
  const changed = type !== binding.subject_type || subject !== String(binding.subject_id) || resource !== String(binding.resource_id);
  return <Modal title="编辑授权" drawer dirty={changed} busy={busy} close={() => { if (!lock.current) close(); }}>
    <div ref={content}><Form submitDisabled={!changed || loading || !!error || !subject || !resource || missingSubject || missingResource} onCancel={cancel} label="保存授权" submit={async () => {
      if (lock.current) throw new Error('正在保存，请稍候。');
      if (loading || error) throw new Error('授权选项尚未加载，请稍后再试。');
      if (!subject || !resource || missingSubject || missingResource) throw new Error('请选择有效的授权对象和 Skill / MCP。');
      if (!changed) return;
      lock.current = true; setBusy(true);
      try { return await api(`/bindings/${encodeURIComponent(binding.id)}`, { method: 'PUT', body: JSON.stringify({ subject_type: type, subject_id: subject, resource_id: resource }) }); }
      finally { lock.current = false; setBusy(false); }
    }} onSuccess={() => saved({ subject_type: type, subject_id: subject, resource_id: resource })}>
      <Field label="授权对象类型"><select aria-label="授权对象类型" value={type} disabled={loading} onChange={e => { setType(e.target.value as Binding['subject_type']); setSubject(''); }}>
        {Object.entries(labels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
      </select></Field>
      <SearchableSelect key={type} label="授权对象" value={subject} onChange={setSubject} options={options} required disabled={loading} placeholder={`请选择${labels[type]}`}/>
      <SearchableSelect label="Skill / MCP" value={resource} onChange={setResource} required disabled={loading} options={availableResources.map(r => ({ id: r.id, name: `${r.name} · ${r.kind === 'mcp' ? 'MCP' : 'SKILL'} · ${resourceScope(r, directory)}` }))} placeholder="请选择 Skill 或 MCP"/>
      <p role="status" className="muted">{changed ? '有未保存的修改' : '未做修改'}</p>
      <p className="notice">保存后撤销旧的授权组合并授予新的组合。群聊仍取发送者和群组两侧授权的交集；保存失败不会撤销原授权。</p>
      <Feedback error={error}/>
    </Form></div>
  </Modal>;
}
