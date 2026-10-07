// @vitest-environment jsdom
import { afterEach, beforeEach, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, cleanup, waitFor, within } from '@testing-library/react';
import { Toaster } from 'sonner';
import { DiscoveryPanel, Management } from './Management';
import { OnboardingPolicy } from './OnboardingPolicy';
import type { User } from './api';
const { request } = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock('./api', async original => ({ ...await original<typeof import('./api')>(), api: request, post: (path: string, body: unknown) => request(path, body) }));
beforeEach(() => { HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); }; });
afterEach(() => { cleanup(); request.mockReset(); vi.restoreAllMocks(); });

const directory = { organizations: [{ id: 'org', name: '运营公司' }], departments: [{ id: 'dept', org_id: 'org', name: '广告部' }], can_create_org: true, can_create_department: true };
const root: User = { id: 'root', name: 'Root', email: 'root@example.invalid', role: 'super_admin', org_id: null, team_id: null, active: true };
const person = (overrides: Partial<User>): User => ({ id: 'u1', name: '王小明', email: 'im-0001@im.invalid', role: 'member', org_id: 'org', team_id: 'dept', active: true, login_enabled: false, can_manage: true, ...overrides });
const patches = () => request.mock.calls.filter(([, options]) => options?.method === 'PATCH');

function serveUsers(people: User[]) {
  request.mockImplementation(async (path: string, options?: RequestInit) => options?.method === 'PATCH' ? { ok: true } : path === '/users' ? people : path === '/directory' ? directory : []);
}
const renderUsers = () => render(<><Toaster/><Management page="users" user={root} navigate={vi.fn()}/></>);

it('offers stop and rename only for people the administrator manages', async () => {
  serveUsers([person({}), person({ id: 'u2', name: '已停用同事', active: false }), person({ id: 'root', name: 'Root', can_manage: false })]);
  renderUsers();
  await screen.findByText('王小明');
  expect(screen.getAllByText('改名').length).toBe(2);
  expect(screen.getByText('停用')).toBeTruthy();
  expect(screen.getByText('启用')).toBeTruthy();
  expect(within(screen.getByText('Root').closest('tr')!).queryByText('改名')).toBeNull();
});

it('stops a person only after the consequences are confirmed, and explains them', async () => {
  serveUsers([person({})]);
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
  renderUsers();
  fireEvent.click(await screen.findByText('停用'));
  expect(confirm.mock.calls[0][0]).toContain('取消其排队或执行中的任务');
  expect(confirm.mock.calls[0][0]).toContain('飞书/钉钉本人授权');
  expect(patches()).toHaveLength(0);
  confirm.mockReturnValue(true);
  fireEvent.click(screen.getByText('停用'));
  await waitFor(() => expect(request).toHaveBeenCalledWith('/users/u1', { method: 'PATCH', body: JSON.stringify({ active: false }) }));
  expect(await screen.findByText('已停用，其访问已全部切断。')).toBeTruthy();
});

it('re-enables a stopped person and warns that platform authorization has to be redone', async () => {
  serveUsers([person({ active: false })]);
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
  renderUsers();
  fireEvent.click(await screen.findByText('启用'));
  expect(confirm.mock.calls[0][0]).toContain('重新授权');
  await waitFor(() => expect(request).toHaveBeenCalledWith('/users/u1', { method: 'PATCH', body: JSON.stringify({ active: true }) }));
});

it('shows the server refusal instead of pretending a change happened', async () => {
  request.mockImplementation(async (path: string, options?: RequestInit) => {
    if (options?.method === 'PATCH') throw new Error('只能管理当前范围内的下级用户。');
    return path === '/users' ? [person({})] : path === '/directory' ? directory : [];
  });
  vi.spyOn(window, 'confirm').mockReturnValue(true);
  renderUsers();
  fireEvent.click(await screen.findByText('停用'));
  expect(await screen.findByText('只能管理当前范围内的下级用户。')).toBeTruthy();
  expect(patches()).toHaveLength(1);
  expect(request.mock.calls.filter(([path]) => path === '/users')).toHaveLength(2);  // The list was not reloaded as if it had worked.
});

it('renames a member through the dialog', async () => {
  serveUsers([person({})]);
  renderUsers();
  fireEvent.click(await screen.findByText('改名'));
  const input = await screen.findByLabelText('姓名') as HTMLInputElement;
  expect(input.value).toBe('王小明');
  fireEvent.change(input, { target: { value: '王小明（广告）' } });
  fireEvent.click(screen.getByText('保存'));
  await waitFor(() => expect(request).toHaveBeenCalledWith('/users/u1', { method: 'PATCH', body: JSON.stringify({ name: '王小明（广告）' }) }));
  expect(await screen.findByText('姓名已更新。')).toBeTruthy();
});

const found = (id: string, overrides: Record<string, unknown> = {}) => ({ id, provider: 'feishu', sender_id: 'ou_' + id, chat_id: 'oc_' + id, chat_type: 'p2p', nickname: null, nickname_status: 'not_resolved', chat_name: null, chat_name_status: 'private_chat', first_seen: '2026-10-04T00:00:00Z', last_seen: '2026-10-04T00:00:00Z', reason: 'unknown_sender', current_reason: 'unknown_sender', status: 'pending', user_id: null, ...overrides });

function serveDiscoveries(rows: () => unknown[], batch: (body: unknown) => unknown) {
  request.mockImplementation(async (path: string, body?: unknown) => path === '/directory' ? directory : path.startsWith('/im/discoveries?') ? rows() : path === '/im/discoveries/onboard-batch' ? batch(body) : { ok: true });
}
const batchCalls = () => request.mock.calls.filter(([path]) => path === '/im/discoveries/onboard-batch');

it('only lets one platform and one row per sender be picked', async () => {
  serveDiscoveries(() => [found('a', { nickname: '小王' }), found('b'), found('c', { provider: 'dingtalk' }), found('a2', { sender_id: 'ou_a', nickname: '小王' }),
    found('g', { status: 'authorized', user_id: 'u9', current_reason: null }), found('s', { status: 'stale_application' })], () => ({}));
  render(<DiscoveryPanel kind="private" users={[]} groups={[]} reloadMappings={() => {}}/>);
  await screen.findAllByLabelText('选择 小王');
  const box = (label: string, index = 0) => screen.getAllByLabelText(label)[index] as HTMLInputElement;
  expect((screen.getByText('批量接入') as HTMLButtonElement).disabled).toBe(true);
  expect(box('选择 ou_g').disabled && box('选择 ou_s').disabled).toBe(true);
  fireEvent.click(box('选择 小王'));
  expect(screen.getByText('批量接入（1）')).toBeTruthy();
  expect(box('选择 ou_c').disabled).toBe(true);
  expect(box('选择 小王', 1).disabled).toBe(true);
  expect(box('选择 ou_b').disabled).toBe(false);
  fireEvent.click(box('选择 小王'));
  expect(box('选择 ou_c').disabled).toBe(false);
  expect(screen.getByText('批量接入')).toBeTruthy();
});

it('onboards the picked senders together, keeps failures for a retry, and closes when everyone is in', async () => {
  const onboarded = new Set<string>();
  const reload = vi.fn();
  let attempt = 0;
  serveDiscoveries(() => [found('a', { nickname: '小王', ...(onboarded.has('a') ? { status: 'authorized', user_id: 'x' } : {}) }),
    found('b', onboarded.has('b') ? { status: 'authorized', user_id: 'y' } : {})], () => {
    attempt += 1;
    if (attempt === 1) { onboarded.add('a'); return { results: [{ id: 'a', ok: true, user_id: 'x' }, { id: 'b', ok: false, error: 'External identity already bound; choose the existing user' }], created: 1, failed: 1 }; }
    onboarded.add('b');
    return { results: [{ id: 'b', ok: true, user_id: 'y' }], created: 1, failed: 0 };
  });
  render(<><Toaster/><DiscoveryPanel kind="private" users={[]} groups={[]} reloadMappings={reload}/></>);
  fireEvent.click(await screen.findByLabelText('选择 小王'));
  fireEvent.click(screen.getByLabelText('选择 ou_b'));
  fireEvent.click(screen.getByText('批量接入（2）'));
  expect(await screen.findByText('平台：飞书。以下成员使用相同的角色、组织和部门。')).toBeTruthy();
  expect((screen.getByLabelText('姓名 ou_a') as HTMLInputElement).value).toBe('小王');
  expect((screen.getByLabelText('姓名 ou_b') as HTMLInputElement).value).toBe('');
  expect(Array.from((screen.getByLabelText('角色') as HTMLSelectElement).options).map(o => o.textContent)).toEqual(['成员', '团队负责人']);
  await screen.findByRole('option', { name: '运营公司' });
  fireEvent.change(screen.getByLabelText('组织'), { target: { value: 'org' } });
  fireEvent.change(screen.getByLabelText('部门'), { target: { value: 'dept' } });
  fireEvent.change(screen.getByLabelText('姓名 ou_b'), { target: { value: '李四' } });
  fireEvent.click(screen.getByText('确认接入 2 人'));
  await waitFor(() => expect(batchCalls()[0][1]).toEqual({ items: [{ discovery_id: 'a', name: '小王' }, { discovery_id: 'b', name: '李四' }], role: 'member', org_id: 'org', team_id: 'dept' }));
  expect(await screen.findByText('1 人已接入，1 人未成功，原因见下表；修正后可再次提交剩余的人。')).toBeTruthy();
  expect(screen.getByText('该发送者已绑定内部用户，请改用「绑定已有用户」。')).toBeTruthy();
  expect(screen.queryByLabelText('姓名 ou_a')).toBeNull();
  expect(reload).toHaveBeenCalled();
  fireEvent.click(screen.getByText('确认接入 1 人'));
  await waitFor(() => expect(batchCalls()[1][1]).toEqual({ items: [{ discovery_id: 'b', name: '李四' }], role: 'member', org_id: 'org', team_id: 'dept' }));
  expect(await screen.findByText('已批量创建 IM 成员并绑定身份。请让他们重新发送消息；Skill / MCP 仍需单独授权。')).toBeTruthy();
  await waitFor(() => expect(screen.queryByText('批量接入 IM 成员')).toBeNull());
  expect((screen.getByText('批量接入') as HTMLButtonElement).disabled).toBe(true);
});

const policies = () => [
  { provider: 'feishu', enabled: false, org_id: null, team_id: null, daily_cap: 20, used_24h: 0, valid: null },
  { provider: 'dingtalk', enabled: true, org_id: 'org', team_id: 'dept', daily_cap: 5, used_24h: 3, valid: false }];

it('keeps automatic onboarding off by default and asks for confirmation before turning it on', async () => {
  request.mockImplementation(async (path: string, options?: RequestInit) => options?.method === 'PUT' ? { ok: true } : path === '/directory' ? directory : policies());
  render(<><Toaster/><OnboardingPolicy/></>);
  expect(await screen.findByText('近 24 小时已自动接入 3 / 5 人。')).toBeTruthy();
  expect(screen.getByText('已选组织或部门已不存在，新发送者会退回人工接入；请重新选择后保存。')).toBeTruthy();
  expect((screen.getByLabelText('启用飞书自动接入') as HTMLInputElement).checked).toBe(false);
  expect((screen.getByLabelText('启用钉钉自动接入') as HTMLInputElement).checked).toBe(true);
  expect(screen.queryByText(/我确认已在钉钉后台/)).toBeNull();
  fireEvent.click(screen.getByLabelText('启用飞书自动接入'));
  const acknowledgement = screen.getByLabelText('我确认已在飞书后台限制了应用的可用范围') as HTMLInputElement;
  expect(acknowledgement.required).toBe(true);
  await waitFor(() => expect(screen.getAllByRole('option', { name: '运营公司' }).length).toBeGreaterThan(0));
  fireEvent.change(screen.getAllByLabelText('组织')[0], { target: { value: 'org' } });
  fireEvent.change(screen.getAllByLabelText('部门')[0], { target: { value: 'dept' } });
  fireEvent.change(screen.getAllByLabelText('每日上限（人）')[0], { target: { value: '7' } });
  fireEvent.click(screen.getAllByText('保存')[0]);
  expect(request.mock.calls.filter(([, o]) => o?.method === 'PUT')).toHaveLength(0);  // Not without the confirmation.
  fireEvent.click(acknowledgement);
  fireEvent.click(screen.getAllByText('保存')[0]);
  await waitFor(() => expect(request).toHaveBeenCalledWith('/im/onboarding-policy/feishu', { method: 'PUT', body: JSON.stringify({ enabled: true, org_id: 'org', team_id: 'dept', daily_cap: 7 }) }));
  expect(await screen.findByText('飞书自动接入策略已保存。')).toBeTruthy();
});

it('turning a policy off needs no organization and sends no stale scope', async () => {
  request.mockImplementation(async (path: string, options?: RequestInit) => options?.method === 'PUT' ? { ok: true } : path === '/directory' ? directory : policies());
  render(<OnboardingPolicy/>);
  fireEvent.click(await screen.findByLabelText('启用钉钉自动接入'));
  expect(screen.queryByLabelText('每日上限（人）')).toBeNull();
  fireEvent.click(screen.getAllByText('保存')[1]);
  await waitFor(() => expect(request).toHaveBeenCalledWith('/im/onboarding-policy/dingtalk', { method: 'PUT', body: JSON.stringify({ enabled: false, org_id: 'org', team_id: 'dept', daily_cap: 5 }) }));
});
