export type ID = string | number;
export type Role = 'super_admin' | 'org_admin' | 'team_lead' | 'member';
export interface User { id: ID; email: string; name: string; role: Role; org_id: ID | null; team_id: ID | null; active: boolean; login_enabled?: boolean; can_manage?: boolean }
export interface Group { can_edit?: boolean; can_delete?: boolean; org_name?: string | null; team_name?: string | null; id: ID; name: string; org_id: ID | null; team_id: ID | null; member_ids: ID[]; provider: string; external_id?: string | null }
export interface Resource { id: ID; name: string; kind: 'skill' | 'mcp'; description: string; org_id: ID | null; team_id?: ID | null; enabled: boolean; config: Record<string, unknown> }
export interface Binding { id: ID; subject_type: 'user' | 'group'; subject_id: ID; resource_id: ID }
export interface Conversation { can_delete?: boolean; id: ID; title: string; owner_id: ID; group_id?: ID | null; created_at: string }
export interface Attachment { id: string; filename: string; size: number; mime: string; status: 'received' | 'fetching' | 'parsing' | 'ready' | 'failed' | 'revoked'; error?: string | null }
export interface Message { platform_authorization?: { provider: 'feishu' | 'dingtalk'; state: string; can_open: boolean } | null; attachments?: Attachment[]; id: ID; conversation_id: ID; role: string; content: string; created_at: string }
export interface Run { id: ID; message_id: ID; provider?: 'web' | 'feishu' | 'dingtalk'; status: 'waiting_attachments' | 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'interrupted'; error?: string; created_at: string }
export interface ConversationState { messages: Message[]; active_run: Run | null; latest_run: Run | null }
export interface Identity { id: ID; provider: string; external_user_id: string; user_id: ID }
export const roles: Record<Role, string> = { super_admin: '超级管理员', org_admin: '组织管理员', team_lead: '团队负责人', member: '成员' };
export const sameId = (a: ID | null | undefined, b: ID | null | undefined) => a != null && b != null && String(a) === String(b);
// IM-only members have a placeholder address that must never be shown as if it were a real one.
export const contact = (user: User) => user.login_enabled === false ? '仅 IM 接入' : user.email;
export class ApiError extends Error { constructor(public status: number, message: string) { super(message); } }
export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api${path}`, { ...options, credentials: 'same-origin', headers: { ...(options.body && !(options.body instanceof FormData) ? { 'Content-Type': 'application/json' } : {}), ...options.headers } });
  const text = await response.text();
  let data: unknown;
  try { data = text ? JSON.parse(text) : null; } catch { throw new ApiError(response.status, '服务器返回了无法解析的响应，请确认 API 服务及代理配置。'); }
  if (!response.ok) {
    if (response.status === 401 && path !== '/auth/login' && path !== '/auth/me') window.dispatchEvent(new Event('hub-session-expired'));
    const detail = data && typeof data === 'object' && 'detail' in data ? (data as { detail: unknown }).detail : data;
    const message = typeof detail === 'string' ? detail : Array.isArray(detail) ? detail.map(x => typeof x === 'object' && x && 'msg' in x ? String(x.msg) : JSON.stringify(x)).join('；') : `请求失败（${response.status}）`;
    throw new ApiError(response.status, translateError(message));
  }
  return data as T;
}
const translations: Record<string, string> = { 'Invalid credentials': '邮箱或密码错误，请重新输入。', 'Origin rejected': '请求来源校验失败，请确认后端 APP_ORIGIN 与当前页面来源一致。', 'Forbidden': '当前账号没有执行此操作的权限。', 'Not found': '目标记录不存在，或已被删除。', 'Can only create lower-ranked users in your scope': '只能创建当前管理范围内的下级用户。', 'Can only manage lower-ranked users in your scope': '只能管理当前范围内的下级用户。', 'Member outside group scope': '所选成员不属于当前群组的组织或团队。', 'Conflicting or invalid record': '记录冲突或关联无效，请检查是否重复创建。', 'Unknown organization': '所选组织不存在，请重新选择。', 'Unknown department': '所选部门不存在或不属于该组织，请重新选择。', 'Member name required': '请填写有效的成员姓名。', 'External identity already bound; choose the existing user': '该发送者已绑定内部用户，请改用「绑定已有用户」。', 'Application changed; discover a new message': '飞书/钉钉应用已更换，请让对方重新发一条消息后再接入。', 'Duplicate sender in this batch': '同一发送者在本批次中重复，已跳过。', 'Onboard one platform at a time': '批量接入一次只能处理同一个平台。' };
export const translateError = (message: string) => translations[message] ?? message;
export const post = <T>(path: string, body: unknown) => api<T>(path, { method: 'POST', body: JSON.stringify(body) });
export const errorText = (error: unknown) => error instanceof Error ? error.message : '操作失败，请重试。';
export const dateText = (value: unknown) => typeof value === 'string' && !Number.isNaN(Date.parse(value)) ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—';
