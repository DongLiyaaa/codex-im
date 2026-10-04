import { useEffect, useState } from 'react';
import type { Page } from './Management';
import type { User } from './api';
export type Route = { page: Page; conversationId: string | null };
const pages = new Set(['directory', 'overview', 'chat', 'users', 'groups', 'resources', 'bindings', 'integrations', 'audit']);
export function readRoute(hash = window.location.hash): Route {
  if (hash === '#/connections') return { page: 'chat', conversationId: null };
  const match = /^#\/([^/?]+)(?:\/([^/?]+))?$/.exec(hash);
  if (!match || !pages.has(match[1]) || (match[2] && match[1] !== 'chat')) return { page: 'overview', conversationId: null };
  try { return { page: match[1] as Page, conversationId: match[2] ? decodeURIComponent(match[2]) : null }; }
  catch { return { page: 'overview', conversationId: null }; }
}
export function routeHash(route: Route) { return `#/${route.page}${route.page === 'chat' && route.conversationId ? '/' + encodeURIComponent(route.conversationId) : ''}`; }
export function canAccessPage(user: User, page: Page) { return user.active && (page !== 'directory' || ['super_admin', 'org_admin'].includes(user.role)) && (page !== 'audit' || user.role !== 'member'); }
export function useRoute() {
  const [route, setRoute] = useState(readRoute);
  useEffect(() => {
    const update = () => { const next = readRoute(); if (window.location.hash !== routeHash(next)) window.history.replaceState(null, '', routeHash(next)); setRoute(next); };
    update(); window.addEventListener('hashchange', update); window.addEventListener('popstate', update);
    return () => { window.removeEventListener('hashchange', update); window.removeEventListener('popstate', update); };
  }, []);
  function navigate(next: Route, replace = false) {
    const hash = routeHash(next);
    if (window.location.hash !== hash) window.history[replace ? 'replaceState' : 'pushState'](null, '', hash);
    setRoute(next);
  }
  return { route, navigate };
}
