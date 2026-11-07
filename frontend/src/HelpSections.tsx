import { helpSectionText } from './imHelpContent';
import type { HelpSection } from './imHelpContent';
import './HelpSections.css';

export function HelpSections({ sections = [], search = '' }: { sections?: HelpSection[]; search?: string }) {
  return <div className="help-sections">{sections.map(section => <details key={section.title} open={!!search && helpSectionText(section).toLocaleLowerCase().includes(search)}>
    <summary>{section.title}</summary>
    {section.steps.length > 0 && <ol>{section.steps.map(step => <li key={step}>{step}</li>)}</ol>}
    {section.table && <div className="help-table-scroll" role="region" aria-label={`${section.title}表格`} tabIndex={0}>
      <table><caption>{section.title}</caption><thead><tr>{section.table.headers.map(header => <th key={header} scope="col">{header}</th>)}</tr></thead>
        <tbody>{section.table.rows.map(row => <tr key={row[0]}>{row.map((cell, index) => index === 0 ? <th scope="row" key={index}>{cell}</th> : <td key={index}>{cell}</td>)}</tr>)}</tbody></table>
    </div>}
    {section.sources && <p className="help-sources">官方参考：{section.sources.map(source => <a key={source.url} href={source.url} target="_blank" rel="noopener noreferrer">{source.title}</a>)}</p>}
  </details>)}</div>;
}
