import { useEffect, useRef, useState } from 'react';
import { api, errorText } from './api';
import { Feedback } from './ui';
type Connection = { provider: 'feishu' | 'dingtalk'; state: string; message: string; scope?: string; expires_at?: string; authorization_url?: string; user_code?: string };
export const authorizationLabels: Record<string, string> = { unknown: '尚未查看', setup_required: '需要管理员配置', configuration_missing: '个人应用未配置', configuration_changed: '应用快照已变化', provider_not_supported: '应用不支持设备授权', provider_invalid_config: '平台应用配置无效', provider_denied: '平台或本人拒绝授权', organization_denied: '组织未允许本人 CLI 访问', identity_missing: '缺少本人平台绑定', identity_app_mismatch: 'OAuth 与机器人应用不匹配', private_delivery_unsupported: '当前应用不支持私发', private_delivery_failed: '本人私发失败', starting: '正在发起', pending: '等待本人授权', connected: '已连接，请重发任务', disconnected: '未连接', expired: '已过期', platform_rejected: '平台拒绝或暂不可用', identity_mismatch: '授权身份不符', identity_unverified: '无法核验本人身份', user_not_allowed: '未列入个人授权可用人员', interrupted: '授权中断' };
export function AuthorizationCard({ provider, state, canOpen }: { provider: 'feishu' | 'dingtalk'; state: string; canOpen: boolean }) {
  const [connection, setConnection] = useState<Connection | null>(null);
  const [error, setError] = useState(''); const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState<'cancel' | 'disconnect' | null>(null);
  const lock = useRef(false); const epoch = useRef(0);
  useEffect(() => {
    epoch.current++; setConnection(null); setError(''); setConfirm(null); setBusy(false); lock.current = false;
    return () => { epoch.current++; };
  }, [provider, canOpen]);
  async function load(action?: 'start' | 'refresh' | 'cancel' | 'disconnect') {
    if (!canOpen || lock.current) return;
    const version = epoch.current;
    lock.current = true; setBusy(true); setError('');
    if (action === 'cancel' || action === 'disconnect') setConnection(null);
    try {
      const result = action ? await api<Connection>(`/platform-connections/${provider}/${action}`, { method: 'POST' }) :
        (await api<Connection[]>('/platform-connections')).find(c => c.provider === provider);
      if (version === epoch.current) {
        if (!result || result.provider !== provider) throw new Error('未能读取本人授权状态');
        setConnection(result); setConfirm(null);
      }
    } catch (e) { if (version === epoch.current) setError(errorText(e)); }
    finally { if (version === epoch.current) { lock.current = false; setBusy(false); } }
  }
  useEffect(() => {
    if (!canOpen || !connection || !['pending', 'starting'].includes(connection.state) || busy || confirm) return;
    const timer = setTimeout(() => void load(), 5000);
    return () => clearTimeout(timer);
  }, [canOpen, connection, busy, confirm]);
  let safe = false;
  try { const u = new URL(connection?.authorization_url ?? ''); safe = u.protocol === 'https:' && !u.hash && !/[\s\x00-\x1f\x7f]/.test(connection?.authorization_url ?? '') && !u.username && !u.password && (!u.port || u.port === '443') && (provider === 'feishu' ? ['accounts.feishu.cn','open.feishu.cn'] : ['login.dingtalk.com','open-dev.dingtalk.com']).includes(u.hostname); } catch { /* Invalid issuer links are never rendered. */ }
  const currentState = canOpen && connection ? connection.state : state;
  return <div className="notice"><strong>{provider === 'feishu' ? '飞书' : '钉钉'}个人授权 · {authorizationLabels[currentState] ?? currentState}</strong>{canOpen ? <>
    <div className="actions"><button type="button" className="secondary" disabled={busy} onClick={() => void load()}>查看本人授权状态</button>{['setup_required','configuration_missing','configuration_changed','provider_invalid_config','provider_not_supported','identity_app_mismatch','private_delivery_unsupported'].includes(currentState) && <a href="#/integrations">管理员 OAuth 配置</a>}</div>
    {connection && <><p>{connection.message}</p>{connection.scope && <p>权限：{connection.scope}</p>}{connection.expires_at && <p>有效期至：{new Date(connection.expires_at).toLocaleString()}</p>}
      {connection.state === 'pending' && safe && <><p>以下授权材料仅供本人使用，请勿转发到群聊。</p><a href={connection.authorization_url} target="_blank" rel="noopener noreferrer">打开官方授权页面</a><p>本人设备码：<code>{connection.user_code}</code></p></>}
      <div className="actions"><button type="button" className="secondary" disabled={busy} onClick={() => void load('refresh')}>刷新状态</button>
        {!['pending','connected','starting','setup_required','configuration_missing','configuration_changed'].includes(connection.state) && <button type="button" className="primary" disabled={busy} onClick={() => void load('start')}>发起授权</button>}
        {['pending','starting'].includes(connection.state) && <button type="button" className="secondary" disabled={busy} onClick={() => setConfirm('cancel')}>取消授权</button>}
        {connection.state === 'connected' && <button type="button" className="danger-button" disabled={busy} onClick={() => setConfirm('disconnect')}>断开 Hub 连接</button>}
      </div></>}
    {confirm && <div role="alert"><p>{confirm === 'cancel' ? '取消本次授权' : '断开本人 Hub 连接'}只清除 Hub 当前保存的授权材料和凭据，不会撤销平台授权；如需彻底撤权，请前往平台授权管理。</p><div className="actions"><button type="button" className="secondary" disabled={busy} onClick={() => setConfirm(null)}>返回</button><button type="button" className="danger-button" disabled={busy} onClick={() => void load(confirm)}>{confirm === 'cancel' ? '确认取消授权' : '确认断开'}</button></div></div>}
    <Feedback error={error}/></> : <p>仅授权请求者本人可打开材料。</p>}</div>;
}
