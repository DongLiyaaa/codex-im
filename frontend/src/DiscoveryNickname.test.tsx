// @vitest-environment jsdom
import { afterEach, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react';
import { DiscoveryPanel, nicknameSummary } from './Management';
const { request } = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock('./api', async original => ({ ...await original<typeof import('./api')>(), api: request, post: (path: string, body: unknown) => request(path, body) }));
afterEach(() => { cleanup(); request.mockReset(); });

const row = (id: string, provider: string, nickname: string | null, nickname_status: string, chat: { type?: string; name?: string | null; status?: string } = {}) => ({ id, provider, sender_id: 'ou_' + id, chat_id: 'oc_' + id, chat_type: chat.type ?? 'p2p', nickname, nickname_status, chat_name: chat.name ?? null, chat_name_status: chat.status ?? 'private_chat', first_seen: '2026-09-28T00:00:00Z', last_seen: '2026-09-28T00:00:00Z', reason: 'unknown_sender', current_reason: 'unknown_sender', status: 'pending', user_id: null });
const privateRow = row('a', 'feishu', null, 'outside_contact_scope');
const groupRows = [row('b', 'dingtalk', '钉钉昵称', 'available', { type: 'group', name: '研发群', status: 'available' }),
  row('c', 'feishu', null, 'not_resolved', { type: 'group', status: 'permission_missing' })];
const listCalls = (kind: string) => request.mock.calls.filter(([p]) => p === `/im/discoveries?chat=${kind}`).length;
const serve = (listing: unknown[], refresh: unknown = { status: 'ok', resolved: 0, unresolved: 0, remaining: 0, cached: 0 }) =>
  request.mockImplementation(async (path: string) => path.startsWith('/im/discoveries/nicknames') ? refresh : listing);

it('private: asks the server for private chats only and shows nickname reasons, never groups or group names', async () => {
  serve([privateRow, groupRows[0]], { status: 'ok', resolved: 1, unresolved: 1, remaining: 0, cached: 0 });
  render(<DiscoveryPanel kind="private" users={[]} groups={[]} reloadMappings={() => {}}/>);
  expect(await screen.findByText('不在飞书通讯录权限范围')).toBeTruthy();
  expect(request).toHaveBeenCalledWith('/im/discoveries?chat=private', expect.anything());
  expect(screen.getByRole('heading', { name: '待接入发现' })).toBeTruthy();
  expect(screen.queryByText('研发群')).toBeNull();           // A group row from an older server that ignores ?chat=.
  expect(screen.queryByText('钉钉 / 钉钉昵称')).toBeNull();
  expect(screen.queryByRole('columnheader', { name: '群 / 会话 ID' })).toBeNull();
  expect(screen.queryByText('类型')).toBeNull();
  fireEvent.click(screen.getByText('刷新发现'));
  expect(await screen.findByText('已补全 1 个飞书昵称；1 个昵称未能获取（原因见昵称列）。')).toBeTruthy();
  expect(request).toHaveBeenCalledWith('/im/discoveries/nicknames?chat=private', {});
  await waitFor(() => expect(listCalls('private')).toBe(2));
  expect(screen.getByText(/群聊里的发送者、群登记和群成员确认，都在「协作群组」页处理/)).toBeTruthy();
});

it('group: shows the group senders with group names and the reason a name is missing', async () => {
  serve([privateRow, ...groupRows], { status: 'ok', resolved: 1, unresolved: 1, remaining: 0, cached: 0, chat_resolved: 2, chat_unresolved: 0, chat_remaining: 0 });
  render(<DiscoveryPanel kind="group" users={[]} groups={[]} reloadMappings={() => {}}/>);
  expect(await screen.findByText('研发群')).toBeTruthy();
  expect(request).toHaveBeenCalledWith('/im/discoveries?chat=group', expect.anything());
  expect(screen.getByRole('heading', { name: '群聊发送者待接入' })).toBeTruthy();
  expect(screen.getByRole('columnheader', { name: '群 / 会话 ID' })).toBeTruthy();
  expect(screen.getByText('钉钉 / 钉钉昵称')).toBeTruthy();
  expect(screen.getByText('oc_b')).toBeTruthy();
  expect(screen.getByText('（应用未开通群信息读取权限）')).toBeTruthy();
  expect(screen.queryByText('不在飞书通讯录权限范围')).toBeNull(); // The private row belongs to IM 集成.
  fireEvent.click(screen.getByText('刷新发现'));
  expect(await screen.findByText('已补全 1 个飞书昵称；1 个昵称未能获取（原因见昵称列）；已补全 2 个飞书群名。')).toBeTruthy();
  expect(request).toHaveBeenCalledWith('/im/discoveries/nicknames?chat=group', {});
  await waitFor(() => expect(listCalls('group')).toBe(2));
});

it.each([['private', [privateRow], '飞书昵称补全失败：暂时失败'], ['group', groupRows, '飞书昵称与群名补全失败：暂时失败']] as const)(
  '%s: still reloads the list when refresh fails', async (kind, listing, message) => {
    request.mockImplementation(async (path: string) => { if (path.startsWith('/im/discoveries/nicknames')) throw new Error('暂时失败'); return listing; });
    render(<DiscoveryPanel kind={kind} users={[]} groups={[]} reloadMappings={() => {}}/>);
    await screen.findByText(kind === 'private' ? '不在飞书通讯录权限范围' : '研发群');
    fireEvent.click(screen.getByText('刷新发现'));
    expect(await screen.findByText(message)).toBeTruthy();
    await waitFor(() => expect(listCalls(kind)).toBe(2));
  });

it('shows an empty table for a kind with no rows rather than the other kind', async () => {
  serve([privateRow]);
  render(<DiscoveryPanel kind="group" users={[]} groups={[]} reloadMappings={() => {}}/>);
  expect(await screen.findByText('暂无数据')).toBeTruthy();
  expect(screen.queryByText('不在飞书通讯录权限范围')).toBeNull();
});

it('summarizes non-ok refresh outcomes without inventing success', () => {
  const base = { resolved: 0, unresolved: 0, remaining: 0, cached: 0 };
  expect(nicknameSummary({ ...base, status: 'unconfigured' })).toContain('未配置完整');
  expect(nicknameSummary({ ...base, status: 'token_failed' })).toContain('访问令牌失败');
  expect(nicknameSummary({ ...base, status: 'busy' })).toContain('正在进行');
  expect(nicknameSummary({ ...base, status: 'application_changed' })).toContain('结果已丢弃');
  expect(nicknameSummary({ ...base, status: 'ok' })).toBeNull();
  expect(nicknameSummary({ ...base, status: 'ok', remaining: 3, chat_remaining: 2, chat_unresolved: 1 })).toBe('1 个群名未能获取（原因见群列）；还有 5 项待下次刷新。');
});
