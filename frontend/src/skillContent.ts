export type SkillInputMode = 'body' | 'document';

// JSON double-quoted strings are YAML-compatible; escape YAML line separators too.
const yamlString = (text: string) => JSON.stringify(text).replace(/[\u0085\u2028\u2029]/g, c => `\\u${c.charCodeAt(0).toString(16).padStart(4, '0')}`);

export async function prepareSkillContent(name: string, description: string, content: string, mode: SkillInputMode): Promise<string> {
  if (!description.trim()) throw new Error('请填写描述，说明技能的用途和适用任务，作为触发依据。');
  if (!content.trim()) throw new Error('请填写 Skill 内容。');
  let result = content;
  if (mode === 'body') {
    if (/^\s*---(?:\s|$)/.test(content)) throw new Error('内容以 YAML 分隔符开头。请切换“完整 SKILL.md”模式检查头部，避免重复生成。');
    const displayName = name.trim();
    if (!displayName) throw new Error('请填写资源名称，可使用中文。');
    let slug = displayName;
    if (!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(slug) || slug.length > 64) {
      const hash = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(displayName))), b => b.toString(16).padStart(2, '0')).join('').slice(0, 16);
      const prefix = displayName.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 40).replace(/-$/, '') || 'skill';
      slug = `${prefix}-${hash}`;
    }
    result = `---\nname: ${yamlString(slug)}\ndescription: ${yamlString(description)}\n---\n${content}`;
  }
  // Match Python len(), including astral characters; count the generated header.
  if (Array.from(result).length > 64000) throw new Error('Skill 完整内容（含 YAML 头部）不能超过 64000 个字符，请缩短正文或描述。');
  return result;
}
