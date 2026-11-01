import { useEffect, useRef, useState } from 'react';
import { api, errorText, post } from './api';
import { Feedback } from './ui';

type Status = { mode?: 'api' | 'personal' | 'shared' | 'unavailable'; state: string; message: string; url?: string; user_code?: string; expires_at?: string; account?: { email?: string; planType?: string } };
const pending = (state?: string) => state === 'starting' || state === 'pending';

export function CodexAuthorization({ compact = false }: { compact?: boolean }) {
  const [status, setStatus] = useState<Status | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const [revision, setRevision] = useState(0);
  const lock = useRef(false);
  const epoch = useRef(0);
  useEffect(() => {
    let live = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const controller = new AbortController();
    const current = ++epoch.current;
    async function load() {
      try {
        const result = await api<Status>('/auth/codex', { signal: controller.signal });
        if (live && current === epoch.current) { setStatus(result); setError(''); }
        if (live && current === epoch.current && pending(result.state)) timer = setTimeout(load, 3000);
      } catch (err) { if (live && current === epoch.current) { setStatus(null); setError(errorText(err)); } }
    }
    void load();
    return () => { live = false; controller.abort(); clearTimeout(timer); };
  }, [revision]);
  async function act(action: 'start' | 'disconnect') {
    if (lock.current) return;
    lock.current = true; setBusy(true); setError(''); setCopied(false);
    ++epoch.current;
    try {
      const result = await post<Status>(`/auth/codex/${action}`, {});
      setStatus(previous => ({ ...result, mode: previous?.mode }));
      setRevision(value => value + 1);
    } catch (err) { setError(errorText(err)); }
    finally { lock.current = false; setBusy(false); }
  }
  if (compact && status?.mode !== 'personal' && !error) return null;
  const official = status?.url === 'https://auth.openai.com/codex/device';
  return <section className={compact ? 'notice' : 'settings-card'} aria-label="本人 ChatGPT 授权">
    <div>
      <h3>本人 ChatGPT 授权</h3>
      {status?.mode === 'api' ? <p>当前使用系统配置的 API，无需个人授权。</p>
        : status?.mode === 'shared' ? <p>当前明确配置了系统共享 ChatGPT 凭据，不是你的个人连接。</p>
        : status?.mode === 'unavailable' ? <p>Codex 未启用、未安装或模型配置不可用，请联系管理员检查执行器。</p>
        : <p>{status?.message || '正在检查授权状态…'}</p>}
      {status?.state === 'connected' && <p>已连接{status.account?.email ? `：${status.account.email}` : ''}。仅用于你发起的任务，请重新发送任务。</p>}
      {pending(status?.state) && official && status?.user_code && <>
        <p>仅供本人，请勿转发。请在官方页面登录并输入一次性代码；可能需要先在 ChatGPT 安全设置中开启设备登录。</p>
        <p><code>{status.user_code}</code> <button type="button" className="secondary" onClick={async () => {
          try { await navigator.clipboard.writeText(status.user_code!); setCopied(true); }
          catch { setError('复制失败，请手动复制上方代码。'); }
        }}>{copied ? '已复制' : '复制代码'}</button></p>
        <a href={status.url} target="_blank" rel="noopener noreferrer">打开 ChatGPT 官方授权页面</a>
        <p>短时有效，完成后自动更新状态。授权不保证账号拥有可用额度。</p>
      </>}
      <Feedback error={error}/>
      <div className="actions">
        {status?.mode === 'personal' && !pending(status.state) && <button type="button" className="primary" disabled={busy} onClick={() => void act('start')}>{busy ? '正在处理…' : status.state === 'connected' ? '重新授权 ChatGPT' : '登录 ChatGPT 授权'}</button>}
        {(pending(status?.state) || status?.state === 'connected') && <button type="button" className="secondary" disabled={busy} onClick={() => void act('disconnect')}>{pending(status?.state) ? '取消授权' : '断开本人连接'}</button>}
        <button type="button" className="text-button" disabled={busy} onClick={() => setRevision(value => value + 1)}>刷新授权状态</button>
      </div>
      {!compact && <small>凭据按用户隔离保存；断开只清除 Hub 的连接，不会注销你在其他设备上的 OpenAI 登录。</small>}
    </div>
  </section>;
}
