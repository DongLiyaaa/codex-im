// @vitest-environment jsdom
import { afterEach, beforeEach, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react';
import { DiscoveryPanel, Management } from './Management';
import { contact } from './api';
import type { Group, User } from './api';
const { request, notify } = vi.hoisted(() => ({ request: vi.fn(), notify: vi.fn() }));
vi.mock('./api', async original => ({ ...await original<typeof import('./api')>(), api: request, post: (path: string, body: unknown) => request(path, body) }));
vi.mock('sonner', () => ({ toast: { success: notify, error: vi.fn() } }));
beforeEach(() => { HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); }; });
afterEach(() => { cleanup(); request.mockReset(); });

const directory = { organizations: [{ id: 'org', name: '运营公司' }], departments: [{ id: 'dept', org_id: 'org', name: '广告部' }], can_create_org: true, can_create_department: true };
const found = (overrides: Record<string, unknown> = {}) => ({ id: 'a', provider: 'feishu', sender_id: 'ou_a', chat_id: 'oc_a', chat_type: 'p2p', nickname: '小王', nickname_status: 'available', chat_name: null, chat_name_status: 'private_chat', first_seen: '2026-10-04T00:00:00Z', last_seen: '2026-10-04T00:00:00Z', reason: 'unknown_sender', current_reason: 'unknown_sender', status: 'pending', user_id: null, ...overrides });
const serve = (rows: unknown[]) => request.mockImplementation(async (path: string) => path === '/directory' ? directory : path.startsWith('/im/discoveries?') ? rows : { ok: true });
const approvals = () => request.mock.calls.filter(([path]) => String(path).endsWith('/approve'));

it('onboards a discovered sender in one step without asking for any account details', async () => {
  serve([found()]);
  const reload = vi.fn();
  render(<DiscoveryPanel kind="private" users={[]} groups={[]} reloadMappings={reload}/>);
  fireEvent.click(await screen.findByText('处理接入'));
  expect((await screen.findByLabelText('接入方式') as HTMLSelectElement).value).toBe('new');
  expect((screen.getByLabelText('姓名') as HTMLInputElement).value).toBe('小王');
  expect(screen.queryByLabelText('邮箱')).toBeNull();
  expect(screen.queryByLabelText('初始密码')).toBeNull();
  await screen.findByRole('option', { name: '运营公司' });
  fireEvent.change(screen.getByLabelText('组织'), { target: { value: 'org' } });
  fireEvent.change(screen.getByLabelText('部门'), { target: { value: 'dept' } });
  fireEvent.change(screen.getByLabelText('姓名'), { target: { value: '王小明' } });
  fireEvent.click(screen.getByText('确认接入'));
  await waitFor(() => expect(request).toHaveBeenCalledWith('/im/discoveries/a/approve', { new_user: { name: '王小明', role: 'member', org_id: 'org', team_id: 'dept' } }));
  await waitFor(() => expect(reload).toHaveBeenCalled());
  expect(screen.queryByText('确认接入')).toBeNull();
});

it('offers only member roles and asks for a department before submitting', async () => {
  serve([found({ nickname: null, nickname_status: 'not_resolved' })]);
  render(<DiscoveryPanel kind="private" users={[]} groups={[]} reloadMappings={() => {}}/>);
  fireEvent.click(await screen.findByText('处理接入'));
  await screen.findByRole('option', { name: '运营公司' });
  expect(screen.getByText('平台未提供昵称，请手动填写；也可先点「刷新发现」补全。')).toBeTruthy();
  expect((screen.getByLabelText('姓名') as HTMLInputElement).value).toBe('');
  expect(Array.from((screen.getByLabelText('角色') as HTMLSelectElement).options).map(o => o.textContent)).toEqual(['成员', '团队负责人']);
  expect((screen.getByLabelText('部门') as HTMLSelectElement).required).toBe(true);
  fireEvent.change(screen.getByLabelText('角色'), { target: { value: 'team_lead' } });
  fireEvent.change(screen.getByLabelText('组织'), { target: { value: 'org' } });
  fireEvent.change(screen.getByLabelText('部门'), { target: { value: 'dept' } });
  fireEvent.change(screen.getByLabelText('姓名'), { target: { value: '李四' } });
  fireEvent.click(screen.getByText('确认接入'));
  await waitFor(() => expect(approvals()[0][1]).toEqual({ new_user: { name: '李四', role: 'team_lead', org_id: 'org', team_id: 'dept' } }));
});

it('shows a failure and keeps the dialog open so nothing is silently lost', async () => {
  request.mockImplementation(async (path: string, body?: unknown) => {
    if (path === '/directory') return directory;
    if (path.startsWith('/im/discoveries?')) return [found()];
    if (path.endsWith('/approve') && body) throw new Error('该发送者已绑定内部用户，请改用「绑定已有用户」。');
    return { ok: true };
  });
  render(<DiscoveryPanel kind="private" users={[]} groups={[]} reloadMappings={() => {}}/>);
  fireEvent.click(await screen.findByText('处理接入'));
  await screen.findByRole('option', { name: '运营公司' });
  fireEvent.change(screen.getByLabelText('组织'), { target: { value: 'org' } });
  fireEvent.change(screen.getByLabelText('部门'), { target: { value: 'dept' } });
  fireEvent.click(screen.getByText('确认接入'));
  expect(await screen.findByText('该发送者已绑定内部用户，请改用「绑定已有用户」。')).toBeTruthy();
  expect((screen.getByLabelText('姓名') as HTMLInputElement).value).toBe('小王');
});

it('a sender that is already bound can only continue with the existing-user form', async () => {
  serve([found({ user_id: 'u1', status: 'authorized', current_reason: null })]);
  render(<DiscoveryPanel kind="private" users={[{ id: 'u1', name: '已有员工', email: 'staff@example.invalid', role: 'member', org_id: 'org', team_id: 'dept', active: true }]} groups={[]} reloadMappings={() => {}}/>);
  fireEvent.click(await screen.findByText('处理接入'));
  expect((await screen.findByLabelText('接入方式') as HTMLSelectElement).value).toBe('existing');
  expect((screen.getByRole('option', { name: '新建 IM 成员（无需网页账号）' }) as HTMLOptionElement).disabled).toBe(true);
  expect(screen.getByLabelText('绑定内部已有用户')).toBeTruthy();
  expect(screen.queryByLabelText('姓名')).toBeNull();
});

it('lets the administrator switch to an existing user and keeps the old request shape', async () => {
  const staff: User = { id: 'u1', name: '已有员工', email: 'staff@example.invalid', role: 'member', org_id: 'org', team_id: 'dept', active: true };
  serve([found()]);
  render(<DiscoveryPanel kind="private" users={[staff, { ...staff, id: 'u2', name: '仅 IM 成员', email: 'im-secret@im.invalid', login_enabled: false }]} groups={[]} reloadMappings={() => {}}/>);
  fireEvent.click(await screen.findByText('处理接入'));
  fireEvent.change(await screen.findByLabelText('接入方式'), { target: { value: 'existing' } });
  expect(screen.getByRole('option', { name: '已有员工 · staff@example.invalid · org' })).toBeTruthy();
  expect(screen.getByRole('option', { name: '仅 IM 成员 · 仅 IM 接入 · org' })).toBeTruthy();
  expect(document.body.textContent).not.toContain('im-secret@im.invalid');
  fireEvent.change(screen.getByLabelText('绑定内部已有用户'), { target: { value: 'u1' } });
  fireEvent.click(screen.getByText('确认保存'));
  await waitFor(() => expect(request).toHaveBeenCalledWith('/im/discoveries/a/approve', { user_id: 'u1', group_id: null, new_group: null, confirm_member: false }));
});

const groupRow = () => found({ chat_type: 'group', chat_id: 'oc_group', chat_name: '研发群', chat_name_status: 'available' });
const registered: Group = { id: 'g1', name: '研发协作群', org_id: 'org', team_id: 'dept', member_ids: ['u1'], provider: 'feishu', external_id: null };
const staffUser: User = { id: 'u1', name: '已有员工', email: 'staff@example.invalid', role: 'member', org_id: 'org', team_id: 'dept', active: true };

it('group senders are told how to continue after onboarding, on the same page', async () => {
  serve([groupRow()]);
  render(<DiscoveryPanel kind="group" users={[]} groups={[]} reloadMappings={() => {}}/>);
  fireEvent.click(await screen.findByText('处理接入'));
  expect(await screen.findByText('该消息来自群聊：接入后，群尚未登记的，请在上方「已发现群 / 待绑定」登记；群已登记的，请再点一次「处理接入」，选择这位成员并确认加入群。')).toBeTruthy();
  expect(screen.queryByLabelText('群登记')).toBeNull(); // Registering is a separate step with an existing member.
  expect(request).toHaveBeenCalledWith('/im/discoveries?chat=group', expect.anything());
});

it('group senders can be added to a group in the same step as binding an existing user', async () => {
  serve([groupRow()]);
  render(<DiscoveryPanel kind="group" users={[staffUser]} groups={[registered]} reloadMappings={() => {}}/>);
  fireEvent.click(await screen.findByText('处理接入'));
  fireEvent.change(await screen.findByLabelText('接入方式'), { target: { value: 'existing' } });
  fireEvent.change(screen.getByLabelText('绑定内部已有用户'), { target: { value: 'u1' } });
  fireEvent.change(await screen.findByLabelText('群登记'), { target: { value: 'g1' } });
  expect(screen.getByText(/本次会明确加入所选发送者/)).toBeTruthy();
  fireEvent.click(screen.getByLabelText(/我确认群映射与上述成员关系/));
  fireEvent.click(screen.getByText('确认保存'));
  await waitFor(() => expect(request).toHaveBeenCalledWith('/im/discoveries/a/approve', { user_id: 'u1', group_id: 'g1', new_group: null, confirm_member: true }));
});

it('the private panel never offers group registration, even if the server sent a group row', async () => {
  serve([found(), groupRow()]);
  render(<DiscoveryPanel kind="private" users={[staffUser]} groups={[registered]} reloadMappings={() => {}}/>);
  expect(await screen.findAllByText('处理接入')).toHaveLength(1);
  fireEvent.click(screen.getByText('处理接入'));
  fireEvent.change(await screen.findByLabelText('接入方式'), { target: { value: 'existing' } });
  expect(screen.queryByLabelText('群登记')).toBeNull();
  expect(screen.queryByText(/该消息来自群聊/)).toBeNull();
  expect(screen.queryByText('研发群')).toBeNull();
});

it('private onboarding finishes with a message that does not mention groups', async () => {
  serve([found()]);
  render(<DiscoveryPanel kind="private" users={[staffUser]} groups={[]} reloadMappings={() => {}}/>);
  fireEvent.click(await screen.findByText('处理接入'));
  fireEvent.change(await screen.findByLabelText('接入方式'), { target: { value: 'existing' } });
  fireEvent.change(screen.getByLabelText('绑定内部已有用户'), { target: { value: 'u1' } });
  fireEvent.click(screen.getByText('确认保存'));
  await waitFor(() => expect(notify).toHaveBeenCalledWith('已保存。请让对方重新发送消息。'));
});

it('group onboarding finishes with the reminder that unregistered groups still do not run', async () => {
  serve([groupRow()]);
  render(<DiscoveryPanel kind="group" users={[staffUser]} groups={[]} reloadMappings={() => {}}/>);
  fireEvent.click(await screen.findByText('处理接入'));
  fireEvent.change(await screen.findByLabelText('接入方式'), { target: { value: 'existing' } });
  fireEvent.change(screen.getByLabelText('绑定内部已有用户'), { target: { value: 'u1' } });
  fireEvent.click(screen.getByText('确认保存'));
  await waitFor(() => expect(notify).toHaveBeenCalledWith('已保存。请重新发送消息；未完成群登记和成员确认时，群消息仍不会执行。'));
});

it('never shows the placeholder address of an IM-only member in the user list', async () => {
  const people: User[] = [{ id: 'm', name: '仅 IM 成员', email: 'im-0123456789@im.invalid', role: 'member', org_id: 'org', team_id: 'dept', active: true, login_enabled: false },
    { id: 'w', name: '网页成员', email: 'web@example.invalid', role: 'member', org_id: 'org', team_id: 'dept', active: true, login_enabled: true }];
  request.mockImplementation(async (path: string) => path === '/users' ? people : path === '/directory' ? directory : []);
  render(<Management page="users" user={{ id: 'root', name: 'Root', email: 'root@example.invalid', role: 'super_admin', org_id: null, team_id: null, active: true }} navigate={vi.fn()}/>);
  expect(await screen.findByText('仅 IM 接入')).toBeTruthy();
  expect(screen.getByText('web@example.invalid')).toBeTruthy();
  expect(document.body.textContent).not.toContain('im.invalid');
  expect(screen.getByText(/员工无需网页账号/)).toBeTruthy();
});

it('treats users without the flag as normal accounts', () => {
  const base = { id: 'x', name: 'x', email: 'x@example.invalid', role: 'member', org_id: null, team_id: null, active: true } as const;
  expect(contact(base)).toBe('x@example.invalid');
  expect(contact({ ...base, login_enabled: true })).toBe('x@example.invalid');
  expect(contact({ ...base, login_enabled: false })).toBe('仅 IM 接入');
});
