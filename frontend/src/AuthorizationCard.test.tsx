// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import { AuthorizationCard } from './AuthorizationCard';
import { OAuthConfig } from './OAuthConfig';
const { request } = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock('./api', async original => ({ ...await original<typeof import('./api')>(), api: request }));
afterEach(() => {cleanup();request.mockReset();});
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
it('never renders wrong issuer link',async()=>{
 request.mockResolvedValue([{provider:'feishu',state:'pending',message:'本人授权',authorization_url:'https://evil.invalid',user_code:'PRIVATE'}]);
 render(<AuthorizationCard provider="feishu" state="pending" canOpen/>);fireEvent.click(screen.getByText('查看本人授权状态'));
 await screen.findByText('本人授权');expect(screen.queryByText('打开官方授权页面')).toBeNull();expect(screen.queryByText('PRIVATE')).toBeNull();
});
it('shows honest setup required and configuration fetch error',async()=>{
 request.mockResolvedValue({provider:'feishu',revision:0,configured:false,message:'独立配置',fields:{CLIENT_ID:'',SCOPES:'docx:document:readonly'},secrets_set:{CLIENT_SECRET:false}});
 render(<OAuthConfig provider="feishu"/>);await screen.findByText('setup_required：缺少独立个人 OAuth 应用配置');cleanup();
 request.mockRejectedValue(new Error('配置读取失败'));render(<OAuthConfig provider="feishu"/>);await screen.findByRole('alert');expect(screen.getByText('配置读取失败')).toBeTruthy();
});
