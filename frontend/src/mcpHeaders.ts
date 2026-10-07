export type HeaderMode = 'none' | 'headers';
export interface HeaderRow { name: string; value: string }
export interface McpConfig { url: string; headers?: Record<string, string> }

// Matches what the server accepts: at most 30 headers, names of 1-128 letters, digits and hyphens, values up to 8192
// characters, and a few names that belong to the HTTP connection itself.
export const HEADER_LIMIT = 10;
const RESERVED = new Set(['host', 'content-length', 'transfer-encoding', 'connection']);
const VALUE_LIMIT = 8192;

export function readMcpForm(form: FormData): { url: string; mode: HeaderMode; rows: HeaderRow[] } {
  const names = form.getAll('header_name').map(String);
  const values = form.getAll('header_value').map(String);
  return {
    url: String(form.get('url') ?? '').trim(),
    mode: form.get('header_mode') === 'headers' ? 'headers' : 'none',
    rows: names.map((name, index) => ({ name, value: values[index] ?? '' })),
  };
}

export function buildMcpConfig(rawUrl: string, mode: HeaderMode, rows: HeaderRow[]): McpConfig {
  let url: URL;
  try { url = new URL(rawUrl.trim()); } catch { throw new Error('请填写完整的 MCP 服务地址，例如 https://mcp.example.com/mcp。'); }
  if (url.protocol !== 'https:') throw new Error('MCP 服务地址必须以 https:// 开头。');
  if (mode === 'none') return { url: url.href };

  // A row the user never touched is ignored; a half-filled row is an error so a missing secret is never silently dropped.
  const filled = rows.filter(row => row.name.trim() || row.value.trim());
  if (!filled.length) throw new Error('请填写至少一个请求头，或选择「不需要请求头」。');
  if (filled.length > HEADER_LIMIT) throw new Error(`最多可以添加 ${HEADER_LIMIT} 个请求头。`);
  const headers: Record<string, string> = {};
  const seen = new Set<string>();
  for (const row of filled) {
    const name = row.name.trim();
    // Pasting a token usually brings a trailing space or line break along; those are never part of it.
    const value = row.value.trim();
    if (!name) throw new Error('有一行填了密钥，但没有填请求头名称。');
    if (!/^[A-Za-z0-9-]{1,128}$/.test(name)) throw new Error(`请求头名称「${name}」只能包含英文字母、数字和短横线（-）。`);
    if (RESERVED.has(name.toLowerCase())) throw new Error(`请求头「${name}」由系统自动处理，不能自定义。`);
    if (seen.has(name.toLowerCase())) throw new Error(`请求头「${name}」重复了，请合并成一行。`);
    seen.add(name.toLowerCase());
    if (!value) throw new Error(`请填写请求头「${name}」的密钥。`);
    if (/[\u0000-\u001f\u007f]/.test(value)) throw new Error(`请求头「${name}」的密钥里有换行或不可见字符，请重新复制。`);
    if (value.length > VALUE_LIMIT) throw new Error(`请求头「${name}」的密钥太长了（最多 ${VALUE_LIMIT} 个字符）。`);
    headers[name] = value;
  }
  return { url: url.href, headers };
}
