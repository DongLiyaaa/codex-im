import { useState } from 'react';
import { api, errorText } from './api';
import { Feedback } from './ui';
type Connection = { provider: 'feishu' | 'dingtalk'; state: string; message: string; authorization_url?: string; user_code?: string };
export function AuthorizationCard({ provider, state, canOpen }: { provider: 'feishu' | 'dingtalk'; state: string; canOpen: boolean }) {
  const [connection, setConnection] = useState<Connection | null>(null);
  const [error, setError] = useState(''); const [busy, setBusy] = useState(false);
  async function open() {
    setBusy(true); setError('');
    try { const list = await api<Connection[]>('/platform-connections'); setConnection(list.find(c => c.provider === provider) ?? null); }
    catch(e) { setError(errorText(e)); } finally { setBusy(false); }
  }
  let safe = false;
  try { const u = new URL(connection?.authorization_url ?? ''); safe = u.protocol === 'https:' && !u.username && !u.password && (!u.port || u.port === '443') && (provider === 'feishu' ? ['accounts.feishu.cn','open.feishu.cn'] : ['login.dingtalk.com','open-dev.dingtalk.com']).includes(u.hostname); } catch { /* Invalid issuer links are never rendered. */ }
  const labels: Record<string,string> = { setup_required: '需要管理员配置', pending: '等待本人授权', connected: '已连接，请重发任务', disconnected: '未连接', expired: '已过期', platform_rejected: '平台拒绝或暂不可用', identity_mismatch: '授权身份不符', interrupted: '授权中断' };
  return <div className="notice"><strong>{provider === 'feishu' ? '飞书' : '钉钉'}个人授权 · {labels[state] ?? state}</strong>{canOpen ? <><div className="actions"><button type="button" className="secondary" disabled={busy} onClick={() => void open()}>查看本人授权状态</button><a href="#/connections">个人连接</a>{state === 'setup_required' && <a href="#/integrations">管理员 OAuth 配置</a>}</div>{connection && <><p>{connection.message}</p>{connection.state === 'pending' && safe && <><a href={connection.authorization_url} target="_blank" rel="noopener noreferrer">打开官方授权页面</a><p>本人设备码：<code>{connection.user_code}</code></p></>}</>}</> : <p>仅授权请求者本人可打开材料。</p>}<Feedback error={error}/></div>;
}
