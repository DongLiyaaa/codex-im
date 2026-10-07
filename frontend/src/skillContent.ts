export type SkillInputMode = 'body' | 'document';

// JSON double-quoted strings are YAML-compatible; escape YAML line separators too.
const yamlString = (text: string) => JSON.stringify(text).replace(/[\u0085\u2028\u2029]/g, c => `\\u${c.charCodeAt(0).toString(16).padStart(4, '0')}`);

export async function prepareSkillContent(name: string, description: string, content: string, mode: SkillInputMode): Promise<string> {
  if (!description.trim()) throw new Error('请用一两句话写明这个技能是做什么的，助手会据此判断什么时候使用它。');
  if (!content.trim()) throw new Error('请填写技能内容：直接用文字写出怎么做就可以。');
  let result = content;
  if (mode === 'body') {
    // Plain text is always plain text, even if it happens to start with dashes: the generated header comes first, so
    // the validator reads that one and everything after it is the body.
    const displayName = name.trim();
    if (!displayName) throw new Error('请填写名称，可以用中文。');
    let slug = displayName;
    if (!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(slug) || slug.length > 64) {
      const hash = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(displayName))), b => b.toString(16).padStart(2, '0')).join('').slice(0, 16);
      const prefix = displayName.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 40).replace(/-$/, '') || 'skill';
      slug = `${prefix}-${hash}`;
    }
    result = `---\nname: ${yamlString(slug)}\ndescription: ${yamlString(description)}\n---\n${content}`;
  }
  // Match Python len(), including astral characters; count the generated header.
  if (Array.from(result).length > 64000) throw new Error('技能内容太长了，请缩短到大约 6 万字以内（上面的说明文字也计入）。');
  return result;
}
