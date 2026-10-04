// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest';
import { render, screen, fireEvent, cleanup, act } from '@testing-library/react';
import { AuthorizationCard } from './AuthorizationCard';
import { OAuthConfig } from './OAuthConfig';
const { request } = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock('./api', async original => ({ ...await original<typeof import('./api')>(), api: request }));
afterEach(() => {cleanup();request.mockReset();vi.useRealTimers();});
it('never loads private material for group peers or supervisors', () => {
 render(<AuthorizationCard provider="feishu" state="pending" canOpen={false}/>);
 expect(screen.queryByText('查看本人授权状态')).toBeNull();expect(request).not.toHaveBeenCalled();
});
it('loads only actor personal API on explicit open', async () => {
 request.mockResolvedValue([{provider:'feishu',state:'pending',message:'本人授权',authorization_url:'https://accounts.feishu.cn/device',user_code:'PRIVATE'}]);
 render(<AuthorizationCard provider="feishu" state="pending" canOpen/>);
 expect(screen.queryByText('PRIVATE')).toBeNull();fireEvent.click(screen.getByText('查看本人授权状态'));
 await screen.findByText('PRIVATE');expect(request.mock.calls[0][0]).toBe('/platform-connections');
});
it.each(['https://evil.invalid', 'https://user@accounts.feishu.cn/', 'https://accounts.feishu.cn:123/', 'https://accounts.feishu.cn/device#private', 'http://accounts.feishu.cn/'])('never renders wrong issuer link %s',async(url)=>{
 request.mockResolvedValue([{provider:'feishu',state:'pending',message:'本人授权',authorization_url:url,user_code:'PRIVATE'}]);
 render(<AuthorizationCard provider="feishu" state="pending" canOpen/>);fireEvent.click(screen.getByText('查看本人授权状态'));
 await screen.findByText('本人授权');expect(screen.queryByText('打开官方授权页面')).toBeNull();expect(screen.queryByText('PRIVATE')).toBeNull();
});
it('requires explicit local cancellation confirmation and removes device materials', async () => {
 request.mockImplementation(async(p:string) => p.endsWith('/cancel') ? {provider:'feishu',state:'disconnected',message:'已取消'} : [{provider:'feishu',state:'pending',message:'等待',authorization_url:'https://accounts.feishu.cn/device',user_code:'PRIVATE'}]);
 render(<AuthorizationCard provider="feishu" state="pending" canOpen/>);
 fireEvent.click(screen.getByText('查看本人授权状态')); await screen.findByText('PRIVATE');
 fireEvent.click(screen.getByText('取消授权'));
 expect(screen.getByText(/不会撤销平台授权/)).toBeTruthy();
 expect(request.mock.calls.some(([p])=>p.endsWith('/cancel'))).toBe(false);
 fireEvent.click(screen.getByText('确认取消授权')); await screen.findByText('已取消');
 expect(screen.queryByText('PRIVATE')).toBeNull();
 expect(request.mock.calls.some(([p,o])=>p==='/platform-connections/feishu/cancel'&&o.method==='POST')).toBe(true);
});
it('allows only actor local disconnect and keeps failed operation retryable', async () => {
 request.mockResolvedValue([{provider:'dingtalk',state:'connected',message:'已连接'}]);
 render(<AuthorizationCard provider="dingtalk" state="pending" canOpen/>);
 fireEvent.click(screen.getByText('查看本人授权状态')); await screen.findByText('断开 Hub 连接');
 fireEvent.click(screen.getByText('断开 Hub 连接'));
 request.mockRejectedValueOnce(new Error('断开失败'));
 fireEvent.click(screen.getByText('确认断开')); await screen.findByText('断开失败');
 request.mockResolvedValueOnce({provider:'dingtalk',state:'disconnected',message:'已断开'});
 fireEvent.click(screen.getByText('确认断开')); await screen.findByText('已断开');
 expect(request.mock.calls.filter(([p])=>p==='/platform-connections/dingtalk/disconnect')).toHaveLength(2);
});
it('drops private material and ignores late response when owner access is revoked', async () => {
 let finish!:(v:unknown)=>void; request.mockImplementation(()=>new Promise(resolve=>{finish=resolve;}));
 const view=render(<AuthorizationCard provider="feishu" state="pending" canOpen/>);
 fireEvent.click(screen.getByText('查看本人授权状态'));
 view.rerender(<AuthorizationCard provider="feishu" state="pending" canOpen={false}/>);
 await act(async()=>finish([{provider:'feishu',state:'pending',message:'等待',authorization_url:'https://accounts.feishu.cn/device',user_code:'PRIVATE'}]));
 expect(screen.queryByText('PRIVATE')).toBeNull();expect(screen.queryByText('取消授权')).toBeNull();
});
it('refreshes an opened pending card without issuing new authorization', async () => {
 vi.useFakeTimers();
 request.mockResolvedValueOnce([{provider:'feishu',state:'pending',message:'等待',authorization_url:'https://accounts.feishu.cn/device',user_code:'PRIVATE'}])
  .mockResolvedValueOnce([{provider:'feishu',state:'connected',message:'完成'}]);
 render(<AuthorizationCard provider="feishu" state="pending" canOpen/>);
 await act(async()=>fireEvent.click(screen.getByText('查看本人授权状态')));
 expect(screen.getByText('PRIVATE')).toBeTruthy();
 await act(async()=>vi.advanceTimersByTimeAsync(5000));
 expect(screen.getByText('断开 Hub 连接')).toBeTruthy();expect(screen.queryByText('PRIVATE')).toBeNull();
 expect(request.mock.calls.every(([p])=>p==='/platform-connections')).toBe(true);
});
it('starts and refreshes using existing personal endpoints', async () => {
 request.mockResolvedValueOnce([{provider:'feishu',state:'disconnected',message:'未连接'}])
 .mockResolvedValueOnce({provider:'feishu',state:'starting',message:'正在发起'})
 .mockResolvedValueOnce({provider:'feishu',state:'pending',message:'等待',authorization_url:'https://accounts.feishu.cn/device',user_code:'PRIVATE'});
 render(<AuthorizationCard provider="feishu" state="unknown" canOpen/>);
 fireEvent.click(screen.getByText('查看本人授权状态')); await screen.findByText('发起授权');
 fireEvent.click(screen.getByText('发起授权')); await screen.findByText('正在发起');
 fireEvent.click(screen.getByText('刷新状态')); await screen.findByText('PRIVATE');
 expect(request.mock.calls.some(([p])=>p==='/platform-connections/feishu/start')).toBe(true);
 expect(request.mock.calls.some(([p])=>p==='/platform-connections/feishu/refresh')).toBe(true);
});
it('shows honest setup required and configuration fetch error',async()=>{
 request.mockResolvedValue({provider:'feishu',revision:0,configured:false,message:'独立配置',fields:{CLIENT_ID:'',SCOPES:'docx:document:readonly'},secrets_set:{CLIENT_SECRET:false}});
 render(<OAuthConfig provider="feishu"/>);await screen.findByText('需要管理员配置：缺少个人 OAuth 应用配置');cleanup();
 request.mockRejectedValue(new Error('配置读取失败'));render(<OAuthConfig provider="feishu"/>);await screen.findByRole('alert');expect(screen.getByText('配置读取失败')).toBeTruthy();
});
