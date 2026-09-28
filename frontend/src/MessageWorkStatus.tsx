import { LoaderCircle } from 'lucide-react';
import { sameId } from './api';
import type { Message, Run } from './api';

export function MessageWorkStatus({ message, messages, run }: { message: Message; messages: Message[]; run: Run | null }) {
  if (!run || !['queued', 'running'].includes(run.status) || message.role !== 'user' || !sameId(run.message_id, message.id)) return null;
  const index = messages.findIndex(entry => sameId(entry.id, message.id));
  // Hide as soon as assistant body exists, including if a status snapshot lags.
  if (index < 0 || messages.slice(index + 1).some(entry => entry.role === 'assistant' && entry.content.trim())) return null;
  const label = run.provider === 'feishu' ? '飞书：工作' : run.provider === 'dingtalk' ? '钉钉：工作' : '工作中';
  return <div className="run-state message-work-status" role="status" aria-label={label}><LoaderCircle size={14} className="spin" aria-hidden="true"/><span>{label}</span></div>;
}
