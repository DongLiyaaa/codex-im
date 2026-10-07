import { describe, expect, it } from 'vitest';
import { buildMcpConfig, HEADER_LIMIT, readMcpForm } from './mcpHeaders';

const URL_OK = 'https://mcp.example.com/mcp';
const row = (name: string, value: string) => ({ name, value });
const fails = (rawUrl: string, mode: 'none' | 'headers', rows: { name: string; value: string }[], message: RegExp) =>
  expect(() => buildMcpConfig(rawUrl, mode, rows)).toThrow(message);

describe('MCP without request headers', () => {
  it('sends only the address, even if header rows were typed and then switched off', () => {
    expect(buildMcpConfig(URL_OK, 'none', [row('Authorization', 'Bearer secret')])).toEqual({ url: URL_OK });
  });
  it.each([['', /完整的 MCP 服务地址/], ['not a url', /完整的 MCP 服务地址/], ['http://mcp.example.com/mcp', /https:\/\//], ['ftp://mcp.example.com', /https:\/\//]])('rejects the address %j', (address, message) => fails(address, 'none', [], message));
  it('normalizes the address and ignores surrounding spaces', () => expect(buildMcpConfig('  https://mcp.example.com  ', 'none', []).url).toBe('https://mcp.example.com/'));
});

describe('MCP with request headers', () => {
  it('accepts several headers, each with its own secret', () => {
    const config = buildMcpConfig(URL_OK, 'headers', [row('Authorization', 'Bearer abc'), row('X-API-Key', 'key-123'), row('X-Tenant', 'acme')]);
    expect(config).toEqual({ url: URL_OK, headers: { Authorization: 'Bearer abc', 'X-API-Key': 'key-123', 'X-Tenant': 'acme' } });
  });
  it('trims the spaces and line breaks that come along when a token is pasted', () => {
    expect(buildMcpConfig(URL_OK, 'headers', [row('  Authorization ', '  Bearer abc \n')]).headers).toEqual({ Authorization: 'Bearer abc' });
  });
  it('ignores rows that were never touched', () => {
    expect(buildMcpConfig(URL_OK, 'headers', [row('', ''), row('Authorization', 'Bearer abc'), row('  ', ' ')]).headers).toEqual({ Authorization: 'Bearer abc' });
  });
  it('asks for at least one header instead of silently saving a service that needs none', () => fails(URL_OK, 'headers', [row('', '')], /至少一个请求头/));
  it('never drops a half-filled row: a missing secret or name is an error that names the header', () => {
    fails(URL_OK, 'headers', [row('Authorization', '')], /请填写请求头「Authorization」的密钥/);
    fails(URL_OK, 'headers', [row('', 'secret-without-name')], /没有填请求头名称/);
    fails(URL_OK, 'headers', [row('Authorization', 'Bearer abc'), row('X-API-Key', '  ')], /「X-API-Key」的密钥/);
  });
  it.each(['Auth orization', 'Autorización', '名称', 'a_b', 'a:b', 'x'.repeat(129)])('rejects the name %j', name => fails(URL_OK, 'headers', [row(name, 'v')], /只能包含英文字母、数字和短横线/));
  it.each(['Host', 'content-length', 'Transfer-Encoding', 'CONNECTION'])('rejects the reserved name %s', name => fails(URL_OK, 'headers', [row(name, 'v')], /由系统自动处理/));
  it('rejects the same header twice, whatever its capitalization', () => fails(URL_OK, 'headers', [row('Authorization', 'a'), row('authorization', 'b')], /「authorization」重复了/));
  it('rejects secrets that contain line breaks or control characters in the middle', () => {
    fails(URL_OK, 'headers', [row('Authorization', 'abc\ndef')], /换行或不可见字符/);
    fails(URL_OK, 'headers', [row('Authorization', 'abc\u0000def')], /换行或不可见字符/);
    fails(URL_OK, 'headers', [row('Authorization', 'abc\u007fdef')], /换行或不可见字符/);
  });
  it('limits the number of headers and the length of a secret', () => {
    const many = Array.from({ length: HEADER_LIMIT + 1 }, (_, index) => row(`X-H${index}`, 'v'));
    fails(URL_OK, 'headers', many, new RegExp(`最多可以添加 ${HEADER_LIMIT} 个`));
    expect(Object.keys(buildMcpConfig(URL_OK, 'headers', many.slice(0, HEADER_LIMIT)).headers!)).toHaveLength(HEADER_LIMIT);
    expect(buildMcpConfig(URL_OK, 'headers', [row('Authorization', 'x'.repeat(8192))]).headers!.Authorization).toHaveLength(8192);
    fails(URL_OK, 'headers', [row('Authorization', 'x'.repeat(8193))], /太长了/);
  });
  it('never puts a secret into an error message', () => {
    for (const rows of [[row('Authorization', 'sk-live-very-secret\n')], [row('Bad Name', 'sk-live-very-secret')], [row('Authorization', 'sk-live-very-secret'), row('authorization', 'sk-live-very-secret')]]) {
      try { buildMcpConfig(URL_OK, 'headers', rows); } catch (error) { expect((error as Error).message).not.toContain('sk-live-very-secret'); }
    }
  });
});

describe('reading the form', () => {
  it('keeps the rows in order and tolerates a missing value', () => {
    const form = new FormData();
    form.set('url', ` ${URL_OK} `); form.set('header_mode', 'headers');
    form.append('header_name', 'Authorization'); form.append('header_value', 'Bearer abc');
    form.append('header_name', 'X-API-Key');
    expect(readMcpForm(form)).toEqual({ url: URL_OK, mode: 'headers', rows: [row('Authorization', 'Bearer abc'), row('X-API-Key', '')] });
  });
  it('treats anything but the explicit choice as "no headers"', () => {
    const form = new FormData();
    form.set('url', URL_OK); form.set('header_mode', 'surprise');
    expect(readMcpForm(form).mode).toBe('none');
    expect(readMcpForm(new FormData())).toEqual({ url: '', mode: 'none', rows: [] });
  });
});
