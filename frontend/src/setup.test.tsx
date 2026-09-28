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
it('registers first admin, verifies passwords and retains route for login',async()=>{
 window.history.replaceState(null,'','#/resources');request.mockImplementation(async(p:string)=>p==='/setup/status'?false:{ok:true});render(<App/>);
 await screen.findByRole('heading',{name:'初始化管理员'});
 const fill=(name:string,value:string)=>fireEvent.change(document.querySelector(`input[name="${name}"]`)!,{target:{value}});
 fill('name','Owner');fill('email','owner@example.invalid');fill('password','test-password-123');fill('confirmPassword','wrong-password-123');
 fireEvent.click(screen.getByRole('button',{name:'创建管理员'}));await screen.findByText('两次输入的密码不一致');
 expect(request.mock.calls.filter(([p])=>p==='/setup/bootstrap')).toHaveLength(0);
 fill('confirmPassword','test-password-123');fireEvent.click(screen.getByRole('button',{name:'创建管理员'}));
 await screen.findByText('管理员创建成功，请使用刚刚设置的邮箱和密码登录。');expect(window.location.hash).toBe('#/resources');
 expect(request).toHaveBeenCalledWith('/setup/bootstrap',{name:'Owner',email:'owner@example.invalid',password:'test-password-123'});
});
it('does not expose registration while status is pending',async()=>{
 request.mockImplementation(()=>new Promise(()=>{}));render(<App/>);
 await waitFor(()=>expect(request).toHaveBeenCalled());expect(screen.queryByRole('button',{name:'创建管理员'})).toBeNull();
});
