// @vitest-environment jsdom
import { useState } from 'react';
import { afterEach, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { Onboarding } from './Onboarding';
import type { User } from './api';
const { request } = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock('./api', async original => ({ ...await original<typeof import('./api')>(), api: request }));
afterEach(() => { cleanup(); request.mockReset(); });
const user: User = { id: 'root', name: 'Owner', email: 'owner@example.invalid', role: 'super_admin', active: true, org_id: null, team_id: null };
function Wrapped({ initial = user, navigate = vi.fn() }: { initial?: User; navigate?: ReturnType<typeof vi.fn> }) {
  const [current, setCurrent] = useState(initial);
  return <Onboarding user={current} onSaved={setCurrent} navigate={navigate}/>;
}
function serve(initial = user) {
  request.mockImplementation(async (_path, options) => ({ ...initial, onboarding: { ...JSON.parse(options.body), auto_start: false } }));
}

it('saves the first administrator entry once but does not mutate an old account just by opening', async () => {
  serve();
  const view = render(<Wrapped initial={{ ...user, onboarding: { status: 'pending', step: 'welcome', auto_start: true } }}/>);
  await waitFor(() => expect(request).toHaveBeenCalledTimes(1));
  expect(request).toHaveBeenCalledWith('/auth/me/onboarding', { method: 'PUT', body: JSON.stringify({ status: 'in_progress', step: 'welcome' }) });
  await waitFor(() => expect((screen.getByRole('button', { name: '下一步' }) as HTMLButtonElement).disabled).toBe(false));
  view.unmount(); request.mockClear();
  render(<Wrapped/>);
  expect(request).not.toHaveBeenCalled();
  expect(screen.getByText(/第 1 \/ 6 步/)).toBeTruthy();
});

it('does not advance on failure and retries the failed target', async () => {
  request.mockRejectedValueOnce(new Error('保存失败')).mockResolvedValueOnce({ ...user, onboarding: { status: 'in_progress', step: 'model', auto_start: false } });
  render(<Wrapped/>);
  fireEvent.click(screen.getByRole('button', { name: '下一步' }));
  await screen.findByText('保存失败');
  expect(screen.getByRole('heading', { name: '认识工作空间' })).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: '重试保存进度' }));
  await screen.findByRole('heading', { name: '模型与本人连接' });
  expect(request.mock.calls[0][1].body).toBe(request.mock.calls[1][1].body);
});

it('blocks duplicate submissions and leaves only after deferred progress is saved', async () => {
  let resolve!: (value: User) => void;
  request.mockImplementation(() => new Promise(r => { resolve = r; }));
  const navigate = vi.fn();
  render(<Wrapped initial={{ ...user, onboarding: { status: 'in_progress', step: 'resources', auto_start: true } }} navigate={navigate}/>);
  const button = screen.getByRole('button', { name: '稍后继续' });
  fireEvent.click(button); fireEvent.click(button);
  expect(request).toHaveBeenCalledTimes(1);
  expect(navigate).not.toHaveBeenCalled();
  expect((screen.getByRole('button', { name: '添加或检查资源' }) as HTMLButtonElement).disabled).toBe(true);
  await act(async () => resolve({ ...user, onboarding: { status: 'deferred', step: 'resources', auto_start: false } }));
  expect(navigate).toHaveBeenCalledWith('overview');
});

it('completes reading on the final step and allows restart', async () => {
  serve();
  const navigate = vi.fn();
  render(<Wrapped initial={{ ...user, onboarding: { status: 'in_progress', step: 'try' } }} navigate={navigate}/>);
  fireEvent.click(screen.getByRole('button', { name: '完成阅读' }));
  await screen.findByText('已完成阅读，可随时重新查看。');
  expect(request).toHaveBeenCalledWith('/auth/me/onboarding', { method: 'PUT', body: JSON.stringify({ status: 'completed', step: 'try' }) });
  expect(navigate).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '重新查看引导' }));
  await screen.findByRole('heading', { name: '认识工作空间' });
  expect(request.mock.calls.every(([path]) => path === '/auth/me/onboarding')).toBe(true);
});

it('ignores a delayed save response after the guide is unmounted', async () => {
  let resolve!: (value: User) => void;
  request.mockImplementation(() => new Promise(r => { resolve = r; }));
  const onSaved = vi.fn(), navigate = vi.fn();
  const view = render(<Onboarding user={user} onSaved={onSaved} navigate={navigate}/>);
  fireEvent.click(screen.getByRole('button', { name: '稍后继续' }));
  view.unmount();
  await act(async () => resolve({ ...user, onboarding: { status: 'deferred', step: 'welcome', auto_start: false } }));
  expect(onSaved).not.toHaveBeenCalled();
  expect(navigate).not.toHaveBeenCalled();
});

it('shows member guidance rather than administrative configuration actions', async () => {
  const member: User = { ...user, id: 'member', role: 'member', org_id: 'org', team_id: 'team', onboarding: { status: 'deferred', step: 'people' } };
  serve(member);
  render(<Wrapped initial={member}/>);
  expect(screen.getByRole('heading', { name: '核对身份与群组' })).toBeTruthy();
  expect(screen.queryByRole('button', { name: '管理组织与部门' })).toBeNull();
  expect(screen.queryByText('先创建组织和部门，再安排成员归属。')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: /4 了解可用能力/ }));
  await screen.findByRole('heading', { name: '了解可用能力' });
  expect(screen.queryByRole('button', { name: '添加或检查资源' })).toBeNull();
  expect(screen.getByRole('button', { name: '查看资源' })).toBeTruthy();
});
