import { useEffect, useId, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { BookOpen, ChevronDown, X } from 'lucide-react';
import type { Role } from './api';
import { getPageHelp } from './pageHelpContent';
import type { HelpPage } from './pageHelpContent';
import { HelpSections } from './HelpSections';

export function PageHelp({ pageId, title, role, suspended = false }: { pageId: HelpPage; title: string; role?: Role; suspended?: boolean }) {
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLElement>(null);
  const id = useId();
  const titleId = `${id}-title`;
  const guide = getPageHelp(pageId, role);
  function collapse() { setOpen(false); trigger.current?.focus({ preventScroll: true }); }
  useEffect(() => {
    if (!open || suspended) return;
    panel.current?.focus({ preventScroll: true });
    function escape(event: KeyboardEvent) {
      if (event.key !== 'Escape' || event.defaultPrevented || document.querySelector('dialog[open]')) return;
      event.preventDefault();
      setOpen(false); trigger.current?.focus({ preventScroll: true });
    }
    document.addEventListener('keydown', escape);
    return () => document.removeEventListener('keydown', escape);
  }, [open, suspended]);
  return <div className="page-help" data-page={pageId} hidden={suspended}>
    {open && <section ref={panel} id={id} className="page-help-panel" role="dialog" aria-modal="false" aria-labelledby={titleId} tabIndex={-1}>
      <header className="page-help-heading">
        <BookOpen size={19} aria-hidden="true"/>
        <div><h2 id={titleId}>{title} · 使用说明</h2><p>跟随当前页面，随时查阅</p></div>
        <button type="button" className="icon-button" aria-label="收起本页说明" onClick={collapse}><X size={18} aria-hidden="true"/></button>
      </header>
      <div className="page-help-body" key={`${pageId}:${role ?? 'guest'}`}>
        <section><h3>本页能做什么</h3><p className="page-help-purpose">{guide.purpose}</p></section>
        <HelpSections sections={guide.sections}/>
        <section><h3>操作步骤</h3><ol className="page-help-steps">
          {guide.steps.map(step => <li key={step.text}>{step.text}</li>)}
        </ol></section>
        <details><summary>注意事项<ChevronDown size={16} aria-hidden="true"/></summary><ul className="page-help-notes">
          {guide.notes.map(note => <li key={note.text}>{note.text}</li>)}
        </ul></details>
        <details><summary>常见问题<ChevronDown size={16} aria-hidden="true"/></summary><dl className="page-help-questions">
          {guide.questions.map(item => <div key={item.question}><dt>{item.question}</dt><dd>{item.answer}</dd></div>)}
        </dl></details>
        
      </div>
      <footer className="page-help-footnote">可继续操作页面，按 Esc 收起</footer>
    </section>}
    <button ref={trigger} type="button" className="page-help-trigger" aria-label="本页说明" aria-expanded={open} aria-controls={open ? id : undefined}
      aria-haspopup="dialog" onClick={() => open ? collapse() : setOpen(true)}>
      <BookOpen size={18} aria-hidden="true"/><span>本页说明</span>
    </button>
  </div>;
}

export function PageHelpLayout({ pageId, title, identityKey, role, suspended = false, children }: {
  pageId: HelpPage; title: string; identityKey: string; role?: Role; suspended?: boolean; children: ReactNode;
}) {
  return <>
    {children}
    <PageHelp key={identityKey} pageId={pageId} title={title} role={role} suspended={suspended}/>
  </>;
}
