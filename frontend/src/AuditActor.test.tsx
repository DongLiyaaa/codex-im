// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen, within } from '@testing-library/react';
import { AuditActor, chatLabel } from './AuditActor';
import type { AuditContext } from './AuditActor';
import { AuditPanel } from './AuditPanel';

const { request } = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock('./api', async original => ({ ...await original<typeof import('./api')>(), api: request }));

const base: AuditContext = { actor_name: '张三账号', channel: 'feishu', nickname: '张三', group: null, private: false };
const context = (change: Partial<AuditContext> = {}): AuditContext => ({ ...base, ...change });
const ID = 'c2205b84-29d0-4263-8d36-f361a8e23cc9';

beforeEach(() => request.mockReset());
afterEach(() => cleanup());

it('shows the chat-platform nickname, the channel, the group, the Hub account and keeps the id', () => {
  render(<AuditActor id={ID} context={context({ group: { name: '产品群', state: 'ok', archived: false } })}/>);
  expect(screen.getByText('张三').tagName).toBe('STRONG');
  expect(screen.getByText('飞书')).toBeTruthy();
  expect(screen.getByText('群：产品群')).toBeTruthy();
  expect(screen.getByText('Hub 账号：张三账号')).toBeTruthy();
  expect(screen.getByText(ID)).toBeTruthy();
});

it('says private chat when there is no group, and names the DingTalk channel', () => {
  render(<AuditActor id={ID} context={context({ channel: 'dingtalk', nickname: '李四', actor_name: '李四账号', private: true })}/>);
  expect(screen.getByText('钉钉')).toBeTruthy();
  expect(screen.getByText('私聊')).toBeTruthy();
  expect(screen.queryByText(/^群：/)).toBeNull();
});

it('falls back to the account name when there is no nickname, without a duplicate account line', () => {
  render(<AuditActor id={ID} context={context({ nickname: null, channel: 'web' })}/>);
  expect(screen.getByText('张三账号').tagName).toBe('STRONG');
  expect(screen.getByText('网页')).toBeTruthy();
  expect(screen.queryByText(/Hub 账号/)).toBeNull();
  expect(screen.queryByText('私聊')).toBeNull();
});

it('does not repeat the account line when the nickname is the same as the account name', () => {
  render(<AuditActor id={ID} context={context({ nickname: '张三账号' })}/>);
  expect(screen.queryByText(/Hub 账号/)).toBeNull();
});

it.each([
  [{ name: null, state: 'hidden', archived: false }, '群：无权查看'],
  [{ name: null, state: 'missing', archived: false }, '群：已删除'],
  [{ name: '旧项目群', state: 'ok', archived: true }, '群：旧项目群（已归档）'],
] as const)('describes a group that is %j', (group, text) => {
  render(<AuditActor id={ID} context={context({ group })}/>);
  expect(screen.getByText(text)).toBeTruthy();
  expect(chatLabel(context({ group }))).toBe(text);
});

it('never prints a hidden group name', () => {
  render(<AuditActor id={ID} context={context({ group: { name: '机密群', state: 'hidden', archived: false } })}/>);
  expect(screen.queryByText(/机密群/)).toBeNull();
});

it('is just the id when the server sends no context or the account is unknown', () => {
  const plain = render(<table><tbody><tr><td><AuditActor id={ID}/></td></tr></tbody></table>);
  expect(within(plain.container).getByText(ID)).toBeTruthy();
  expect(plain.container.querySelector('.audit-actor')).toBeNull();
  plain.unmount();
  const unknown = render(<table><tbody><tr><td><AuditActor id={ID} context={context({ actor_name: null, nickname: null })}/></td></tr></tbody></table>);
  expect(unknown.container.querySelector('.audit-actor')).toBeNull();
  expect(within(unknown.container).getByText(ID)).toBeTruthy();
  unknown.unmount();
  const none = render(<table><tbody><tr><td><AuditActor id={null}/></td></tr></tbody></table>);
  expect(within(none.container).getByText('—')).toBeTruthy();
});

it('treats a nickname that looks like markup as plain text', () => {
  const hostile = '<img src=x onerror=alert(1)>';
  const view = render(<AuditActor id={ID} context={context({ nickname: hostile })}/>);
  expect(screen.getByText(hostile)).toBeTruthy();
  expect(view.container.querySelector('img')).toBeNull();
});

const row = (change: Record<string, unknown> = {}) => ({ id: 'row-1', actor_id: ID, action: 'run.succeeded', target_id: 'run-1', details: {}, created_at: '2026-10-05T07:24:51Z', ...change });
const page = (items: unknown[]) => ({ items, total: items.length, page: 1, page_size: 50, pages: 1 });

it('puts the person and the group in the audit table under an 操作人 header', async () => {
  request.mockImplementation(async () => page([row({ context: context({ group: { name: '产品群', state: 'ok', archived: false } }) })]));
  render(<AuditPanel userId="root"/>);
  expect(await screen.findByText('张三')).toBeTruthy();
  expect(screen.getByRole('columnheader', { name: '操作人' })).toBeTruthy();
  expect(screen.queryByRole('columnheader', { name: '操作人 ID' })).toBeNull();
  expect(screen.getByText('群：产品群')).toBeTruthy();
  expect(screen.getByText(ID)).toBeTruthy();
});

it('still renders every row when an older server sends no context', async () => {
  request.mockImplementation(async () => page([row(), row({ id: 'row-2', actor_id: null, action: 'im.outbox.discarded' })]));
  render(<AuditPanel userId="root"/>);
  expect(await screen.findByText(ID)).toBeTruthy();
  expect(screen.getByText('im.outbox.discarded')).toBeTruthy();
  expect(screen.getAllByText('—').length).toBeGreaterThan(0);
});
