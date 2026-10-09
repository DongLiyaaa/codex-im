// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { Chat } from './Chat';
import type { CodexRunnerStatus } from './CodexStatus';
import { claudeRows } from './CodexStatus';

const { request, agents } = vi.hoisted(() => ({ request: vi.fn(), agents: { value: { agents: [{ id: 'codex', label: 'Codex' }, { id: 'claude', label: 'Claude' }], default: 'codex', preferred: 'claude' } as unknown } }));
vi.mock('./api', async original => ({ ...await original<typeof import('./api')>(), api: request }));
vi.mock('./ui', async original => ({ ...await original<typeof import('./ui')>(), useData: (path: string) => ({
  data: path === '/agents' ? agents.value : path === '/conversations' ? [{ id: 'a', title: 'A', owner_id: 'u', agent: 'claude' }, { id: 'b', title: 'B', owner_id: 'u', agent: 'codex' }] : [],
  loading: false, reload: vi.fn() }) }));
const user = { id: 'u', role: 'member' as const, org_id: null, team_id: null, email: '', name: '', active: true };
HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
HTMLDialogElement.prototype.close = function () { this.removeAttribute('open'); };
afterEach(() => { cleanup(); request.mockReset(); });

describe('agent choice in the workbench', () => {
  it('labels each conversation with its agent and offers the choice with the user preference preselected', async () => {
    Element.prototype.scrollIntoView = vi.fn();
    request.mockImplementation(async (path: string) => path.endsWith('/state') ? { messages: [], active_run: null, latest_run: null } : path.endsWith('/capabilities') ? { skills: [], mcps: [] } : {});
    render(<Chat user={user as never}/>);
    await act(async () => {});
    expect(screen.getByText('我的会话 · Claude')).toBeTruthy();
    expect(screen.getByText('我的会话 · Codex')).toBeTruthy();
    fireEvent.click(screen.getByLabelText('创建会话'));
    const select = (await screen.findByRole('combobox', { name: /^Agent/ })) as HTMLSelectElement;
    expect(select.value).toBe('claude');
    expect([...select.options].map(o => o.text)).toEqual(['Codex', 'Claude']);
  });

  it('hides the agent choice and labels when only one agent is enabled', async () => {
    Element.prototype.scrollIntoView = vi.fn();
    agents.value = { agents: [{ id: 'codex', label: 'Codex' }], default: 'codex', preferred: 'codex' };
    request.mockImplementation(async (path: string) => path.endsWith('/state') ? { messages: [], active_run: null, latest_run: null } : path.endsWith('/capabilities') ? { skills: [], mcps: [] } : {});
    render(<Chat user={user as never}/>);
    await act(async () => {});
    expect(screen.queryByText(/· Claude/)).toBeNull();
    fireEvent.click(screen.getByLabelText('创建会话'));
    expect(screen.queryByRole('combobox', { name: /^Agent/ })).toBeNull();
  });
});

describe('Claude status rows', () => {
  const ok: NonNullable<NonNullable<CodexRunnerStatus['agents']>['claude']> = {
    enabled: true, ready: true, installed: true, version: '2.1.286', pinned_version: '2.1.286',
    model: { id: 'claude-x', endpoint_host: 'gw.example.com', credential_configured: true },
    config_error: null, model_endpoint: { state: 'ok', http_status: 200, model_listed: true, reason: null },
  };
  it('passes every row when the setup is complete', () => {
    expect(claudeRows(ok).map(r => r.tone)).toEqual(['ok', 'ok', 'ok', 'ok']);
  });
  it('names a missing key, an invalid gateway and an unauthorized endpoint', () => {
    const rows = claudeRows({ ...ok, ready: false, config_error: 'INVALID_CLAUDE_BASE_URL', model: { ...ok.model!, credential_configured: false }, model_endpoint: { state: 'unauthorized', http_status: 401, model_listed: null, reason: null } });
    expect(rows.map(r => r.tone)).toEqual(['ok', 'fail', 'fail', 'fail']);
    expect(rows[1].detail).toContain('不带结尾的 /v1');
    expect(rows[3].detail).toContain('HTTP 401');
  });
  it('flags a missing CLI', () => {
    expect(claudeRows({ ...ok, installed: false })[0]).toMatchObject({ tone: 'fail' });
  });
});
