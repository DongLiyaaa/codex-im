import { useState } from 'react';
import type { ID } from './api';
import { Field } from './ui';

type Option = { id: ID; name: string; disabled?: boolean };
export function SearchableSelect({ label, value, options, onChange, disabled = false, required = false, name, placeholder = '请选择' }: {
  label: string; value: string; options: Option[]; onChange: (value: string) => void;
  disabled?: boolean; required?: boolean; name?: string; placeholder?: string;
}) {
  const [query, setQuery] = useState('');
  const matches = options.filter(option => option.name.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));
  const selected = options.find(option => String(option.id) === value);
  const visible = selected && !matches.includes(selected) ? [selected, ...matches] : matches;
  return <div className="searchable-select">
    <Field label={`搜索${label}`}><input type="search" aria-label={`搜索${label}`} value={query} disabled={disabled}
      placeholder={`输入名称搜索${label}`} onChange={e => setQuery(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') e.preventDefault(); }}/></Field>
    <Field label={label}><select aria-label={label} name={name} value={value} disabled={disabled} required={required} onChange={e => onChange(e.target.value)}>
      <option value="" disabled={required}>{placeholder}</option>
      {value && !selected && <option value={value} disabled>原选项已不可用（{value}）</option>}
      {visible.map(option => <option key={option.id} value={option.id} disabled={option.disabled}>{option.name}{selected === option && !matches.includes(option) ? '（当前已选）' : ''}</option>)}
    </select></Field>
    {query && !matches.length && <small role="status">无匹配结果，当前选择不会被自动修改。</small>}
  </div>;
}
