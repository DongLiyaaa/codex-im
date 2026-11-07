// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { Management } from './Management';
import { useData } from './ui';
import type { User } from './api';
const { request } = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock('./api', async original => ({ ...await original<typeof import('./api')>(), api: request, post: (path: string, body: unknown) => request(path, { method: 'POST', body }) }));
beforeEach(() => { HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); }; });
afterEach(() => { cleanup(); request.mockReset(); });
const user: User = { id: 'root', name: '管理员', email: 'r@example.invalid', role: 'super_admin', active: true, org_id: null, team_id: null };

function Cached({ owner = 'a', path = '/items' }: { owner?: string; path?: string }) {
  const list = useData<string[]>(path, 0, true, owner);
  return <><button onClick={list.reload}>刷新</button><p>{list.refreshing ? '刷新中' : ''}</p><p>{list.error}</p>{list.data?.map(s => <p key={s}>{s}</p>)}</>;
}

it('retains same-scope data through refresh and failure but never across users', async () => {
  request.mockResolvedValueOnce(['previous']);
  const view = render(<Cached/>);
  await screen.findByText('previous');
  let reject!: (error: Error) => void;
  request.mockImplementationOnce(() => new Promise((_resolve, r) => { reject = r; }));
  fireEvent.click(screen.getByText('刷新'));
  await screen.findByText('刷新中');
  expect(screen.getByText('previous')).toBeTruthy();
  await act(async () => reject(new Error('网络失败')));
  await screen.findByText('网络失败');
  expect(screen.getByText('previous')).toBeTruthy();
  request.mockImplementationOnce(() => new Promise(() => {}));
  view.rerender(<Cached owner="b"/>);
  expect(screen.queryByText('previous')).toBeNull();
  expect(screen.queryByText('网络失败')).toBeNull();
});

it('does not resurrect a stale response after a path change', async () => {
  let resolve!: (value: string[]) => void;
  request.mockImplementationOnce(() => new Promise(r => { resolve = r; })).mockResolvedValueOnce(['new-path']);
  const view = render(<Cached/>);
  view.rerender(<Cached path="/other"/>);
  await screen.findByText('new-path');
  await act(async () => resolve(['old-path']));
  expect(screen.queryByText('old-path')).toBeNull();
});

it('preserves search and lets saved user authorization open the corresponding preview', async () => {
  request.mockImplementation(async (path: string) => {
    if (path === '/bindings') return [{ id: 'b', subject_type: 'org', subject_id: 'org', resource_id: 'r', can_manage: true }];
    if (path === '/users') return [user, { ...user, id: 'member', name: '员工', role: 'member', org_id: 'org' }];
    if (path === '/resources') return [{ id: 'r', name: '关键词库', kind: 'skill', enabled: true, can_manage: true, config: {}, org_id: null }];
    if (path === '/directory') return { organizations: [{ id: 'org', name: '公司' }], departments: [] };
    if (path.startsWith('/bindings/preview')) return { user_id: 'member', group_id: null, can_send: true, context_reasons: [], resources: [] };
    return [];
  });
  render(<Management page="bindings" user={user} navigate={vi.fn()}/>);
  await screen.findByText('关键词库');
  fireEvent.change(screen.getByLabelText('搜索授权'), { target: { value: '关键词' } });
  fireEvent.click(screen.getByRole('button', { name: '编辑授权' }));
  const box = screen.getByRole('dialog');
  fireEvent.change(within(box).getByLabelText('授权对象类型'), { target: { value: 'user' } });
  fireEvent.change(within(box).getByLabelText('搜索授权对象'), { target: { value: '员工' } });
  fireEvent.change(within(box).getByLabelText('授权对象'), { target: { value: 'member' } });
  fireEvent.click(within(box).getByRole('button', { name: '保存授权' }));
  await screen.findByRole('button', { name: '查看生效结果' });
  expect((screen.getByLabelText('搜索授权') as HTMLInputElement).value).toBe('关键词');
  fireEvent.click(screen.getByRole('button', { name: '查看生效结果' }));
  await waitFor(() => expect(request.mock.calls.some(([path]) => path === '/bindings/preview?user_id=member')).toBe(true));
  expect((screen.getByLabelText('预览用户') as HTMLSelectElement).value).toBe('member');
});
