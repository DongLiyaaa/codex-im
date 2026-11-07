import { useRef, useState } from 'react';
import { ArrowRight, Search } from 'lucide-react';
import type { User } from './api';
import { pageNames } from './Management';
import type { Page } from './Management';
import { getPageHelp } from './pageHelpContent';
import type { HelpPage } from './pageHelpContent';
import { canAccessPage } from './route';
import { Empty } from './ui';
import './Guides.css';
import { HelpSections } from './HelpSections';
import { helpSectionText } from './imHelpContent';

const chapters: { id: HelpPage; title: string; page?: Page }[] = [
  ...(['onboarding', 'overview', 'chat', 'directory', 'users', 'groups', 'resources', 'bindings', 'integrations', 'audit', 'settings', 'manual'] as Page[]).map(page => ({ id: page, title: pageNames[page], page })),
  { id: 'login', title: '登录工作空间' }, { id: 'setup', title: '首次注册管理员' }, { id: 'connection', title: '连接问题' },
];

export function Manual({ user, navigate }: { user: User; navigate: (page: Page) => void }) {
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState<HelpPage>('onboarding');
  const article = useRef<HTMLElement>(null);
  const available = chapters.filter(chapter => !chapter.page || canAccessPage(user, chapter.page)).map(chapter => ({ ...chapter, guide: getPageHelp(chapter.id, user.role) }));
  const term = query.trim().toLocaleLowerCase();
  const matches = available.filter(chapter => [chapter.title, chapter.guide.purpose, ...chapter.guide.steps.map(x => x.text), ...chapter.guide.notes.map(x => x.text), ...chapter.guide.questions.flatMap(x => [x.question, x.answer]), ...(chapter.guide.sections ?? []).map(helpSectionText)].join(' ').toLocaleLowerCase().includes(term));
  const current = matches.find(chapter => chapter.id === selected) ?? matches[0];
  function choose(id: HelpPage) {
    setSelected(id);
    article.current?.focus({ preventScroll: true });
    article.current?.scrollIntoView({ behavior: 'auto', block: 'start' });
  }
  return <div className="manual-page">
    <div className="page-intro"><div><h2>使用说明</h2><p>按章节查阅，或搜索关键词。内容与右下角“本页说明”保持一致。</p></div></div>
    <label className="manual-search"><Search size={18} aria-hidden="true"/><input type="search" aria-label="搜索使用说明" value={query} onChange={e => setQuery(e.target.value)} placeholder="搜索操作、权限或常见问题"/></label>
    <p className="muted" role="status">{term ? `找到 ${matches.length} 个相关章节` : `${matches.length} 个可阅读章节`}</p>
    <div className="guide-layout">
      <nav className="guide-steps manual-toc" aria-label="说明书目录">{matches.map(chapter => <button type="button" key={chapter.id} aria-current={chapter.id === current?.id ? 'page' : undefined} onClick={() => choose(chapter.id)}>{chapter.title}</button>)}</nav>
      {current ? <article ref={article} className="guide-article manual-article" tabIndex={-1} aria-labelledby="manual-chapter-title">
        <div className="manual-article-title"><h3 id="manual-chapter-title">{current.title}</h3>{current.page && current.page !== 'manual' && <button className="secondary" type="button" onClick={() => navigate(current.page!)}>前往页面<ArrowRight size={15}/></button>}</div>
        <section><h4>本页能做什么</h4><p>{current.guide.purpose}</p></section>
        <HelpSections key={`${current.id}:${!!term}`} sections={current.guide.sections} search={term}/>
        <section><h4>操作步骤</h4><ol>{current.guide.steps.map(item => <li key={item.text}>{item.text}</li>)}</ol></section>
        <section><h4>注意事项</h4><ul>{current.guide.notes.map(item => <li key={item.text}>{item.text}</li>)}</ul></section>
        <section><h4>常见问题</h4><dl>{current.guide.questions.map(item => <div key={item.question}><dt>{item.question}</dt><dd>{item.answer}</dd></div>)}</dl></section>
      
      </article> : <div className="guide-article"><Empty text="没有匹配的说明" description="试试更短的关键词，或清空搜索查看全部章节。"/><button type="button" className="secondary" onClick={() => setQuery('')}>清空搜索</button></div>}
    </div>
  </div>;
}
