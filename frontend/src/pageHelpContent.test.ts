import { describe, expect, it } from 'vitest';
import { pageNames } from './Management';
import type { Page } from './Management';
import type { Role } from './api';
import { getPageHelp } from './pageHelpContent';
import type { HelpPage } from './pageHelpContent';

const pages: HelpPage[] = [...Object.keys(pageNames) as Page[], 'login', 'setup', 'connection'];
const roles: Role[] = ['super_admin', 'org_admin', 'team_lead', 'member'];
const text = (page: HelpPage, role?: Role) => JSON.stringify(getPageHelp(page, role));

describe('per-page manual coverage', () => {
  it.each(pages)('%s has complete real instructions and no placeholders', page => {
    for (const role of roles) {
      const guide = getPageHelp(page, role);
      expect(guide.purpose.trim().length).toBeGreaterThan(0);
      expect(guide.steps.length).toBeGreaterThan(0);
      expect(guide.notes.length).toBeGreaterThan(0);
      expect(guide.questions.length).toBeGreaterThan(0);
      expect(JSON.stringify(guide)).not.toMatch(/UI 预览|样式确认后补充|这里将|操作步骤占位/);
      for (const item of [...guide.steps, ...guide.notes]) expect(item.text.trim().length).toBeGreaterThan(0);
      for (const item of guide.questions) expect(item.question && item.answer).toBeTruthy();
    }
  });

  it('gives guests specific public instructions without administrative action guidance', () => {
    for (const page of ['login', 'setup', 'connection'] as const) {
      expect(getPageHelp(page).steps.length).toBeGreaterThan(1);
      expect(text(page)).not.toContain('点击“新增授权”');
    }
  });

  it('filters administrator-only actions for resource readers', () => {
    for (const role of ['member', 'team_lead'] as const) {
      expect(text('resources', role)).not.toContain('点击“新增 Skill / MCP”');
      expect(text('bindings', role)).not.toContain('点击“新增授权”');
      expect(text('bindings', role)).not.toContain('点击行内“编辑授权”');
    }
    expect(text('resources', 'org_admin')).toContain('点击“新增 Skill / MCP”');
    expect(text('bindings', 'super_admin')).toContain('点击“新增授权”');
  });

  it('limits platform configuration and global organization creation to super administrators', () => {
    for (const role of ['member', 'org_admin', 'team_lead'] as const) {
      expect(text('integrations', role)).not.toContain('保存应用配置会影响');
      expect(text('overview', role)).not.toContain('查看 Codex CLI 接入检查');
    }
    expect(text('directory', 'org_admin')).not.toContain('点击“新增组织”');
    expect(text('directory', 'super_admin')).toContain('点击“新增组织”');
    expect(text('audit', 'member')).toContain('当前角色无权');
    expect(text('directory', 'member')).toContain('当前角色无权');
  });

  it('keeps critical scope, security and authentication distinctions explicit', () => {
    expect(text('bindings', 'super_admin')).toContain('交集');
    expect(text('bindings', 'super_admin')).toContain('只读');
    expect(text('resources', 'member')).toContain('仅有资源管理权限');
    expect(text('integrations', 'super_admin')).toContain('不会自动合并');
    expect(text('settings', 'member')).toContain('系统 API 优先');
    expect(text('settings', 'member')).toContain('实际返回');
    expect(text('overview', 'super_admin')).toContain('不等于本人已授权');
    expect(text('login')).toContain('没有匿名直接重置');
    expect(text('chat', 'member')).toContain('不会自动重放');
  });
});
