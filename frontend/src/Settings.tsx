import { useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { LoaderCircle } from 'lucide-react';
import { toast } from 'sonner';
import { api, errorText, roles } from './api';
import type { User } from './api';
import { Avatar } from './Avatar';
import { avatarFromFile, AVATAR_TYPES } from './avatarImage';
import { Feedback, Field } from './ui';
import './Settings.css';

export function Settings({ user, onSaved }: { user: User; onSaved: (user: User) => void }) {
  const [name, setName] = useState(user.name);
  const [avatar, setAvatar] = useState<string | null>(user.avatar ?? null);
  const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  const picker = useRef<HTMLInputElement>(null);
  const trimmed = name.trim();
  const nameChanged = trimmed !== user.name; const avatarChanged = avatar !== (user.avatar ?? null);

  async function choose(file: File | undefined) {
    if (!file) return;
    setError('');
    try { setAvatar(await avatarFromFile(file)); } catch (e) { setError(errorText(e)); }
  }
  async function save(e: FormEvent) {
    e.preventDefault();
    if (busy || !trimmed || (!nameChanged && !avatarChanged)) return;
    setBusy(true); setError('');
    try {
      const saved = await api<User>('/auth/me', { method: 'PATCH', body: JSON.stringify({ ...(nameChanged ? { name: trimmed } : {}), ...(avatarChanged ? { avatar } : {}) }) });
      onSaved({ ...user, name: saved.name, avatar: saved.avatar ?? null });
      setName(saved.name); setAvatar(saved.avatar ?? null);
      toast.success('个人设置已保存。');
    } catch (err) { setError(errorText(err)); } finally { setBusy(false); }
  }

  return <>
    <div className="page-intro"><div><h2>个人设置</h2><p>修改你在工作空间里显示的头像和名字。</p></div></div>
    <form className="settings-card" onSubmit={e => void save(e)}>
      <div className="settings-avatar">
        <span className="settings-avatar-preview"><Avatar user={{ name: trimmed || user.name, avatar }}/></span>
        <div className="settings-avatar-actions">
          <input ref={picker} className="visually-hidden" type="file" accept={AVATAR_TYPES.join(',')} aria-label="选择头像图片" onChange={e => { void choose(e.target.files?.[0]); e.target.value = ''; }}/>
          <button type="button" className="secondary" disabled={busy} onClick={() => picker.current?.click()}>更换头像</button>
          {avatar && <button type="button" className="text-button" disabled={busy} onClick={() => setAvatar(null)}>移除头像</button>}
          <small>支持 PNG、JPG、WebP，会自动裁成正方形。</small>
        </div>
      </div>
      <Field label="名字"><input name="name" value={name} maxLength={200} required onChange={e => setName(e.target.value)}/></Field>
      <Field label="登录邮箱" hint="邮箱和角色由管理员维护，不能在这里修改。"><input value={user.login_enabled === false ? '仅 IM 接入' : user.email} disabled readOnly/></Field>
      <Field label="角色"><input value={roles[user.role]} disabled readOnly/></Field>
      <Feedback error={error}/>
      <div className="form-actions"><button className="primary" type="submit" disabled={busy || !trimmed || (!nameChanged && !avatarChanged)}>{busy && <LoaderCircle size={16} className="spin"/>}{busy ? '正在保存…' : '保存'}</button></div>
    </form>
  </>;
}
