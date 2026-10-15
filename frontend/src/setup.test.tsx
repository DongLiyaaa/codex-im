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
it('offers forgot password only when the server allows it, and resets after both entries match',async()=>{
 let enabled=false;
 request.mockImplementation(async(p:string)=>p==='/setup/status'?true:p==='/auth/me'?Promise.reject(new (await import('./api')).ApiError(401,'x')):p==='/auth/password-reset'?{enabled}:{ok:true});
 render(<App/>);await screen.findByRole('heading',{name:'登录你的工作空间'});
 await waitFor(()=>expect(request).toHaveBeenCalledWith('/auth/password-reset'));expect(screen.queryByRole('button',{name:'忘记密码？'})).toBeNull();
 cleanup();enabled=true;render(<App/>);
 fireEvent.click(await screen.findByRole('button',{name:'忘记密码？'}));
 await screen.findByRole('heading',{name:'重置密码'});
 const fill=(name:string,value:string)=>fireEvent.change(document.querySelector(`input[name="${name}"]`)!,{target:{value}});
 fill('email','owner@example.invalid');fill('password','new-password-123');fill('confirmPassword','new-password-999');
 fireEvent.click(screen.getByRole('button',{name:'重置密码'}));await screen.findByText('两次输入的密码不一致');
 expect(request.mock.calls.some(([p,b])=>p==='/auth/password-reset'&&b)).toBe(false);
 fill('confirmPassword','new-password-123');fireEvent.click(screen.getByRole('button',{name:'重置密码'}));
 await screen.findByText('密码已重置，请使用新密码登录。');
 expect(request).toHaveBeenCalledWith('/auth/password-reset',{email:'owner@example.invalid',password:'new-password-123'});
 expect(screen.getByRole('heading',{name:'登录你的工作空间'})).toBeTruthy();
});
