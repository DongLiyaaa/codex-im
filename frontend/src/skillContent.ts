export function prepareSkillContent(content: string): string {
  if (!content.trim()) throw new Error('请填写 Markdown 正文。');
  if (Array.from(content).length > 64000) throw new Error('技能内容太长了，不能超过 64000 个字符。');
  return content;
}
