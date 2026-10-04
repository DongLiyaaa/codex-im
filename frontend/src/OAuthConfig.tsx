import { useState } from 'react';
import { Copy, Settings2 } from 'lucide-react';
import { toast } from 'sonner';
import { api } from './api';
import { useData, Loading, Feedback, Modal, Form, Field, value } from './ui';
type Config = { provider: string; revision: number; configured: boolean; message: string; fields: Record<string,string>; secrets_set: Record<string,boolean>; credential_source?: string; bot_revision?: number; bot_copy_stale?: boolean; bot_app?: { available: boolean; revision: number; snapshot: string; client_id: string; message: string } };
export function OAuthConfig({ provider }: { provider: 'feishu' | 'dingtalk' }) {
  const config = useData<Config>(`/integrations/oauth/${provider}`);
  const [open, setOpen] = useState(false); const [clear, setClear] = useState<string[]>([]);
  const [reuse, setReuse] = useState<Config | null>(null);
  function saved() {
    setOpen(false); setReuse(null); config.reload();
    toast.success('个人授权配置已保存'); window.dispatchEvent(new Event('hub-integration-config-saved'));
  }
  return <section className="panel">
    <div className="section-heading"><h2>{provider === 'feishu' ? '飞书' : '钉钉'}个人 OAuth / CLI 授权配置</h2>
      <div className="actions"><button className="secondary" disabled={!config.data} onClick={() => { setClear([]); setOpen(true); }}><Settings2 size={16}/>配置个人授权</button>
        <button className="secondary" disabled={!config.data?.bot_app?.available} onClick={() => setReuse(config.data)}><Copy size={16}/>使用当前机器人应用（管理员确认）</button></div>
    </div>
    <Feedback error={config.error}/>{config.loading ? <Loading/> : config.data && <>
      <p>{config.data.configured ? '应用字段已配置；设备授权与本人 Identity 仍须验证' : '需要管理员配置：缺少个人 OAuth 应用配置'}</p><p>{config.data.message}</p>
      {config.data.bot_app && <p>{config.data.bot_app.message}</p>}
      {config.data.credential_source === 'bot_app_copy' && <p>来源：机器人应用复制快照，版本 {config.data.bot_revision}；机器人变更不自动同步，需重新确认。</p>}
      {config.data.bot_copy_stale && <Feedback error="机器人应用已变化，复制快照已停用。请重新确认或填写独立应用配置。"/>}
    </>}
    {reuse?.bot_app && <Modal title="确认使用当前机器人应用" close={() => setReuse(null)}>
      <p>将当前应用 {reuse.bot_app.client_id}（机器人版本 {reuse.bot_app.revision}）复制为独立加密 OAuth 配置，替换已有应用字段。密钥仅在服务器内复制，不回显。</p>
      <p>机器人配置变更不会自动同步；旧快照将停用并要求重新确认。复制不代表平台已开通设备授权，也不会自动发起本人授权或修改组织权限。</p>
      <Form label="确认复制机器人应用" submit={() => api(`/integrations/oauth/${provider}/use-bot-app`, {method:'POST', body:JSON.stringify({revision:reuse.revision, bot_revision:reuse.bot_app!.revision, bot_snapshot:reuse.bot_app!.snapshot, confirm:true})})} onSuccess={saved}>
        <button type="button" className="secondary" onClick={() => setReuse(null)}>取消</button>
      </Form>
    </Modal>}
    {open && config.data && <Modal title="个人 OAuth / CLI 授权配置" close={() => setOpen(false)}>
      <Form label="保存个人授权配置" submit={f => api(`/integrations/oauth/${provider}`, {method:'PUT', body:JSON.stringify({revision:config.data!.revision, fields:Object.fromEntries(['CLIENT_ID','CLIENT_SECRET','SCOPES'].map(name => [name,clear.includes(name) ? '' : value(f,name)])),clear})})} onSuccess={saved}>
        {['CLIENT_ID','CLIENT_SECRET','SCOPES'].map(name => <div key={name}><Field label={`PLATFORM_${provider.toUpperCase()}_${name}`} hint={name === 'CLIENT_SECRET' ? config.data!.secrets_set[name] ? '已设置；留空保留，永不回显' : '未设置；不会自动复用机器人密钥' : undefined}><input name={name} type={name === 'CLIENT_SECRET' ? 'password' : 'text'} defaultValue={config.data!.fields[name] ?? ''} readOnly={name === 'SCOPES'} disabled={clear.includes(name)} autoComplete="off" maxLength={4096}/></Field>{name !== 'SCOPES' && <label className="checkbox"><input type="checkbox" checked={clear.includes(name)} onChange={e=>setClear(e.target.checked ? [...clear,name] : clear.filter(x=>x!==name))}/>明确清除 {name}</label>}</div>)}
        <p>仅允许已批准的最小权限。自建应用不保证支持设备授权，请在官方后台确认开通。员工仍需绑定同一应用下的本人平台 Identity。</p>
      </Form>
    </Modal>}
  </section>;
}
