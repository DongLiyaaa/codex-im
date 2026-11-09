import { useEffect, useRef, useState } from 'react';
import { api, errorText, post } from './api';
import { Feedback } from './ui';
import './CodexAuthorization.css';

export type CodexAuthStatus = { mode?: 'api' | 'personal' | 'shared' | 'unavailable'; state: string; message: string; url?: string; user_code?: string; expires_at?: string; account?: { email?: string; planType?: string }; models?: { model: string; display_name: string; is_default: boolean }[]; selected_model?: string | null; models_loading?: boolean; model_error?: string | null };
type Status = CodexAuthStatus;
const pending = (state?: string) => state === 'starting' || state === 'pending';

export function CodexAuthorization({ compact = false, onStatus }: { compact?: boolean; onStatus?: (status: Status | null) => void }) {
  const [status, setStatus] = useState<Status | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const [model, setModel] = useState('');
  const [modelMessage, setModelMessage] = useState('');
  const [revision, setRevision] = useState(0);
  const lock = useRef(false);
  const epoch = useRef(0);
  useEffect(() => { onStatus?.(status); }, [status, onStatus]);
  useEffect(() => {
    let live = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const controller = new AbortController();
    const current = ++epoch.current;
    async function load() {
      try {
        const result = await api<Status>('/auth/codex', { signal: controller.signal });
        if (live && current === epoch.current) { setStatus(result); setModel(result.selected_model ?? ''); setError(''); }
        if (live && current === epoch.current && (pending(result.state) || result.models_loading)) timer = setTimeout(load, 3000);
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
  async function modelAction(save: boolean) {
    if (lock.current) return;
    lock.current = true; setBusy(true); setError(''); setModelMessage(''); ++epoch.current;
    try {
      const result = save
        ? await api<Status>('/auth/codex/model', { method: 'PUT', body: JSON.stringify({ model: model || null }) })
        : await post<Status>('/auth/codex/models/refresh', {});
      setStatus(previous => ({ ...result, mode: previous?.mode }));
      setModelMessage(save ? '模型已保存，将用于你后续发起的 Codex 任务。' : '正在从本人账号读取可用模型。');
      setRevision(value => value + 1);
    } catch (err) { setError(errorText(err)); }
    finally { lock.current = false; setBusy(false); }
  }
  async function copyCode() {
    if (!status?.user_code) return;
    try { await navigator.clipboard.writeText(status.user_code); setCopied(true); }
    catch { setCopied(false); setError('复制失败，请手动复制上方代码，再打开官方页面。'); }
  }
  if (compact && status?.mode !== 'personal' && !error) return null;
  const official = status?.url === 'https://auth.openai.com/codex/device';
  return <section className="codex-authorization" aria-label="本人 ChatGPT 授权">
    <div>
      <h3>本人 ChatGPT 授权</h3>
      {status?.mode === 'api' ? <p>当前使用系统配置的 API，无需个人授权。</p>
        : status?.mode === 'shared' ? <p>当前明确配置了系统共享 ChatGPT 凭据，不是你的个人连接。</p>
        : status?.mode === 'unavailable' ? <p>Codex 未启用、未安装或模型配置不可用，请联系管理员检查执行器。</p>
        : <p>{status?.message || '正在检查授权状态…'}</p>}
      {status?.state === 'connected' && <p>已连接{status.account?.email ? `：${status.account.email}` : ''}。仅用于你发起的任务，请重新发送任务。</p>}
      {pending(status?.state) && official && status?.user_code && <>
        <p>仅供本人，请勿转发。点击下面按钮会复制一次性代码并打开官方页面；可能需要先在 ChatGPT 安全设置中开启设备登录。</p>
        <p><code>{status.user_code}</code></p>
        <div className="actions"><a className="primary authorization-open" href={status.url} target="_blank" rel="noopener noreferrer" onClick={() => { void copyCode(); }}>复制代码并打开官方页面</a><button type="button" className="text-button" onClick={() => void copyCode()}>{copied ? '已复制' : '仅复制代码'}</button></div>
        {copied && <p className="authorization-copy-state" role="status">代码已复制，请在官方页面粘贴。</p>}
        <p>短时有效，完成后这里会自动更新。授权不保证账号拥有可用额度。</p>
      </>}
      {status?.mode === 'personal' && status.state === 'connected' && <div className="codex-model-picker">
        <label className="field"><span>当前模型</span><select aria-label="当前模型" value={model} disabled={busy || status.models_loading} onChange={e => setModel(e.target.value)}>
          <option value="">Codex 默认模型</option>
          {model && !status.models?.some(item => item.model === model) && <option value={model} disabled>{model}（已不可用）</option>}
          {status.models?.map(item => <option key={item.model} value={item.model}>{item.display_name}{item.is_default ? '（推荐）' : ''}</option>)}
        </select></label>
        <div className="actions"><button type="button" className="primary" disabled={busy || status.models_loading || model === (status.selected_model ?? '')} onClick={() => void modelAction(true)}>保存模型</button>
        <button type="button" className="secondary" disabled={busy || status.models_loading} onClick={() => void modelAction(false)}>{status.models_loading ? '读取模型中…' : '刷新模型列表'}</button></div>
        <p className="muted">仅显示本人账号实际返回的模型；此选择不修改系统 API 模型。</p>
        <Feedback error={status.model_error ?? ''} success={modelMessage}/>
      </div>}
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
