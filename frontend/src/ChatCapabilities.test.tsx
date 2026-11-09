// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { ChatCapabilities } from './ChatCapabilities';
import type { User } from './api';

const { request } = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock('./api', async original => ({ ...await original<typeof import('./api')>(), api: request, post: (path: string, body: unknown) => request(path, body) }));
const admin: User = { id: 'admin', name: 'Admin', email: '', role: 'super_admin', active: true, org_id: null, team_id: null };
const conversation = { id: 'c', title: '监管会话', owner_id: 'owner', group_id: null, created_at: '' };
const data = { skills: [], mcps: [] };

beforeEach(() => {
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
  request.mockImplementation(async path => path === '/resources' ? [
    { id: 's', name: '测试技能', kind: 'skill', enabled: true, can_manage: true },
    { id: 'm', name: '测试工具', kind: 'mcp', enabled: true, can_manage: true },
  ] : []);
});
afterEach(() => { cleanup(); request.mockReset(); });

it('lets an administrator open authorization editing from a read-only supervised conversation', async () => {
  render(<ChatCapabilities user={admin} conversation={conversation} subjectUserId="owner" data={data} loading={false} error="" changed={vi.fn()}/>);
  const button = screen.getByRole('button', { name: '管理 Skills 授权' });
  fireEvent.click(button);
  expect(await screen.findByRole('heading', { name: '管理 Skills 授权' })).toBeTruthy();
  expect(await screen.findByRole('option', { name: '测试技能' })).toBeTruthy();
  expect(screen.queryByRole('option', { name: '测试工具' })).toBeNull();
});

it('opens MCP authorization editing with only MCP resources', async () => {
  render(<ChatCapabilities user={admin} conversation={conversation} subjectUserId="owner" data={data} loading={false} error="" changed={vi.fn()}/>);
  fireEvent.click(screen.getByRole('button', { name: '管理 MCP 授权' }));
  expect(await screen.findByRole('heading', { name: '管理 MCP 授权' })).toBeTruthy();
  expect(await screen.findByRole('option', { name: '测试工具' })).toBeTruthy();
  expect(screen.queryByRole('option', { name: '测试技能' })).toBeNull();
});

it('does not expose authorization management to ordinary members', () => {
  render(<ChatCapabilities user={{ ...admin, role: 'member' }} conversation={conversation} subjectUserId="owner" data={data} loading={false} error="" changed={vi.fn()}/>);
  expect(screen.queryByRole('button', { name: /管理.*授权/ })).toBeNull();
  expect(screen.getAllByText('如需调整，请联系组织管理员。')).toHaveLength(2);
});
