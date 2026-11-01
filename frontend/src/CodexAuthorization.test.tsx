// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { CodexAuthorization } from './CodexAuthorization';
const { request, send } = vi.hoisted(() => ({ request: vi.fn(), send: vi.fn() }));
vi.mock('./api', async original => ({ ...await original<typeof import('./api')>(), api: request, post: send }));
afterEach(() => { cleanup(); request.mockReset(); send.mockReset(); });
const empty = { mode: 'personal', state: 'disconnected', message: '尚未连接本人 ChatGPT', account: {} };
const waiting = { ...empty, state: 'pending', message: '等待本人确认', url: 'https://auth.openai.com/codex/device', user_code: 'ABCD-1234' };

it('starts only on click, prevents duplicate submissions, and shows the official link and code', async () => {
  request.mockResolvedValue(empty);
  let resolve!: (value: unknown) => void;
  send.mockImplementation(() => new Promise(r => { resolve = r; }));
  render(<CodexAuthorization/>);
  const button = await screen.findByRole('button', { name: '登录 ChatGPT 授权' });
  expect(send).not.toHaveBeenCalled();
  fireEvent.click(button); fireEvent.click(button);
  expect(send).toHaveBeenCalledTimes(1);
  request.mockResolvedValue(waiting); resolve(waiting);
  const link = await screen.findByRole('link', { name: '打开 ChatGPT 官方授权页面' });
  expect(link.getAttribute('href')).toBe(waiting.url);
  expect(link.getAttribute('rel')).toBe('noopener noreferrer');
  expect(screen.getByText('ABCD-1234')).toBeTruthy();
  expect(send).toHaveBeenCalledWith('/auth/codex/start', {});
});

it('clears authorization material after cancel and reports disconnected', async () => {
  request.mockResolvedValue(waiting);
  send.mockResolvedValue(empty);
  render(<CodexAuthorization/>);
  await screen.findByText('ABCD-1234');
  request.mockResolvedValue(empty);
  fireEvent.click(screen.getByRole('button', { name: '取消授权' }));
  await screen.findByRole('button', { name: '登录 ChatGPT 授权' });
  expect(screen.queryByText('ABCD-1234')).toBeNull();
  expect(send).toHaveBeenCalledWith('/auth/codex/disconnect', {});
});

it('restores pending status on mount and refreshes to connected without resending a task', async () => {
  request.mockResolvedValue(waiting);
  render(<CodexAuthorization/>);
  await screen.findByText('ABCD-1234');
  request.mockResolvedValue({ ...empty, state: 'connected', message: '本人 ChatGPT 已连接', account: { email: 'owner@example.invalid' } });
  fireEvent.click(screen.getByRole('button', { name: '刷新授权状态' }));
  await screen.findByText(/owner@example.invalid/);
  expect(screen.queryByText('ABCD-1234')).toBeNull();
  expect(send).not.toHaveBeenCalled();
});

it('never renders an unexpected authorization link', async () => {
  request.mockResolvedValue({ ...waiting, url: 'https://evil.invalid' });
  render(<CodexAuthorization/>);
  await screen.findByText('等待本人确认');
  expect(screen.queryByRole('link')).toBeNull();
  expect(screen.queryByText('ABCD-1234')).toBeNull();
});

it('does not offer login when system API credentials are configured', async () => {
  request.mockResolvedValue({ ...empty, mode: 'api' });
  render(<CodexAuthorization/>);
  await screen.findByText('当前使用系统配置的 API，无需个人授权。');
  expect(screen.queryByRole('button', { name: '登录 ChatGPT 授权' })).toBeNull();
});

it('shows service errors without stale authorization material', async () => {
  request.mockResolvedValue(waiting);
  render(<CodexAuthorization/>);
  await screen.findByText('ABCD-1234');
  request.mockRejectedValue(new Error('执行器不可用'));
  fireEvent.click(screen.getByRole('button', { name: '刷新授权状态' }));
  await screen.findByText('执行器不可用');
  expect(screen.queryByText('ABCD-1234')).toBeNull();
});

it('ignores stale polling responses after disconnect', async () => {
  request.mockResolvedValueOnce(waiting);
  render(<CodexAuthorization/>);
  await screen.findByText('ABCD-1234');
  let resolve!: (value: unknown) => void;
  request.mockImplementationOnce(() => new Promise(r => { resolve = r; }));
  fireEvent.click(screen.getByRole('button', { name: '刷新授权状态' }));
  await waitFor(() => expect(request).toHaveBeenCalledTimes(2));
  send.mockResolvedValue(empty); request.mockResolvedValue(empty);
  fireEvent.click(screen.getByRole('button', { name: '取消授权' }));
  await screen.findByRole('button', { name: '登录 ChatGPT 授权' });
  resolve(waiting);
  await waitFor(() => expect(screen.queryByText('ABCD-1234')).toBeNull());
});
