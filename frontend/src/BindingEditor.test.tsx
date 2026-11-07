// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { BindingEditor } from './BindingEditor';
import type { Binding, Resource, User } from './api';
const { request } = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock('./api', async original => ({ ...await original<typeof import('./api')>(), api: request }));
beforeEach(() => { HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); }; });
afterEach(() => { cleanup(); request.mockReset(); });
const binding: Binding = { id: 'binding', subject_type: 'org', subject_id: 'org', resource_id: 'skill', can_manage: true };
const resource: Resource = { id: 'skill', name: '关键词库', kind: 'skill', description: '', enabled: true, org_id: null, config: {}, can_manage: true };
const user: User = { id: 'member', name: '员工', email: 'test@example.invalid', role: 'member', org_id: 'org', team_id: 'team', active: true };
function setup(extra = {}) {
  const close = vi.fn(), saved = vi.fn();
  render(<BindingEditor binding={binding} users={[user]} groups={[]} resources={[resource]} directory={{ organizations: [{ id: 'org', name: '公司' }], departments: [{ id: 'team', org_id: 'org', name: '运营' }] }} loading={false} error="" close={close} saved={saved} {...extra}/>);
  return { close, saved };
}

it('prefills a binding, clears the old subject on type change and updates it without deleting', async () => {
  request.mockResolvedValue({});
  const { saved } = setup();
  expect((screen.getByLabelText('授权对象类型') as HTMLSelectElement).value).toBe('org');
  expect((screen.getByLabelText('授权对象') as HTMLSelectElement).value).toBe('org');
  fireEvent.change(screen.getByLabelText('授权对象类型'), { target: { value: 'user' } });
  expect((screen.getByLabelText('授权对象') as HTMLSelectElement).value).toBe('');
  fireEvent.change(screen.getByLabelText('授权对象'), { target: { value: 'member' } });
  fireEvent.click(screen.getByRole('button', { name: '保存授权' }));
  await waitFor(() => expect(saved).toHaveBeenCalledTimes(1));
  expect(request).toHaveBeenCalledTimes(1);
  expect(request).toHaveBeenCalledWith('/bindings/binding', { method: 'PUT', body: JSON.stringify({ subject_type: 'user', subject_id: 'member', resource_id: 'skill' }) });
});

it('retains changed selections after server rejection', async () => {
  request.mockRejectedValue(new Error('该对象已拥有此授权'));
  const { saved } = setup();
  fireEvent.change(screen.getByLabelText('授权对象类型'), { target: { value: 'team' } });
  fireEvent.change(screen.getByLabelText('授权对象'), { target: { value: 'team' } });
  fireEvent.click(screen.getByRole('button', { name: '保存授权' }));
  await screen.findByText('该对象已拥有此授权');
  expect((screen.getByLabelText('授权对象') as HTMLSelectElement).value).toBe('team');
  expect(saved).not.toHaveBeenCalled();
});

it('prevents duplicate submission and closing while saving', async () => {
  let resolve!: (value: unknown) => void;
  request.mockImplementation(() => new Promise(r => { resolve = r; }));
  const { close, saved } = setup();
  fireEvent.change(screen.getByLabelText('授权对象类型'), { target: { value: 'user' } });
  fireEvent.change(screen.getByLabelText('授权对象'), { target: { value: 'member' } });
  const button = screen.getByRole('button', { name: '保存授权' });
  fireEvent.click(button); fireEvent.click(button);
  fireEvent.click(screen.getByRole('button', { name: '关闭弹窗' }));
  expect(request).toHaveBeenCalledTimes(1);
  expect(close).not.toHaveBeenCalled();
  resolve({});
  await waitFor(() => expect(saved).toHaveBeenCalled());
});

it('allows cancellation without a network mutation', () => {
  const { close } = setup();
  fireEvent.click(screen.getByRole('button', { name: '取消' }));
  expect(close).toHaveBeenCalledTimes(1);
  expect(request).not.toHaveBeenCalled();
});

it('does not silently replace a missing target', () => {
  setup({ binding: { ...binding, subject_id: 'missing' } });
  expect((screen.getByLabelText('授权对象') as HTMLSelectElement).value).toBe('missing');
  expect(screen.getByText('原选项已不可用（missing）')).toBeTruthy();
  expect(request).not.toHaveBeenCalled();
});

it('asks before discarding changes and preserves edits when continuing', async () => {
  const { close } = setup();
  fireEvent.change(screen.getByLabelText('授权对象类型'), { target: { value: 'user' } });
  fireEvent.change(screen.getByLabelText('授权对象'), { target: { value: 'member' } });
  fireEvent.click(screen.getByRole('button', { name: '关闭弹窗' }));
  expect(screen.getByText('有未保存的修改，是否放弃？')).toBeTruthy();
  expect(close).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '继续编辑' }));
  expect((screen.getByLabelText('授权对象') as HTMLSelectElement).value).toBe('member');
  fireEvent.click(screen.getByRole('button', { name: '取消' }));
  fireEvent.click(screen.getByRole('button', { name: '放弃修改' }));
  expect(close).toHaveBeenCalledTimes(1);
  expect(request).not.toHaveBeenCalled();
});

it('disables saving until a valid change is made and ignores search-only changes', () => {
  setup();
  const save = screen.getByRole('button', { name: '保存授权' }) as HTMLButtonElement;
  expect(save.disabled).toBe(true);
  fireEvent.change(screen.getByLabelText('搜索授权对象'), { target: { value: 'nothing' } });
  expect(save.disabled).toBe(true);
  expect((screen.getByLabelText('授权对象') as HTMLSelectElement).value).toBe('org');
  fireEvent.change(screen.getByLabelText('授权对象类型'), { target: { value: 'user' } });
  expect(save.disabled).toBe(true);
  fireEvent.change(screen.getByLabelText('授权对象'), { target: { value: 'member' } });
  expect(save.disabled).toBe(false);
});
