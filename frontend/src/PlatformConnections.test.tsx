// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest';
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react';
import { PlatformConnections } from './PlatformConnections';
import { readRoute, canAccessPage } from './route';
const { request } = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock('./api', async original => ({ ...await original<typeof import('./api')>(), api: request }));
afterEach(() => { cleanup(); request.mockReset(); });
it('preserves personal route and permits active members', () => {
 expect(readRoute('#/connections').page).toBe('connections');
 expect(canAccessPage({active:true,role:'member'} as never,'connections')).toBe(true);
});
it('starts then shows only returned personal device material and cancels', async () => {
 let pending = false;
 request.mockImplementation(async (path:string) => { if(path.endsWith('/start')) pending=true; if(path.endsWith('/cancel')) pending=false; return [{provider:'feishu',state:pending?'pending':'disconnected',scope:'read',message:'重发任务',authorization_url:pending?'https://accounts.feishu.cn/device':undefined,user_code:pending?'TEST-CODE':undefined}]; });
 render(<PlatformConnections/>); fireEvent.click(await screen.findByText('发起授权'));
 await screen.findByText('TEST-CODE'); expect(screen.getByText('打开官方授权页面').getAttribute('rel')).toBe('noopener noreferrer');
 fireEvent.click(screen.getByText('取消')); await waitFor(() => expect(screen.queryByText('TEST-CODE')).toBeNull());
 expect(request.mock.calls.some(([p])=>p==='/platform-connections/feishu/cancel')).toBe(true);
});
it('never renders untrusted authorization URL', async () => {
 request.mockResolvedValue([{provider:'dingtalk',state:'pending',scope:'openid corpid',message:'等待',authorization_url:'https://evil.test/',user_code:'TEST'}]);
 render(<PlatformConnections/>); await screen.findByText('TEST'); expect(screen.queryByText('打开官方授权页面')).toBeNull();
});
