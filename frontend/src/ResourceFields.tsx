import { useRef, useState } from 'react';
import type { ChangeEvent } from 'react';
import { Eye, EyeOff, Plus, Trash2, Upload } from 'lucide-react';
import { Field } from './ui';
import { HEADER_LIMIT } from './mcpHeaders';
import type { HeaderMode } from './mcpHeaders';
import './ResourceFields.css';

const FILE_LIMIT = 256 * 1024;
const EXAMPLE = '例如：\n当同事让你整理周报时：\n1. 先列出本周完成的事项\n2. 再列出下周的计划\n3. 最后用三句话总结重点\n注意：语气保持简洁、客观。';
const readText = (file: File) => new Promise<string>((resolve, reject) => {
  const reader = new FileReader();
  reader.onload = () => resolve(String(reader.result ?? ''));
  reader.onerror = () => reject(new Error('读取文件失败，请重试。'));
  reader.readAsText(file, 'utf-8');
});

function SkillFields() {
  const [text, setText] = useState('');
  const [note, setNote] = useState(''); const [problem, setProblem] = useState('');
  const picker = useRef<HTMLInputElement>(null);
  async function importFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = ''; // The same file can be chosen again after editing it.
    if (!file) return;
    setNote(''); setProblem('');
    if (file.size > FILE_LIMIT) { setProblem('文件太大了（最多 256KB），请精简后再导入。'); return; }
    try {
      const content = (await readText(file)).replace(/^\uFEFF/, '');
      if (content.includes('\u0000') || content.includes('\uFFFD')) { setProblem('这个文件看起来不是文字文件，请选择 .txt 或 .md 文件。'); return; }
      setText(content); setNote(`已导入「${file.name}」，可以继续修改。`);
    } catch (error) { setProblem(error instanceof Error ? error.message : '读取文件失败，请重试。'); }
  }
  return <>
    <Field label="这个技能是做什么的？（必填）" hint="用一两句话说明用途。助手会据此判断什么时候使用它。">
      <textarea aria-label="这个技能是做什么的" name="description" required maxLength={5000} rows={2} placeholder="例如：帮同事把每周的工作内容整理成周报。"/>
    </Field>
    <div className="field">
      <div className="skill-text-head">
        <label htmlFor="skill-text">技能内容（必填）</label>
        <button type="button" className="text-button" onClick={() => picker.current?.click()}><Upload size={15}/>从文件导入</button>
        <input ref={picker} type="file" hidden accept=".txt,.md,.markdown,text/plain,text/markdown" onChange={event => void importFile(event)} aria-label="选择要导入的文字文件"/>
      </div>
      <textarea id="skill-text" name="content" required rows={10} className="skill-text-area" value={text} onChange={event => setText(event.target.value)} placeholder={EXAMPLE}/>
      <small>直接用文字写就行，不需要任何格式或符号。可以写：什么时候用、要做哪几步、要注意什么。</small>
      <div className="skill-text-meta" aria-live="polite">
        <span className={problem ? 'skill-text-error' : 'skill-text-note'}>{problem || note}</span>
        <span>已输入 {Array.from(text).length} 字</span>
      </div>
    </div>
  </>;
}

let rowKey = 0;
function McpFields() {
  const [mode, setMode] = useState<HeaderMode>('none');
  const [rows, setRows] = useState<number[]>(() => [++rowKey]);
  const [shown, setShown] = useState<number[]>([]);
  const toggle = (key: number) => setShown(current => current.includes(key) ? current.filter(item => item !== key) : [...current, key]);
  return <>
    <Field label="MCP 服务地址" hint="向服务提供方索取，必须以 https:// 开头。">
      <input aria-label="MCP 服务地址" type="url" name="url" required placeholder="https://mcp.example.com/mcp"/>
    </Field>
    <Field label="说明（选填）" hint="写给管理员自己看的备注，比如这个服务是做什么的。">
      <textarea aria-label="说明" name="description" maxLength={5000} rows={2} placeholder="例如：查询公司内部的商品库存"/>
    </Field>
    <fieldset className="header-mode">
      <legend>访问这个服务需要请求头吗？</legend>
      <label><input type="radio" name="header_mode" value="none" checked={mode === 'none'} onChange={() => setMode('none')}/>不需要（公开服务，不用密钥）</label>
      <label><input type="radio" name="header_mode" value="headers" checked={mode === 'headers'} onChange={() => setMode('headers')}/>需要（用密钥访问，可以填多个）</label>
    </fieldset>
    <div className="header-rows" hidden={mode !== 'headers'}>
      <p className="im-help">常见写法：名称填 Authorization，密钥填「Bearer 你的令牌」；或者名称填 X-API-Key，密钥直接填令牌。具体以服务提供方的说明为准。</p>
      {rows.map((key, index) => <div className="header-row" key={key}>
        <label>请求头名称
          <input name="header_name" list="common-header-names" defaultValue={index === 0 ? 'Authorization' : ''} placeholder="例如 Authorization" maxLength={128} autoComplete="off" spellCheck={false} aria-label={`第 ${index + 1} 个请求头的名称`}/>
        </label>
        <label>密钥
          <input name="header_value" type={shown.includes(key) ? 'text' : 'password'} placeholder="粘贴密钥" autoComplete="off" spellCheck={false} data-1p-ignore data-lpignore="true" aria-label={`第 ${index + 1} 个请求头的密钥`}/>
        </label>
        <button type="button" className="icon-button" aria-label={shown.includes(key) ? `隐藏第 ${index + 1} 个密钥` : `显示第 ${index + 1} 个密钥`} onClick={() => toggle(key)}>{shown.includes(key) ? <EyeOff size={17}/> : <Eye size={17}/>}</button>
        <button type="button" className="icon-button" aria-label={`删除第 ${index + 1} 个请求头`} disabled={rows.length === 1} onClick={() => { setRows(current => current.filter(item => item !== key)); setShown(current => current.filter(item => item !== key)); }}><Trash2 size={17}/></button>
      </div>)}
      <datalist id="common-header-names"><option value="Authorization"/><option value="X-API-Key"/><option value="api-key"/></datalist>
      <div className="header-rows-foot">
        <button type="button" className="secondary" disabled={rows.length >= HEADER_LIMIT} onClick={() => setRows(current => [...current, ++rowKey])}><Plus size={15}/>添加请求头</button>
        <small>密钥保存时会加密存放，之后在页面上不会再显示。</small>
      </div>
    </div>
  </>;
}

/** The type-specific part of the "new Skill or MCP" form; everything is plain language and nothing needs a format. */
export function ResourceFields({ kind, onKind }: { kind: string; onKind: (kind: string) => void }) {
  return <>
    <Field label="类型">
      <select value={kind} onChange={event => onKind(event.target.value)}>
        <option value="skill">Skill 技能（教助手怎么做一件事）</option>
        <option value="mcp">MCP 服务（让助手连接外部工具）</option>
      </select>
    </Field>
    {kind === 'skill' ? <SkillFields/> : <McpFields/>}
    <label className="checkbox"><input type="checkbox" name="enabled" defaultChecked/>创建后立即启用</label>
  </>;
}
