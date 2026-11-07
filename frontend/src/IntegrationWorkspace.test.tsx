// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { Management } from './Management';
import type { User } from './api';
const { request } = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock('./api', async original => ({ ...await original<typeof import('./api')>(), api: request, post: (path: string, body: unknown) => request(path, body) }));
const user: User = { id: 'root', name: 'Admin', email: 'root@example.invalid', role: 'super_admin', active: true, org_id: null, team_id: null };
const people = [{ ...user, id: 'a', name: '小王' }, { ...user, id: 'b', name: '小李' }];
function serve(path: string) {
  if (path === '/users') return people;
  if (path === '/identities') return [{ provider: 'feishu', external_user_id: 'ou_wang', user_id: 'a' }, { provider: 'dingtalk', external_user_id: 'ding_li', user_id: 'b' }];
  if (path === '/integrations/status') return { feishu: { configured: true, state: 'connected', transport: 'websocket' }, dingtalk: { configured: false, state: 'missing_credentials' } };
  if (path.startsWith('/integrations/config/')) return { provider: path.split('/').pop(), revision: 1, transport: path.endsWith('feishu') ? 'websocket' : 'stream', fields: {}, secrets_set: { APP_SECRET: false, CLIENT_SECRET: false } };
  return [];
}
beforeEach(() => {
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
  window.matchMedia = vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() });
  request.mockImplementation(async (path: string) => serve(path));
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); request.mockReset(); });
const start = (actor = user) => render(<Management page="integrations" user={actor} navigate={vi.fn()}/>);
const section = (name: string) => fireEvent.click(within(screen.getByRole('navigation', { name: '集成操作分区' })).getByRole('button', { name }));
it('leads with connection state and actions, loads employee discovery only on demand', async () => {
  start(); await screen.findByText('已连接');
  expect(screen.queryByText('飞书 / 钉钉接入与使用指南')).toBeNull();
  expect(request.mock.calls.some(([path]) => path.startsWith('/im/discoveries'))).toBe(false);
  section('员工接入'); await screen.findByText('ou_wang');
  expect(screen.queryByText('ding_li')).toBeNull();
  fireEvent.change(screen.getByRole('searchbox', { name: '搜索已绑定身份' }), { target: { value: '不存在' } });
  expect(screen.getByText('没有匹配的身份')).toBeTruthy();
  fireEvent.click(screen.getByText('清空搜索')); expect(screen.getByText('ou_wang')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: '钉钉' }));
  await screen.findByText('ding_li'); expect(screen.queryByText('ou_wang')).toBeNull();
});
it('keeps separate platform drafts and cancels destructive reload', async () => {
  start(); await screen.findByText('已连接'); section('机器人连接');
  fireEvent.change(await screen.findByLabelText(/^App ID/), { target: { value: 'draft-feishu' } });
  fireEvent.click(screen.getByRole('button', { name: '钉钉' }));
  fireEvent.change(await screen.findByLabelText(/^Client ID/), { target: { value: 'draft-ding' } });
  fireEvent.click(screen.getByRole('button', { name: '飞书' }));
  expect((screen.getByLabelText(/^App ID/) as HTMLInputElement).value).toBe('draft-feishu');
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
  fireEvent.click(screen.getByRole('button', { name: '重新加载' }));
  expect(confirm).toHaveBeenCalled(); expect((screen.getByLabelText(/^App ID/) as HTMLInputElement).value).toBe('draft-feishu');
  section('接入概况'); section('机器人连接');
  expect((screen.getByLabelText(/^App ID/) as HTMLInputElement).value).toBe('draft-feishu');
});
it('binds only the selected platform, retains failed input and retries', async () => {
  start(); await screen.findByText('已连接'); section('员工接入');
  await screen.findByText('ou_wang');
  fireEvent.click(screen.getByText('手动绑定身份'));
  fireEvent.change(screen.getByLabelText('飞书 open_id'), { target: { value: 'ou_new' } });
  fireEvent.change(screen.getByLabelText('内部成员'), { target: { value: 'b' } });
  fireEvent.click(screen.getByRole('button', { name: '关闭弹窗' }));
  expect(screen.getByText('有未保存的修改，是否放弃？')).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: '继续编辑' }));
  expect((screen.getByLabelText('飞书 open_id') as HTMLInputElement).value).toBe('ou_new');
  request.mockImplementation(async (path: string) => { if (path === '/identities') throw new Error('暂时失败'); return serve(path); });
  fireEvent.click(screen.getByRole('button', { name: '确认绑定' }));
  await screen.findByText('暂时失败'); expect((screen.getByLabelText('飞书 open_id') as HTMLInputElement).value).toBe('ou_new');
  request.mockImplementation(async (path: string) => serve(path));
  fireEvent.click(screen.getByRole('button', { name: '确认绑定' }));
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  expect(request).toHaveBeenCalledWith('/identities', { provider: 'feishu', external_user_id: 'ou_new', user_id: 'b' });
});
it('does not load admin configuration or offer identity mutations for members', async () => {
  start({ ...user, role: 'member' }); await screen.findByRole('heading', { name: '飞书 · 接入概况' });
  section('机器人连接'); section('个人文档授权'); section('员工接入');
  expect(request.mock.calls.some(([path]) => path.startsWith('/integrations/') || path.startsWith('/im/'))).toBe(false);
  expect(screen.queryByText('手动绑定身份')).toBeNull();
});
it('retains connection status during refresh and exposes a retry after failure', async () => {
  start(); await screen.findByText('已连接');
  let rejectRefresh!: (reason: Error) => void;
  request.mockImplementation((path: string) => path === '/integrations/status' ? new Promise((_, reject) => { rejectRefresh = reject; }) : Promise.resolve(serve(path)));
  fireEvent.click(screen.getByRole('button', { name: '刷新状态' }));
  await screen.findByText('正在读取连接状态…');
  expect(screen.getByText('已连接')).toBeTruthy();
  expect((screen.getByRole('button', { name: '读取中…' }) as HTMLButtonElement).disabled).toBe(true);
  rejectRefresh(new Error('连接状态暂时不可用'));
  await screen.findByText('连接状态暂时不可用');
  expect(screen.getByText('刷新失败，以下保留上次连接状态。')).toBeTruthy();
  request.mockImplementation(async (path: string) => serve(path));
  fireEvent.click(screen.getByRole('button', { name: '重试连接状态' }));
  await waitFor(() => expect(screen.queryByText('连接状态暂时不可用')).toBeNull());
});
