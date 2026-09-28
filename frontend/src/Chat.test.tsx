// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MessageWorkStatus } from './MessageWorkStatus';
import { Chat } from './Chat';
import type { Message, Run } from './api';
const { request } = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock('./api', async original => ({ ...await original<typeof import('./api')>(), api: request }));
vi.mock('./ui', async original => ({ ...await original<typeof import('./ui')>(), useData: (path: string) => ({ data: path === '/conversations' ? [{ id: 'a', title: 'A', owner_id: 'owner' }, { id: 'b', title: 'B', owner_id: 'owner' }] : [], loading: false, reload: vi.fn() }) }));
const message: Message = { id: 'm', conversation_id: 'a', role: 'user', content: 'Task', created_at: '2026-09-27T00:00:00Z' };
const run: Run = { id: 'r', message_id: 'm', status: 'queued', created_at: message.created_at };
afterEach(() => { cleanup(); vi.useRealTimers(); request.mockReset(); });
describe('message working feedback', () => {
  it.each([['web', '工作中'], ['feishu', '飞书：工作'], ['dingtalk', '钉钉：工作']] as const)('renders %s and clears when body arrives', (provider, label) => {
    const props = { message, messages: [message], run: { ...run, provider } };
    const view = render(<MessageWorkStatus {...props}/>);
    expect(screen.getByRole('status').textContent).toBe(label);
    view.rerender(<MessageWorkStatus {...props} messages={[message, { ...message, id: 'answer', role: 'assistant', content: 'Answer' }]}/>);
    expect(screen.queryByRole('status')).toBeNull();
  });
  it.each(['succeeded', 'failed', 'cancelled', 'interrupted'] as const)('clears %s', status => {
    render(<MessageWorkStatus message={message} messages={[message]} run={{ ...run, status }}/>);
    expect(screen.queryByRole('status')).toBeNull();
  });
  it('does not infer work from last user message or unrelated run', () => {
    const view = render(<MessageWorkStatus message={message} messages={[message]} run={null}/>);
    expect(screen.queryByRole('status')).toBeNull();
    view.rerender(<MessageWorkStatus message={message} messages={[message]} run={{ ...run, message_id: 'other' }}/>);
    expect(screen.queryByRole('status')).toBeNull();
  });
  it('restores an IM run for readonly supervision and observes completion', async () => {
    vi.useFakeTimers(); Element.prototype.scrollIntoView = vi.fn();
    let state = { messages: [message], active_run: { ...run, provider: 'feishu' }, latest_run: run };
    request.mockImplementation(async (path: string) => path.endsWith('/state') ? state : path.endsWith('/capabilities') ? { skills: [], mcps: [] } : [message]);
    render(<Chat user={{ id: 'admin', role: 'super_admin', org_id: null, team_id: null, email: '', name: '', active: true }}/>);
    await act(async () => {});
    expect(screen.getByRole('status').textContent).toBe('飞书：工作');
    expect((screen.getByRole('button', { name: '发送' }) as HTMLButtonElement).disabled).toBe(true);
    state = { ...state, messages: [message, { ...message, id: 'answer', role: 'assistant', content: 'Done' }], active_run: null as never, latest_run: { ...run, status: 'succeeded' } };
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(screen.queryByRole('status')).toBeNull(); expect(screen.getByText('Done')).toBeTruthy();
    expect(request.mock.calls.filter(([path]) => path.endsWith('/messages'))).toHaveLength(1);
  });
  it('aborts previous conversation polling and ignores its late response', async () => {
    Element.prototype.scrollIntoView = vi.fn(); let resolveOld!: (value: unknown) => void; let oldSignal!: AbortSignal;
    request.mockImplementation((path: string, options: RequestInit) => {
      if (path === '/conversations/a/state') { oldSignal = options.signal as AbortSignal; return new Promise(resolve => { resolveOld = resolve; }); }
      return Promise.resolve(path.endsWith('/state') ? { messages: [], active_run: null, latest_run: null } : path.endsWith('/capabilities') ? { skills: [], mcps: [] } : []);
    });
    render(<Chat user={{ id: 'owner', role: 'member', org_id: null, team_id: null, email: '', name: '', active: true }}/>);
    await act(async () => {}); fireEvent.click(screen.getByRole('button', { name: /B 我的会话/ }));
    await act(async () => {}); expect(oldSignal.aborted).toBe(true);
    await act(async () => { resolveOld({ messages: [message], active_run: run, latest_run: run }); });
    expect(screen.queryByText('Task')).toBeNull(); expect(screen.queryByRole('status')).toBeNull();
  });
});
