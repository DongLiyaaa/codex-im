// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { Management } from './Management';
import type { User } from './api';
const { request } = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock('./api', async original => ({ ...await original<typeof import('./api')>(), api: request, post: (p: string, b: unknown) => request(p, b) }));
const actor: User = { id: 'root', name: '管理员', email: 'root@example.invalid', role: 'super_admin', org_id: null, team_id: null, active: true };
beforeEach(() => {
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
  request.mockImplementation(async (p: string) => p === '/users' ? [actor, { ...actor, id: 'u', name: '停用员工', role: 'member', active: false }] : p === '/directory' ? { organizations: [], departments: [] } : p === '/groups' ? [{ id: 'g', name: '测试群', provider: 'feishu', member_ids: [], can_edit: false, can_delete: false }] : []);
});
afterEach(() => { cleanup(); request.mockReset(); });
it('combines user status and text filters and restores all rows', async () => {
  render(<Management page="users" user={actor} navigate={vi.fn()}/>);
  await screen.findByText('停用员工');
  fireEvent.change(screen.getByLabelText('筛选条件'), { target: { value: 'inactive' } });
  expect(screen.queryByText('管理员')).toBeNull();
  fireEvent.change(screen.getByRole('searchbox'), { target: { value: '不存在' } });
  expect(screen.getByText('没有匹配的结果')).toBeTruthy();
  fireEvent.click(screen.getByText('清空筛选'));
  expect(screen.getByText('管理员')).toBeTruthy();
  expect(screen.getByText('停用员工')).toBeTruthy();
});
it('does not carry a create dialog or filters into a different management page', async () => {
  const props = { user: actor, navigate: vi.fn() };
  const view = render(<Management {...props} page="users"/>);
  fireEvent.click(await screen.findByText('新增'));
  fireEvent.change(screen.getByLabelText('名称'), { target: { value: '未保存姓名' } });
  fireEvent.click(screen.getByRole('button', { name: '关闭弹窗' }));
  expect(screen.getByText('有未保存的修改，是否放弃？')).toBeTruthy();
  fireEvent.click(screen.getByText('继续编辑'));
  expect((screen.getByLabelText('名称') as HTMLInputElement).value).toBe('未保存姓名');
  view.rerender(<Management {...props} page="groups"/>);
  await screen.findByText('测试群');
  expect(screen.queryByRole('dialog')).toBeNull();
  expect((screen.getByRole('searchbox', { name: '搜索协作群组' }) as HTMLInputElement).value).toBe('');
});
