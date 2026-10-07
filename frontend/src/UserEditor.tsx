import { useState } from 'react';
import { api, roles } from './api';
import type { Role, User } from './api';
import { Field, Form, Modal, nullable, value } from './ui';
import { ScopeFields } from './ScopeFields';

/** The roles an administrator may hand out: strictly below their own, as when creating a user. */
export function assignableRoles(actor: Role): Role[] {
  return (Object.keys(roles) as Role[]).filter(r => actor === 'super_admin' ? r !== 'super_admin'
    : (actor === 'org_admin' && ['team_lead', 'member'].includes(r)) || (actor === 'team_lead' && r === 'member'));
}

export function UserEditor({ target, actor, close, saved }: { target: User; actor: User; close: () => void; saved: (moved: boolean) => void }) {
  const [role, setRole] = useState<Role>(target.role);
  const [scope, setScope] = useState({ org: String(target.org_id ?? ''), team: String(target.team_id ?? '') });
  const options = assignableRoles(actor.role);
  const moved = role !== target.role || scope.org !== String(target.org_id ?? '') || scope.team !== String(target.team_id ?? '');
  // Role, organization and department go out together and only when one of them changed, so a rename stays a rename.
  const submit = (f: FormData) => api(`/users/${encodeURIComponent(target.id)}`, { method: 'PATCH', body: JSON.stringify(
    moved ? { name: value(f, 'name'), role, org_id: nullable(f, 'org_id'), team_id: nullable(f, 'team_id') } : { name: value(f, 'name') }) });
  return <Modal title="编辑成员" close={close}>
    <Form label="保存" submit={submit} onSuccess={() => saved(moved)}>
      <Field label="姓名"><input aria-label="姓名" name="name" required maxLength={200} defaultValue={target.name}/></Field>
      <Field label="角色"><select aria-label="角色" value={role} onChange={e => setRole(e.target.value as Role)}>
        {(options.includes(target.role) ? options : [target.role, ...options]).map(r => <option key={r} value={r}>{roles[r]}</option>)}
      </select></Field>
      <ScopeFields scope={scope} change={setScope} requireTeam={['team_lead', 'member'].includes(role)}
        fixedOrg={actor.role !== 'super_admin'} fixedTeam={actor.role === 'team_lead'}/>
      {moved && <p className="notice">保存后立即生效：Skill / MCP 按新的组织和部门重新计算——部门、组织授权随新归属生效；超出新范围的个人授权会被撤销，并移出不在新范围内的群组（群组有任务进行中时需稍后再改）。</p>}
    </Form>
  </Modal>;
}
