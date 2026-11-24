import React, { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import {
  ArrowRight, BarChart3, BookOpen, BriefcaseBusiness, CalendarDays, Check, FileText,
  Landmark, Layers3, Newspaper, Search, ShieldCheck, Sparkles, TrendingUp, Youtube, Zap,
} from 'lucide-react';
import Brand from '../components/Brand';

const SECTORS = [
  'Automotive', 'Solar', 'Pharma & Healthcare', 'Chemical', 'Semiconductors',
  'Defence', 'Commodities', 'TradeStat', 'LPG / CNG', 'GPU / Compute', 'Battery Installation',
];

const FEATURES = [
  {
    eyebrow: 'Market Pulse',
    title: 'See what is moving, and why it may be moving.',
    text: 'Sector and coverage-stock heatmaps, unusual-move signals and intraday charts, refreshed on every scan. Each move opens to related news and exchange filings.',
    points: ['Sector and index heatmaps', 'Coverage-stock movers', 'Commodity and currency reference prices'],
    icon: Zap,
  },
  {
    eyebrow: 'Sector research',
    title: 'Industry dashboards built for screening.',
    text: 'Key indicators, charts and data tables for automotive, solar, trade flows, LPG/CNG and more, with each source linked back to its origin.',
    points: ['KPI cards and trend charts', 'Sorted, searchable data tables', 'Source views per report'],
    icon: Layers3,
  },
  {
    eyebrow: 'News & filings',
    title: 'Company news and announcements in one feed.',
    text: 'Covered-company news, exchange filings and interviews, classified for what matters to fundamentals, with the original source one click away.',
    points: ['Regulatory and exchange announcements', 'Shared and personal publisher sources', 'Feed filters and source management'],
    icon: Newspaper,
  },
  {
    eyebrow: 'Earnings calendar',
    title: 'Results dates for the companies you cover.',
    text: 'A calendar of results for covered companies, with an export to Excel for your own models and team trackers.',
    points: ['Covered-company results dates', 'Export to Excel', 'Limited to approved coverage'],
    icon: CalendarDays,
  },
  {
    eyebrow: 'Portfolio & debt',
    title: 'Exposure across holdings and brokers.',
    text: 'Consolidate debt exposure from holdings and broker portfolios, and compare portfolios side by side.',
    points: ['Consolidated debt exposure', 'Portfolio comparison', 'Mapped holdings and brokers'],
    icon: Landmark,
  },
  {
    eyebrow: 'Research notes',
    title: 'Keep calls, commentary and coverage organised.',
    text: 'Investor-call notes, transcribed and summarised, alongside YouTube commentary and a shared coverage list.',
    points: ['CRISP call notes and summaries', 'YouTube intelligence', 'Coverage list and ops tracker'],
    icon: FileText,
  },
];

const MODULES = [
  { name: 'Market Pulse', area: 'Markets', what: 'Sector heatmaps, coverage movers, signals and reference prices.', icon: Zap },
  { name: 'Sector Screener', area: 'Markets', what: 'Industry dashboards with KPIs, charts and data tables.', icon: Layers3 },
  { name: 'Earnings Calendar', area: 'Markets', what: 'Results dates for covered companies, exportable to Excel.', icon: CalendarDays },
  { name: 'NewsAlert Desk', area: 'Research', what: 'Classified company and market news with source links.', icon: Newspaper },
  { name: 'Regulatory Activity', area: 'Research', what: 'Exchange filings and insider activity in a single feed.', icon: ShieldCheck },
  { name: 'YouTube Intelligence', area: 'Research', what: 'Investment commentary from selected channels.', icon: Youtube },
  { name: 'CRISP Notes', area: 'Research', what: 'Investor-call notes, transcribed and summarised.', icon: FileText },
  { name: 'Debt Tracker', area: 'Portfolio', what: 'Debt exposure consolidated across holdings and brokers.', icon: Landmark },
  { name: 'Portfolio Comparison', area: 'Portfolio', what: 'Compare portfolios side by side.', icon: BriefcaseBusiness },
  { name: 'Coverage List', area: 'Portfolio', what: 'The companies your desk covers, in one reference list.', icon: BookOpen },
  { name: 'Ops Tracker', area: 'Portfolio', what: 'Operational links and workflow tracking for the team.', icon: BarChart3 },
];

const STEPS = [
  { n: '01', title: 'Sign in', text: 'Use the account your administrator has set up for you.' },
  { n: '02', title: 'Start with Market Pulse', text: 'The dashboard shows sector and coverage moves from the latest scan.' },
  { n: '03', title: 'Drill into the research', text: 'Open a move, a sector or a filing and follow it into news, data and notes.' },
];

const FAQS = [
  {
    q: 'Where does the market data come from?',
    a: 'Quotes come from public market-data providers and are indicative. Filings and announcements link back to NSE and BSE. Each module shows its source, and provider timing is not guaranteed.',
  },
  {
    q: 'Is this investment advice?',
    a: 'No. NIVESHAM is a research workspace. Signals and summaries are prompts for your own analysis, not recommendations to buy or sell.',
  },
  {
    q: 'Which companies are covered?',
    a: 'Market Pulse follows the companies on the shared Coverage List, plus sector and index references. Your administrator controls the list.',
  },
  {
    q: 'Can I change what I see?',
    a: 'Yes. Modules you do not have access to are hidden from your menu, and you can switch between light and dark appearance from your account menu.',
  },
];

export default function Home({ user }) {
  const navigate = useNavigate();
  const [query, setQuery] = useState('');
  const primaryTo = user ? '/dashboard' : '/login';
  const primaryLabel = user ? 'Open Market Pulse' : 'Sign in to research';

  const onSearch = (event) => {
    event.preventDefault();
    navigate(primaryTo);
  };

  return (
    <div className="site-light min-h-screen bg-[#f7f8fb] font-sans text-[#0f1b2d]">
      {/* Top bar */}
      <header className="sticky top-0 z-30 border-b border-[#dfe4ec] bg-white/95 backdrop-blur">
        <div className="mx-auto flex max-w-[1240px] items-center gap-6 px-5 py-3.5 sm:px-8">
          <Link to="/" aria-label="NIVESHAM home"><Brand /></Link>
          <nav className="hidden items-center gap-6 text-sm font-semibold text-[#4b5869] lg:flex" aria-label="Sections">
            <a href="#features" className="hover:text-[#0b1f3a]">Features</a>
            <a href="#sectors" className="hover:text-[#0b1f3a]">Sectors</a>
            <a href="#modules" className="hover:text-[#0b1f3a]">Modules</a>
            <a href="#workflow" className="hover:text-[#0b1f3a]">Workflow</a>
            <a href="#faq" className="hover:text-[#0b1f3a]">FAQ</a>
          </nav>
          <div className="ml-auto flex items-center gap-2">
            {!user && <Link to="/login" className="rounded-lg px-3 py-2 text-sm font-semibold text-[#0b1f3a] hover:bg-[#eef2f7]">Sign in</Link>}
            <Link to={primaryTo} className="rounded-lg bg-[#0b1f3a] px-4 py-2 text-sm font-semibold text-white shadow-sm transition hover:bg-[#14304f]">
              {user ? 'Open workspace' : 'Get started'}
            </Link>
          </div>
        </div>
      </header>

      {/* Hero: search-led, like a research terminal homepage */}
      <section className="border-b border-[#dfe4ec] bg-white">
        <div className="mx-auto grid max-w-[1240px] items-center gap-12 px-5 pb-16 pt-14 sm:px-8 md:pt-20 lg:grid-cols-[1.05fr_.95fr]">
          <div>
            <div className="inline-flex items-center gap-2 rounded-full bg-[#fdf6e7] px-3 py-1.5 text-xs font-bold text-[#8a6320]">
              <Sparkles size={13} /> Stock and sector research platform
            </div>
            <h1 className="font-display mt-5 text-[2.6rem] font-bold leading-[1.07] tracking-tight text-[#0b1f3a] sm:text-[3.3rem]">
              Research companies, sectors and filings in one place.
            </h1>
            <p className="mt-5 max-w-xl text-[17px] leading-relaxed text-[#4b5869]">
              NIVESHAM brings market pulse, sector dashboards, exchange filings, news and portfolio exposure into a single workspace built for research teams.
            </p>

            <form onSubmit={onSearch} className="mt-8 flex max-w-xl items-center gap-2 rounded-2xl border border-[#cfd6e1] bg-white p-2 shadow-[0_12px_30px_-18px_rgba(11,31,58,.35)] focus-within:border-[#0b1f3a]">
              <Search size={18} className="ml-2 shrink-0 text-[#6b7689]" />
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search companies, sectors or filings"
                aria-label="Search companies, sectors or filings"
                className="min-w-0 flex-1 bg-transparent px-1 py-2 text-[15px] text-[#0f1b2d] outline-none placeholder:text-[#8a94a6]"
              />
              <button type="submit" className="inline-flex shrink-0 items-center gap-2 rounded-xl bg-[#0b1f3a] px-4 py-2.5 text-sm font-semibold text-white transition hover:bg-[#14304f]">
                {primaryLabel} <ArrowRight size={16} />
              </button>
            </form>

            <div className="mt-6 flex flex-wrap gap-2 text-xs font-semibold text-[#4b5869]">
              {['Exchange filings', 'Sector dashboards', 'Earnings calendar', 'Coverage list', 'Investor-call notes'].map((chip) => (
                <span key={chip} className="rounded-md border border-[#dfe4ec] bg-[#f7f8fb] px-2.5 py-1.5">{chip}</span>
              ))}
            </div>
          </div>

          {/* Product preview: structure only, no figures */}
          <div aria-hidden="true" className="rounded-2xl border border-[#dfe4ec] bg-[#f7f8fb] p-5 shadow-[0_30px_60px_-30px_rgba(11,31,58,.25)]">
            <div className="mb-4 flex items-center justify-between">
              <div className="flex items-center gap-2 text-xs font-bold uppercase tracking-[.14em] text-[#0b1f3a]"><Zap size={14} className="text-[#b07f1f]" /> Market Pulse</div>
              <span className="rounded-md bg-white px-2 py-1 text-[10px] font-semibold text-[#6b7689] ring-1 ring-[#dfe4ec]">Preview</span>
            </div>
            <div className="grid grid-cols-3 gap-2">
              {['Automotive', 'Banking', 'IT', 'Pharma', 'Metals', 'Energy', 'FMCG', 'Realty', 'Finance'].map((name, i) => (
                <div key={name} className={`rounded-lg px-2.5 py-3 text-[11px] font-semibold text-[#0b1f3a] ${['bg-[#dcefe6]', 'bg-[#f7dfe3]', 'bg-[#e9edf3]'][i % 3]}`}>{name}</div>
              ))}
            </div>
            <div className="mt-4 space-y-2">
              {['Coverage movers', 'Exchange filings', 'Related news'].map((label) => (
                <div key={label} className="flex items-center justify-between rounded-lg bg-white px-3 py-2.5 text-[12px] font-semibold text-[#0b1f3a] ring-1 ring-[#e7ebf1]">
                  <span>{label}</span>
                  <span className="h-2 w-16 rounded-full bg-[#e7ebf1]" />
                </div>
              ))}
            </div>
          </div>
        </div>
      </section>

      {/* Sector coverage strip */}
      <section id="sectors" className="scroll-mt-20 border-b border-[#dfe4ec] bg-[#f7f8fb]">
        <div className="mx-auto max-w-[1240px] px-5 py-8 sm:px-8">
          <div className="text-xs font-bold uppercase tracking-[.16em] text-[#6b7689]">Sectors and industries covered</div>
          <div className="mt-3 flex flex-wrap gap-2">
            {SECTORS.map((s) => (
              <span key={s} className="rounded-full border border-[#dfe4ec] bg-white px-3.5 py-1.5 text-[13px] font-semibold text-[#0b1f3a]">{s}</span>
            ))}
          </div>
        </div>
      </section>

      {/* Feature sections */}
      <section id="features" className="scroll-mt-20 mx-auto max-w-[1240px] space-y-6 px-5 py-20 sm:px-8">
        <div className="max-w-2xl">
          <div className="text-xs font-bold uppercase tracking-[.16em] text-[#8a6320]">Platform</div>
          <h2 className="font-display mt-3 text-4xl font-bold tracking-tight text-[#0b1f3a]">A research workflow, not a pile of tabs.</h2>
          <p className="mt-4 text-lg text-[#4b5869]">Move from a market move to the filing, the news and the sector data behind it.</p>
        </div>
        <div className="mt-10 grid gap-5 md:grid-cols-2">
          {FEATURES.map(({ eyebrow, title, text, points, icon: Icon }) => (
            <article key={title} className="rounded-2xl border border-[#dfe4ec] bg-white p-7 shadow-[0_1px_2px_rgba(15,27,45,.04)]">
              <div className="flex items-center gap-3">
                <span className="grid h-10 w-10 place-items-center rounded-xl bg-[#0b1f3a] text-[#e0b25a]"><Icon size={18} /></span>
                <span className="text-xs font-bold uppercase tracking-[.14em] text-[#8a6320]">{eyebrow}</span>
              </div>
              <h3 className="font-display mt-5 text-2xl font-bold text-[#0b1f3a]">{title}</h3>
              <p className="mt-3 text-[15px] leading-relaxed text-[#4b5869]">{text}</p>
              <ul className="mt-5 space-y-2 border-t border-[#eef1f5] pt-5">
                {points.map((p) => (
                  <li key={p} className="flex items-start gap-2 text-sm text-[#2b3649]"><Check size={15} className="mt-0.5 shrink-0 text-[#2e7d5b]" />{p}</li>
                ))}
              </ul>
            </article>
          ))}
        </div>
      </section>

      {/* Module directory: table format, like a screener listing */}
      <section id="modules" className="scroll-mt-20 border-y border-[#dfe4ec] bg-white">
        <div className="mx-auto max-w-[1240px] px-5 py-20 sm:px-8">
          <div className="max-w-2xl">
            <div className="text-xs font-bold uppercase tracking-[.16em] text-[#8a6320]">Module directory</div>
            <h2 className="font-display mt-3 text-4xl font-bold tracking-tight text-[#0b1f3a]">Every module, in one list</h2>
            <p className="mt-4 text-lg text-[#4b5869]">You see only the modules your administrator has enabled for your account.</p>
          </div>
          <div className="mt-10 overflow-x-auto rounded-2xl border border-[#dfe4ec]">
            <table className="w-full min-w-[640px] text-left text-sm">
              <thead className="bg-[#f7f8fb] text-[11px] font-bold uppercase tracking-wider text-[#6b7689]">
                <tr>
                  <th className="px-5 py-3">Module</th>
                  <th className="px-5 py-3">Area</th>
                  <th className="px-5 py-3">What you get</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[#eef1f5]">
                {MODULES.map(({ name, area, what, icon: Icon }) => (
                  <tr key={name} className="hover:bg-[#fbfaf7]">
                    <td className="px-5 py-4">
                      <span className="flex items-center gap-3 font-semibold text-[#0b1f3a]"><Icon size={16} className="text-[#8a6320]" />{name}</span>
                    </td>
                    <td className="px-5 py-4 text-[#4b5869]">{area}</td>
                    <td className="px-5 py-4 text-[#4b5869]">{what}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </section>

      {/* Workflow */}
      <section id="workflow" className="scroll-mt-20 bg-[#0b1f3a] text-white">
        <div className="mx-auto max-w-[1240px] px-5 py-20 sm:px-8">
          <div className="max-w-2xl">
            <div className="text-xs font-bold uppercase tracking-[.16em] text-[#e0b25a]">Workflow</div>
            <h2 className="font-display mt-3 text-4xl font-bold tracking-tight">From first login to first insight</h2>
          </div>
          <div className="mt-12 grid gap-5 md:grid-cols-3">
            {STEPS.map(({ n, title, text }) => (
              <div key={n} className="rounded-2xl border border-white/10 bg-white/[0.04] p-7">
                <div className="font-display text-sm font-bold text-[#e0b25a]">{n}</div>
                <h3 className="font-display mt-4 text-xl font-bold">{title}</h3>
                <p className="mt-3 text-[15px] leading-relaxed text-white/70">{text}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* FAQ */}
      <section id="faq" className="scroll-mt-20 mx-auto max-w-[1240px] px-5 py-20 sm:px-8">
        <div className="max-w-[900px]">
        <div className="text-xs font-bold uppercase tracking-[.16em] text-[#8a6320]">FAQ</div>
        <h2 className="font-display mt-3 text-4xl font-bold tracking-tight text-[#0b1f3a]">Common questions</h2>
        <div className="mt-8 divide-y divide-[#dfe4ec] rounded-2xl border border-[#dfe4ec] bg-white">
          {FAQS.map(({ q, a }) => (
            <details key={q} className="group p-5">
              <summary className="flex cursor-pointer list-none items-center justify-between gap-4 text-[15px] font-semibold text-[#0b1f3a]">
                {q}
                <span className="text-[#8a6320] transition group-open:rotate-45 text-xl leading-none">+</span>
              </summary>
              <p className="mt-3 text-[15px] leading-relaxed text-[#4b5869]">{a}</p>
            </details>
          ))}
        </div>
        </div>
      </section>

      {/* Research disclaimer band */}
      <section className="mx-auto max-w-[1240px] px-5 pb-20 sm:px-8">
        <div className="flex flex-col items-start gap-6 rounded-3xl border border-[#e7d9b5] bg-[#fdf8ec] p-8 md:flex-row md:items-center md:justify-between md:p-10">
          <div>
            <h2 className="font-display text-2xl font-bold text-[#0b1f3a]">Research tool, not investment advice</h2>
            <p className="mt-2 max-w-2xl text-[15px] leading-relaxed text-[#5b5240]">Signals, summaries and quotes are indicative and may be delayed. Verify material information against primary filings before you act on it.</p>
          </div>
          <Link to={primaryTo} className="inline-flex shrink-0 items-center gap-2 rounded-xl bg-[#0b1f3a] px-6 py-3 text-sm font-semibold text-white transition hover:bg-[#14304f]">
            {primaryLabel} <ArrowRight size={16} />
          </Link>
        </div>
      </section>

      <footer className="border-t border-[#dfe4ec] bg-white">
        <div className="mx-auto flex max-w-[1240px] flex-col gap-4 px-5 py-8 text-xs text-[#6b7689] sm:px-8 md:flex-row md:items-center md:justify-between">
          <Brand compact />
          <span>© {new Date().getFullYear()} NIVESHAM. Investment intelligence workspace.</span>
        </div>
      </footer>
    </div>
  );
}
