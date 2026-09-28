import { useEffect, useState, useRef, useId } from 'react';
import type { ReactNode, FormEvent } from 'react';
import { LoaderCircle, AlertCircle, Inbox, X, CheckCircle2 } from 'lucide-react';
import useSWR from 'swr';
import { api, errorText } from './api';

export function useData<T>(path: string | null) {
  const { data, error, isLoading, mutate } = useSWR<T>(path, async (p: string) => {
    return api(p) as Promise<T>;
  });

  return {
    data: data ?? null,
    loading: isLoading,
    error: error ? errorText(error) : '',
    reload: () => mutate()
  };
}
export function Feedback({ error, success }: { error?: string; success?: string }) { return <>{error && <div className="notice error" role="alert"><AlertCircle size={18}/>{error}</div>}{success && <div className="notice success" role="status"><CheckCircle2 size={18}/>{success}</div>}</>; }
export function Loading() { return <div className="empty" role="status"><LoaderCircle className="spin" size={24}/><span>正在加载数据…</span></div>; }
export function Empty({ text = '暂无数据', description = '创建第一条记录，开始协作。' }: { text?: string; description?: string }) { return <div className="empty"><Inbox size={32}/><strong>{text}</strong><span>{description}</span></div>; }
export function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) { return <label className="field"><span>{label}</span>{children}{hint && <small>{hint}</small>}</label>; }
export function Modal({ title, children, close }: { title: string; children: ReactNode; close: () => void }) {
  const ref = useRef<HTMLDialogElement>(null); const titleId = useId();
  useEffect(() => { const before = document.activeElement as HTMLElement | null; const overflow = document.body.style.overflow; document.body.style.overflow = 'hidden'; const dialog = ref.current; dialog?.showModal(); dialog?.querySelector<HTMLElement>('input, select, textarea, button')?.focus(); return () => { document.body.style.overflow = overflow; before?.focus(); }; }, []);
  return <dialog ref={ref} aria-labelledby={titleId} onCancel={e => { e.preventDefault(); close(); }} onClick={(e) => { if (e.target === ref.current) close(); }}><div className="modal-head"><h2 id={titleId}>{title}</h2><button className="icon-button" aria-label="关闭弹窗" onClick={close}><X size={20}/></button></div><div className="modal-body">{children}</div></dialog>;
}
export function Form({ children, submit, onSuccess, label = '确认创建' }: { children: ReactNode; submit: (form: FormData) => Promise<unknown>; onSuccess: () => void; label?: string }) {
  const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  async function handle(e: FormEvent<HTMLFormElement>) { e.preventDefault(); if (busy) return; const data = new FormData(e.currentTarget); setBusy(true); setError(''); try { await submit(data); onSuccess(); } catch (e) { setError(errorText(e)); } finally { setBusy(false); } }
  return <form onSubmit={handle}><fieldset disabled={busy} style={{ border: 'none', padding: 0, margin: 0 }}>{children}<Feedback error={error}/><div className="form-actions"><button className="primary" type="submit" disabled={busy}>{busy && <LoaderCircle size={16} className="spin"/>}{busy ? '正在提交…' : label}</button></div></fieldset></form>;
}
export const value = (form: FormData, name: string) => String(form.get(name) ?? '').trim();
export const nullable = (form: FormData, name: string) => value(form, name) || null;
export function Badge({ children, tone = '' }: { children: ReactNode; tone?: string }) { return <span className={`badge ${tone}`}>{children}</span>; }
