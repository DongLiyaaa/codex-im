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
const disconnected = { mode: 'personal', state: 'disconnected', message: '尚未连接本人 ChatGPT', account: {} };
function Wrapped({ initial = user, navigate = vi.fn() }: { initial?: User; navigate?: ReturnType<typeof vi.fn> }) {
  const [current, setCurrent] = useState(initial);
  return <Onboarding user={current} onSaved={setCurrent} navigate={navigate}/>;
}
function serve(initial = user) {
  request.mockImplementation(async (path, options) => path === '/auth/codex' ? disconnected : ({ ...initial, onboarding: { ...JSON.parse(options.body), auto_start: false } }));
}

it('saves the first administrator entry once but does not mutate an old account just by opening', async () => {
  serve();
  const view = render(<Wrapped initial={{ ...user, onboarding: { status: 'pending', step: 'model', auto_start: true } }}/>);
  await waitFor(() => expect(request).toHaveBeenCalledWith('/auth/me/onboarding', { method: 'PUT', body: JSON.stringify({ status: 'in_progress', step: 'model' }) }));
  expect(await screen.findByRole('button', { name: '登录 ChatGPT 授权' })).toBeTruthy();
  view.unmount(); request.mockClear(); serve();
  render(<Wrapped/>);
  await screen.findByRole('button', { name: '登录 ChatGPT 授权' });
  expect(request.mock.calls.some(([path]) => path === '/auth/me/onboarding')).toBe(false);
  expect(screen.getByText(/第 1 \/ 3 步/)).toBeTruthy();
});

it('does not advance on failure and retries the failed target', async () => {
  let fail = true;
  request.mockImplementation(async (path, options) => {
    if (path === '/auth/codex') return disconnected;
    if (fail) { fail = false; throw new Error('保存失败'); }
    return { ...user, onboarding: { ...JSON.parse(options.body), auto_start: false } };
  });
  render(<Wrapped/>);
  fireEvent.click(await screen.findByRole('button', { name: '下一步' }));
  await screen.findByText('保存失败');
  expect(screen.getByRole('heading', { name: '连接模型' })).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: '重试保存进度' }));
  await screen.findByRole('heading', { name: '选择使用方式' });
  const writes = request.mock.calls.filter(([path]) => path === '/auth/me/onboarding');
  expect(writes[0][1].body).toBe(writes[1][1].body);
});

it('blocks duplicate submissions and leaves only after deferred progress is saved', async () => {
  let resolve!: (value: User) => void;
  request.mockImplementation(() => new Promise(r => { resolve = r; }));
  const navigate = vi.fn();
  render(<Wrapped initial={{ ...user, onboarding: { status: 'in_progress', step: 'usage', auto_start: true } }} navigate={navigate}/>);
  const button = screen.getByRole('button', { name: '稍后继续' });
  fireEvent.click(button); fireEvent.click(button);
  expect(request).toHaveBeenCalledTimes(1);
  expect(navigate).not.toHaveBeenCalled();
  await act(async () => resolve({ ...user, onboarding: { status: 'deferred', step: 'usage', auto_start: false } }));
  expect(navigate).toHaveBeenCalledWith('overview');
});

it('completes on the final step, enters chat and allows restart', async () => {
  serve();
  const navigate = vi.fn();
  render(<Wrapped initial={{ ...user, onboarding: { status: 'in_progress', step: 'try' } }} navigate={navigate}/>);
  fireEvent.click(screen.getByRole('button', { name: '完成并开始对话' }));
  await waitFor(() => expect(navigate).toHaveBeenCalledWith('chat'));
  expect(request).toHaveBeenCalledWith('/auth/me/onboarding', { method: 'PUT', body: JSON.stringify({ status: 'completed', step: 'try' }) });
  await screen.findByText('已完成初始化，可随时重新查看。');
  fireEvent.click(screen.getByRole('button', { name: '重新查看' }));
  await screen.findByRole('heading', { name: '连接模型' });
});

it('ignores a delayed save response after the guide is unmounted', async () => {
  let resolve!: (value: User) => void;
  request.mockImplementation(async path => path === '/auth/codex' ? disconnected : new Promise(r => { resolve = r; }));
  const onSaved = vi.fn(), navigate = vi.fn();
  const view = render(<Onboarding user={user} onSaved={onSaved} navigate={navigate}/>);
  fireEvent.click(await screen.findByRole('button', { name: '稍后继续' }));
  view.unmount();
  await act(async () => resolve({ ...user, onboarding: { status: 'deferred', step: 'model', auto_start: false } }));
  expect(onSaved).not.toHaveBeenCalled();
  expect(navigate).not.toHaveBeenCalled();
});

it('offers web use to members without exposing administrative integration choices', async () => {
  const member: User = { ...user, id: 'member', role: 'member', org_id: 'org', team_id: 'team', onboarding: { status: 'deferred', step: 'usage' } };
  serve(member);
  render(<Wrapped initial={member}/>);
  expect(screen.getByRole('heading', { name: '选择使用方式' })).toBeTruthy();
  expect(screen.queryByRole('button', { name: /接入飞书/ })).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: /先用网页版/ }));
  await screen.findByRole('heading', { name: '开始对话' });
  expect(screen.queryByRole('button', { name: '组织与成员' })).toBeNull();
});
