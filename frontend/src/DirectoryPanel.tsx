import { useRef, useState } from 'react';
import { api, post, errorText } from './api';
import { useData, Loading, Feedback, Modal, Form, Field, value } from './ui';
import './DirectoryPanel.css';
interface Entry { id: string; name: string; org_id?: string; legacy: boolean; can_edit: boolean; can_delete: boolean }
interface Catalog { organizations: Entry[]; departments: Entry[]; can_create_org: boolean; can_create_department: boolean }
type Kind = 'organizations' | 'departments';
type Target = { kind: Kind; row?: Entry };
export function DirectoryPanel() {
  const list = useData<Catalog>('/directory/management', 0, true);
  const [query, setQuery] = useState('');
  const [dirty, setDirty] = useState(false);
  const [org, setOrg] = useState('');
  const [editing, setEditing] = useState<Target | null>(null);
  const [deleting, setDeleting] = useState<{ kind: Kind; row: Entry } | null>(null);
  const [busy, setBusy] = useState(false);
  const lock = useRef(false);
  const [error, setError] = useState(''); const [success, setSuccess] = useState('');
  const selected = list.data?.organizations.some(row => row.id === org) ? org : '';
  function open(target: Target) { setDirty(false); setError(''); setSuccess(''); setEditing(target); }
  async function remove() {
    if (!deleting || lock.current) return;
    lock.current = true; setBusy(true); setError('');
    try {
      await api(`/directory/${deleting.kind}/${encodeURIComponent(deleting.row.id)}`, { method: 'DELETE' });
      if (selected === deleting.row.id) setOrg('');
      setDeleting(null); list.reload(); setSuccess('目录已删除。');
    } catch (err) { setError(errorText(err)); }
    finally { lock.current = false; setBusy(false); }
  }
  function identity(row: Entry) {
    return <div className="directory-identity"><strong>{row.name}</strong><details><summary>查看 ID{row.legacy ? '（历史目录）' : ''}</summary><code>{row.id}</code><button type="button" className="text-button" onClick={async () => {
      try { await navigator.clipboard.writeText(row.id); setSuccess('ID 已复制。'); }
      catch { setError('复制失败，请展开 ID 后手动复制。'); }
    }}>复制 ID</button></details></div>;
  }
  function actions(kind: Kind, row: Entry) {
    return <div className="actions">{row.can_edit && <button className="secondary" onClick={() => open({ kind, row })}>编辑</button>}
      {row.can_delete && <button className="danger-button" onClick={() => { setError(''); setSuccess(''); setDeleting({ kind, row }); }}>删除</button>}
      {!row.can_edit && !row.can_delete && <span className="muted">只读</span>}</div>;
  }
  return <div className="directory-page">
    <div className="page-intro"><div><h2>组织与部门</h2><p>先选择组织，再管理它的部门。名称可修改，目录 ID 保持不变。</p></div><button className="secondary" disabled={list.loading || busy} onClick={list.reload}>刷新</button></div>
    {!editing && !deleting && <Feedback error={error || list.error} success={success}/>}
    {list.error && list.data && <p className="muted">刷新失败，以下为上次加载的目录。</p>}{list.refreshing && <p role="status">正在刷新，暂显示上次目录。</p>}{list.error && <button className="secondary" onClick={list.reload}>重试加载目录</button>}{list.loading && !list.data ? <Loading/> : list.data && <div className="directory-layout">
      <section className="directory-organizations" aria-label="组织列表"><div className="directory-heading"><h3>组织</h3>{list.data.can_create_org && <button className="primary" onClick={() => open({ kind: 'organizations' })}>新增组织</button>}</div>
        <button className={`directory-filter ${selected === '' ? 'selected' : ''}`} onClick={() => setOrg('')} aria-pressed={selected === ''}>全部组织</button>
        {list.data.organizations.map(row => <article className={`directory-org ${selected === row.id ? 'selected' : ''}`} key={row.id}>
          <button className="directory-filter" aria-pressed={selected === row.id} onClick={() => setOrg(row.id)}>{row.name}</button>
          <details><summary>目录详情</summary>{identity(row)}{actions('organizations', row)}</details>
        </article>)}
        {!list.data.organizations.length && <p className="muted">暂无组织，请先创建组织。</p>}
      </section>
      <section className="directory-departments" aria-label="部门列表"><div className="directory-heading"><div><h3>{selected ? list.data.organizations.find(row => row.id === selected)?.name : '全部组织'}的部门</h3><p className="muted">删除有成员或资源引用的目录会被拒绝，不会级联删除。</p></div>{list.data.can_create_department && <button className="primary" disabled={!list.data.organizations.length} onClick={() => open({ kind: 'departments' })}>新增部门</button>}</div>
        <Field label="搜索部门"><input type="search" value={query} onChange={e => setQuery(e.target.value)} placeholder="部门名称或 ID"/></Field>{query && <button className="text-button" onClick={() => setQuery('')}>清空搜索</button>}<div className="table-wrap"><table><thead><tr><th>部门名称</th><th>所属组织</th><th>操作</th></tr></thead><tbody>
          {list.data.departments.filter(row => (!selected || row.org_id === selected) && `${row.name} ${row.id}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())).map(row => <tr key={`${row.org_id}/${row.id}`}><td>{identity(row)}</td><td>{list.data!.organizations.find(org => org.id === row.org_id)?.name || row.org_id}</td><td>{actions('departments', row)}</td></tr>)}
        </tbody></table></div>
        {!list.data.departments.some(row => (!selected || row.org_id === selected) && `${row.name} ${row.id}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())) && <p className="directory-empty">该范围暂无匹配部门，可切换组织或清空搜索。</p>}
      </section>
    </div>}
    {editing && <Modal title={`${editing.row ? '编辑' : '新增'}${editing.kind === 'organizations' ? '组织' : '部门'}`} dirty={dirty} busy={busy} close={() => { if (!lock.current) setEditing(null); }}>
      <Form label="保存" onDirty={() => setDirty(true)} submit={async f => {
        if (lock.current) throw new Error('正在处理，请稍候');
        lock.current = true; setBusy(true);
        try {
          const body = { name: value(f, 'name'), ...(!editing.row && editing.kind === 'departments' ? { org_id: value(f, 'org_id') } : {}) };
          return await (editing.row ? api(`/directory/${editing.kind}/${encodeURIComponent(editing.row.id)}`, { method: 'PATCH', body: JSON.stringify(body) }) : post(`/directory/${editing.kind}`, body));
        } finally { lock.current = false; setBusy(false); }
      }} onSuccess={() => { setEditing(null); list.reload(); setSuccess('已保存。'); }}>
        <Field label="名称"><input name="name" required maxLength={200} defaultValue={editing.row?.name}/></Field>
        {editing.row ? <p className="directory-id">ID：{editing.row.id}；所属组织保持不变。</p> : editing.kind === 'departments' && <Field label="所属组织"><select name="org_id" required defaultValue={selected}><option value="">请选择组织</option>{list.data?.organizations.map(row => <option value={row.id} key={row.id}>{row.name}</option>)}</select></Field>}
      </Form>
    </Modal>}
    {deleting && <Modal title={`删除${deleting.kind === 'organizations' ? '组织' : '部门'}`} close={() => { if (!lock.current) setDeleting(null); }}>
      <p>确认删除“{deleting.row.name}”？有用户、部门、群组或资源引用时无法删除；不会级联删除，历史 ID 不可复用。</p>
      <Feedback error={error}/><div className="actions"><button className="secondary" disabled={busy} onClick={() => setDeleting(null)}>取消</button><button className="danger-button" disabled={busy} onClick={() => void remove()}>{busy ? '正在删除…' : '确认删除'}</button></div>
    </Modal>}
  </div>;
}
