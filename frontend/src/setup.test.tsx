// @vitest-environment jsdom
import {afterEach, expect, it, vi} from 'vitest';
import {cleanup, fireEvent, render, screen, waitFor} from '@testing-library/react';
import {App} from './main';
const {request}=vi.hoisted(()=>({request:vi.fn()}));
vi.mock('./api',async original=>({...await original<typeof import('./api')>(),api:request,post:(path:string,body:unknown)=>request(path,body)}));
afterEach(()=>{cleanup();request.mockReset();window.history.replaceState(null,'','#/overview');});
it('keeps setup closed on network failure and allows retry',async()=>{
 request.mockRejectedValue(new Error('offline'));render(<App/>);
 await screen.findByText('offline');expect(screen.queryByRole('button',{name:'创建管理员'})).toBeNull();
 request.mockResolvedValue(false);fireEvent.click(screen.getByRole('button',{name:'重新检查服务连接'}));
 await screen.findByRole('heading',{name:'初始化管理员'});
});
it('registers the first admin, verifies passwords and enters onboarding without a second login',async()=>{
 window.history.replaceState(null,'','#/resources');
 const created={id:'root',name:'Owner',email:'owner@example.invalid',role:'super_admin',active:true,org_id:null,team_id:null,onboarding:{status:'pending',step:'model',auto_start:true}};
 request.mockImplementation(async(p:string,o?:RequestInit)=>p==='/setup/status'?false:p==='/setup/bootstrap'?created:p==='/auth/codex'?{mode:'api',state:'disconnected',message:'系统 API 已配置'}:p==='/auth/me/onboarding'?{...created,onboarding:{...JSON.parse(String(o?.body)),auto_start:false}}:{});
 render(<App/>);
 await screen.findByRole('heading',{name:'初始化管理员'});
 const fill=(name:string,value:string)=>fireEvent.change(document.querySelector(`input[name="${name}"]`)!,{target:{value}});
 fill('name','Owner');fill('email','owner@example.invalid');fill('password','test-password-123');fill('confirmPassword','wrong-password-123');
 fireEvent.click(screen.getByRole('button',{name:'创建管理员并进入'}));await screen.findByText('两次输入的密码不一致');
 expect(request.mock.calls.filter(([p])=>p==='/setup/bootstrap')).toHaveLength(0);
 fill('confirmPassword','test-password-123');fireEvent.click(screen.getByRole('button',{name:'创建管理员并进入'}));
 await screen.findByRole('heading',{name:'快速初始化'});expect(window.location.hash).toBe('#/onboarding');
 expect(screen.queryByRole('heading',{name:'登录你的工作空间'})).toBeNull();
 expect(request).toHaveBeenCalledWith('/setup/bootstrap',{name:'Owner',email:'owner@example.invalid',password:'test-password-123'});
 expect(request.mock.calls.some(([p])=>p==='/auth/login')).toBe(false);
});
it('does not expose registration while status is pending',async()=>{
 request.mockImplementation(()=>new Promise(()=>{}));render(<App/>);
 await waitFor(()=>expect(request).toHaveBeenCalled());expect(screen.queryByRole('button',{name:'创建管理员'})).toBeNull();
});
it('does not offer or query anonymous password recovery',async()=>{
 request.mockImplementation(async(p:string)=>p==='/setup/status'?true:p==='/auth/me'?Promise.reject(new (await import('./api')).ApiError(401,'x')):{enabled:true});
 render(<App/>);await screen.findByRole('heading',{name:'登录你的工作空间'});
 expect(screen.queryByRole('button',{name:'忘记密码？'})).toBeNull();
 expect(request.mock.calls.some(([p])=>p==='/auth/password-reset')).toBe(false);
});
it('shows the author links on the setup and login pages',async()=>{
 request.mockImplementation(async(p:string)=>p==='/setup/status'?false:{ok:true});render(<App/>);
 await screen.findByRole('heading',{name:'初始化管理员'});
 expect(screen.getByRole('link',{name:'GitHub：DongLiyaaa'}).getAttribute('href')).toBe('https://github.com/DongLiyaaa');
 cleanup();
 request.mockImplementation(async(p:string)=>p==='/setup/status'?true:p==='/auth/me'?Promise.reject(new (await import('./api')).ApiError(401,'x')):p==='/auth/password-reset'?{enabled:false}:{ok:true});render(<App/>);
 await screen.findByRole('heading',{name:'登录你的工作空间'});
 expect(screen.getByRole('link',{name:'GitHub：DongLiyaaa'})).toBeTruthy();
 expect(screen.getByText('微信公众号：冬离鸭冬离')).toBeTruthy();expect(screen.getByText('抖音号：冬离鸭冬离')).toBeTruthy();
});
it.each([false,true])('offers the page help UI on public pages (initialized=%s)',async initialized=>{
 request.mockImplementation(async(p:string)=>p==='/setup/status'?initialized:p==='/auth/me'?Promise.reject(new (await import('./api')).ApiError(401,'x')):{});
 render(<App/>);
 fireEvent.click(await screen.findByRole('button',{name:'本页说明'}));
 expect(screen.getByRole('dialog',{name:`${initialized?'登录工作空间':'初始化管理员'} · 使用说明`})).toBeTruthy();
 expect(screen.queryByText(/UI 预览|样式确认后补充/)).toBeNull();
 expect(screen.getByText(initialized ? /输入完整登录邮箱和密码/ : /填写显示姓名和有效邮箱/)).toBeTruthy();
});
