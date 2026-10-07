// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { Management } from './Management';
import type { Role, User } from './api';

const { request } = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock('./api', async original => ({ ...await original<typeof import('./api')>(), api: request, post: (path: string, body: unknown) => request(path, body) }));
// Credential and OAuth forms are not what this file is about; they only need to stay out of the way.
vi.mock('./OAuthConfig', () => ({ OAuthConfig: () => null }));

const directory = { organizations: [], departments: [], can_create_org: true, can_create_department: true };
const person = (role: Role): User => ({ id: role, name: role, email: `${role}@example.invalid`, role, org_id: role === 'super_admin' ? null : 'org', team_id: null, active: true });
const lists = ['/identities', '/users', '/groups', '/resources', '/im/onboarding-policy', '/im/discoveries/groups', '/im/discoveries?chat=private', '/im/discoveries?chat=group'];
const asked = (prefix: string) => request.mock.calls.some(([path]) => String(path).startsWith(prefix));

beforeEach(() => {
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
  request.mockImplementation(async (path: string) => path === '/directory' ? directory : lists.includes(path) ? []
    : path.startsWith('/integrations/config/') ? { provider: 'feishu', revision: 1, source: 'database', transport: 'websocket', fields: {}, secrets_set: {} } : {});
});
afterEach(() => { cleanup(); request.mockReset(); });

it('IM 集成 handles private chats only: no group senders, no group requests, and a pointer to 协作群组', async () => {
  const navigate = vi.fn();
  render(<Management page="integrations" user={person('super_admin')} navigate={navigate}/>);
  expect(await screen.findByRole('heading', { name: '待接入发现' })).toBeTruthy();
  await waitFor(() => expect(request).toHaveBeenCalledWith('/im/discoveries?chat=private', expect.anything()));
  expect(asked('/im/discoveries?chat=group')).toBe(false);
  expect(asked('/im/discoveries/groups')).toBe(false);
  expect(screen.queryByRole('heading', { name: '群聊发送者待接入' })).toBeNull();
  expect(screen.queryByRole('heading', { name: '已发现群 / 待绑定' })).toBeNull();
  expect(screen.queryByText('配置群外部 ID')).toBeNull();
  expect(screen.queryByText(/员工与群组接入/)).toBeNull();
  expect(screen.queryByText(/群填 chat_id/)).toBeNull();
  expect(screen.getByRole('heading', { name: '员工接入' })).toBeTruthy();
  fireEvent.click(screen.getByText('到协作群组处理群聊'));
  expect(navigate).toHaveBeenCalledWith('groups');
});

it('协作群组 handles group discovery: group binding and the group senders, both asking only for group data', async () => {
  render(<Management page="groups" user={person('super_admin')} navigate={vi.fn()}/>);
  expect(await screen.findByRole('heading', { name: '已发现群 / 待绑定' })).toBeTruthy();
  expect(await screen.findByRole('heading', { name: '群聊发送者待接入' })).toBeTruthy();
  await waitFor(() => expect(request).toHaveBeenCalledWith('/im/discoveries?chat=group', expect.anything()));
  expect(asked('/im/discoveries?chat=private')).toBe(false);
  expect(screen.queryByRole('heading', { name: '待接入发现' })).toBeNull();
});

it.each(['org_admin', 'team_lead', 'member'] as const)('%s sees the groups page without any discovery panel or discovery request', async role => {
  render(<Management page="groups" user={person(role)} navigate={vi.fn()}/>);
  await waitFor(() => expect(request).toHaveBeenCalledWith('/groups', expect.anything()));
  expect(screen.queryByRole('heading', { name: '群聊发送者待接入' })).toBeNull();
  expect(screen.queryByRole('heading', { name: '已发现群 / 待绑定' })).toBeNull();
  expect(asked('/im/discoveries')).toBe(false);
});

it('a super administrator gets the group sender panel in the groups page and not the private one', async () => {
  render(<Management page="groups" user={person('super_admin')} navigate={vi.fn()}/>);
  await screen.findByRole('heading', { name: '群聊发送者待接入' });
  expect(screen.getAllByText(/刷新发现/).length).toBe(1);
  expect(screen.getByText(/群聊里 Skill \/ MCP 取用户授权与群授权的交集/)).toBeTruthy();
});
