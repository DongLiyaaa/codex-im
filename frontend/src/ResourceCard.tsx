import { Layers } from 'lucide-react';
import type { Resource } from './api';
import { Badge } from './ui';
import './ResourceFields.css';

export interface DirectoryNames { organizations: { id: string; name: string }[]; departments: { id: string; org_id: string; name: string }[] }

/** "全局", "公司" or "公司 · 运营部": where a Skill or MCP can be used, by name instead of by internal id. */
export function resourceScope(resource: Pick<Resource, 'org_id' | 'team_id'>, directory?: DirectoryNames | null): string {
  if (resource.org_id == null) return '全局';
  const org = directory?.organizations.find(item => item.id === String(resource.org_id))?.name ?? String(resource.org_id);
  if (resource.team_id == null) return org;
  const team = directory?.departments.find(item => item.id === String(resource.team_id) && item.org_id === String(resource.org_id))?.name ?? String(resource.team_id);
  return `${org} · ${team}`;
}

// The generated header is an implementation detail: show administrators only what they wrote.
const skillBody = (content: string) => content.replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n?/, '');

export function ResourceCard({ resource, directory }: { resource: Resource; directory?: DirectoryNames | null }) {
  const config = resource.config as { content?: string; url?: string; headers?: Record<string, string> };
  const headerNames = Object.keys(config.headers ?? {});
  const isMcp = resource.kind === 'mcp';
  return <article className="resource-card">
    <div className="resource-head"><span className="resource-icon"><Layers size={22}/></span><Badge tone={resource.enabled ? 'green' : ''}>{resource.enabled ? '已启用' : '已停用'}</Badge></div>
    <h3>{resource.name}</h3>
    <p>{resource.description || '暂无说明'}</p>
    <div className="resource-footer"><Badge>{isMcp ? 'MCP' : 'SKILL'}</Badge><span>适用范围：{resourceScope(resource, directory)}</span></div>
    {isMcp && config.url && <details className="resource-detail">
      <summary>查看连接信息</summary>
      <dl>
        <dt>服务地址</dt><dd>{config.url}</dd>
        <dt>请求头</dt><dd>{headerNames.length ? `${headerNames.join('、')}（密钥已隐藏）` : '无'}</dd>
      </dl>
    </details>}
    {!isMcp && typeof config.content === 'string' && config.content && <details className="resource-detail">
      <summary>查看技能内容</summary>
      <pre className="skill-body">{skillBody(config.content)}</pre>
    </details>}
  </article>;
}
