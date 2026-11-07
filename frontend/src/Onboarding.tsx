import { useEffect, useRef, useState } from 'react';
import { ArrowRight, BookOpen, CheckCircle2 } from 'lucide-react';
import { api, errorText } from './api';
import type { OnboardingProgress, OnboardingStep, Role, User } from './api';
import type { Page } from './Management';
import { Feedback } from './ui';
import './Guides.css';

type Step = { id: OnboardingStep; title: string; purpose: string; actions: string[]; links: { page: Page; text: string }[]; note: string };
function stepsFor(role: Role): Step[] {
  const root = role === 'super_admin';
  const admin = root || role === 'org_admin';
  return [
    { id: 'welcome', title: '认识工作空间', purpose: '先了解账号、资源和会话之间的关系。', actions: ['本引导只保存你的阅读进度，不会自动创建组织、修改授权或执行任务。', '按步骤前往已有页面设置，随时从导航第一项返回继续。'], links: [{ page: 'manual', text: '阅读完整说明' }], note: '“完成阅读”不代表配置成功，请以实际状态和测试结果为准。' },
    { id: 'model', title: '模型与本人连接', purpose: root ? '确认工作空间的执行器与模型接入方式。' : '了解当前模型来源，按需连接本人的 ChatGPT 账号。', actions: root ? ['到工作台概览查看 Codex CLI 接入检查，确认执行器及沙箱状态。', '系统 API 已配置时无需个人登录；个人 OAuth 模式下由本人打开官方链接确认。', '连接成功后可在本人授权卡片读取和选择可用模型。'] : ['在个人设置查看正在使用系统 API、共享登录还是个人 OAuth。', '仅个人 OAuth 模式需要本人确认授权；系统配置问题请联系管理员。'], links: [{ page: root ? 'overview' : 'settings', text: root ? '检查模型接入' : '查看本人连接' }], note: '执行器就绪、本人已连接和模型有可用额度是不同状态，不要转发授权码。' },
    { id: 'people', title: admin ? '组织与成员' : '核对身份与群组', purpose: admin ? '明确成员属于哪里、通过什么身份使用工作空间。' : '确认自己的内部身份及可访问群组。', actions: admin ? [root ? '先创建组织和部门，再安排成员归属。' : '在有权限的组织内维护部门与下级成员。', root ? '网页账号在用户与角色管理；仅 IM 成员从机器人待接入发现处理。' : '需要机器人或外部身份接入时联系超级管理员。', '同名网页用户与 IM 用户不会自动合并，需明确核对身份绑定。'] : ['在个人设置核对自己的姓名、角色和账号类型。', '查看协作群组；需要加入或调整归属时联系管理员。'], links: admin ? [{ page: 'directory', text: '管理组织与部门' }, { page: 'users', text: '查看用户与角色' }, ...(root ? [{ page: 'integrations' as Page, text: '接入飞书／钉钉' }] : [])] : [{ page: 'settings', text: '查看个人资料' }, { page: 'groups', text: '查看协作群组' }], note: '加入群组或完成身份绑定不会自动获得 Skill/MCP 授权。' },
    { id: 'resources', title: admin ? '准备 Skill / MCP' : '了解可用能力', purpose: '区分任务指导与外部工具，确认需要哪些能力。', actions: admin ? ['Skill 直接填写名称与完整 Markdown，不必拆成多个文档。', 'MCP 填写公网 HTTPS 服务地址，密钥只放指定请求头字段。', '检查适用范围和启用状态，再明确授权给使用对象。'] : ['在 Skill和MCP管理查看当前可见资源。', '需要新能力时告诉管理员目标任务和会话范围；不索取受保护的技能原文。'], links: [{ page: 'resources', text: admin ? '添加或检查资源' : '查看资源' }], note: '资源可见或可管理不等于会话已获使用权；原文保护也不代表模型绝不会复述。' },
    { id: 'permissions', title: '核验授权', purpose: admin ? '用具体成员与会话检查权限是否真正生效。' : '检查当前任务能使用哪些资源。', actions: admin ? ['在绑定与授权选择用户、群组或整个组织／部门分配资源。', '通过查看生效权限，选择具体用户和私聊或群聊范围。', '根据“用户侧未授权”“群侧未授权”或范围限制排查，不直接放开所有权限。'] : ['查看本人可见授权；群聊还必须获得群侧授权。', '在已接入的 IM 中发送 /skills 获取服务端清单，需要调整时联系管理员。'], links: [{ page: 'bindings', text: admin ? '检查授权生效结果' : '查看本人授权' }], note: '群聊使用用户与群授权的交集；权限预览不等于远端工具网络或模型额度检查。' },
    { id: 'try', title: '开始试用', purpose: '用一个明确、低风险的任务检查完整流程。', actions: ['前往智能会话，选择或创建自己可发送的会话。', '先提交一个简单任务，检查回复和当前能力；需要外部文档时再由本人授权。', '如遇错误，记录时间和提示，请勿附带密码、令牌或私有 Skill 正文。'], links: [{ page: 'chat', text: '打开智能会话' }, { page: 'manual', text: '查看使用说明' }], note: '点击完成阅读只结束引导，不会发送测试消息、调用模型或自动完成任何授权。' },
  ];
}

export function Onboarding({ user, onSaved, navigate }: { user: User; onSaved: (user: User) => void; navigate: (page: Page) => void }) {
  const steps = stepsFor(user.role);
  const [stepId, setStepId] = useState<OnboardingStep>(user.onboarding?.step ?? 'welcome');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [retry, setRetry] = useState<{ status: Exclude<OnboardingProgress['status'], 'pending'>; target: OnboardingStep; leave: boolean } | null>(null);
  const lock = useRef(false);
  const started = useRef(false);
  const mounted = useRef(true);
  const index = steps.findIndex(step => step.id === stepId);
  const step = steps[index];
  const completed = user.onboarding?.status === 'completed';
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);

  async function save(status: Exclude<OnboardingProgress['status'], 'pending'>, target: OnboardingStep, leave = false) {
    if (lock.current) return;
    lock.current = true; setBusy(true); setError(''); setRetry(null);
    try {
      const saved = await api<User>('/auth/me/onboarding', { method: 'PUT', body: JSON.stringify({ status, step: target }) });
      if (!mounted.current) return;
      onSaved(saved); setStepId(target);
      if (leave) navigate('overview');
    } catch (err) { if (mounted.current) { setError(errorText(err)); setRetry({ status, target, leave }); } }
    finally { lock.current = false; if (mounted.current) setBusy(false); }
  }
  useEffect(() => {
    if (!started.current && user.onboarding?.status === 'pending') {
      started.current = true;
      void save('in_progress', user.onboarding.step);
    }
  }, []);

  return <div className="onboarding-page">
    <div className="page-intro"><div><h2>初始化引导</h2><p>按步骤熟悉工作空间，可随时离开并从这里继续。</p></div><button type="button" className="secondary" disabled={busy} onClick={() => navigate('manual')}><BookOpen size={16}/>使用说明</button></div>
    <p className="guide-progress" role="status">{completed ? '已完成阅读，可随时重新查看。' : `阅读进度：第 ${index + 1} / ${steps.length} 步（不是配置检测结果）`}</p>
    <Feedback error={error}/>
    {error && retry && <button type="button" className="text-button" disabled={busy} onClick={() => void save(retry.status, retry.target, retry.leave)}>重试保存进度</button>}
    <div className="guide-layout">
      <nav className="guide-steps" aria-label="初始化引导步骤">{steps.map((item, i) => <button key={item.id} type="button" disabled={busy} aria-current={item.id === stepId ? 'step' : undefined} onClick={() => void save('in_progress', item.id)}><span className="guide-step-number">{i + 1}</span><span>{item.title}</span></button>)}</nav>
      <section className="guide-article" aria-labelledby="guide-step-title">
        <h3 id="guide-step-title">{step.title}</h3><p>{step.purpose}</p>
        <ol>{step.actions.map(action => <li key={action}>{action}</li>)}</ol>
        <div className="guide-links">{step.links.map(link => <button key={link.page} type="button" className="secondary" disabled={busy} onClick={() => navigate(link.page)}>{link.text}<ArrowRight size={15}/></button>)}</div>
        <p className="notice">{step.note}</p>
        {completed ? <div className="guide-footer"><button type="button" className="primary" disabled={busy} onClick={() => void save('in_progress', 'welcome')}>重新查看引导</button><button type="button" className="secondary" onClick={() => navigate('overview')}>进入工作台</button></div> : <div className="guide-footer">
          <button type="button" className="text-button" disabled={busy} onClick={() => void save('deferred', stepId, true)}>稍后继续</button>
          <button type="button" className="secondary" disabled={busy || index === 0} onClick={() => void save('in_progress', steps[index - 1].id)}>上一步</button>
          <button type="button" className="primary" disabled={busy} onClick={() => void save(index === steps.length - 1 ? 'completed' : 'in_progress', steps[Math.min(index + 1, steps.length - 1)].id)}>{busy ? '保存进度中…' : index === steps.length - 1 ? <><CheckCircle2 size={16}/>完成阅读</> : '下一步'}</button>
        </div>}
      </section>
    </div>
  </div>;
}
