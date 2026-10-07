// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { Management } from './Management';
import type { Resource, Role, User } from './api';

const { request } = vi.hoisted(() => ({ request: vi.fn() }));
// Reads go through api(path, options) and writes through post(path, body); only writes carry a second argument here.
vi.mock('./api', async original => ({ ...await original<typeof import('./api')>(), api: (path: string) => request(path), post: (path: string, body: unknown) => request(path, body) }));

const directory = { organizations: [{ id: 'org', name: '公司', legacy: false }, { id: 'org2', name: '分公司', legacy: false }],
  departments: [{ id: 'dept', org_id: 'org', name: '运营部', legacy: false }, { id: 'dept2', org_id: 'org2', name: '财务部', legacy: false }], can_create_org: true, can_create_department: true };
const person = (role: Role, org: string | null = null): User => ({ id: role, name: role, email: `${role}@example.invalid`, role, org_id: org, team_id: null, active: true });
const root = person('super_admin');
let stored: Resource[];
const created = () => request.mock.calls.filter(call => call[0] === '/resources' && call.length > 1);

beforeEach(() => {
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
  stored = [];
  request.mockImplementation(async (path: string, body?: unknown) => {
    if (path === '/directory') return directory;
    if (path === '/resources') return body === undefined ? stored : { ok: true };
    return [];
  });
});
afterEach(() => { cleanup(); request.mockReset(); vi.unstubAllGlobals(); });

async function openForm(user: User = root) {
  render(<Management page="resources" user={user} navigate={vi.fn()}/>);
  fireEvent.click(await screen.findByText('新增 Skill / MCP'));
  await screen.findByRole('dialog');
  await screen.findByRole('option', { name: '公司' });
}
const dialog = () => screen.getByRole('dialog');
const type = (label: string | RegExp, text: string) => fireEvent.change(within(dialog()).getByLabelText(label), { target: { value: text } });
const submit = () => fireEvent.click(within(dialog()).getByText('确认创建'));
const sent = () => created().at(-1)?.[1] as Record<string, unknown>;

it('is called Skill和MCP管理 and tells an empty workspace what to do next', async () => {
  render(<Management page="resources" user={root} navigate={vi.fn()}/>);
  expect(await screen.findByRole('heading', { name: 'Skill和MCP管理' })).toBeTruthy();
  expect(await screen.findByText('还没有 Skill 或 MCP')).toBeTruthy();
  expect(screen.getByText(/点击右上角「新增 Skill \/ MCP」/)).toBeTruthy();
  expect(document.body.textContent).not.toContain('能力资源');
});

it('tells someone who cannot manage resources to ask an administrator, and gives them no add button', async () => {
  render(<Management page="resources" user={person('member', 'org')} navigate={vi.fn()}/>);
  expect(await screen.findByText(/请联系管理员/)).toBeTruthy();
  expect(screen.queryByText('新增 Skill / MCP')).toBeNull();
});

it('a skill is plain text: no mode choice, no YAML wording, and anything typed is accepted', async () => {
  await openForm();
  expect(within(dialog()).queryByText('Skill 输入模式')).toBeNull();
  expect(document.body.textContent).not.toMatch(/YAML|SKILL\.md|分隔符/);
  type('名称', 'weekly-report');
  type('这个技能是做什么的', '帮同事整理周报');
  // Text that merely looks like the dashes of a file header is still just text.
  type('技能内容（必填）', '---\n先列出本周完成的事项\n---\n再写下周计划');
  submit();
  await waitFor(() => expect(created()).toHaveLength(1));
  const body = sent();
  expect(body).toMatchObject({ name: 'weekly-report', description: '帮同事整理周报', kind: 'skill', org_id: null, team_id: null, enabled: true });
  const content = (body.config as { content: string }).content;
  expect(content.startsWith('---\nname: "weekly-report"\ndescription: "帮同事整理周报"\n---\n')).toBe(true);
  expect(content.endsWith('---\n先列出本周完成的事项\n---\n再写下周计划')).toBe(true);
});

it('a skill with a Chinese name gets a stable technical id without asking the user for one', async () => {
  await openForm();
  type('名称', '周报助手');
  type('这个技能是做什么的', '整理周报');
  type('技能内容（必填）', '先列出完成的事项');
  submit();
  await waitFor(() => expect(created()).toHaveLength(1));
  expect((sent().config as { content: string }).content).toMatch(/^---\nname: "skill-[0-9a-f]{16}"\n/);
});

it('says in plain words what is missing instead of mentioning YAML', async () => {
  await openForm();
  type('名称', '周报助手');
  type('这个技能是做什么的', '   ');
  type('技能内容（必填）', '正文');
  submit();
  expect(await screen.findByText('请用一两句话写明这个技能是做什么的，助手会据此判断什么时候使用它。')).toBeTruthy();
  expect(created()).toHaveLength(0);
});

it('imports a text file into the box, can be edited afterwards, and refuses files that are too big or not text', async () => {
  await openForm();
  const picker = within(dialog()).getByLabelText('选择要导入的文字文件') as HTMLInputElement;
  const choose = (file: File) => fireEvent.change(picker, { target: { files: [file] } });
  choose(new File(['\uFEFF第一步：打招呼\n第二步：整理周报'], '周报.md', { type: 'text/markdown' }));
  await waitFor(() => expect((within(dialog()).getByLabelText('技能内容（必填）') as HTMLTextAreaElement).value).toBe('第一步：打招呼\n第二步：整理周报'));
  expect(screen.getByText('已导入「周报.md」，可以继续修改。')).toBeTruthy();
  expect(screen.getByText('已输入 16 字')).toBeTruthy();
  type('技能内容（必填）', '改过的内容');
  choose(new File(['x'.repeat(256 * 1024 + 1)], 'big.txt', { type: 'text/plain' }));
  expect(await screen.findByText('文件太大了（最多 256KB），请精简后再导入。')).toBeTruthy();
  expect((within(dialog()).getByLabelText('技能内容（必填）') as HTMLTextAreaElement).value).toBe('改过的内容');
  choose(new File(['abc\u0000def'], 'image.png', { type: 'image/png' }));
  expect(await screen.findByText('这个文件看起来不是文字文件，请选择 .txt 或 .md 文件。')).toBeTruthy();
  expect((within(dialog()).getByLabelText('技能内容（必填）') as HTMLTextAreaElement).value).toBe('改过的内容');
});

it('offers a department only once an organization is chosen, and sends both', async () => {
  await openForm();
  expect(within(dialog()).queryByLabelText('部门')).toBeNull();
  fireEvent.change(within(dialog()).getByLabelText('组织'), { target: { value: 'org' } });
  const department = within(dialog()).getByLabelText('部门') as HTMLSelectElement;
  expect(Array.from(department.options).map(option => option.textContent)).toEqual(['整个组织（不限部门）', '运营部']); // Only that organization's departments.
  fireEvent.change(department, { target: { value: 'dept' } });
  type('名称', 'ops-skill'); type('这个技能是做什么的', '运营'); type('技能内容（必填）', '正文');
  submit();
  await waitFor(() => expect(created()).toHaveLength(1));
  expect(sent()).toMatchObject({ org_id: 'org', team_id: 'dept' });
});

it('an organization-wide resource sends no department, and switching organization drops the old department', async () => {
  await openForm();
  fireEvent.change(within(dialog()).getByLabelText('组织'), { target: { value: 'org' } });
  fireEvent.change(within(dialog()).getByLabelText('部门'), { target: { value: 'dept' } });
  fireEvent.change(within(dialog()).getByLabelText('组织'), { target: { value: 'org2' } });
  expect((within(dialog()).getByLabelText('部门') as HTMLSelectElement).value).toBe('');
  type('名称', 'org-skill'); type('这个技能是做什么的', '说明'); type('技能内容（必填）', '正文');
  submit();
  await waitFor(() => expect(created()).toHaveLength(1));
  expect(sent()).toMatchObject({ org_id: 'org2', team_id: null });
});

it('going back to a global resource removes the department choice', async () => {
  await openForm();
  fireEvent.change(within(dialog()).getByLabelText('组织'), { target: { value: 'org' } });
  fireEvent.change(within(dialog()).getByLabelText('部门'), { target: { value: 'dept' } });
  fireEvent.change(within(dialog()).getByLabelText('组织'), { target: { value: '' } });
  expect(within(dialog()).queryByLabelText('部门')).toBeNull();
  type('名称', 'global-skill'); type('这个技能是做什么的', '说明'); type('技能内容（必填）', '正文');
  submit();
  await waitFor(() => expect(created()).toHaveLength(1));
  expect(sent()).toMatchObject({ org_id: null, team_id: null });
});

const chooseMcp = () => fireEvent.change(within(dialog()).getByLabelText('类型'), { target: { value: 'mcp' } });

it('an MCP without request headers sends only its address', async () => {
  await openForm();
  chooseMcp();
  expect((within(dialog()).getByLabelText(/不需要（公开服务/) as HTMLInputElement).checked).toBe(true);
  type('名称', '库存查询'); type('MCP 服务地址', 'https://mcp.example.com/mcp');
  submit();
  await waitFor(() => expect(created()).toHaveLength(1));
  expect(sent()).toMatchObject({ kind: 'mcp', config: { url: 'https://mcp.example.com/mcp' } });
  expect((sent().config as Record<string, unknown>).headers).toBeUndefined();
});

it('an MCP with several request headers keeps each one with its own secret, hidden while typing', async () => {
  await openForm();
  chooseMcp();
  fireEvent.click(within(dialog()).getByLabelText(/需要（用密钥访问/));
  const secret = (index: number) => within(dialog()).getByLabelText(`第 ${index} 个请求头的密钥`) as HTMLInputElement;
  expect((within(dialog()).getByLabelText('第 1 个请求头的名称') as HTMLInputElement).value).toBe('Authorization');
  expect(secret(1).type).toBe('password');
  fireEvent.click(within(dialog()).getByText('添加请求头'));
  fireEvent.change(within(dialog()).getByLabelText('第 2 个请求头的名称'), { target: { value: 'X-API-Key' } });
  fireEvent.change(secret(1), { target: { value: 'Bearer abc123' } });
  fireEvent.change(secret(2), { target: { value: 'key-456' } });
  fireEvent.click(within(dialog()).getByLabelText('显示第 2 个密钥'));
  expect(secret(2).type).toBe('text');
  expect(secret(1).type).toBe('password');
  type('名称', '库存查询'); type('MCP 服务地址', 'https://mcp.example.com/mcp');
  submit();
  await waitFor(() => expect(created()).toHaveLength(1));
  expect(sent().config).toEqual({ url: 'https://mcp.example.com/mcp', headers: { Authorization: 'Bearer abc123', 'X-API-Key': 'key-456' } });
});

it('a header row can be deleted, but the last one cannot, and a typed secret survives switching the choice back and forth', async () => {
  await openForm();
  chooseMcp();
  fireEvent.click(within(dialog()).getByLabelText(/需要（用密钥访问/));
  expect((within(dialog()).getByLabelText('删除第 1 个请求头') as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(within(dialog()).getByText('添加请求头'));
  fireEvent.change(within(dialog()).getByLabelText('第 1 个请求头的密钥'), { target: { value: 'Bearer keepme' } });
  fireEvent.click(within(dialog()).getByLabelText(/不需要（公开服务/));
  fireEvent.click(within(dialog()).getByLabelText(/需要（用密钥访问/));
  expect((within(dialog()).getByLabelText('第 1 个请求头的密钥') as HTMLInputElement).value).toBe('Bearer keepme');
  fireEvent.click(within(dialog()).getByLabelText('删除第 2 个请求头'));
  expect(within(dialog()).queryByLabelText('第 2 个请求头的名称')).toBeNull();
});

it('refuses to save a header without its secret and names the header', async () => {
  await openForm();
  chooseMcp();
  fireEvent.click(within(dialog()).getByLabelText(/需要（用密钥访问/));
  type('名称', '库存查询'); type('MCP 服务地址', 'https://mcp.example.com/mcp');
  submit();
  expect(await screen.findByText('请填写请求头「Authorization」的密钥。')).toBeTruthy();
  expect(created()).toHaveLength(0);
});

it('the add button stops at the limit', async () => {
  await openForm();
  chooseMcp();
  fireEvent.click(within(dialog()).getByLabelText(/需要（用密钥访问/));
  for (let count = 1; count < 10; count += 1) fireEvent.click(within(dialog()).getByText('添加请求头'));
  expect(within(dialog()).getAllByLabelText(/个请求头的名称/)).toHaveLength(10);
  expect((within(dialog()).getByText('添加请求头') as HTMLButtonElement).closest('button')!.disabled).toBe(true);
});

const skill: Resource = { id: 's1', name: '周报助手', kind: 'skill', description: '整理周报', org_id: 'org', team_id: 'dept', enabled: true,
  config: { content: '---\nname: "skill-abc"\ndescription: "整理周报"\n---\n第一步：列事项\n---\n第二步：写计划' } };
const mcp: Resource = { id: 'm1', name: '库存查询', kind: 'mcp', description: '', org_id: null, team_id: null, enabled: false,
  config: { url: 'https://mcp.example.com/mcp', headers: { Authorization: '***', 'X-API-Key': '***' } } };

it('shows each resource with its scope by name, the skill text without the generated header, and never a secret', async () => {
  stored = [skill, mcp, { ...skill, id: 's2', name: '全组织技能', team_id: null, org_id: 'org2' }];
  render(<Management page="resources" user={root} navigate={vi.fn()}/>);
  expect(await screen.findByText('适用范围：公司 · 运营部')).toBeTruthy();
  expect(screen.getByText('适用范围：全局')).toBeTruthy();
  expect(screen.getByText('适用范围：分公司')).toBeTruthy();
  expect(screen.getByText('已停用')).toBeTruthy();
  const body = document.querySelector('pre.skill-body')!;
  expect(body.textContent).toBe('第一步：列事项\n---\n第二步：写计划');
  expect(body.textContent).not.toContain('name:');
  expect(screen.getByText('https://mcp.example.com/mcp')).toBeTruthy();
  expect(screen.getByText('Authorization、X-API-Key（密钥已隐藏）')).toBeTruthy();
  expect(document.body.textContent).not.toContain('***');
});

it('an MCP with no headers says so', async () => {
  stored = [{ ...mcp, config: { url: 'https://mcp.example.com/mcp' } }];
  render(<Management page="resources" user={root} navigate={vi.fn()}/>);
  expect(await screen.findByText('无')).toBeTruthy();
});

it('the binding form lists each Skill or MCP with its type and scope so same-named ones can be told apart', async () => {
  request.mockImplementation(async (path: string) => path === '/directory' ? directory : path === '/resources' ? [skill, { ...skill, id: 's3', team_id: null }, { ...mcp, enabled: true }] : []);
  render(<Management page="bindings" user={root} navigate={vi.fn()}/>);
  fireEvent.click(await screen.findByText('新增授权'));
  await screen.findByRole('dialog');
  expect(await screen.findByRole('option', { name: '周报助手 · SKILL · 公司 · 运营部' })).toBeTruthy();
  expect(screen.getByRole('option', { name: '周报助手 · SKILL · 公司' })).toBeTruthy();
  expect(screen.getByRole('option', { name: '库存查询 · MCP · 全局' })).toBeTruthy();
});

it('the binding list names its third column Skill / MCP', async () => {
  request.mockImplementation(async (path: string) => path === '/directory' ? directory : path === '/resources' ? [skill]
    : path === '/bindings' ? [{ id: 'b1', subject_type: 'user', subject_id: 'u1', resource_id: 's1' }] : []);
  render(<Management page="bindings" user={root} navigate={vi.fn()}/>);
  expect(await screen.findByRole('columnheader', { name: 'Skill / MCP' })).toBeTruthy();
  expect(screen.getByRole('cell', { name: '周报助手' })).toBeTruthy();
});
