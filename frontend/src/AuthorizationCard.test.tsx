// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest';
import { render, screen, fireEvent, cleanup, act, within } from '@testing-library/react';
import { AuthorizationCard } from './AuthorizationCard';
import { OAuthConfig } from './OAuthConfig';
import { Toaster } from 'sonner';
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
 request.mockRejectedValue(new Error('配置读取失败'));render(<OAuthConfig provider="feishu"/>);await screen.findAllByRole('alert');expect(screen.getAllByText('配置读取失败').length).toBeGreaterThan(0);
});
it('keeps the personal authorization people list in Hub and shows who each person is', async () => {
 const people = [
  {id:'u1',name:'冬离',role:'member',active:true,email:null,organization:'公司',department:'运营',account:'326850072533581332',authorization:'identity_unverified'},
  {id:'u2',name:'蓝海明',role:'member',active:true,email:null,organization:'公司',department:'运营',account:null,authorization:null},
  {id:'u3',name:'管理员',role:'super_admin',active:true,email:'root@example.com',organization:null,department:null,account:null,authorization:null},
  {id:'u4',name:'外部成员',role:'member',active:true,email:null,organization:'11',department:null,account:'9001',authorization:null}];
 const responses: Record<string, unknown> = {
  '/integrations/oauth/dingtalk': {provider:'dingtalk',revision:1,configured:true,message:'已配置',fields:{CLIENT_ID:'cli',SCOPES:''},secrets_set:{CLIENT_SECRET:true}},
  '/integrations/oauth/dingtalk/access': {provider:'dingtalk',revision:0,user_scope:'all',user_ids:[],people}};
 request.mockImplementation(async (path: string, init?: RequestInit) => init?.method === 'PUT' ? {provider:'dingtalk',revision:1,user_scope:'specified',user_ids:['u1'],people} : responses[path]);
 HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
 render(<><Toaster/><OAuthConfig provider="dingtalk"/></>);
 // Everyone mode lists who can actually authorize now: the people with a bound DingTalk account.
 await screen.findByText('全员可用：已绑定本人钉钉账号的成员都可以发起本人授权，当前 2 人。');
 const table = screen.getByRole('table');
 expect(table.textContent).toContain('冬离');expect(table.textContent).toContain('公司 / 运营');expect(table.textContent).toContain('326850072533581332');expect(table.textContent).toContain('无法核验本人身份');
 expect(table.textContent).toContain('11 / 组织级');expect(table.textContent).not.toContain('蓝海明');
 expect(screen.getByText(/可用人员设置请选「全员可用」/)).toBeTruthy();
 fireEvent.click(screen.getByText('设置可用人员'));
 fireEvent.click(screen.getByLabelText('指定人员范围可用'));
 fireEvent.click(screen.getByText('保存可用人员')); await screen.findByText('请至少选择一位成员，或改为全员可用。');
 const dialog = within(screen.getByRole('dialog'));
 expect(dialog.getAllByText('未绑定钉钉账号，绑定后才能发起本人授权 · 本人授权：未发起')).toHaveLength(2);
 expect(dialog.getByText('全局（不属于组织） · root@example.com')).toBeTruthy();
 fireEvent.change(dialog.getByLabelText('按组织筛选'), {target:{value:'11'}});
 expect(dialog.queryByText('冬离')).toBeNull();
 fireEvent.change(dialog.getByLabelText('按组织筛选'), {target:{value:''}});
 fireEvent.click(dialog.getByLabelText('只看已绑定钉钉账号'));
 expect(dialog.queryByText('蓝海明')).toBeNull();
 fireEvent.change(dialog.getByLabelText('搜索成员'), {target:{value:'运营'}});
 fireEvent.click(dialog.getByText('选中筛选结果（1）'));
 expect(dialog.getByLabelText('选择 冬离（公司 / 运营）')).toHaveProperty('checked', true);
 responses['/integrations/oauth/dingtalk/access'] = {provider:'dingtalk',revision:1,user_scope:'specified',user_ids:['u1'],people};
 fireEvent.click(screen.getByText('保存可用人员'));
 await screen.findByText('指定人员范围可用：以下 1 人可以发起本人钉钉授权。');
 expect(screen.getByRole('table').textContent).toContain('公司 / 运营');
 const put = request.mock.calls.find(([, init]) => init?.method === 'PUT')!;
 expect(put[0]).toBe('/integrations/oauth/dingtalk/access');
 expect(JSON.parse(put[1].body)).toEqual({revision:0,user_scope:'specified',user_ids:['u1']});
});
