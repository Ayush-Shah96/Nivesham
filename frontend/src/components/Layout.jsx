import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Link, Navigate, NavLink, useLocation, useNavigate } from 'react-router-dom';
import {
  Activity,
  Bell,
  BookOpen,
  BriefcaseBusiness,
  CalendarDays,
  ChevronDown,
  FileText,
  Landmark,
  Layers3,
  LogOut,
  Menu,
  Moon,
  Send,
  Settings,
  ShieldCheck,
  Sun,
  X,
  Youtube,
} from 'lucide-react';
import { api, clearToken, warmCaches } from '../lib/api';
import { applyTheme, getCurrentTheme, getEffectiveTheme } from '../lib/theme';
import { firstAllowedAppPath, isPathAllowed, permissionsForUser, readAppAccessStore } from '../lib/appAccess';
import Brand from './Brand';

// Navigation is grouped the way a research desk thinks about its work.
// Visibility still comes from the per-user app access settings.
const GROUPS = [
  {
    id: 'markets',
    label: 'Markets',
    items: [
      { to: '/sector-screener', label: 'Sector Screener', desc: 'Sector-wise industry research', icon: Layers3 },
      { to: '/earnings-calendar', label: 'Earnings Calendar', desc: 'Results dates, exportable to Excel', icon: CalendarDays },
    ],
  },
  {
    id: 'research',
    label: 'Research',
    items: [
      { to: '/news-alert', label: 'NewsAlert Desk', desc: 'Classified company and market news', icon: Bell },
      { to: '/regulatory-signals', label: 'Regulatory Activity', desc: 'Filings and insider activity', icon: ShieldCheck },
      { to: '/youtube-intelligence', label: 'YouTube Intelligence', desc: 'Commentary from tracked channels', icon: Youtube },
      { to: '/crisp-notes', label: 'CRISP Notes', desc: 'Investor call notes and summaries', icon: FileText },
    ],
  },
  {
    id: 'portfolio',
    label: 'Portfolio',
    items: [
      { to: '/debt-tracker', label: 'Debt Tracker', desc: 'Consolidated debt exposure', icon: Landmark },
      { to: '/portfolio-comparison', label: 'Portfolio Comparison', desc: 'Compare portfolios side by side', icon: BriefcaseBusiness },
      { to: '/coverage-list', label: 'Coverage List', desc: 'Companies under coverage', icon: BookOpen },
      { to: '/ops-tracker', label: 'Ops Tracker', desc: 'Operational links and tracking', icon: Send },
    ],
  },
];

const topLinkClass = ({ isActive }) =>
  `rounded-lg px-3 py-2 text-sm font-semibold transition ${isActive ? 'bg-navy-900 text-white' : 'text-slate-600 hover:bg-slate-100 hover:text-navy-900'}`;

const menuItemClass = ({ isActive }) =>
  `flex gap-3 rounded-xl p-3 transition ${isActive ? 'bg-amber-50' : 'hover:bg-slate-50'}`;

export default function Layout({ children, user, setUser }) {
  const navigate = useNavigate();
  const location = useLocation();
  const headerRef = useRef(null);

  const [openMenu, setOpenMenu] = useState(null); // 'user' | group id | null
  const [mobileNav, setMobileNav] = useState(false);
  const [theme, setTheme] = useState(() => getCurrentTheme());
  const isDarkTheme = ['dark', 'slate'].includes(getEffectiveTheme(theme));
  const [accessVersion, setAccessVersion] = useState(0);

  useEffect(() => {
    const fn = () => {
      setUser(null);
      navigate('/login');
    };
    window.addEventListener('auth-expired', fn);
    return () => window.removeEventListener('auth-expired', fn);
  }, [navigate, setUser]);

  useEffect(() => {
    const onTheme = (event) => setTheme(event?.detail?.theme || getCurrentTheme());
    window.addEventListener('chemnine-theme-updated', onTheme);
    return () => window.removeEventListener('chemnine-theme-updated', onTheme);
  }, []);

  useEffect(() => {
    const syncAccess = () => setAccessVersion((value) => value + 1);
    window.addEventListener('storage', syncAccess);
    window.addEventListener('app-access-updated', syncAccess);
    return () => {
      window.removeEventListener('storage', syncAccess);
      window.removeEventListener('app-access-updated', syncAccess);
    };
  }, []);

  // Close open menus on navigation and on outside click / Escape.
  useEffect(() => {
    setOpenMenu(null);
    setMobileNav(false);
  }, [location.pathname]);

  useEffect(() => {
    const onDown = (event) => {
      if (headerRef.current && !headerRef.current.contains(event.target)) setOpenMenu(null);
    };
    const onKey = (event) => {
      if (event.key === 'Escape') {
        setOpenMenu(null);
        setMobileNav(false);
      }
    };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, []);

  const accessStore = useMemo(() => readAppAccessStore(), [accessVersion]);
  const currentPermissions = useMemo(() => permissionsForUser(user?.id, accessStore), [user?.id, accessStore]);
  const currentPathAllowed = isPathAllowed(location.pathname, currentPermissions);

  const visibleGroups = useMemo(
    () =>
      GROUPS.map((group) => ({
        ...group,
        items: group.items.filter((item) => isPathAllowed(item.to, currentPermissions)),
      })).filter((group) => group.items.length > 0),
    [currentPermissions],
  );
  const dashboardVisible = isPathAllowed('/dashboard', currentPermissions);

  useEffect(() => {
    if (!user?.id || typeof window === 'undefined') return undefined;
    // Only warm worker/snapshot-backed endpoints that the dashboard uses.
    const warmupPaths = ['/regulatory-signals?limit=100&offset=0'];
    const timer = window.setTimeout(() => {
      void warmCaches(warmupPaths);
    }, 900);
    return () => window.clearTimeout(timer);
  }, [user?.id]);

  const setThemeMode = (mode) => setTheme(applyTheme(mode));

  const logout = async () => {
    // Close the server-side session when reachable; local sign-out must always work.
    try {
      await api.logout();
    } catch {
      // Best effort only.
    } finally {
      clearToken();
      setUser(null);
      navigate('/login');
    }
  };

  if (user?.id && !currentPathAllowed) {
    return <Navigate to={firstAllowedAppPath(currentPermissions)} replace />;
  }

  const initial = (user?.username || 'U').charAt(0).toUpperCase();

  return (
    <div className="flex min-h-screen flex-col bg-[var(--app-bg)] text-navy-900">
      <header ref={headerRef} className="sticky top-0 z-30 border-b border-slate-200/80 bg-white/95 backdrop-blur">
        <div className="mx-auto flex h-16 max-w-[1400px] items-center gap-4 px-4 sm:px-6 lg:gap-8 lg:px-8">
          <button
            type="button"
            onClick={() => setMobileNav(true)}
            className="grid h-10 w-10 place-items-center rounded-lg text-slate-700 hover:bg-slate-100 lg:hidden"
            aria-label="Open navigation"
          >
            <Menu size={20} />
          </button>

          <Link to="/dashboard" className="shrink-0" aria-label="NIVESHAM home">
            <Brand compact />
          </Link>

          <nav className="hidden min-w-0 items-center gap-1 lg:flex" aria-label="Primary">
            {dashboardVisible && (
              <NavLink to="/dashboard" className={topLinkClass}>Dashboard</NavLink>
            )}

            {visibleGroups.map((group) => {
              const isOpen = openMenu === group.id;
              const groupActive = group.items.some((item) => location.pathname.startsWith(item.to));
              return (
                <div key={group.id} className="relative">
                  <button
                    type="button"
                    onClick={() => setOpenMenu(isOpen ? null : group.id)}
                    aria-expanded={isOpen}
                    className={`inline-flex items-center gap-1 rounded-lg px-3 py-2 text-sm font-semibold transition ${groupActive ? 'text-navy-900' : 'text-slate-600 hover:bg-slate-100 hover:text-navy-900'}`}
                  >
                    {group.label}
                    <ChevronDown size={15} className={`transition ${isOpen ? 'rotate-180' : ''}`} />
                  </button>

                  {isOpen && (
                    <div className="absolute left-0 top-full z-40 mt-2 w-[320px] rounded-2xl border border-slate-200 bg-white p-2 shadow-[0_24px_60px_-20px_rgba(11,31,58,.25)]">
                      {group.items.map(({ to, label, desc, icon: Icon }) => (
                        <NavLink key={to} to={to} className={menuItemClass}>
                          <span className="mt-0.5 grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-navy-900/5 text-navy-800">
                            <Icon size={17} />
                          </span>
                          <span className="min-w-0">
                            <span className="block text-sm font-semibold text-navy-900">{label}</span>
                            <span className="mt-0.5 block text-xs leading-snug text-slate-500">{desc}</span>
                          </span>
                        </NavLink>
                      ))}
                    </div>
                  )}
                </div>
              );
            })}
          </nav>

          <div className="ml-auto flex items-center gap-2">
            <div className="relative">
              <button
                type="button"
                onClick={() => setOpenMenu(openMenu === 'user' ? null : 'user')}
                aria-expanded={openMenu === 'user'}
                aria-label="Account menu"
                className="flex items-center gap-2 rounded-full border border-slate-200 py-1 pl-1 pr-3 text-sm font-semibold text-slate-700 hover:bg-slate-50"
              >
                <span className="grid h-8 w-8 place-items-center rounded-full bg-navy-900 text-xs font-bold text-white">{initial}</span>
                <span className="hidden max-w-[120px] truncate md:inline">{user?.username || 'Account'}</span>
                <ChevronDown size={15} className="text-slate-400" />
              </button>

              {openMenu === 'user' && (
                <div className="absolute right-0 top-full z-40 mt-2 w-[240px] rounded-2xl border border-slate-200 bg-white p-2 shadow-[0_24px_60px_-20px_rgba(11,31,58,.25)]">
                  <div className="px-3 py-2.5">
                    <div className="truncate text-sm font-bold text-navy-900">{user?.username}</div>
                    <div className="text-xs text-slate-500">{user?.role === 'ADMIN' ? 'Administrator' : 'Analyst'}</div>
                  </div>
                  <div className="my-1 h-px bg-slate-100" />
                  <NavLink to="/settings" className={menuItemClass}>
                    <Settings size={17} className="mt-0.5 text-navy-800" />
                    <span className="text-sm font-semibold text-navy-900">Account settings</span>
                  </NavLink>
                  <div className="px-3 pb-2 pt-3">
                    <div className="mb-2 text-[11px] font-bold uppercase tracking-wider text-slate-500">Appearance</div>
                    <div className="grid grid-cols-2 gap-1 rounded-xl bg-slate-100 p-1">
                      <button
                        type="button"
                        onClick={() => setThemeMode('light')}
                        aria-pressed={!isDarkTheme}
                        className={`flex items-center justify-center gap-1.5 rounded-lg py-1.5 text-xs font-semibold ${!isDarkTheme ? 'bg-white text-navy-900 shadow-sm' : 'text-slate-600'}`}
                      >
                        <Sun size={14} /> Light
                      </button>
                      <button
                        type="button"
                        onClick={() => setThemeMode('dark')}
                        aria-pressed={isDarkTheme}
                        className={`flex items-center justify-center gap-1.5 rounded-lg py-1.5 text-xs font-semibold ${isDarkTheme ? 'bg-navy-900 text-white shadow-sm' : 'text-slate-600'}`}
                      >
                        <Moon size={14} /> Dark
                      </button>
                    </div>
                  </div>
                  <div className="my-1 h-px bg-slate-100" />
                  <button
                    type="button"
                    onClick={logout}
                    className="flex w-full items-center gap-3 rounded-xl p-3 text-left text-sm font-semibold text-red-600 hover:bg-red-50"
                  >
                    <LogOut size={17} /> Sign out
                  </button>
                </div>
              )}
            </div>
          </div>
        </div>
      </header>

      <main className="flex-1">
        <div className="page-enter mx-auto w-full max-w-[1400px] px-4 pb-16 pt-8 sm:px-6 lg:px-8 lg:pt-10">
          {children}
        </div>
      </main>

      <footer className="border-t border-slate-200 bg-white">
        <div className="mx-auto flex max-w-[1400px] flex-col gap-4 px-4 py-6 text-xs text-slate-500 sm:px-6 md:flex-row md:items-center md:justify-between lg:px-8">
          <Brand compact />
          <span>© {new Date().getFullYear()} NIVESHAM. All rights reserved.</span>
        </div>
      </footer>

      {/* MOBILE NAVIGATION */}
      {mobileNav && (
        <>
          <div className="fixed inset-0 z-40 bg-navy-950/40" onClick={() => setMobileNav(false)} />
          <aside className="fixed left-0 top-0 z-50 flex h-full w-[300px] max-w-[88vw] flex-col bg-white shadow-2xl">
            <div className="flex h-16 items-center justify-between border-b border-slate-200 px-4">
              <Brand compact />
              <button type="button" onClick={() => setMobileNav(false)} className="grid h-9 w-9 place-items-center rounded-lg hover:bg-slate-100" aria-label="Close navigation">
                <X size={20} />
              </button>
            </div>

            <nav className="flex-1 overflow-y-auto p-3" aria-label="Mobile">
              {dashboardVisible && (
                <NavLink to="/dashboard" className={({ isActive }) => `mb-2 block rounded-xl px-3 py-3 text-sm font-semibold ${isActive ? 'bg-navy-900 text-white' : 'text-navy-900 hover:bg-slate-50'}`}>
                  <span className="inline-flex items-center gap-2"><Activity size={17} /> Dashboard</span>
                </NavLink>
              )}

              {visibleGroups.map((group) => (
                <div key={group.id} className="mb-3">
                  <div className="px-3 pb-1 pt-3 text-[11px] font-bold uppercase tracking-[.16em] text-slate-500">{group.label}</div>
                  {group.items.map(({ to, label, icon: Icon }) => (
                    <NavLink key={to} to={to} className={({ isActive }) => `flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium ${isActive ? 'bg-amber-50 font-semibold text-navy-900' : 'text-slate-700 hover:bg-slate-50'}`}>
                      <Icon size={17} className="text-navy-800" />
                      {label}
                    </NavLink>
                  ))}
                </div>
              ))}
            </nav>

            <div className="border-t border-slate-200 p-3">
              <NavLink to="/settings" className="flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium text-slate-700 hover:bg-slate-50">
                <Settings size={17} /> Account settings
              </NavLink>
              <button type="button" onClick={logout} className="mt-1 flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left text-sm font-medium text-red-600 hover:bg-red-50">
                <LogOut size={17} /> Sign out
              </button>
            </div>
          </aside>
        </>
      )}
    </div>
  );
}
