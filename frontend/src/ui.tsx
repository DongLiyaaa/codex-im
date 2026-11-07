import { useEffect, useState, useRef, useId } from 'react';
import type { ReactNode, FormEvent } from 'react';
import { LoaderCircle, AlertCircle, Inbox, X, CheckCircle2 } from 'lucide-react';
import { api, errorText } from './api';

export function useData<T>(path: string | null, refreshInterval = 0, retainOnRefresh = false, scopeKey = '') {
  const [snapshot, setSnapshot] = useState<{ key: string; data: T } | null>(null);
  const key = JSON.stringify([path, scopeKey]);
  const data = snapshot?.key === key ? snapshot.data : null;
  const [loading, setLoading] = useState(!!path);
  const [errorState, setError] = useState<{ key: string; message: string } | null>(null);
  const error = errorState?.key === key ? errorState.message : '';
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    if (!retainOnRefresh) setSnapshot(null);
    setError(null); setLoading(!!path);
    async function load() {
      if (!path) return;
      try { const result = await api<T>(path, { signal: controller.signal }); if (!controller.signal.aborted) { setSnapshot({ key, data: result }); setError(null); } }
      catch (e) { if (!controller.signal.aborted) { setError({ key, message: errorText(e) }); if (!retainOnRefresh) setSnapshot(null); } }
      finally { if (!controller.signal.aborted) { setLoading(false); if (refreshInterval) timer = setTimeout(load, refreshInterval); } }
    }
    void load();
    return () => { controller.abort(); if (timer) clearTimeout(timer); };
  }, [path, key, revision, refreshInterval, retainOnRefresh]);
  return { data, loading, refreshing: loading && data !== null, error, reload: () => setRevision(v => v + 1) };
}
export function Feedback({ error, success }: { error?: string; success?: string }) { return <>{error && <div className="notice error" role="alert"><AlertCircle size={18}/>{error}</div>}{success && <div className="notice success" role="status"><CheckCircle2 size={18}/>{success}</div>}</>; }
export function Loading() { return <div className="empty" role="status"><LoaderCircle className="spin" size={24}/><span>正在加载数据…</span></div>; }
export function Empty({ text = '暂无数据', description = '创建第一条记录，开始协作。' }: { text?: string; description?: string }) { return <div className="empty"><Inbox size={32}/><strong>{text}</strong><span>{description}</span></div>; }
export function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) { return <label className="field"><span>{label}</span>{children}{hint && <small>{hint}</small>}</label>; }
export function Modal({ title, children, close, drawer = false, dirty = false, busy = false }: { title: string; children: ReactNode; close: () => void; drawer?: boolean; dirty?: boolean; busy?: boolean }) {
  const [confirmDiscard, setConfirmDiscard] = useState(false);
  const requestClose = () => { if (busy) return; if (dirty) setConfirmDiscard(true); else close(); };
  const ref = useRef<HTMLDialogElement>(null); const titleId = useId();
  const discardFocus = useRef<HTMLElement | null>(null);
  useEffect(() => {
    if (confirmDiscard) { discardFocus.current = document.activeElement as HTMLElement; ref.current?.querySelector<HTMLButtonElement>('.discard-confirm button')?.focus(); }
    else if (discardFocus.current?.isConnected) { discardFocus.current.focus(); discardFocus.current = null; }
  }, [confirmDiscard]);
  useEffect(() => { const before = document.activeElement as HTMLElement | null; const overflow = document.body.style.overflow; document.body.style.overflow = 'hidden'; const dialog = ref.current; dialog?.showModal(); dialog?.querySelector<HTMLElement>('input, select, textarea, button')?.focus(); return () => { document.body.style.overflow = overflow; before?.focus(); }; }, []);
  return <dialog ref={ref} className={drawer ? 'editor-drawer' : undefined} aria-labelledby={titleId} onCancel={e => { e.preventDefault(); requestClose(); }} onClick={(e) => { if (e.target === ref.current) requestClose(); }}><div className="modal-head"><h2 id={titleId}>{title}</h2><button className="icon-button" aria-label="关闭弹窗" disabled={busy} onClick={requestClose}><X size={20}/></button></div><div className="modal-body">{confirmDiscard ? <div className="discard-confirm" role="alert"><p>有未保存的修改，是否放弃？</p><div className="actions"><button type="button" className="secondary" onClick={() => setConfirmDiscard(false)}>继续编辑</button><button type="button" className="danger-button" onClick={close}>放弃修改</button></div></div> : null}{drawer ? <div hidden={confirmDiscard}>{children}</div> : children}</div></dialog>;
}
export function Form({ children, submit, onSuccess, label = '确认创建', onCancel, onDirty, submitDisabled = false }: { submitDisabled?: boolean; onCancel?: () => void; onDirty?: () => void; children: ReactNode; submit: (form: FormData) => Promise<unknown>; onSuccess: () => void; label?: string }) {
  const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  async function handle(e: FormEvent<HTMLFormElement>) { e.preventDefault(); if (busy || submitDisabled) return; const data = new FormData(e.currentTarget); setBusy(true); setError(''); try { await submit(data); onSuccess(); } catch (e) { setError(errorText(e)); } finally { setBusy(false); } }
  return <form onSubmit={handle} onChange={onDirty}><fieldset disabled={busy} style={{ border: 'none', padding: 0, margin: 0 }}>{children}<Feedback error={error}/><div className="form-actions">{onCancel && <button type="button" className="secondary" disabled={busy} onClick={onCancel}>取消</button>}<button className="primary" type="submit" disabled={busy || submitDisabled}>{busy && <LoaderCircle size={16} className="spin"/>}{busy ? '正在提交…' : label}</button></div></fieldset></form>;
}
export const value = (form: FormData, name: string) => String(form.get(name) ?? '').trim();
export const nullable = (form: FormData, name: string) => value(form, name) || null;
export function Badge({ children, tone = '' }: { children: ReactNode; tone?: string }) { return <span className={`badge ${tone}`}>{children}</span>; }
