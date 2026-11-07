// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { BindingPreview } from './BindingPreview';
import type { User } from './api';
const { request } = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock('./api', async original => ({ ...await original<typeof import('./api')>(), api: request }));
beforeEach(() => { HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); }; });
afterEach(() => { cleanup(); request.mockReset(); });
const users: User[] = ['a', 'b'].map(id => ({ id, name: `用户${id}`, role: 'member', email: `${id}@test.invalid`, active: true, org_id: 'o', team_id: 't' }));
const result = (user_id = 'a') => ({ user_id, group_id: null, can_send: true, context_reasons: [], resources: [
  { id: 'r', name: '关键词库', kind: 'skill', enabled: true, effective: true, reasons: [], sources: [{ side: 'user', subject_type: 'team', subject_id: 't' }] },
  { id: 'm', name: '受限 MCP', kind: 'mcp', enabled: false, effective: false, reasons: ['disabled', 'user_not_granted'], sources: [] },
] });
const setup = () => render(<BindingPreview users={users} groups={[]} loading={false} error="" close={vi.fn()}/>);

it('does not query until a user is selected and renders reasons from the server', async () => {
  request.mockResolvedValue(result());
  setup();
  expect(request).not.toHaveBeenCalled();
  fireEvent.change(screen.getByLabelText('预览用户'), { target: { value: 'a' } });
  await screen.findByText('关键词库');
  expect(screen.getByText('用户侧 · 部门授权')).toBeTruthy();
  expect(screen.getByText('资源已停用；用户侧未授权')).toBeTruthy();
  expect(request).toHaveBeenCalledWith('/bindings/preview?user_id=a', expect.objectContaining({ signal: expect.any(AbortSignal) }));
});

it('discards stale responses after switching users', async () => {
  let resolve!: (value: unknown) => void;
  request.mockImplementationOnce(() => new Promise(r => { resolve = r; })).mockResolvedValueOnce({ ...result('b'), resources: [] });
  setup();
  fireEvent.change(screen.getByLabelText('预览用户'), { target: { value: 'a' } });
  await waitFor(() => expect(request).toHaveBeenCalledTimes(1));
  fireEvent.change(screen.getByLabelText('预览用户'), { target: { value: 'b' } });
  await screen.findByText('当前没有可使用的资源。');
  resolve(result('a'));
  await waitFor(() => expect(screen.queryByText('关键词库')).toBeNull());
  expect(request.mock.calls[0][1].signal.aborted).toBe(true);
});

it('shows failures rather than interpreting them as no authorization and supports retry', async () => {
  request.mockRejectedValueOnce(new Error('预览失败')).mockResolvedValueOnce(result());
  setup();
  fireEvent.change(screen.getByLabelText('预览用户'), { target: { value: 'a' } });
  await screen.findByText('预览失败');
  expect(screen.queryByText('当前没有可使用的资源。')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '重新预览' }));
  await screen.findByText('关键词库');
});

it('clears private results when moving to a group and requests the selected group', async () => {
  request.mockResolvedValueOnce(result()).mockResolvedValueOnce({ ...result(), group_id: 'g', resources: [{ ...result().resources[0], effective: false, reasons: ['group_not_granted'] }] });
  render(<BindingPreview users={users} groups={[{ id: 'g', name: '群', org_id: 'o', team_id: 't', member_ids: ['a'], provider: 'feishu' }]} loading={false} error="" close={vi.fn()}/>);
  fireEvent.change(screen.getByLabelText('预览用户'), { target: { value: 'a' } });
  await screen.findByText('关键词库');
  fireEvent.change(screen.getByLabelText('会话范围'), { target: { value: 'g' } });
  await screen.findByText('群侧未授权');
  expect(request).toHaveBeenLastCalledWith('/bindings/preview?user_id=a&group_id=g', expect.anything());
});
