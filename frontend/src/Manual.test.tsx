// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { Manual } from './Manual';
import { getPageHelp } from './pageHelpContent';
import type { User } from './api';
const user: User = { id: 'root', name: 'Owner', email: 'owner@example.invalid', role: 'super_admin', active: true, org_id: null, team_id: null };
beforeEach(() => { Element.prototype.scrollIntoView = vi.fn(); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it('uses the same role-filtered chapter content as page help and links to allowed pages', () => {
  const navigate = vi.fn();
  render(<Manual user={user} navigate={navigate}/>);
  fireEvent.click(within(screen.getByRole('navigation', { name: '说明书目录' })).getByRole('button', { name: 'Skill和MCP管理' }));
  const article = screen.getByRole('article');
  const content = getPageHelp('resources', user.role);
  expect(within(article).getByText(content.purpose)).toBeTruthy();
  for (const step of content.steps) expect(within(article).getByText(step.text)).toBeTruthy();
  expect(within(article).getByRole('heading', { name: '注意事项' })).toBeTruthy();
  expect(within(article).getByRole('heading', { name: '常见问题' })).toBeTruthy();
  fireEvent.click(within(article).getByRole('button', { name: '前往页面' }));
  expect(navigate).toHaveBeenCalledWith('resources');
});

it('searches visible text and handles an empty result', () => {
  render(<Manual user={user} navigate={vi.fn()}/>);
  const search = screen.getByRole('searchbox', { name: '搜索使用说明' });
  fireEvent.change(search, { target: { value: '64000' } });
  expect(screen.getByRole('heading', { name: 'Skill和MCP管理' })).toBeTruthy();
  expect(screen.getByText('找到 1 个相关章节')).toBeTruthy();
  fireEvent.change(search, { target: { value: 'no-such-phrase-xyz' } });
  expect(screen.getByText('没有匹配的说明')).toBeTruthy();
  expect(screen.queryByRole('article')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '清空搜索' }));
  expect((search as HTMLInputElement).value).toBe('');
  expect(screen.getByRole('article')).toBeTruthy();
});

it('does not expose restricted chapters or match hidden administrator instructions', () => {
  render(<Manual user={{ ...user, role: 'member' }} navigate={vi.fn()}/>);
  const toc = screen.getByRole('navigation', { name: '说明书目录' });
  expect(within(toc).queryByRole('button', { name: '组织与部门' })).toBeNull();
  expect(within(toc).queryByRole('button', { name: '审计日志' })).toBeNull();
  fireEvent.change(screen.getByRole('searchbox'), { target: { value: '点击“新增 Skill / MCP”' } });
  expect(screen.getByText('没有匹配的说明')).toBeTruthy();
});

it('does not turn public initialization documentation into an executable setup link', () => {
  render(<Manual user={user} navigate={vi.fn()}/>);
  fireEvent.click(screen.getByRole('button', { name: '首次注册管理员' }));
  expect(screen.getByRole('heading', { name: '首次注册管理员' })).toBeTruthy();
  expect(screen.queryByRole('button', { name: '前往页面' })).toBeNull();
});

it('searches expanded IM cases and excludes restricted configuration details', () => {
  const view = render(<Manual user={user} navigate={vi.fn()}/>);
  fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'im.message.receive_v1' } });
  expect(screen.getByRole('heading', { name: 'IM 集成' })).toBeTruthy();
  const article = screen.getByRole('article');
  expect(within(article).getByText(/添加“接收消息 v2.0”/)).toBeTruthy();
  expect(within(article).getByText(/添加“接收消息 v2.0”/).closest('details')?.open).toBe(true);
  expect(article.querySelector('details')?.open).toBe(false);
  view.unmount();
  render(<Manual user={{ ...user, role: 'member' }} navigate={vi.fn()}/>);
  fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'im.message.receive_v1' } });
  expect(screen.getByText('没有匹配的说明')).toBeTruthy();
  fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'private_chat_required' } });
  expect(screen.getByRole('heading', { name: 'IM 集成' })).toBeTruthy();
  expect(screen.getByRole('region', { name: '卡住时按症状排查表格' })).toBeTruthy();
  expect(screen.queryByText('管理员：准备个人 OAuth 应用和可用人员')).toBeNull();
});
