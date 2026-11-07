// @vitest-environment jsdom
import { afterEach, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { PageHelp, PageHelpLayout } from './PageHelp';

afterEach(cleanup);

it('adds only a floating sibling without wrapping or reserving page space', () => {
  const view = render(<PageHelpLayout pageId="overview" title="工作台概览" identityKey="user"><main data-testid="page">原页面</main></PageHelpLayout>);
  const page = screen.getByTestId('page');
  expect(page.parentElement).toBe(view.container);
  expect(page.nextElementSibling?.className).toBe('page-help');
  expect(view.container.children).toHaveLength(2);
  expect(view.container.querySelector('.page-help-layout, .page-help-viewport')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '本页说明' }));
  expect(page.parentElement).toBe(view.container);
  expect(view.container.children).toHaveLength(2);
});

it('starts collapsed and opens non-modal instructions for the current page', () => {
  render(<PageHelp pageId="bindings" title="绑定与授权" role="super_admin"/>);
  const trigger = screen.getByRole('button', { name: '本页说明' });
  expect(trigger.getAttribute('aria-expanded')).toBe('false');
  expect(screen.queryByRole('dialog')).toBeNull();
  fireEvent.click(trigger);
  const panel = screen.getByRole('dialog', { name: '绑定与授权 · 使用说明' });
  expect(panel.getAttribute('aria-modal')).toBe('false');
  expect(document.activeElement).toBe(panel);
  expect(trigger.getAttribute('aria-controls')).toBe(panel.id);
  expect(screen.queryByText(/UI 预览|样式确认后补充/)).toBeNull();
  expect(within(panel).getByText(/点击“新增授权”/)).toBeTruthy();
  expect(within(panel).getByRole('heading', { name: '本页能做什么' })).toBeTruthy();
  expect(within(panel).getByRole('heading', { name: '操作步骤' })).toBeTruthy();
  expect(document.body.style.overflow).not.toBe('hidden');
});

it('collapses on Escape or its close button and restores trigger focus', () => {
  render(<PageHelp pageId="chat" title="智能会话"/>);
  const trigger = screen.getByRole('button', { name: '本页说明' });
  fireEvent.click(trigger);
  fireEvent.keyDown(document, { key: 'Escape' });
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(document.activeElement).toBe(trigger);
  fireEvent.click(trigger);
  fireEvent.click(screen.getByRole('button', { name: '收起本页说明' }));
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(document.activeElement).toBe(trigger);
});

it('updates the page title and resets body scroll and details on route change', () => {
  const view = render(<PageHelp pageId="chat" title="智能会话"/>);
  fireEvent.click(screen.getByRole('button', { name: '本页说明' }));
  const oldBody = view.container.querySelector('.page-help-body')!;
  const details = oldBody.querySelector('details')!;
  details.open = true;
  oldBody.scrollTop = 100;
  view.rerender(<PageHelp pageId="resources" title="Skill和MCP管理"/>);
  expect(screen.queryByRole('heading', { name: '智能会话 · 使用说明' })).toBeNull();
  expect(screen.getByRole('dialog', { name: 'Skill和MCP管理 · 使用说明' })).toBeTruthy();
  const newBody = view.container.querySelector('.page-help-body')!;
  expect(newBody).not.toBe(oldBody);
  expect(newBody.scrollTop).toBe(0);
  expect(newBody.querySelector('details')!.open).toBe(false);
});

it('does not consume Escape from an existing editing dialog', () => {
  render(<><PageHelp pageId="users" title="用户与角色"/><dialog open aria-label="编辑成员"/></>);
  fireEvent.click(screen.getByRole('button', { name: '本页说明' }));
  const event = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true });
  document.dispatchEvent(event);
  expect(event.defaultPrevented).toBe(false);
  expect(screen.getByRole('dialog', { name: '用户与角色 · 使用说明' })).toBeTruthy();
});

it('hides while mobile navigation is active and resets when the identity changes', () => {
  const view = render(<PageHelpLayout pageId="overview" title="工作台概览" identityKey="u1:super_admin"><button>页面操作</button></PageHelpLayout>);
  fireEvent.click(screen.getByRole('button', { name: '本页说明' }));
  expect(screen.getByRole('button', { name: '页面操作' })).toBeTruthy();
  view.rerender(<PageHelpLayout pageId="overview" title="工作台概览" identityKey="u1:super_admin" suspended><button>页面操作</button></PageHelpLayout>);
  expect(screen.queryByRole('button', { name: '本页说明' })).toBeNull();
  view.rerender(<PageHelpLayout pageId="login" title="登录工作空间" identityKey="login"><p>登录</p></PageHelpLayout>);
  expect(screen.getByRole('button', { name: '本页说明' }).getAttribute('aria-expanded')).toBe('false');
  expect(screen.queryByRole('dialog')).toBeNull();
});
