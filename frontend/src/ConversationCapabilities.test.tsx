// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ConversationCapabilities } from './ConversationCapabilities';
const { request } = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock('./api', async original => ({ ...await original<typeof import('./api')>(), api: request, post: (p: string, b: unknown) => request(p, b) }));
const user = { id: 'admin', name: 'Admin', email: '', role: 'super_admin' as const, active: true, org_id: null, team_id: null };
const conversation = { id: 'c', title: '测试会话', owner_id: 'owner', group_id: 'g', created_at: '' };
beforeEach(() => { HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); }; request.mockImplementation(async p => p === '/resources' ? [{ id: 'r', name: '测试技能', kind: 'skill', enabled: true, can_manage: true }, { id: 'm', name: '测试工具', kind: 'mcp', enabled: true, can_manage: true }] : []); });
afterEach(() => { cleanup(); request.mockReset(); });
it('requires explicit group selection, writes one side and refreshes effective capabilities', async () => {
 const changed = vi.fn(); render(<ConversationCapabilities conversation={conversation} user={user} kind="skill" close={vi.fn()} changed={changed}/>);
 await screen.findByRole('option', { name: '测试技能' }); expect(screen.queryByRole('option', { name: '测试工具' })).toBeNull();
 fireEvent.change(screen.getByLabelText('授权对象'), { target: { value: 'group' } });
 fireEvent.change(screen.getByLabelText('选择 Skill'), { target: { value: 'r' } }); fireEvent.click(screen.getByText('添加授权'));
 await waitFor(() => expect(changed).toHaveBeenCalledOnce());
 expect(request).toHaveBeenCalledWith('/bindings', { subject_type: 'group', subject_id: 'g', resource_id: 'r' });
});
it('retains the selected resource on failure and protects unsaved close', async () => {
 render(<ConversationCapabilities conversation={{...conversation, group_id: null}} user={user} kind="skill" close={vi.fn()} changed={vi.fn()}/>);
 await screen.findByRole('option', { name: '测试技能' });
 fireEvent.change(screen.getByLabelText('选择 Skill'), { target: { value: 'r' } });
 request.mockRejectedValue(new Error('无权授权')); fireEvent.click(screen.getByText('添加授权'));
 await screen.findByText('无权授权'); expect((screen.getByLabelText('选择 Skill') as HTMLSelectElement).value).toBe('r');
 fireEvent.click(screen.getByLabelText('关闭弹窗')); expect(screen.getByText('有未保存的修改，是否放弃？')).toBeTruthy();
});
