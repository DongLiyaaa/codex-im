import { useEffect, useRef, useState } from 'react';
import { ArrowRight, BookOpen, Cable, CheckCircle2, MessagesSquare } from 'lucide-react';
import { api, errorText } from './api';
import type { OnboardingProgress, OnboardingStep, User } from './api';
import type { Page } from './Management';
import { CodexAuthorization } from './CodexAuthorization';
import type { CodexAuthStatus } from './CodexAuthorization';
import { Feedback } from './ui';
import './Guides.css';

const steps: { id: OnboardingStep; title: string; purpose: string }[] = [
  { id: 'model', title: '连接模型', purpose: '使用系统已配置的模型，或连接你本人的 ChatGPT。' },
  { id: 'usage', title: '选择使用方式', purpose: '先用网页版即可；飞书和钉钉可以稍后再接入。' },
  { id: 'try', title: '开始对话', purpose: '进入智能会话，组织、技能和精细授权按需再配置。' },
];

function modelSummary(status: CodexAuthStatus | null) {
  if (!status) return '尚未读取模型连接状态，可返回上一步检查。';
  if (status.mode === 'api') return '系统 API 已配置，无需个人授权；实际任务结果以模型服务为准。';
  if (status.mode === 'shared') return '当前使用部署方配置的共享 ChatGPT 连接。';
  if (status.mode === 'personal' && status.state === 'connected') return '本人 ChatGPT 已连接，可以用于你后续发起的 Codex 任务。';
  if (status.mode === 'personal') return '本人 ChatGPT 尚未连接。可以返回上一步授权，或稍后继续。';
  return '模型执行器当前不可用，请联系管理员检查；这里不会伪报连接成功。';
}

export function Onboarding({ user, onSaved, navigate }: { user: User; onSaved: (user: User) => void; navigate: (page: Page) => void }) {
  const [stepId, setStepId] = useState<OnboardingStep>(user.onboarding?.step ?? 'model');
  const [modelStatus, setModelStatus] = useState<CodexAuthStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [retry, setRetry] = useState<{ status: Exclude<OnboardingProgress['status'], 'pending'>; target: OnboardingStep; destination?: Page } | null>(null);
  const lock = useRef(false);
  const started = useRef(false);
  const mounted = useRef(true);
  const index = Math.max(0, steps.findIndex(step => step.id === stepId));
  const step = steps[index];
  const completed = user.onboarding?.status === 'completed';
  const root = user.role === 'super_admin';
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);

  async function save(status: Exclude<OnboardingProgress['status'], 'pending'>, target: OnboardingStep, destination?: Page) {
    if (lock.current) return;
    lock.current = true; setBusy(true); setError(''); setRetry(null);
    try {
      const saved = await api<User>('/auth/me/onboarding', { method: 'PUT', body: JSON.stringify({ status, step: target }) });
      if (!mounted.current) return;
      onSaved(saved); setStepId(target);
      if (destination) navigate(destination);
    } catch (err) { if (mounted.current) { setError(errorText(err)); setRetry({ status, target, destination }); } }
    finally { lock.current = false; if (mounted.current) setBusy(false); }
  }
  useEffect(() => {
    if (!started.current && user.onboarding?.status === 'pending') {
      started.current = true;
      void save('in_progress', user.onboarding.step);
    }
  }, []);

  return <div className="onboarding-page">
    <div className="page-intro"><div><h2>快速初始化</h2><p>先连接模型并开始对话，其他能力需要时再配置。</p></div><button type="button" className="secondary" disabled={busy} onClick={() => navigate('manual')}><BookOpen size={16}/>使用说明</button></div>
    <p className="guide-progress" role="status">{completed ? '已完成初始化，可随时重新查看。' : `第 ${index + 1} / ${steps.length} 步 · 只保存进度，不代表环境已验收`}</p>
    <Feedback error={error}/>
    {error && retry && <button type="button" className="text-button" disabled={busy} onClick={() => void save(retry.status, retry.target, retry.destination)}>重试保存进度</button>}
    <div className="guide-layout">
      <nav className="guide-steps" aria-label="初始化引导步骤">{steps.map((item, i) => <button key={item.id} type="button" disabled={busy} aria-current={item.id === stepId ? 'step' : undefined} onClick={() => void save('in_progress', item.id)}><span className="guide-step-number">{i + 1}</span><span>{item.title}</span></button>)}</nav>
      <section className="guide-article" aria-labelledby="guide-step-title">
        <h3 id="guide-step-title">{step.title}</h3><p>{step.purpose}</p>
        {stepId === 'model' && <>
          <CodexAuthorization onStatus={setModelStatus}/>
          <p className="notice">授权只在官方页面由本人确认。系统 API 已配置时无需登录 ChatGPT；模型额度和执行器状态仍需实际核验。</p>
          {root && <button type="button" className="text-button" onClick={() => navigate('overview')}>查看执行器与高级接入检查<ArrowRight size={15}/></button>}
        </>}
        {stepId === 'usage' && <div className="guide-choice-grid">
          <button type="button" className="guide-choice" disabled={busy} onClick={() => void save('in_progress', 'try')}><MessagesSquare size={22}/><strong>先用网页版</strong><span>无需配置组织或 IM，直接进入下一步。</span><span className="guide-choice-action">选择网页版<ArrowRight size={15}/></span></button>
          {root && <button type="button" className="guide-choice" disabled={busy} onClick={() => navigate('integrations')}><Cable size={22}/><strong>接入飞书／钉钉</strong><span>按需配置机器人和员工身份，不影响稍后使用网页版。</span><span className="guide-choice-action">前往接入<ArrowRight size={15}/></span></button>}
        </div>}
        {stepId === 'try' && <>
          <div className="guide-model-status" role="status"><strong>模型连接</strong><p>{modelSummary(modelStatus)}</p></div>
          <p>完成初始化只会保存当前进度，不会调用模型、发送测试消息或自动授予 Skill / MCP。</p>
          <details className="guide-more"><summary>以后按需配置</summary><p>需要团队协作时，再维护组织与成员、添加 Skill / MCP，并用生效权限预览核对用户与群两侧授权。</p><div className="guide-links">{root && <button type="button" className="secondary" onClick={() => navigate('directory')}>组织与成员</button>}<button type="button" className="secondary" onClick={() => navigate('resources')}>Skill / MCP</button><button type="button" className="secondary" onClick={() => navigate('bindings')}>绑定与授权</button></div></details>
        </>}
        {completed ? <div className="guide-footer"><button type="button" className="secondary" disabled={busy} onClick={() => void save('in_progress', 'model')}>重新查看</button><button type="button" className="primary" onClick={() => navigate('chat')}><MessagesSquare size={16}/>进入智能会话</button></div> : <div className="guide-footer">
          <button type="button" className="text-button" disabled={busy} onClick={() => void save('deferred', stepId, 'overview')}>稍后继续</button>
          <button type="button" className="secondary" disabled={busy || index === 0} onClick={() => void save('in_progress', steps[index - 1].id)}>上一步</button>
          <button type="button" className="primary" disabled={busy} onClick={() => void save(index === steps.length - 1 ? 'completed' : 'in_progress', steps[Math.min(index + 1, steps.length - 1)].id, index === steps.length - 1 ? 'chat' : undefined)}>{busy ? '保存进度中…' : index === steps.length - 1 ? <><CheckCircle2 size={16}/>完成并开始对话</> : '下一步'}</button>
        </div>}
      </section>
    </div>
  </div>;
}
