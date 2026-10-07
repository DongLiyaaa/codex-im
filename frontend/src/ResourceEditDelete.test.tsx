// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { Management } from './Management';
import type { Resource, User } from './api';

const { request } = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock('./api', async original => ({ ...await original<typeof import('./api')>(), api: (path: string, options?: RequestInit) => request(path, options) }));

const directory = { organizations: [{ id: 'org', name: '公司', legacy: false }],
  departments: [{ id: 'dept', org_id: 'org', name: '运营部', legacy: false }], can_create_org: true, can_create_department: true };
const root: User = { id: 'root', name: 'root', email: 'r@example.invalid', role: 'super_admin', org_id: null, team_id: null, active: true };
const skill: Resource = { id: 'r1', name: 'weekly', kind: 'skill', description: '周报', org_id: 'org', team_id: null, enabled: true, config: { content: '写周报' }, can_manage: true };
const mcp: Resource = { id: 'r2', name: 'docs', kind: 'mcp', description: '', org_id: null, team_id: null, enabled: true, config: { url: 'https://x.example/mcp', headers: { Authorization: '***' } }, can_manage: false };

beforeEach(() => {
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
  request.mockImplementation(async (path: string, options?: RequestInit) => {
    if (path === '/directory') return directory;
    if (path === '/resources' && !options?.method) return [skill, mcp];
    if (path.startsWith('/resources/')) return { ok: true };
    return [];
  });
});
afterEach(() => { cleanup(); request.mockReset(); vi.restoreAllMocks(); });

it('only offers edit and delete on resources the viewer may manage', async () => {
  render(<Management page="resources" user={root} navigate={vi.fn()}/>);
  await screen.findByText('weekly');
  expect(screen.getAllByText('编辑')).toHaveLength(1);
  expect(screen.getAllByText('删除')).toHaveLength(1);
});

it('edits a skill including its department scope', async () => {
  render(<Management page="resources" user={root} navigate={vi.fn()}/>);
  fireEvent.click(await screen.findByText('编辑'));
  const box = await screen.findByRole('dialog');
  await within(box).findByRole('option', { name: '运营部' });
  fireEvent.change(within(box).getByLabelText('名称'), { target: { value: 'weekly2' } });
  const team = within(box).getAllByRole('combobox').find(select => within(select).queryByRole('option', { name: '运营部' }))!;
  fireEvent.change(team, { target: { value: 'dept' } });
  fireEvent.click(within(box).getByText('保存'));
  await waitFor(() => expect(request.mock.calls.some(call => call[1]?.method === 'PATCH')).toBe(true));
  const call = request.mock.calls.find(item => item[1]?.method === 'PATCH')!;
  expect(call[0]).toBe('/resources/r1');
  const body = JSON.parse(call[1].body);
  expect(body).toMatchObject({ name: 'weekly2', org_id: 'org', team_id: 'dept', grant_scope: true });
  expect(body.config.content).toContain('写周报');
});

it('deletes only after confirmation', async () => {
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
  render(<Management page="resources" user={root} navigate={vi.fn()}/>);
  fireEvent.click(await screen.findByText('删除'));
  expect(request.mock.calls.some(call => call[1]?.method === 'DELETE')).toBe(false);
  confirm.mockReturnValue(true);
  fireEvent.click(screen.getByText('删除'));
  await waitFor(() => expect(request.mock.calls.some(call => call[0] === '/resources/r1' && call[1]?.method === 'DELETE')).toBe(true));
});

it('tells the administrator when a resource is scoped but granted to nobody, and links to granting', async () => {
  const navigate = vi.fn();
  request.mockImplementation(async (path: string) => path === '/directory' ? directory : path === '/resources' ? [{ ...skill, grants: [] }, mcp] : []);
  render(<Management page="resources" user={root} navigate={navigate}/>);
  expect(await screen.findByText(/尚未授权：适用范围只限定能授权给谁/)).toBeTruthy();
  fireEvent.click(screen.getByText('去授权'));
  expect(navigate).toHaveBeenCalledWith('bindings');
});

it('lists who a resource is granted to by name, and does not re-grant the scope by default when grants exist', async () => {
  request.mockImplementation(async (path: string, options?: RequestInit) => path === '/directory' ? directory
    : path === '/resources' && !options?.method ? [{ ...skill, team_id: 'dept', grants: [{ subject_type: 'team', subject_id: 'dept' }] }, mcp] : path.startsWith('/resources/') ? { ok: true } : []);
  render(<Management page="resources" user={root} navigate={vi.fn()}/>);
  expect(await screen.findByText('已授权：部门「公司 · 运营部」')).toBeTruthy();
  fireEvent.click(screen.getByText('编辑'));
  const box = await screen.findByRole('dialog');
  const grant = await within(box).findByLabelText(/同时授权给「公司 · 运营部」的所有成员和群/) as HTMLInputElement;
  expect(grant.checked).toBe(false);
  fireEvent.click(within(box).getByText('保存'));
  await waitFor(() => expect(request.mock.calls.some(call => call[1]?.method === 'PATCH')).toBe(true));
  expect(JSON.parse(request.mock.calls.find(item => item[1]?.method === 'PATCH')![1].body).grant_scope).toBe(false);
});
