// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { App } from './main';
import { Settings } from './Settings';
import type { User } from './api';

const { request, resize } = vi.hoisted(() => ({ request: vi.fn(), resize: vi.fn() }));
vi.mock('./api', async original => ({ ...await original<typeof import('./api')>(), api: request, post: (path: string, body: unknown) => request(path, { method: 'POST', body }) }));
vi.mock('./avatarImage', async original => ({ ...await original<typeof import('./avatarImage')>(), avatarFromFile: resize }));

const user: User = { id: 'u1', role: 'member', org_id: 'org', team_id: 'team', active: true, name: 'Owner', email: 'owner@test.local' };
const picture = 'data:image/jpeg;base64,/9j/AAAA';
beforeEach(() => { window.history.replaceState(null, '', '#/overview'); Element.prototype.scrollIntoView = vi.fn(); });
afterEach(() => { cleanup(); request.mockReset(); resize.mockReset(); window.history.replaceState(null, '', '#/overview'); });

it('opens the settings page from the sidebar profile and the top bar avatar', async () => {
  request.mockImplementation(async (path: string) => path === '/setup/status' ? true : path === '/auth/me' ? user : path === '/overview' ? {} : []);
  render(<App/>);
  const links = await screen.findAllByRole('button', { name: '个人设置' });
  expect(links).toHaveLength(2);
  fireEvent.click(links[1]);
  await waitFor(() => expect(window.location.hash).toBe('#/settings'));
  expect(await screen.findByRole('heading', { name: '个人设置' })).toBeTruthy();
  fireEvent.click(links[0]);
  expect(window.location.hash).toBe('#/settings');
});

it('saves only the changed name and reports the saved user', async () => {
  request.mockResolvedValue({ ...user, name: '新名字', avatar: null });
  const saved = vi.fn();
  render(<Settings user={user} onSaved={saved}/>);
  const save = screen.getByRole('button', { name: '保存' }) as HTMLButtonElement;
  expect(save.disabled).toBe(true);
  fireEvent.change(screen.getByLabelText('名字'), { target: { value: '  新名字  ' } });
  fireEvent.click(save);
  await waitFor(() => expect(saved).toHaveBeenCalledWith({ ...user, name: '新名字', avatar: null }));
  expect(request).toHaveBeenCalledWith('/auth/me', { method: 'PATCH', body: JSON.stringify({ name: '新名字' }) });
});

it('uploads a new avatar, then removes it', async () => {
  resize.mockResolvedValue(picture);
  request.mockImplementation(async (_: string, options: RequestInit) => ({ ...user, avatar: JSON.parse(String(options.body)).avatar ?? null }));
  const saved = vi.fn();
  const view = render(<Settings user={user} onSaved={saved}/>);
  fireEvent.change(screen.getByLabelText('选择头像图片'), { target: { files: [new File(['x'], 'me.png', { type: 'image/png' })] } });
  await waitFor(() => expect(view.container.querySelector('.settings-avatar img')?.getAttribute('src')).toBe(picture));
  fireEvent.click(screen.getByRole('button', { name: '保存' }));
  await waitFor(() => expect(saved).toHaveBeenCalledWith({ ...user, avatar: picture }));
  expect(request).toHaveBeenCalledWith('/auth/me', { method: 'PATCH', body: JSON.stringify({ avatar: picture }) });

  cleanup();
  request.mockClear();
  const withAvatar = { ...user, avatar: picture };
  render(<Settings user={withAvatar} onSaved={saved}/>);
  fireEvent.click(screen.getByRole('button', { name: '移除头像' }));
  fireEvent.click(screen.getByRole('button', { name: '保存' }));
  await waitFor(() => expect(request).toHaveBeenCalledWith('/auth/me', { method: 'PATCH', body: JSON.stringify({ avatar: null }) }));
});

it('shows an unreadable picture error and keeps the current avatar', async () => {
  resize.mockRejectedValue(new Error('无法读取这张图片，请换一张。'));
  render(<Settings user={user} onSaved={vi.fn()}/>);
  fireEvent.change(screen.getByLabelText('选择头像图片'), { target: { files: [new File(['x'], 'bad.png', { type: 'image/png' })] } });
  expect((await screen.findByRole('alert')).textContent).toContain('无法读取这张图片');
  expect((screen.getByRole('button', { name: '保存' }) as HTMLButtonElement).disabled).toBe(true);
});

it('keeps the edits and shows the server error when saving fails', async () => {
  request.mockRejectedValue(new Error('服务器忙，请稍后重试。'));
  render(<Settings user={user} onSaved={vi.fn()}/>);
  fireEvent.change(screen.getByLabelText('名字'), { target: { value: 'Other' } });
  fireEvent.click(screen.getByRole('button', { name: '保存' }));
  expect((await screen.findByRole('alert')).textContent).toContain('服务器忙');
  expect((screen.getByLabelText('名字') as HTMLInputElement).value).toBe('Other');
});
