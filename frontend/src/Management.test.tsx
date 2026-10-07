// @vitest-environment jsdom
import { beforeEach, afterEach, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, cleanup, waitFor, act } from '@testing-library/react';
import { Toaster } from 'sonner';
import { AuditPanel } from './AuditPanel';
import { Management } from './Management';
import { DiscoveredGroups } from './DiscoveredGroups';
import { DirectoryPanel } from './DirectoryPanel';
import { readRoute, canAccessPage } from './route';
import { Modal } from './ui';
const { request } = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock('./api', async original => ({ ...await original<typeof import('./api')>(), api: request, post: (path: string, body: unknown) => request(path, body) }));
beforeEach(() => { sessionStorage.clear(); HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open',''); }; request.mockImplementation(async (path: string) => { const u = new URL(path,'http://test'); const size=Number(u.searchParams.get('page_size')); const page=Math.min(Number(u.searchParams.get('page')),Math.ceil(205/size)); return {items:[],total:205,page,page_size:size,pages:Math.ceil(205/size)}; }); });
afterEach(() => { cleanup(); request.mockReset(); });
it('uses server pages, Enter jump, page-size reset and persisted selection', async () => {
  const view=render(<AuditPanel userId="root"/>); await screen.findByText('共 205 条 · 第 1 / 5 页');
  fireEvent.click(screen.getByText('下一页')); await screen.findByText('共 205 条 · 第 2 / 5 页');
  fireEvent.change(screen.getByLabelText('跳转页码'),{target:{value:'4'}}); fireEvent.submit(screen.getByLabelText('跳转页码').closest('form')!);
  await screen.findByText('共 205 条 · 第 4 / 5 页');
  fireEvent.change(screen.getByLabelText('每页条数'),{target:{value:'100'}}); await screen.findByText('共 205 条 · 第 1 / 3 页');
  fireEvent.click(screen.getByText('下一页')); await screen.findByText('共 205 条 · 第 2 / 3 页'); view.unmount(); render(<AuditPanel userId="root"/>);
  await screen.findByText('共 205 条 · 第 2 / 3 页'); expect(request.mock.calls.some(([p])=>p==='/audit')).toBe(false);
});
it('recovers out of range after refresh and retries errors',async()=>{
  render(<AuditPanel userId="root"/>); await screen.findByText('共 205 条 · 第 1 / 5 页'); fireEvent.click(screen.getByText('下一页')); await screen.findByText('共 205 条 · 第 2 / 5 页');
  request.mockResolvedValue({items:[],total:0,page:1,page_size:50,pages:1}); fireEvent.click(screen.getByText('刷新')); await screen.findByText('共 0 条 · 第 1 / 1 页');
  request.mockRejectedValue(new Error('暂时失败')); fireEvent.click(screen.getByText('刷新')); await screen.findByText('暂时失败');
  request.mockResolvedValue({items:[],total:0,page:1,page_size:50,pages:1}); fireEvent.click(screen.getByText('重试')); await screen.findByText('暂无审计记录');
});
it('aborts obsolete requests and ignores their results',async()=>{
  let finish!: (v:unknown)=>void; let signal!:AbortSignal;
  request.mockImplementationOnce((_p:string,o:RequestInit)=>{signal=o.signal!;return new Promise(resolve=>{finish=resolve;});});
  render(<AuditPanel userId="root"/>); fireEvent.change(screen.getByLabelText('每页条数'),{target:{value:'100'}}); await screen.findByText('共 205 条 · 第 1 / 3 页'); expect(signal.aborted).toBe(true);
  await act(async()=>finish({items:[],total:999,page:1,page_size:50,pages:20})); expect(screen.queryByText(/共 999 条/)).toBeNull();
});
it('prefills discovered group and only submits explicitly selected bound identities',async()=>{
  const found={id:'d',provider:'feishu',external_id:'oc_real',name:null,first_seen:'2026-09-27',last_seen:'2026-09-27',members:[{id:'u',name:'已绑定员工',org_id:'org',team_id:'team'}]};
  request.mockImplementation(async(path:string)=>path==='/directory'?{organizations:[{id:'org',name:'组织'}],departments:[{id:'team',org_id:'org',name:'部门'}]}:[found]); render(<DiscoveredGroups groups={[]} reloadGroups={vi.fn()}/>);
  fireEvent.click(await screen.findByText('绑定 / 一键带入')); expect((screen.getByLabelText('群名称') as HTMLInputElement).value).toBe('oc_real');
  expect((screen.getByLabelText('组织') as HTMLSelectElement).value).toBe('org');
  expect((screen.getByLabelText('已绑定员工') as HTMLInputElement).checked).toBe(false); fireEvent.click(screen.getByLabelText('已绑定员工')); fireEvent.click(screen.getByLabelText(/我确认群映射/));
  fireEvent.click(screen.getByText('确认绑定')); await waitFor(()=>expect(request).toHaveBeenCalledWith('/im/discoveries/groups/d/bind',{name:'oc_real',org_id:'org',team_id:'team',member_ids:['u'],confirm_members:true}));
});
it('requires scope selection when bound users belong to multiple scopes',async()=>{
 request.mockImplementation(async(path:string)=>path==='/directory'?{organizations:[],departments:[]}: [{id:'d',provider:'feishu',external_id:'g',first_seen:'2026-09-27',last_seen:'2026-09-27',members:[{id:'u',name:'One',org_id:'org',team_id:'a'},{id:'v',name:'Two',org_id:'org',team_id:'b'}]}]);
 render(<DiscoveredGroups groups={[]} reloadGroups={vi.fn()}/>); fireEvent.click(await screen.findByText('绑定 / 一键带入')); expect((screen.getByLabelText('组织') as HTMLSelectElement).value).toBe('org'); expect((screen.getByLabelText('部门') as HTMLSelectElement).value).toBe(''); expect(screen.getByLabelText('One')).toBeTruthy();
});
it('links an existing group without rewriting membership or scope',async()=>{
 request.mockImplementation(async(path:string)=>path==='/directory'?{organizations:[],departments:[]}: [{id:'d',provider:'feishu',external_id:'g',first_seen:'2026-09-27',last_seen:'2026-09-27',members:[{id:'u',name:'One',org_id:'org',team_id:'a'}]}]);
 render(<DiscoveredGroups groups={[{id:'internal',name:'已有群',provider:'web',external_id:null,org_id:'org',team_id:'a',member_ids:['u']}]} reloadGroups={vi.fn()}/>);
 fireEvent.click(await screen.findByText('绑定 / 一键带入')); fireEvent.change(screen.getByLabelText('绑定方式'),{target:{value:'internal'}}); expect(screen.queryByLabelText('群名称')).toBeNull(); fireEvent.click(screen.getByLabelText(/我确认群映射/)); fireEvent.click(screen.getByText('确认绑定'));
 await waitFor(()=>expect(request).toHaveBeenCalledWith('/im/discoveries/groups/d/bind',{group_id:'internal',confirm_members:true}));
});
it('keeps modal header separate from scrolling body and restores background and focus',()=>{
 const before=document.createElement('button'); document.body.append(before); before.focus(); document.body.style.overflow='auto';
 const view=render(<Modal title="新增 Skill 或 MCP" close={vi.fn()}><form><input aria-label="字段"/><button>保存</button></form></Modal>);
 const dialog=screen.getByRole('dialog'); expect(dialog.querySelector('.modal-head')).toBeTruthy(); expect(dialog.querySelector('.modal-body form')).toBeTruthy(); expect(dialog.querySelector('.modal-body .modal-head')).toBeNull(); expect(document.body.style.overflow).toBe('hidden'); view.unmount(); expect(document.body.style.overflow).toBe('auto'); expect(document.activeElement).toBe(before); before.remove();
});

it('creates a named organization inline and binds the global administrator explicitly',async()=>{
 const found={id:'d',provider:'feishu',external_id:'real',first_seen:'2026-09-27',last_seen:'2026-09-27',members:[{id:'root',name:'Super Admin',role:'super_admin',org_id:null,team_id:null}]};
 request.mockImplementation(async(path:string)=>path==='/directory'?{organizations:[],departments:[],can_create_org:true,can_create_department:true}:path==='/directory/organizations'?{id:'auto-org',name:'运营公司',legacy:false}:[found]);
 render(<DiscoveredGroups groups={[]} reloadGroups={vi.fn()}/>);fireEvent.click(await screen.findByText('绑定 / 一键带入'));
 fireEvent.click(await screen.findByText('新增组织'));fireEvent.change(screen.getByLabelText('新组织名称'),{target:{value:'运营公司'}});fireEvent.click(screen.getByText('创建并选择'));
 await screen.findByRole('option',{name:'运营公司'});expect((screen.getByLabelText('组织') as HTMLSelectElement).value).toBe('auto-org');
 fireEvent.click(screen.getByLabelText('Super Admin'));fireEvent.click(screen.getByLabelText(/我确认群映射/));fireEvent.click(screen.getByText('确认绑定'));
 await waitFor(()=>expect(request).toHaveBeenCalledWith('/im/discoveries/groups/d/bind',{name:'real',org_id:'auto-org',team_id:null,member_ids:['root'],confirm_members:true}));
});
it('requires explicit organization choice and clears members on department changes',async()=>{
 const found={id:'d',provider:'feishu',external_id:'g',first_seen:'2026-09-27',last_seen:'2026-09-27',members:[{id:'u',name:'One',org_id:'a',team_id:'x'},{id:'v',name:'Two',org_id:'b',team_id:'y'}]};
 request.mockImplementation(async(path:string)=>path==='/directory'?{organizations:[{id:'a',name:'A'},{id:'b',name:'B'}],departments:[{id:'x',org_id:'a',name:'X'}]}:[found]);
 render(<DiscoveredGroups groups={[]} reloadGroups={vi.fn()}/>);fireEvent.click(await screen.findByText('绑定 / 一键带入'));await screen.findByRole('option',{name:'A'});
 expect((screen.getByLabelText('组织') as HTMLSelectElement).value).toBe('');expect(screen.queryByLabelText('One')).toBeNull();
 fireEvent.change(screen.getByLabelText('组织'),{target:{value:'a'}});fireEvent.click(screen.getByLabelText('One'));
 fireEvent.change(screen.getByLabelText('部门'),{target:{value:'x'}});expect((screen.getByLabelText('One') as HTMLInputElement).checked).toBe(false);expect(screen.queryByLabelText('Two')).toBeNull();
});

it('creates a department by name and submits a user with the selected directory IDs',async()=>{
 request.mockImplementation(async(path:string)=>path==='/directory'?{organizations:[{id:'org',name:'运营公司'}],departments:[],can_create_org:true,can_create_department:true}:path==='/directory/departments'?{id:'auto-dept',org_id:'org',name:'广告部'}:[]);
 render(<Management page="users" user={{id:'root',name:'Super Admin',email:'root@example.invalid',role:'super_admin',org_id:null,team_id:null,active:true}} navigate={vi.fn()}/>);
 fireEvent.click(await screen.findByText('新增'));await screen.findByRole('option',{name:'运营公司'});
 fireEvent.change(screen.getByLabelText('组织'),{target:{value:'org'}});fireEvent.change(screen.getByLabelText('角色'),{target:{value:'member'}});
 fireEvent.click(screen.getByText('新增部门'));fireEvent.change(screen.getByLabelText('新部门名称'),{target:{value:'广告部'}});fireEvent.click(screen.getByText('创建并选择'));await screen.findByRole('option',{name:'广告部'});
 fireEvent.change(screen.getByLabelText('名称'),{target:{value:'小王'}});fireEvent.change(screen.getByLabelText('邮箱'),{target:{value:'user@example.invalid'}});fireEvent.change(screen.getByLabelText('初始密码'),{target:{value:'test-password-123'}});fireEvent.click(screen.getByText('确认创建'));
 await waitFor(()=>expect(request).toHaveBeenCalledWith('/users',{name:'小王',email:'user@example.invalid',password:'test-password-123',role:'member',org_id:'org',team_id:'auto-dept',active:true}));
});

it('directory preserves edits on failure, supports cancel/save and confirmed delete',async()=>{
 const catalog={organizations:[{id:'o',name:'中文公司',can_edit:true,can_delete:true}],departments:[],can_create_org:true,can_create_department:true};
 let fail=true;
 request.mockImplementation(async(_p:string,options?:RequestInit)=>{if(options?.method==='PATCH' && fail)throw new Error('名称冲突');return catalog;});
 render(<DirectoryPanel/>);fireEvent.click(await screen.findByText('编辑'));
 fireEvent.change(screen.getByLabelText('名称'),{target:{value:'新名称'}});fireEvent.click(screen.getByText('保存'));
 await screen.findByText('名称冲突');expect((screen.getByLabelText('名称') as HTMLInputElement).value).toBe('新名称');
 fireEvent.click(screen.getByText('取消'));expect(screen.queryByRole('dialog')).toBeNull();
 fireEvent.click(screen.getByText('编辑'));fail=false;fireEvent.click(screen.getByText('保存'));await screen.findByText('已保存。');
 const confirm=vi.spyOn(window,'confirm').mockReturnValue(false);fireEvent.click(screen.getByText('删除'));expect(request.mock.calls.some(([,o])=>o?.method==='DELETE')).toBe(false);
 confirm.mockReturnValue(true);fireEvent.click(screen.getByText('删除'));await screen.findByText('目录已删除。');confirm.mockRestore();
});
it('group shows names, saves members, retains failed edits and confirms archive',async()=>{
 const user={id:'u',name:'员工',email:'u@test',role:'member' as const,org_id:'o',team_id:'t',active:true};
 const group={id:'g',name:'群名',org_id:'o',org_name:'中文公司',team_id:'t',team_name:'广告部',member_ids:['u'],provider:'web',can_edit:true,can_delete:true};
 let fail=true;
 request.mockImplementation(async(p:string,o?:RequestInit)=>{if(o?.method==='PATCH' && fail)throw new Error('任务未结束');return p==='/groups'?[group]:p==='/users'?[user]:[];});
 render(<><Toaster/><Management page="groups" user={{...user,id:'admin',role:'org_admin'}} navigate={vi.fn()}/></>);
 await screen.findByText('中文公司 / 广告部');fireEvent.click(screen.getByText('编辑'));fireEvent.change(screen.getByLabelText('群组名称'),{target:{value:'改名'}});
 fireEvent.click(screen.getByText('保存'));await screen.findByText('任务未结束');expect((screen.getByLabelText('群组名称') as HTMLInputElement).value).toBe('改名');
 fail=false;fireEvent.click(screen.getByText('保存'));await screen.findByText('群组已保存，成员权限立即更新。');
 expect(request.mock.calls.some(([p,o])=>p==='/groups/g' && o?.body===JSON.stringify({name:'改名',member_ids:['u']}))).toBe(true);
 const confirm=vi.spyOn(window,'confirm').mockReturnValue(true);fireEvent.click(screen.getByText('删除'));await screen.findByText('群组已从工作台移除，历史记录保留。');expect(confirm.mock.calls[0][0]).toContain('不会删除飞书或钉钉群');confirm.mockRestore();
});
it('restores directory route and limits directory management to administrators',()=>{
 expect(readRoute('#/directory')).toEqual({page:'directory',conversationId:null});
 const user={id:'u',name:'u',email:'u@test',role:'member' as const,org_id:'o',team_id:'t',active:true};
 expect(canAccessPage(user,'directory')).toBe(false);expect(canAccessPage({...user,role:'org_admin'},'directory')).toBe(true);
});
