// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { CodexStatus } from './CodexStatus';
import type { CodexRunnerStatus, CodexStatusResult } from './CodexStatus';
import { Overview } from './Management';
import type { User } from './api';

const { request } = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock('./api', async original => ({ ...await original<typeof import('./api')>(), api: request }));

const runner = (change: Partial<CodexRunnerStatus> = {}): CodexRunnerStatus => ({
  ready: true, checked_at: 1790000000, auth_mode: 'api',
  codex: { installed: true, version: '0.157.1', pinned_version: '0.157.1' },
  model: { id: 'demo-model', endpoint_host: 'models.example.com', credential_configured: true },
  config_error: null, sandbox: { state: 'ok', error: null },
  model_endpoint: { state: 'ok', http_status: 200, model_listed: true, reason: null }, ...change,
});
const answer = (value: unknown) => request.mockImplementation(async (path: string) => { if (path.startsWith('/system/codex-status')) return value; return {}; });
const result = (change: Partial<CodexRunnerStatus> = {}): CodexStatusResult => ({ reachable: true, error: null, runner: runner(change) });
const rowOf = (name: string) => screen.getByText(name).closest('li') as HTMLElement;

beforeEach(() => { request.mockReset(); });
afterEach(() => cleanup());

it('shows every check as passed and the integration as usable', async () => {
  answer(result());
  render(<CodexStatus/>);
  expect(await screen.findByText('可用')).toBeTruthy();
  for (const name of ['执行器连接', 'Codex CLI', '模型与端点', '模型凭据', '模型端点连通', '沙箱']) expect(within(rowOf(name)).getByText('通过')).toBeTruthy();
  expect(within(rowOf('Codex CLI')).getByText('已安装 0.157.1。')).toBeTruthy();
  expect(within(rowOf('模型与端点')).getByText('模型 demo-model；端点 models.example.com。')).toBeTruthy();
  expect(within(rowOf('模型端点连通')).getByText('端点连通正常，模型列表包含 demo-model。')).toBeTruthy();
  expect(within(rowOf('模型凭据')).getByText('已配置 API Key（不会显示内容）。')).toBeTruthy();
  expect(request).toHaveBeenCalledWith('/system/codex-status', expect.anything());
});

it('names the failing sandbox and marks the whole integration unusable', async () => {
  answer(result({ ready: false, sandbox: { state: 'failed', error: 'SANDBOX_UNAVAILABLE' } }));
  render(<CodexStatus/>);
  expect(await screen.findByText('不可用')).toBeTruthy();
  expect(within(rowOf('沙箱')).getByText('未通过')).toBeTruthy();
  expect(within(rowOf('沙箱')).getByText(/沙箱无法启动，任务会被拒绝/)).toBeTruthy();
  expect(screen.getAllByText('未通过')).toHaveLength(1);
  expect(within(rowOf('模型端点连通')).getByText('通过')).toBeTruthy();
});

it.each([
  ['unauthorized', 401, '端点拒绝了当前凭据。（HTTP 401）'],
  ['model_missing', 200, '端点可以连通，但模型列表里没有配置的模型。（HTTP 200）'],
  ['timeout', null, '连接端点超时。'],
  ['redirect_refused', 302, '端点返回了重定向，为保护凭据已拒绝跟随。（HTTP 302）'],
])('explains a failing model endpoint (%s)', async (state, status, text) => {
  answer(result({ ready: false, model_endpoint: { state, http_status: status, model_listed: null, reason: null } }));
  render(<CodexStatus/>);
  expect(await screen.findByText(text)).toBeTruthy();
  expect(within(rowOf('模型端点连通')).getByText('未通过')).toBeTruthy();
});

it('does not count an endpoint without a model list as a failure', async () => {
  answer(result({ model_endpoint: { state: 'unverified', http_status: 404, model_listed: null, reason: null } }));
  render(<CodexStatus/>);
  expect(await screen.findByText('可用')).toBeTruthy();
  expect(within(rowOf('模型端点连通')).getByText('未检查')).toBeTruthy();
});

it('reports a missing key and an invalid endpoint on their own rows', async () => {
  answer(result({ ready: false, config_error: 'MODEL_API_KEY_NOT_CONFIGURED', model: { id: 'demo-model', endpoint_host: null, credential_configured: false }, model_endpoint: { state: 'skipped', http_status: null, model_listed: null, reason: 'CONFIG_ERROR' } }));
  const view = render(<CodexStatus/>);
  expect(await screen.findByText('没有配置模型 API Key。')).toBeTruthy();
  expect(within(rowOf('模型与端点')).getByText('通过')).toBeTruthy();
  view.unmount();
  answer(result({ ready: false, config_error: 'INVALID_CODEX_BASE_URL', model: { id: null, endpoint_host: null, credential_configured: true }, model_endpoint: { state: 'skipped', http_status: null, model_listed: null, reason: 'CONFIG_ERROR' } }));
  render(<CodexStatus/>);
  expect(await screen.findByText('模型端点地址不合法，需要是公网 https 地址。')).toBeTruthy();
  expect(within(rowOf('模型凭据')).getByText('通过')).toBeTruthy();
});

it.each([
  ['RUNNER_UNREACHABLE', '连不上执行器，请确认 runner 服务已经启动。'],
  ['RUNNER_OUTDATED', '执行器版本较旧，还没有检查接口，请更新并重启执行器。'],
  ['RUNNER_AUTH_FAILED', '执行器拒绝了访问令牌，请确认 API 与 runner 使用同一个 RUNNER_TOKEN。'],
  ['SOMETHING_NEW', '执行器检查失败（SOMETHING_NEW）。'],
])('explains an unreachable runner (%s) and leaves the other checks unchecked', async (code, text) => {
  answer({ reachable: false, error: code, runner: null });
  render(<CodexStatus/>);
  expect(await screen.findByText(text)).toBeTruthy();
  expect(within(rowOf('执行器连接')).getByText('未通过')).toBeTruthy();
  expect(screen.getAllByText('未检查')).toHaveLength(5);
  expect(screen.getByText('不可用')).toBeTruthy();
});

it('shows the sandbox and endpoint as not checked where the platform does not support it', async () => {
  answer(result({ sandbox: { state: 'skipped', error: null }, model: { id: null, endpoint_host: null, credential_configured: true }, model_endpoint: { state: 'skipped', http_status: null, model_listed: null, reason: 'NO_CUSTOM_ENDPOINT' } }));
  render(<CodexStatus/>);
  expect(await screen.findByText('可用')).toBeTruthy();
  expect(within(rowOf('沙箱')).getByText('未检查')).toBeTruthy();
  expect(within(rowOf('模型与端点')).getByText('未指定模型，使用 Codex 默认模型；默认端点。')).toBeTruthy();
});

it('rechecks with a forced refresh, repeatedly', async () => {
  answer(result());
  render(<CodexStatus/>);
  await screen.findByText('可用');
  fireEvent.click(screen.getByRole('button', { name: /重新检查/ }));
  await waitFor(() => expect(request).toHaveBeenCalledWith('/system/codex-status?refresh=true', expect.anything()));
  await screen.findByText('可用');
  const before = request.mock.calls.length;
  fireEvent.click(screen.getByRole('button', { name: /重新检查/ }));
  await waitFor(() => expect(request.mock.calls.length).toBe(before + 1));
  expect(request.mock.calls.at(-1)?.[0]).toBe('/system/codex-status?refresh=true');
});

it('keeps the failure inside the card when the request itself fails or the answer is malformed', async () => {
  request.mockImplementation(async () => { throw new Error('网络中断'); });
  const view = render(<CodexStatus/>);
  expect(await screen.findByText('网络中断')).toBeTruthy();
  expect(screen.queryByText('可用')).toBeNull();
  view.unmount();
  answer([]);
  render(<CodexStatus/>);
  expect(await screen.findByText('检查结果的格式无法识别。')).toBeTruthy();
});

const person = (role: User['role']): User => ({ id: role, role, org_id: null, team_id: null, active: true, name: role, email: `${role}@test.local` });

it('appears on the home page for the super administrator only', async () => {
  answer(result());
  request.mockImplementation(async (path: string) => path === '/overview' ? { users: 1, groups: 1, conversations: 1, runs: 1 } : result());
  const admin = render(<Overview user={person('super_admin')} navigate={() => undefined}/>);
  expect(await screen.findByRole('heading', { name: 'Codex CLI 接入检查' })).toBeTruthy();
  await screen.findByText('可用');
  admin.unmount();
  request.mockClear();
  for (const role of ['org_admin', 'team_lead', 'member'] as const) {
    const view = render(<Overview user={person(role)} navigate={() => undefined}/>);
    await screen.findByText('工作空间概况');
    await waitFor(() => expect(request).toHaveBeenCalledWith('/overview', expect.anything()));
    expect(screen.queryByText('Codex CLI 接入检查')).toBeNull();
    view.unmount();
  }
  expect(request.mock.calls.some(([path]) => String(path).startsWith('/system/codex-status'))).toBe(false);
});

it('does not break the rest of the home page when the check fails', async () => {
  request.mockImplementation(async (path: string) => { if (path === '/overview') return { users: 3, groups: 2, conversations: 5, runs: 7 }; throw new Error('检查服务异常'); });
  render(<Overview user={person('super_admin')} navigate={() => undefined}/>);
  expect(await screen.findByText('检查服务异常')).toBeTruthy();
  expect(await screen.findByText('可见用户')).toBeTruthy();
  expect(screen.getByText('从这里开始')).toBeTruthy();
});
