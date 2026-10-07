import { useState } from 'react';
import { post, roles } from './api';
import type { Role } from './api';
import { ScopeFields } from './ScopeFields';
import type { Scope } from './ScopeFields';
import { Field, Form, value } from './ui';

const memberRoles: Role[] = ['member', 'team_lead'];

// Onboards a discovered IM sender as an IM-only member: no email, password or web login is created.
export function NewMemberForm({ discoveryId, nickname, isGroup, initialScope, done }: {
  discoveryId: string; nickname: string | null; isGroup: boolean; initialScope: Scope; done: (scope: Scope) => void;
}) {
  const [scope, setScope] = useState(initialScope);
  const [role, setRole] = useState<Role>('member');
  return <Form label="确认接入" submit={f => post(`/im/discoveries/${discoveryId}/approve`, { new_user: { name: value(f, 'name'), role, org_id: scope.org, team_id: scope.team } })} onSuccess={() => done(scope)}>
    <Field label="姓名" hint={nickname ? undefined : '平台未提供昵称，请手动填写；也可先点「刷新发现」补全。'}><input aria-label="姓名" name="name" required maxLength={200} defaultValue={nickname ?? ''} placeholder="成员姓名"/></Field>
    <Field label="角色"><select value={role} onChange={e => setRole(e.target.value as Role)}>{memberRoles.map(r => <option key={r} value={r}>{roles[r]}</option>)}</select></Field>
    <ScopeFields scope={scope} change={setScope} requireTeam/>
    <p className="im-help">不创建邮箱和密码，成员只能通过飞书 / 钉钉使用；Skill / MCP 仍需在「绑定与授权」单独授权。</p>
    {isGroup && <p className="im-help">该消息来自群聊：接入后，群尚未登记的，请在上方「已发现群 / 待绑定」登记；群已登记的，请再点一次「处理接入」，选择这位成员并确认加入群。</p>}
  </Form>;
}
