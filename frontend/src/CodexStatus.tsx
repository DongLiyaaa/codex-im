import { useState } from 'react';
import { CircleCheck, CircleMinus, CircleX, RefreshCw } from 'lucide-react';
import { dateText } from './api';
import { Badge, Feedback, Loading, useData } from './ui';
import './CodexStatus.css';

export interface CodexRunnerStatus {
  ready: boolean; checked_at: number; auth_mode: 'api' | 'chatgpt' | 'invalid';
  codex: { installed: boolean; version: string | null; pinned_version: string };
  model: { id: string | null; endpoint_host: string | null; credential_configured: boolean };
  config_error: string | null;
  sandbox: { state: 'ok' | 'failed' | 'skipped'; error: string | null };
  model_endpoint: { state: string; http_status: number | null; model_listed: boolean | null; reason: string | null };
}
export interface CodexStatusResult { reachable: boolean; error: string | null; runner: CodexRunnerStatus | null }
type Tone = 'ok' | 'fail' | 'idle';
interface Row { name: string; tone: Tone; detail: string }

const runnerErrors: Record<string, string> = {
  RUNNER_UNREACHABLE: '连不上执行器，请确认 runner 服务已经启动。',
  RUNNER_TIMEOUT: '执行器响应超时。',
  RUNNER_AUTH_FAILED: '执行器拒绝了访问令牌，请确认 API 与 runner 使用同一个 RUNNER_TOKEN。',
  RUNNER_OUTDATED: '执行器版本较旧，还没有检查接口，请更新并重启执行器。',
  RUNNER_TOKEN_NOT_CONFIGURED: '没有配置 RUNNER_TOKEN。',
  RUNNER_INVALID_RESPONSE: '执行器返回的内容无法识别。',
  RUNNER_ERROR: '执行器返回了异常响应。',
};
const modelConfigErrors: Record<string, string> = {
  INVALID_CODEX_MODEL: '模型 ID 的格式不合法。',
  INVALID_CODEX_BASE_URL: '模型端点地址不合法，需要是公网 https 地址。',
  CODEX_BASE_URL_REQUIRES_API_MODE: '自定义模型端点只能配合 API Key 模式使用。',
  INVALID_CODEX_AUTH_MODE: '认证模式不合法，只能是 api 或 chatgpt。',
};
const credentialErrors: Record<string, string> = {
  MODEL_API_KEY_NOT_CONFIGURED: '没有配置模型 API Key。',
  OAUTH_SOURCE_NOT_CONFIGURED: '没有配置 ChatGPT 登录目录。',
};
const endpointFailures: Record<string, string> = {
  model_missing: '端点可以连通，但模型列表里没有配置的模型。',
  unauthorized: '端点拒绝了当前凭据。',
  timeout: '连接端点超时。',
  unreachable: '无法连接到端点。',
  http_error: '端点返回了异常状态。',
  redirect_refused: '端点返回了重定向，为保护凭据已拒绝跟随。',
  invalid_response: '端点返回的模型列表无法识别。',
};
const skipReasons: Record<string, string> = {
  NO_CUSTOM_ENDPOINT: '使用默认端点，不做探测。',
  CONFIG_ERROR: '配置有误，已跳过。',
  SOCKS_PROXY_NOT_PROBED: '经 SOCKS 代理访问，不做探测。',
};
const toneBadge: Record<Tone, { text: string; badge: string }> = { ok: { text: '通过', badge: 'green' }, fail: { text: '未通过', badge: 'red' }, idle: { text: '未检查', badge: '' } };
const Icon = { ok: CircleCheck, fail: CircleX, idle: CircleMinus };

export function codexRows(result: CodexStatusResult): Row[] {
  const names = ['执行器连接', 'Codex CLI', '模型与端点', '模型凭据', '模型端点连通', '沙箱'];
  const runner = result.runner;
  if (!result.reachable || !runner) {
    const code = result.error ?? '';
    return names.map((name, i) => i === 0
      ? { name, tone: 'fail', detail: runnerErrors[code] ?? `执行器检查失败（${code || '未知原因'}）。` }
      : { name, tone: 'idle', detail: '执行器不可用，未检查。' });
  }
  const { codex, model, sandbox, model_endpoint: endpoint } = runner;
  const error = runner.config_error ?? '';
  const modelBad = error in modelConfigErrors;
  const credentialBad = !!error && !modelBad;
  const rows: Row[] = [{ name: names[0], tone: 'ok', detail: '已连接执行器。' }];
  rows.push(codex.installed
    ? { name: names[1], tone: 'ok', detail: codex.version ? (codex.version === codex.pinned_version ? `已安装 ${codex.version}。` : `已安装 ${codex.version}（部署固定版本 ${codex.pinned_version}）。`) : '已安装，但无法读取版本。' }
    : { name: names[1], tone: 'fail', detail: '执行器里没有找到 Codex CLI。' });
  rows.push(modelBad
    ? { name: names[2], tone: 'fail', detail: modelConfigErrors[error] }
    : { name: names[2], tone: 'ok', detail: `${model.id ? `模型 ${model.id}` : '未指定模型，使用 Codex 默认模型'}；${model.endpoint_host ? `端点 ${model.endpoint_host}` : '默认端点'}。` });
  rows.push(credentialBad
    ? { name: names[3], tone: 'fail', detail: credentialErrors[error] ?? `模型凭据不可用（${error}）。` }
    : !model.credential_configured
      ? { name: names[3], tone: 'fail', detail: '没有配置模型凭据。' }
      : { name: names[3], tone: 'ok', detail: runner.auth_mode === 'chatgpt' ? '使用 ChatGPT 登录。' : '已配置 API Key（不会显示内容）。' });
  if (endpoint.state === 'ok') {
    rows.push({ name: names[4], tone: 'ok', detail: model.id ? `端点连通正常，模型列表包含 ${model.id}。` : '端点连通正常。' });
  } else if (endpoint.state === 'unverified') {
    rows.push({ name: names[4], tone: 'idle', detail: `端点没有提供模型列表${endpoint.http_status ? `（HTTP ${endpoint.http_status}）` : ''}，无法验证，不影响判断。` });
  } else if (endpoint.state === 'skipped') {
    rows.push({ name: names[4], tone: 'idle', detail: skipReasons[endpoint.reason ?? ''] ?? '未检查。' });
  } else {
    rows.push({ name: names[4], tone: 'fail', detail: `${endpointFailures[endpoint.state] ?? '端点检查失败。'}${endpoint.http_status ? `（HTTP ${endpoint.http_status}）` : ''}` });
  }
  rows.push(sandbox.state === 'ok'
    ? { name: names[5], tone: 'ok', detail: '沙箱可以启动，任务能在其中安全执行。' }
    : sandbox.state === 'failed'
      ? { name: names[5], tone: 'fail', detail: '沙箱无法启动，任务会被拒绝。Apple Silicon 上的 amd64 容器是常见原因，详见 docs/DOCKER_IMPACT.md。' }
      : { name: names[5], tone: 'idle', detail: '当前平台使用系统沙箱，在运行任务时检查。' });
  return rows;
}

export function CodexStatus() {
  const [forced, setForced] = useState(false);
  const { data, error, loading, reload } = useData<CodexStatusResult>(forced ? '/system/codex-status?refresh=true' : '/system/codex-status');
  const valid = !!data && typeof data === 'object' && !Array.isArray(data) && typeof data.reachable === 'boolean';
  const ready = valid && data.reachable && data.runner?.ready === true;
  // The first manual refresh switches to the forcing request; later ones repeat it.
  const recheck = () => forced ? reload() : setForced(true);
  return <section aria-labelledby="codex-check-title">
    <div className="section-heading">
      <h2 id="codex-check-title">Codex CLI 接入检查</h2>
      <span className="codex-check-actions">
        {valid && <Badge tone={ready ? 'green' : 'red'}>{ready ? '可用' : '不可用'}</Badge>}
        <button className="text-button" onClick={recheck} disabled={loading}><RefreshCw size={15}/>重新检查</button>
      </span>
    </div>
    <Feedback error={error || (data && !valid ? '检查结果的格式无法识别。' : '')}/>
    {loading ? <Loading/> : valid && <>
      <ul className="codex-check" aria-label="Codex CLI 检查项">
        {codexRows(data).map(row => { const Mark = Icon[row.tone]; const state = toneBadge[row.tone]; return <li className="codex-check-row" key={row.name}>
          <Mark size={18} className={row.tone} aria-hidden="true"/>
          <span className="codex-check-name">{row.name}</span>
          <span className="codex-check-detail">{row.detail}</span>
          <Badge tone={state.badge}>{state.text}</Badge>
        </li>; })}
      </ul>
      {data.runner && <div className="codex-check-foot">上次检查：{dateText(new Date(data.runner.checked_at * 1000).toISOString())}。结果缓存约 30 秒。</div>}
    </>}
  </section>;
}
