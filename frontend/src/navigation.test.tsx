// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { Chat } from './Chat';
import { App } from './main';
import { readRoute, useRoute } from './route';
import type { User } from './api';
const { request } = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock('./api', async original => ({ ...await original<typeof import('./api')>(), api: request, post: (path: string, body: unknown) => request(path, { method: 'POST', body }) }));
const user: User = { id:'owner', role:'member', org_id:'org', team_id:'team', active:true, name:'Owner', email:'owner@test.local' };
let rows: { id: string; title: string; owner_id: string; can_delete: boolean }[];
beforeEach(() => {
  rows = [{ id:'a', title:'Alpha', owner_id:'owner', can_delete:true }, { id:'b', title:'Beta', owner_id:'owner', can_delete:false }];
  window.history.replaceState(null,'','#/chat/a');
  Element.prototype.scrollIntoView = vi.fn(); HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open',''); };
  request.mockImplementation(async (path: string, options?: RequestInit) => {
    if (path==='/auth/me') return user;
    if (path==='/conversations') return rows;
    if (options?.method==='DELETE') { rows=rows.filter(c=>path!==`/conversations/${c.id}`); return {ok:true}; }
    if (path.endsWith('/state')) return { messages:[], active_run:null, latest_run:null };
    if (path.endsWith('/capabilities')) return {skills:[],mcps:[]};
    return [];
  });
});
afterEach(() => { cleanup(); request.mockReset(); window.history.replaceState(null,'','#/overview'); });
function RoutedChat() { const {route,navigate}=useRoute(); return <Chat user={user} conversationId={route.conversationId} onSelect={(id,replace)=>navigate({page:'chat',conversationId:id},replace)}/>; }
it('restores the page and selected conversation after reload', async () => {
  window.history.replaceState(null,'','#/chat/b'); const view=render(<App/>);
  await waitFor(()=>expect(screen.getByRole('heading',{name:'Beta'})).toBeTruthy());
  view.unmount(); render(<App/>);
  await waitFor(()=>expect(screen.getByRole('heading',{name:'Beta'})).toBeTruthy());
  expect(window.location.hash).toBe('#/chat/b');
});
it('preserves management page and rejects member audit route', async () => {
  window.history.replaceState(null,'','#/resources'); const view=render(<App/>);
  await waitFor(()=>expect(screen.getByRole('heading',{name:'能力资源'})).toBeTruthy());
  await act(async()=>{ window.location.hash='#/audit'; window.dispatchEvent(new HashChangeEvent('hashchange')); });
  await waitFor(()=>expect(window.location.hash).toBe('#/overview')); view.unmount();
});
it('restores target after login but reloads current user permissions', async () => {
  let signedIn=false; const impl=request.getMockImplementation()!;
  request.mockImplementation(async (path:string, options?:RequestInit)=>{
    if(path==='/auth/me'&&!signedIn) { const {ApiError}=await import('./api'); throw new ApiError(401,'Login required'); }
    if(path==='/auth/login') {signedIn=true; return user;}
    return impl(path,options);
  });
  window.history.replaceState(null,'','#/chat/b'); render(<App/>);
  await screen.findByPlaceholderText('name@company.com');
  fireEvent.change(screen.getByPlaceholderText('name@company.com'),{target:{value:'owner@test.local'}});
  fireEvent.change(screen.getByPlaceholderText('请输入密码'),{target:{value:'password'}});
  fireEvent.click(screen.getByRole('button',{name:'登录工作空间'}));
  await waitFor(()=>expect(screen.getByRole('heading',{name:'Beta'})).toBeTruthy());
  fireEvent.click(screen.getByRole('button',{name:'退出登录'}));
  await waitFor(()=>expect(window.location.hash).toBe('#/overview'));
});
it('clears inaccessible or deleted conversation selection', async () => {
  window.history.replaceState(null,'','#/chat/missing'); render(<RoutedChat/>);
  await waitFor(()=>expect(window.location.hash).toBe('#/chat'));
  expect(screen.getByText('选择或创建会话')).toBeTruthy();
  expect(request.mock.calls.some(([p])=>p.includes('/missing/'))).toBe(false);
});
it('delete cancel does not select the target or send DELETE', async () => {
  window.history.replaceState(null,'','#/chat/b'); render(<RoutedChat/>);
  fireEvent.click(await screen.findByRole('button',{name:'移除会话 Alpha'}));
  expect(window.location.hash).toBe('#/chat/b');
  expect(screen.getByText(/保留历史消息/)).toBeTruthy();
  fireEvent.click(screen.getByRole('button',{name:'取消'}));
  expect(request.mock.calls.some(([,o])=>o?.method==='DELETE')).toBe(false);
  expect(screen.queryByRole('button',{name:'移除会话 Beta'})).toBeNull();
});
it('failed delete retains conversation and permits retry; confirmation removes it', async () => {
  const impl=request.getMockImplementation()!; let fail=true;
  request.mockImplementation(async(p:string,o?:RequestInit)=>{if(o?.method==='DELETE'&&fail)throw new Error('正在运行'); return impl(p,o);});
  render(<RoutedChat/>); fireEvent.click(await screen.findByRole('button',{name:'移除会话 Alpha'}));
  fireEvent.click(screen.getByRole('button',{name:'确认移除'}));
  await screen.findByText('正在运行'); expect(window.location.hash).toBe('#/chat/a');
  expect(screen.getByRole('heading',{name:'Alpha'})).toBeTruthy(); fail=false;
  fireEvent.click(screen.getByRole('button',{name:'确认移除'}));
  await waitFor(()=>expect(screen.queryByRole('button',{name:'移除会话 Alpha'})).toBeNull());
  expect(window.location.hash).toBe('#/chat'); expect(screen.getByText('选择或创建会话')).toBeTruthy();
});
it('last conversation removal renders empty state', async () => {
  rows=rows.slice(0,1); render(<RoutedChat/>);
  fireEvent.click(await screen.findByRole('button',{name:'移除会话 Alpha'})); fireEvent.click(screen.getByRole('button',{name:'确认移除'}));
  await screen.findByText('暂无会话'); expect(window.location.hash).toBe('#/chat');
});
it('browser back and forward restore selected conversation', async () => {
  render(<RoutedChat/>); fireEvent.click(await screen.findByRole('button',{name:/Beta 我的会话/}));
  expect(window.location.hash).toBe('#/chat/b');
  await act(async()=>{window.history.back();});
  await waitFor(()=>expect(screen.getByRole('heading',{name:'Alpha'})).toBeTruthy());
  await act(async()=>{window.history.forward();});
  await waitFor(()=>expect(screen.getByRole('heading',{name:'Beta'})).toBeTruthy());
});
it('new conversation synchronizes URL and selection', async () => {
  const impl=request.getMockImplementation()!;
  request.mockImplementation(async(p:string,o?:RequestInit)=>{
    if(p==='/conversations'&&o?.method==='POST') { const created={id:'new',title:'New session',owner_id:'owner',can_delete:true}; rows=[created,...rows]; return created; }
    return impl(p,o);
  });
  render(<RoutedChat/>); fireEvent.click(await screen.findByRole('button',{name:'创建会话'}));
  fireEvent.change(screen.getByPlaceholderText('例如：本周运营分析'),{target:{value:'New session'}});
  fireEvent.click(screen.getByRole('button',{name:'确认创建'}));
  await waitFor(()=>expect(window.location.hash).toBe('#/chat/new'));
  await waitFor(()=>expect(screen.getByRole('heading',{name:'New session'})).toBeTruthy());
});
it('pending deletion prevents duplicate submission and closing', async () => {
  const impl=request.getMockImplementation()!; let finish!:()=>void;
  request.mockImplementation((p:string,o?:RequestInit)=>o?.method==='DELETE'?new Promise(resolve=>{finish=()=>{rows=[];resolve({ok:true});};}):impl(p,o));
  render(<RoutedChat/>); fireEvent.click(await screen.findByRole('button',{name:'移除会话 Alpha'}));
  fireEvent.click(screen.getByRole('button',{name:'确认移除'}));
  expect((screen.getByRole('button',{name:'正在移除…'}) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole('button',{name:'关闭弹窗'}));
  expect(screen.getByText('从工作台移除会话')).toBeTruthy();
  expect(request.mock.calls.filter(([,o])=>o?.method==='DELETE')).toHaveLength(1);
  await act(async()=>finish());
});
it.each(['#/unknown','#/users/abc','#/chat/%E0%A4%A','garbage'])('safely normalizes invalid route %s', hash=>{
  expect(readRoute(hash)).toEqual({page:'overview',conversationId:null});
});
