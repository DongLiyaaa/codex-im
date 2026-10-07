import { Badge } from './ui';
import './AuditActor.css';

export interface AuditContext {
  actor_name: string | null;
  channel: 'feishu' | 'dingtalk' | 'web' | null;
  nickname: string | null;
  group: { name: string | null; state: 'ok' | 'hidden' | 'missing'; archived: boolean } | null;
  private: boolean;
}

const channels: Record<string, string> = { feishu: '飞书', dingtalk: '钉钉', web: '网页' };

export function chatLabel(context: AuditContext): string | null {
  const group = context.group;
  if (group) {
    if (group.state === 'hidden') return '群：无权查看';
    if (group.state === 'missing') return '群：已删除';
    return `群：${group.name ?? ''}${group.archived ? '（已归档）' : ''}`;
  }
  return context.private ? '私聊' : null;
}

/** The "who and where" cell of an audit row; the raw id stays visible underneath so a row can still be traced. */
export function AuditActor({ id, context }: { id: string | null; context?: AuditContext }) {
  // Without a name (an older server, or an account that no longer exists) the cell is just the id, as before.
  const name = context?.nickname || context?.actor_name;
  if (!context || !name) return <>{id ?? '—'}</>;
  const chat = chatLabel(context);
  const channel = context.channel ? channels[context.channel] : null;
  return <div className="audit-actor">
    <div className="audit-actor-main"><strong>{name}</strong>{channel && <Badge>{channel}</Badge>}</div>
    {context.nickname && context.actor_name && context.nickname !== context.actor_name && <div className="audit-actor-sub">Hub 账号：{context.actor_name}</div>}
    {chat && <div className="audit-actor-sub">{chat}</div>}
    {id && <div className="audit-actor-id">{id}</div>}
  </div>;
}
