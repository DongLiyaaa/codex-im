import { useState } from 'react';
import { LockKeyhole, Plug, Settings2, Sparkles } from 'lucide-react';
import type { Conversation, ID, Resource, User } from './api';
import { ConversationCapabilities } from './ConversationCapabilities';
import { Badge, Empty, Feedback, Loading } from './ui';

export function ChatCapabilities({ user, conversation, subjectUserId, data, loading, error, changed }: {
  user: User;
  conversation: Conversation | null;
  subjectUserId: ID;
  data: { skills: Resource[]; mcps: Resource[] } | null;
  loading: boolean;
  error: string;
  changed: () => void;
}) {
  const [editor, setEditor] = useState<{ conversation: Conversation; kind: 'skill' | 'mcp' } | null>(null);
  const canManage = ['super_admin', 'org_admin'].includes(user.role);
  return <>
    {editor && <ConversationCapabilities key={`${editor.conversation.id}:${editor.kind}`} conversation={editor.conversation} kind={editor.kind} user={user} subjectUserId={subjectUserId} close={() => setEditor(null)} changed={changed}/>}
    <aside className="capability-panel">
      <div className="conversation-heading"><h2>当前能力</h2><LockKeyhole size={16} className="muted"/></div>
      <p className="muted">由当前会话的有效授权决定</p>
      {!conversation ? <Empty text="尚未选择会话" description="选择后查看可用能力。"/> : loading ? <Loading/> : <>
        <Feedback error={error}/>
        {(['skills', 'mcps'] as const).map(type => <div className="capability-section" key={type}>
          <h3>{type === 'skills' ? <Sparkles size={16}/> : <Plug size={16}/>} {type === 'skills' ? 'Skills 技能' : 'MCP 服务'}<Badge>{data?.[type]?.length ?? 0}</Badge></h3>
          {canManage ? <button className="secondary" aria-label={type === 'skills' ? '管理 Skills 授权' : '管理 MCP 授权'} onClick={() => setEditor({ conversation, kind: type === 'skills' ? 'skill' : 'mcp' })}><Settings2 size={14}/>管理授权</button> : <p className="muted">如需调整，请联系组织管理员。</p>}
          {data?.[type]?.length ? data[type].map((resource, index) => <div className="capability" key={resource.id ?? index}><strong>{resource.name}</strong><p>{resource.description || (type === 'skills' ? '已授权技能' : '已授权远程服务')}</p></div>) : <p className="small-empty">{error ? '未能获取能力数据' : '暂无可用授权'}</p>}
        </div>)}
        <div className="capability-note"><LockKeyhole size={18}/><p>群聊能力取用户授权与群组授权的交集。已禁用资源不会参与执行。</p></div>
      </>}
    </aside>
  </>;
}
