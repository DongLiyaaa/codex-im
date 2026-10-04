// @vitest-environment jsdom
import { afterEach, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react';
import { DiscoveryPanel, nicknameSummary } from './Management';
const { request } = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock('./api', async original => ({ ...await original<typeof import('./api')>(), api: request, post: (path: string, body: unknown) => request(path, body) }));
afterEach(() => { cleanup(); request.mockReset(); });

const row = (id: string, provider: string, nickname: string | null, nickname_status: string, chat: { type?: string; name?: string | null; status?: string } = {}) => ({ id, provider, sender_id: 'ou_' + id, chat_id: 'oc_' + id, chat_type: chat.type ?? 'p2p', nickname, nickname_status, chat_name: chat.name ?? null, chat_name_status: chat.status ?? 'private_chat', first_seen: '2026-09-28T00:00:00Z', last_seen: '2026-09-28T00:00:00Z', reason: 'unknown_sender', current_reason: 'unknown_sender', status: 'pending', user_id: null });
const rows = [row('a', 'feishu', null, 'outside_contact_scope'), row('b', 'dingtalk', '钉钉昵称', 'available', { type: 'group', name: '研发群', status: 'available' }),
  row('c', 'feishu', null, 'not_resolved', { type: 'group', status: 'permission_missing' })];

it('shows nickname and group-name reasons and refreshes before reloading', async () => {
  request.mockImplementation(async (path: string) => path === '/im/discoveries/nicknames'
    ? { status: 'ok', resolved: 1, unresolved: 1, remaining: 0, cached: 0, chat_resolved: 2, chat_unresolved: 0, chat_remaining: 0 } : rows);
  render(<DiscoveryPanel users={[]} groups={[]} reloadMappings={() => {}}/>);
  expect(await screen.findByText('不在飞书通讯录权限范围')).toBeTruthy();
  expect(screen.getByText('点击「刷新发现」获取')).toBeTruthy();
  expect(screen.getByText('钉钉 / 钉钉昵称')).toBeTruthy();
  expect(screen.getByText('研发群')).toBeTruthy();
  expect(screen.getByText('oc_b')).toBeTruthy();
  expect(screen.getByText('（应用未开通群信息读取权限）')).toBeTruthy();
  expect(screen.getByText('oc_a')).toBeTruthy();
  fireEvent.click(screen.getByText('刷新发现'));
  expect(await screen.findByText('已补全 1 个飞书昵称；1 个昵称未能获取（原因见昵称列）；已补全 2 个飞书群名。')).toBeTruthy();
  expect(request).toHaveBeenCalledWith('/im/discoveries/nicknames', {});
  await waitFor(() => expect(request.mock.calls.filter(([p]) => p === '/im/discoveries').length).toBe(2));
});

it('still reloads the list when refresh fails', async () => {
  request.mockImplementation(async (path: string) => { if (path === '/im/discoveries/nicknames') throw new Error('暂时失败'); return rows; });
  render(<DiscoveryPanel users={[]} groups={[]} reloadMappings={() => {}}/>);
  await screen.findByText('不在飞书通讯录权限范围');
  fireEvent.click(screen.getByText('刷新发现'));
  expect(await screen.findByText('飞书昵称补全失败：暂时失败')).toBeTruthy();
  await waitFor(() => expect(request.mock.calls.filter(([p]) => p === '/im/discoveries').length).toBe(2));
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
