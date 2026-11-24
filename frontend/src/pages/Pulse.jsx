import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity, ArrowDownRight, ArrowUpRight, BarChart3, CalendarClock,
  ExternalLink, FileText, Fuel, LoaderCircle, Newspaper, RefreshCw, Search,
  ShieldCheck, Sparkles, X, Zap, DollarSign, TrendingUp, TrendingDown,
} from 'lucide-react';
import {
  CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts';
import { request } from '../lib/api';

const REFRESH_MS = 60_000;
const fmt = (value, digits = 2) => value == null || !Number.isFinite(Number(value))
  ? '—'
  : Number(value).toLocaleString('en-IN', { maximumFractionDigits: digits, minimumFractionDigits: digits });
const pct = (value) => value == null || !Number.isFinite(Number(value)) ? '—' : `${Number(value) > 0 ? '+' : ''}${fmt(value)}%`;
const time = (value) => {
  if (!value) return 'Time unavailable';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('en-IN', { dateStyle: 'medium', timeStyle: 'short' });
};
const movement = (item) => item?.change_5m_pct ?? item?.change_15m_pct ?? item?.change_pct ?? null;
const tone = (value) => Number(value || 0) >= 0 ? 'text-emerald-700' : 'text-rose-700';
const badgeTone = (value) => Number(value || 0) >= 0 ? 'bg-emerald-500/10 text-emerald-700 ring-emerald-600/15' : 'bg-rose-500/10 text-rose-700 ring-rose-600/15';

function heatColor(value) {
  if (value == null || !Number.isFinite(Number(value))) return 'rgba(100,116,139,.10)';
  const strength = Math.min(Math.abs(Number(value)), 4) / 4;
  const alpha = 0.10 + strength * 0.48;
  return Number(value) >= 0 ? `rgba(5,150,105,${alpha})` : `rgba(225,29,72,${alpha})`;
}

function HeatCell({ item, onClick, dense = false }) {
  const value = item.change_pct ?? movement(item);
  const hasValue = value != null && Number.isFinite(Number(value));
  const dayValue = item.change_pct;
  return <button
    type="button"
    onClick={() => onClick(item)}
    title={`${item.name || item.symbol}: ${pct(value)}. Open investigation.`}
    className={`group relative flex min-h-[72px] min-w-0 flex-col justify-between overflow-hidden rounded-xl border border-white/70 p-2.5 text-left shadow-sm transition duration-150 hover:-translate-y-0.5 hover:shadow-md focus:outline-none focus:ring-2 focus:ring-blue-500 ${dense ? 'min-h-[64px]' : ''}`}
    style={{ background: heatColor(value) }}
  >
    <div className="flex w-full items-start justify-between gap-1">
      <span className="line-clamp-2 text-[10px] font-bold leading-4 text-slate-900">{item.name || item.symbol}</span>
      <span className="shrink-0 rounded-md bg-white/75 p-1 text-slate-700 opacity-0 transition group-hover:opacity-100"><ExternalLink size={10}/></span>
    </div>
    <div className="mt-2 flex items-end justify-between gap-1">
      <span className={`text-[13px] font-bold tabular-nums ${hasValue ? tone(value) : 'text-slate-500'}`}>{pct(value)}</span>
      <span className="truncate text-[8px] font-medium text-slate-600">{item.symbol}</span>
    </div>
    {item.data_type === 'end_of_day' ? <span className="mt-1 text-[8px] font-semibold text-slate-600">EOD close</span> : dayValue != null && item.change_5m_pct != null && <span className="mt-1 text-[8px] text-slate-600">5m {pct(item.change_5m_pct)}</span>}
  </button>;
}

function SignalRow({ item, onClick }) {
  const change = movement(item);
  const Up = Number(change || 0) >= 0 ? ArrowUpRight : ArrowDownRight;
  return <button type="button" onClick={() => onClick(item)} className="group flex w-full items-center gap-3 rounded-xl border border-slate-200/80 bg-white p-3 text-left transition hover:border-blue-300 hover:bg-blue-50/30">
    <span className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-xl ${badgeTone(change)}`}><Up size={17}/></span>
    <span className="min-w-0 flex-1">
      <span className="flex flex-wrap items-center gap-2"><span className="truncate text-[12px] font-bold text-slate-900">{item.name || item.symbol}</span><span className="rounded-md bg-slate-100 px-1.5 py-0.5 text-[8px] font-bold uppercase tracking-wide text-slate-500">{item.kind}</span></span>
      <span className="mt-1 block truncate text-[9px] text-slate-500">{item.symbol}{item.sector ? ` · ${item.sector}` : ''} · {(item.reasons || []).join(' · ') || 'Unusual movement detected'}</span>
    </span>
    <span className="shrink-0 text-right"><span className={`block text-[13px] font-bold tabular-nums ${tone(change)}`}>{pct(change)}</span><span className="mt-1 block text-[8px] text-slate-400">{item.volume_ratio ? `${fmt(item.volume_ratio, 1)}x volume` : time(item.last_seen || item.timestamp)}</span></span>
  </button>;
}

function LinkCard({ item }) {
  const href = item.url || item.source_url;
  if (!href) return null;
  return <a href={href} target="_blank" rel="noopener noreferrer" className="group flex gap-3 rounded-xl border border-slate-200 bg-white p-3 transition hover:border-blue-300 hover:bg-blue-50/30">
    <span className={`mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg ${item.kind === 'exchange_filing' ? 'bg-amber-50 text-amber-700' : 'bg-blue-50 text-blue-700'}`}>
      {item.kind === 'exchange_filing' ? <FileText size={15}/> : <Newspaper size={15}/>}
    </span>
    <span className="min-w-0 flex-1">
      <span className="line-clamp-2 text-[11px] font-semibold leading-5 text-slate-800 group-hover:text-blue-800">{item.title || 'Open source'}</span>
      <span className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[9px] text-slate-500"><span>{item.publisher || item.source || 'External source'}</span>{item.published_at && <><span>·</span><span>{time(item.published_at)}</span></>}</span>
    </span>
    <ExternalLink size={13} className="mt-1 shrink-0 text-slate-400 group-hover:text-blue-600"/>
  </a>;
}

function InvestigationModal({ item, payload, loading, error, onClose, onChooseRelated }) {
  const dialogRef = useRef(null);
  const [sectorTab, setSectorTab] = useState('stocks');
  const [stockSearch, setStockSearch] = useState('');
  const isSector = item.kind === 'sector';

  useEffect(() => {
    const previous = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const onKey = (event) => { if (event.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    dialogRef.current?.focus();
    return () => { document.body.style.overflow = previous; window.removeEventListener('keydown', onKey); };
  }, [onClose]);

  useEffect(() => {
    setSectorTab('stocks');
    setStockSearch('');
  }, [item.identifier, item.symbol, item.kind]);

  const quote = payload?.quote || {};
  const information = payload?.information || [];
  const filings = information.filter((row) => row.kind === 'exchange_filing');
  const articles = information.filter((row) => row.kind !== 'exchange_filing' && row.query_type !== 'market_commentary');
  const editorials = information.filter((row) => row.query_type === 'market_commentary');
  const series = (payload?.series || []).map((point) => ({
    ...point,
    time: new Date(point.timestamp).toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' }),
  }));
  const query = encodeURIComponent(`${item.name || item.symbol} India stock news`);
  const quickLinks = [
    { label: 'Google News', url: `https://news.google.com/search?q=${query}&hl=en-IN&gl=IN&ceid=IN%3Aen`, icon: Newspaper },
    { label: 'NSE announcements', url: 'https://www.nseindia.com/companies-listing/corporate-filings-announcements', icon: FileText },
    { label: 'BSE announcements', url: 'https://www.bseindia.com/corporates/ann.html', icon: FileText },
    { label: 'Company search', url: `https://www.google.com/search?q=${query}`, icon: Search },
    { label: 'Investor relations', url: `https://www.google.com/search?q=${encodeURIComponent(`${item.name || item.symbol} investor relations`)}`, icon: ExternalLink },
  ];
  const sectorItems = payload?.related_stocks || [];
  const filteredSectorItems = sectorItems.filter((stock) => !stockSearch || [stock.name, stock.symbol, stock.sector, stock.exchange].join(' ').toLowerCase().includes(stockSearch.toLowerCase()));
  const isUp = Number(movement(item) || 0) >= 0;
  const stockHeatmap = <section className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm sm:p-5">
    <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
      <div>
        <div className="flex items-center gap-2"><Activity size={16} className="text-emerald-700"/><h3 className="text-[14px] font-bold text-slate-900">{item.name || item.sector} · Coverage List stocks</h3></div>
        <p className="mt-1 max-w-2xl text-[10px] leading-5 text-slate-500">Click any company tile to open its individual price chart, news, filings and movement analysis.</p>
      </div>
      <span className="rounded-full bg-blue-50 px-3 py-1.5 text-[10px] font-bold text-blue-800">{sectorItems.length} related stocks</span>
    </div>
    <div className="mb-4 flex flex-wrap items-center gap-2">
      <div className="relative min-w-[180px] flex-1"><Search size={13} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400"/><input value={stockSearch} onChange={(event) => setStockSearch(event.target.value)} placeholder="Search stock in this sector" className="w-full rounded-xl border border-slate-200 bg-slate-50 py-2.5 pl-8 pr-3 text-[10px] outline-none focus:border-blue-400 focus:bg-white"/></div>
      <span className="text-[9px] text-slate-400">Sorted by largest available movement</span>
    </div>
    {filteredSectorItems.length ? <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5">
      {filteredSectorItems.map((stock) => <div key={`${stock.exchange || 'NSE'}-${stock.symbol}`} className="relative">
        <HeatCell item={stock} onClick={() => onChooseRelated({ ...stock, kind: 'stock' })}/>
        {stock.sector_match_type === 'adjacent' && <span className="absolute bottom-2 right-2 rounded bg-white/85 px-1.5 py-0.5 text-[7px] font-bold uppercase tracking-wide text-amber-800">Adjacent</span>}
      </div>)}
    </div> : <div className="rounded-2xl border border-dashed border-slate-300 bg-slate-50 px-5 py-10 text-center">
      <Activity size={24} className="mx-auto text-slate-300"/>
      <p className="mt-2 text-[12px] font-semibold text-slate-700">No matching Coverage List stocks</p>
      <p className="mx-auto mt-1 max-w-xl text-[10px] leading-5 text-slate-500">{payload?.related_stock_note || 'There are no companies in the current Coverage List mapped to this sector. Pulse will not insert unrelated stocks.'}</p>
    </div>}
    {payload?.related_stock_note && <p className="mt-4 rounded-xl bg-slate-50 px-3 py-2.5 text-[9px] leading-5 text-slate-500">{payload.related_stock_note}</p>}
  </section>;

  const researchPanel = <div className="space-y-4">
    <div className="grid gap-4 xl:grid-cols-[minmax(0,1.05fr)_minmax(280px,.95fr)]">
      <section className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm">
        <div className="flex items-center justify-between gap-2"><div><h3 className="text-[13px] font-bold text-slate-900">{isSector ? 'Sector price chart' : 'Price movement'}</h3><p className="mt-0.5 text-[9px] text-slate-500">Intraday context from the available quote source</p></div><span className="rounded-lg bg-slate-100 px-2 py-1 text-[9px] font-semibold text-slate-500">{quote.data_type === 'end_of_day' ? 'EOD' : 'Intraday indicative'}</span></div>
        {series.length > 1 ? <div className="mt-3 h-64 w-full"><ResponsiveContainer width="100%" height="100%"><LineChart data={series} margin={{ top: 8, right: 10, left: 0, bottom: 0 }}><CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0"/><XAxis dataKey="time" tick={{ fontSize: 9, fill: '#64748b' }} minTickGap={24}/><YAxis domain={['auto', 'auto']} tick={{ fontSize: 9, fill: '#64748b' }} width={58}/><Tooltip contentStyle={{ fontSize: 10, borderRadius: 10, borderColor: '#e2e8f0' }}/><Line type="monotone" dataKey="price" stroke={isUp ? '#059669' : '#e11d48'} strokeWidth={2.5} dot={false} activeDot={{ r: 4 }}/></LineChart></ResponsiveContainer></div> : <div className="mt-3 rounded-xl bg-slate-50 p-8 text-center"><BarChart3 size={24} className="mx-auto text-slate-300"/><p className="mt-2 text-[11px] font-semibold text-slate-600">Price chart is unavailable</p><p className="mt-1 text-[9px] leading-5 text-slate-400">The quote provider did not return a usable time series. Try the linked exchange or market-source pages.</p></div>}
      </section>
      <section className="rounded-2xl border border-blue-100 bg-gradient-to-br from-blue-50 to-white p-4 shadow-sm">
        <div className="flex items-center gap-2"><span className="rounded-lg bg-blue-100 p-1.5 text-blue-700"><Sparkles size={14}/></span><h3 className="text-[13px] font-bold text-slate-900">What might explain the move?</h3></div>
        <p className="mt-3 text-[11px] leading-6 text-slate-700">{payload?.explanation?.summary || 'A movement has been detected. Review the related articles and filings below for possible catalysts.'}</p>
        <div className="mt-3 flex flex-wrap items-center gap-2"><span className="rounded-full bg-amber-100 px-2.5 py-1 text-[9px] font-bold text-amber-800">{payload?.explanation?.causality_confirmed ? 'Confirmed catalyst' : 'Cause not confirmed'}</span><span className="text-[9px] text-slate-500">Nearby coverage is evidence to investigate, not proof of causation.</span></div>
        {payload?.related_stock_count > 0 && <div className="mt-4 rounded-xl border border-blue-100 bg-white/80 p-3"><p className="text-[9px] font-bold uppercase tracking-wide text-blue-800">Coverage List context</p><p className="mt-1 text-[10px] leading-5 text-slate-600">{payload.related_stock_count} related Coverage List companies are available in the other tab. Open their individual analysis to compare stock-level moves.</p></div>}
      </section>
    </div>

    <div className="grid items-start gap-4 xl:grid-cols-2">
      <section className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm">
        <div className="mb-3 flex items-center justify-between gap-2"><div><h3 className="flex items-center gap-2 text-[12px] font-bold text-slate-900"><Newspaper size={15} className="text-blue-700"/>News & company coverage</h3><p className="mt-0.5 text-[9px] text-slate-500">Recent company and sector reporting</p></div><span className="rounded-full bg-blue-50 px-2 py-1 text-[9px] font-bold text-blue-700">{articles.length}</span></div>
        <div className="space-y-2">{articles.length ? articles.map((row, index) => <LinkCard key={`${row.url || row.title}-${index}`} item={row}/>) : <p className="rounded-xl bg-slate-50 p-4 text-center text-[10px] text-slate-500">No matching news surfaced in the current search window.</p>}</div>
      </section>
      <section className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm">
        <div className="mb-3 flex items-center justify-between gap-2"><div><h3 className="flex items-center gap-2 text-[12px] font-bold text-slate-900"><FileText size={15} className="text-violet-700"/>Blogs, articles & commentary</h3><p className="mt-0.5 text-[9px] text-slate-500">Research, outlooks, opinion and industry analysis</p></div><span className="rounded-full bg-violet-50 px-2 py-1 text-[9px] font-bold text-violet-700">{editorials.length}</span></div>
        <div className="space-y-2">{editorials.length ? editorials.map((row, index) => <LinkCard key={`${row.url || row.title}-${index}`} item={row}/>) : <p className="rounded-xl bg-slate-50 p-4 text-center text-[10px] text-slate-500">No matching blogs or commentary surfaced. Use the search links for additional coverage.</p>}</div>
      </section>
      <section className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm">
        <div className="mb-3 flex items-center justify-between gap-2"><div><h3 className="flex items-center gap-2 text-[12px] font-bold text-slate-900"><ShieldCheck size={15} className="text-amber-700"/>Exchange announcements & filings</h3><p className="mt-0.5 text-[9px] text-slate-500">Official-source leads where available</p></div><span className="rounded-full bg-amber-50 px-2 py-1 text-[9px] font-bold text-amber-800">{filings.length}</span></div>
        <div className="space-y-2">{filings.length ? filings.map((row, index) => <LinkCard key={`${row.url || row.title}-${index}`} item={row}/>) : <p className="rounded-xl bg-slate-50 p-4 text-center text-[10px] text-slate-500">No matching filing surfaced. Open the exchange search links below to check the latest disclosure directly.</p>}</div>
      </section>
      <section className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm"><h3 className="text-[12px] font-bold text-slate-900">Useful source links</h3><div className="mt-3 grid grid-cols-2 gap-2">{quickLinks.map(({ label, url, icon: Icon }) => <a key={label} href={url} target="_blank" rel="noopener noreferrer" className="flex items-center gap-2 rounded-xl border border-slate-200 px-3 py-2.5 text-[9px] font-semibold text-slate-700 hover:border-blue-300 hover:bg-blue-50"><Icon size={13} className="shrink-0 text-blue-700"/><span className="min-w-0 flex-1">{label}</span><ExternalLink size={10} className="shrink-0 text-slate-400"/></a>)}</div></section>
    </div>
  </div>;

  return <div className="fixed inset-0 z-[100] flex items-center justify-center bg-slate-950/65 p-2 backdrop-blur-sm sm:p-4" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <section ref={dialogRef} tabIndex={-1} role="dialog" aria-modal="true" aria-label={`${item.name || item.symbol} investigation`} className="flex min-h-0 flex-col overflow-hidden rounded-2xl border border-white/20 bg-slate-50 shadow-[0_30px_100px_rgba(2,6,23,.45)] outline-none" style={{ width: typeof window !== 'undefined' && window.innerWidth < 768 ? '96vw' : '75vw', maxWidth: 1360, height: '88vh' }}>
      <header className="relative shrink-0 overflow-hidden bg-slate-950 px-5 py-4 text-white sm:px-7 sm:py-5">
        <div className="pointer-events-none absolute -right-12 -top-24 h-64 w-64 rounded-full bg-blue-500/20 blur-3xl"/>
        <div className="relative flex items-start justify-between gap-3">
          <div className="min-w-0"><div className="flex flex-wrap items-center gap-2 text-[9px] font-bold uppercase tracking-[.17em] text-blue-300"><Sparkles size={12}/> Pulse investigation <span className="text-slate-500">/</span>{item.kind || 'stock'}</div><h2 className="mt-2 truncate text-xl font-semibold tracking-tight sm:text-2xl">{item.name || item.symbol}</h2><p className="mt-1 truncate text-[9px] text-slate-400">{item.symbol}{item.sector ? ` · ${item.sector}` : ''} · Quote {time(quote.timestamp || item.timestamp)}</p></div>
          <div className="flex shrink-0 items-center gap-3"><div className="text-right"><div className={`text-lg font-bold tabular-nums ${isUp ? 'text-emerald-400' : 'text-rose-400'}`}>{pct(movement(item))}</div><div className="text-[8px] text-slate-500">Detected move</div></div><button type="button" onClick={onClose} aria-label="Close investigation" className="rounded-xl border border-white/15 bg-white/10 p-2 text-slate-200 transition hover:bg-white/20"><X size={17}/></button></div>
        </div>
        <div className="relative mt-4 grid grid-cols-2 gap-2 sm:grid-cols-4">{[['Last price', fmt(quote.price ?? item.price)], ['Day change', pct(quote.change_pct ?? item.change_pct)], ['5-minute move', pct(quote.change_5m_pct ?? item.change_5m_pct)], ['Volume vs baseline', (quote.volume_ratio ?? item.volume_ratio) == null ? '—' : `${fmt(quote.volume_ratio ?? item.volume_ratio, 1)}x`]].map(([label, value]) => <div key={label} className="rounded-xl border border-white/10 bg-white/[.07] px-3 py-2"><p className="text-[8px] font-semibold uppercase tracking-wide text-slate-400">{label}</p><p className="mt-1 text-[14px] font-semibold tabular-nums text-white">{value}</p></div>)}</div>
      </header>

      {isSector && <div className="flex shrink-0 items-center gap-1 border-b border-slate-200 bg-white px-4 pt-3 sm:px-6">
        <button type="button" onClick={() => setSectorTab('stocks')} aria-selected={sectorTab === 'stocks'} className={`flex items-center gap-2 rounded-t-xl border-b-2 px-4 py-3 text-[10px] font-bold transition ${sectorTab === 'stocks' ? 'border-blue-700 bg-blue-50/70 text-blue-800' : 'border-transparent text-slate-500 hover:bg-slate-50 hover:text-slate-800'}`}><Activity size={14}/>Sector stock heatmap<span className="rounded-full bg-slate-200/70 px-2 py-0.5 text-[8px]">{sectorItems.length}</span></button>
        <button type="button" onClick={() => setSectorTab('research')} aria-selected={sectorTab === 'research'} className={`flex items-center gap-2 rounded-t-xl border-b-2 px-4 py-3 text-[10px] font-bold transition ${sectorTab === 'research' ? 'border-blue-700 bg-blue-50/70 text-blue-800' : 'border-transparent text-slate-500 hover:bg-slate-50 hover:text-slate-800'}`}><Newspaper size={14}/>News & charts</button>
      </div>}

      <div className="min-h-0 flex-1 overflow-y-auto p-4 sm:p-6">
        {loading && <div className="mb-4 flex items-center gap-2 rounded-xl bg-blue-50 p-3 text-[11px] text-blue-800"><LoaderCircle size={14} className="animate-spin"/>Gathering related news, filings and price context…</div>}
        {error && <div className="mb-4 rounded-xl border border-amber-200 bg-amber-50 p-3 text-[11px] text-amber-900">{error}</div>}
        {isSector ? (sectorTab === 'stocks' ? stockHeatmap : researchPanel) : researchPanel}
        {(payload?.related_events || []).length > 0 && (!isSector || sectorTab === 'research') && <section className="mt-4 rounded-2xl border border-slate-200 bg-white p-4"><h3 className="text-[12px] font-bold text-slate-900">Previous movement history</h3><div className="mt-3 grid gap-2 sm:grid-cols-2">{payload.related_events.slice(0, 8).map((event) => <div key={event.event_id} className="flex items-center justify-between gap-3 rounded-lg bg-slate-50 px-3 py-2"><span className="min-w-0 truncate text-[9px] font-medium text-slate-700">{(event.reasons || []).join(' · ') || event.symbol}</span><span className="shrink-0 text-[9px] text-slate-500">{time(event.first_detected)}</span></div>)}</div></section>}
        {(!isSector || sectorTab === 'research') && <p className="mt-4 border-t border-slate-200 pt-3 text-[9px] leading-4 text-slate-400">{payload?.disclaimer || 'Market data and public search results are indicative. Verify material information against the original company or exchange disclosure. Proximity in time is not proof of causation.'}</p>}
      </div>
    </section>
  </div>;
}

function BannerKpi({ label, value, change, detail, icon: Icon, onClick, unavailable = false }) {
  const up = Number(change || 0) >= 0;
  return <button type="button" onClick={onClick} disabled={!onClick} className="group min-w-0 rounded-2xl border border-slate-200/80 bg-white p-4 text-left shadow-[0_1px_2px_rgba(15,27,45,.04),0_12px_32px_-18px_rgba(15,27,45,.18)] transition hover:-translate-y-0.5 hover:border-amber-300 disabled:cursor-default disabled:hover:translate-y-0 disabled:hover:border-slate-200/80">
    <span className="flex items-center justify-between gap-2"><span className="text-[10px] font-bold uppercase tracking-[.14em] text-slate-500">{label}</span>{Icon && <Icon size={15} className="shrink-0 text-amber-700" />}</span>
    <span className="mt-2 block truncate font-display text-2xl font-bold tracking-tight text-navy-900 tabular-nums">{unavailable ? '—' : value}</span>
    <span className="mt-1 flex items-center justify-between gap-2">
      <span className={`truncate text-[12px] font-semibold tabular-nums ${change == null ? 'text-slate-400' : up ? 'text-emerald-700' : 'text-rose-700'}`}>{change == null ? (detail || 'Unavailable') : pct(change)}</span>
      {change != null && (up ? <TrendingUp size={14} className="shrink-0 text-emerald-600" /> : <TrendingDown size={14} className="shrink-0 text-rose-600" />)}
    </span>
  </button>;
}

function CommoditiesModal({ payload, loading, error, onClose, onRefresh }) {
  const dialogRef = useRef(null);
  useEffect(() => {
    const previous = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const onKey = (event) => { if (event.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    dialogRef.current?.focus();
    return () => { document.body.style.overflow = previous; window.removeEventListener('keydown', onKey); };
  }, [onClose]);

  const items = payload?.items || [];
  return <div className="fixed inset-0 z-[110] flex items-center justify-center bg-slate-950/70 p-2 backdrop-blur-sm sm:p-4" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <section ref={dialogRef} tabIndex={-1} role="dialog" aria-modal="true" aria-label="Commodity prices" className="flex max-h-[88vh] flex-col overflow-hidden rounded-2xl border border-white/20 bg-slate-50 shadow-[0_30px_100px_rgba(2,6,23,.45)] outline-none" style={{ width: typeof window !== 'undefined' && window.innerWidth < 768 ? '96vw' : '75vw', maxWidth: 1180 }}>
      <header className="relative shrink-0 overflow-hidden bg-gradient-to-br from-blue-950 via-blue-900 to-indigo-950 px-5 py-5 text-white sm:px-7">
        <div className="pointer-events-none absolute -right-16 -top-24 h-64 w-64 rounded-full bg-cyan-400/15 blur-3xl"/>
        <div className="relative flex items-start justify-between gap-3"><div><div className="flex items-center gap-2 text-[9px] font-bold uppercase tracking-[.2em] text-blue-200"><Fuel size={13}/> Macro markets</div><h2 className="mt-2 text-xl font-semibold sm:text-2xl">Commodity prices</h2><p className="mt-1 text-[10px] text-blue-100/70">Energy and metals futures · Indicative global prices in USD</p></div><div className="flex items-center gap-2"><button type="button" onClick={onRefresh} disabled={loading} className="rounded-xl border border-white/15 bg-white/10 p-2 text-white hover:bg-white/20 disabled:opacity-50" aria-label="Refresh commodity prices"><RefreshCw size={15} className={loading ? 'animate-spin' : ''}/></button><button type="button" onClick={onClose} className="rounded-xl border border-white/15 bg-white/10 p-2 text-white hover:bg-white/20" aria-label="Close commodities"><X size={17}/></button></div></div>
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto p-4 sm:p-6">
        {error && <div className="mb-3 rounded-xl border border-rose-200 bg-rose-50 px-3 py-2 text-[10px] text-rose-800">{error}</div>}
        {loading && !items.length ? <div className="flex items-center justify-center gap-2 py-16 text-[11px] text-slate-500"><LoaderCircle size={16} className="animate-spin"/> Loading commodity prices…</div> : <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {items.map((item) => <article key={item.key || item.symbol} className="rounded-2xl border border-slate-200 bg-white p-4 shadow-[0_5px_20px_rgba(15,23,42,.035)]">
            <div className="flex items-start justify-between gap-2"><div><p className="text-[11px] font-bold text-slate-800">{item.name}</p><p className="mt-1 text-[9px] text-slate-400">{item.symbol} · {item.unit}</p></div><span className={`rounded-lg px-2 py-1 text-[9px] font-bold ${badgeTone(item.change_pct)}`}>{pct(item.change_pct)}</span></div>
            <p className="mt-4 text-[25px] font-semibold tracking-tight text-slate-950 tabular-nums">{item.price == null ? '—' : fmt(item.price, item.price < 10 ? 3 : 2)}</p>
            <div className="mt-2 flex items-center justify-between gap-2 border-t border-slate-100 pt-2 text-[8px] text-slate-400"><span>{item.status === 'available' ? (item.data_type === 'end_of_day' ? 'End of day' : 'Indicative quote') : 'Quote unavailable'}</span>{item.timestamp && <span>{time(item.timestamp)}</span>}</div>
            {item.source_url && <a href={item.source_url} target="_blank" rel="noopener noreferrer" className="mt-2 inline-flex items-center gap-1 text-[9px] font-semibold text-blue-700 hover:text-blue-900">Open source <ExternalLink size={10}/></a>}
          </article>)}
          {!items.length && !loading && <div className="col-span-full rounded-xl border border-dashed border-slate-300 bg-white p-10 text-center text-[10px] text-slate-500">No commodity prices were returned by the current public quote source.</div>}
        </div>}
        <p className="mt-4 rounded-xl bg-slate-100 px-3 py-2.5 text-[9px] leading-5 text-slate-500">{payload?.source_note || 'Prices are indicative futures quotes, not local MCX spot prices. Provider delivery timing is not guaranteed.'}</p>
      </div>
    </section>
  </div>;
}

function Panel({ eyebrow, title, description, icon: Icon, right, children, className = '' }) {
  return <section className={`rounded-2xl border border-slate-200/80 bg-white p-5 shadow-[0_1px_2px_rgba(15,27,45,.04),0_12px_32px_-18px_rgba(15,27,45,.14)] ${className}`}>
    <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0">
        {eyebrow && <div className="text-[10px] font-bold uppercase tracking-[.16em] text-amber-700">{eyebrow}</div>}
        <h2 className="mt-1 flex items-center gap-2 font-display text-xl font-bold tracking-tight text-navy-900">{Icon && <Icon size={17} className="text-navy-700" />}{title}</h2>
        {description && <p className="mt-1 max-w-2xl text-[13px] leading-relaxed text-slate-500">{description}</p>}
      </div>
      {right}
    </div>
    {children}
  </section>;
}

function MoverRow({ item, onClick }) {
  const change = Number(item.change_pct ?? 0);
  return <button type="button" onClick={() => onClick({ ...item, kind: 'stock' })} className="flex w-full items-center justify-between gap-3 rounded-xl px-3 py-2.5 text-left transition hover:bg-slate-50">
    <span className="min-w-0"><span className="block truncate text-[13px] font-semibold text-navy-900">{item.name || item.symbol}</span><span className="block truncate text-[11px] text-slate-500">{item.symbol}{item.sector ? ` · ${item.sector}` : ''}</span></span>
    <span className="shrink-0 text-right"><span className={`block text-[13px] font-bold tabular-nums ${change >= 0 ? 'text-emerald-700' : 'text-rose-700'}`}>{pct(item.change_pct)}</span><span className="block text-[11px] tabular-nums text-slate-500">{item.price == null ? '' : `₹${fmt(item.price)}`}</span></span>
  </button>;
}

export default function Pulse() {
  const [snapshot, setSnapshot] = useState(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState('');
  const [filter, setFilter] = useState('all');
  const [search, setSearch] = useState('');
  const [selected, setSelected] = useState(null);
  const [investigation, setInvestigation] = useState(null);
  const [investigationLoading, setInvestigationLoading] = useState(false);
  const [investigationError, setInvestigationError] = useState('');
  const [commoditiesOpen, setCommoditiesOpen] = useState(false);
  const [commodities, setCommodities] = useState(null);
  const [commoditiesLoading, setCommoditiesLoading] = useState(false);
  const [commoditiesError, setCommoditiesError] = useState('');
  const loadedRef = useRef(false);
  const closeInvestigation = useCallback(() => { setSelected(null); setInvestigation(null); }, []);

  const loadDashboard = useCallback(async (manual = false) => {
    if (manual) setRefreshing(true);
    else if (!loadedRef.current) setLoading(true);
    try {
      const data = await request('/pulse', { persist: false, retry: false, maxRetries: 0, timeoutMs: 45_000 });
      setSnapshot(data);
      setError('');
      loadedRef.current = true;
    } catch (err) {
      setError(err?.message || 'Pulse could not load market movements.');
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  const selectInstrument = useCallback(async (instrument) => {
    if (!instrument) return;
    const kind = instrument.kind === 'sector' ? 'sector' : instrument.kind === 'index' ? 'index' : 'stock';
    const identifier = kind === 'stock' ? instrument.symbol : (instrument.key || instrument.symbol || instrument.name);
    setSelected({ ...instrument, kind, identifier });
    setInvestigation(null);
    setInvestigationError('');
    setInvestigationLoading(true);
    try {
      const payload = await request(`/pulse/investigate/${encodeURIComponent(identifier)}?kind=${kind}`, { persist: false, retry: false, maxRetries: 0, timeoutMs: 40_000 });
      setInvestigation(payload);
    } catch (err) {
      setInvestigationError(err?.message || 'Could not retrieve the investigation details.');
    } finally {
      setInvestigationLoading(false);
    }
  }, []);

  const loadCommodities = useCallback(async () => {
    setCommoditiesOpen(true);
    setCommoditiesLoading(true);
    setCommoditiesError('');
    try {
      const payload = await request('/pulse/commodities', { persist: false, retry: false, maxRetries: 0, timeoutMs: 35_000 });
      setCommodities(payload);
    } catch (err) {
      setCommoditiesError(err?.message || 'Could not load commodity prices.');
    } finally {
      setCommoditiesLoading(false);
    }
  }, []);

  useEffect(() => {
    loadDashboard(false);
    const timer = window.setInterval(() => loadDashboard(false), REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [loadDashboard]);

  const sectors = useMemo(() => (snapshot?.sectors || []).filter((row) => row.kind === 'sector'), [snapshot]);
  const anchors = useMemo(() => (snapshot?.sectors || []).filter((row) => row.kind === 'index'), [snapshot]);
  const nifty50 = anchors.find((row) => row.key === 'NIFTY50');
  const sensex = anchors.find((row) => row.key === 'SENSEX');
  const stocks = useMemo(() => [...(snapshot?.stocks || [])].filter((row) => row.watchlist_source === 'coverage_list').sort((a, b) => Math.abs(Number(b.change_pct ?? movement(b) ?? 0)) - Math.abs(Number(a.change_pct ?? movement(a) ?? 0))), [snapshot]);
  const signals = useMemo(() => {
    const query = search.trim().toLowerCase();
    return (snapshot?.signals || []).filter((row) => (filter === 'all' || row.kind === filter)
      && (!query || [row.name, row.symbol, row.sector, ...(row.reasons || [])].join(' ').toLowerCase().includes(query)));
  }, [snapshot, filter, search]);
  const recentEvents = snapshot?.recent_events || [];

  const withChange = stocks.filter((row) => Number.isFinite(Number(row.change_pct)));
  const gainers = [...withChange].filter((row) => Number(row.change_pct) > 0).sort((a, b) => Number(b.change_pct) - Number(a.change_pct)).slice(0, 6);
  const losers = [...withChange].filter((row) => Number(row.change_pct) < 0).sort((a, b) => Number(a.change_pct) - Number(b.change_pct)).slice(0, 6);
  const sectorAdvancing = sectors.filter((row) => Number(row.change_pct) > 0).length;
  const sectorDeclining = sectors.filter((row) => Number(row.change_pct) < 0).length;

  return <main className="mx-auto w-full max-w-[1400px] space-y-7 pb-10">
    <header className="flex flex-col gap-5 md:flex-row md:items-end md:justify-between">
      <div className="min-w-0">
        <div className="mb-2 inline-flex items-center gap-2 text-[11px] font-bold uppercase tracking-[.18em] text-amber-700"><span className="h-px w-6 bg-amber-600/60" />Market Pulse</div>
        <h1 className="font-display text-3xl font-bold tracking-tight text-navy-900 md:text-[2.25rem]">Today’s market at a glance</h1>
        <p className="mt-2 max-w-2xl text-[15px] leading-relaxed text-slate-600">Sector and coverage moves from the latest scan, with the signals, news and filings behind them.</p>
      </div>
      <div className="flex items-center gap-2">
        <span className="hidden rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs text-slate-500 sm:inline-flex">Updated {time(snapshot?.as_of)}</span>
        <button type="button" onClick={() => loadDashboard(true)} disabled={refreshing} className="inline-flex items-center gap-2 rounded-lg bg-navy-900 px-4 py-2.5 text-sm font-semibold text-white shadow-sm transition hover:bg-navy-800 disabled:opacity-60">
          <RefreshCw size={15} className={refreshing ? 'animate-spin' : ''} /> Refresh
        </button>
      </div>
    </header>

    {snapshot?.stale && <div className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900">Showing the last successful market scan. {snapshot.stale_reason || ''}</div>}
    {error && <div className="rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800">{error}</div>}

    <section className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      <BannerKpi label="NIFTY 50" value={nifty50?.price == null ? '—' : fmt(nifty50.price)} change={nifty50?.change_pct} detail="Index quote unavailable" icon={Activity} unavailable={nifty50?.price == null} onClick={() => { if (nifty50) selectInstrument({ ...nifty50, kind: 'index' }); }} />
      <BannerKpi label="SENSEX" value={sensex?.price == null ? '—' : fmt(sensex.price)} change={sensex?.change_pct} detail="Index quote unavailable" icon={Activity} unavailable={sensex?.price == null} onClick={() => { if (sensex) selectInstrument({ ...sensex, kind: 'index' }); }} />
      <BannerKpi label="USD / INR" value={snapshot?.macro?.usd_inr?.price == null ? '—' : `₹${fmt(snapshot.macro.usd_inr.price, 4)}`} change={snapshot?.macro?.usd_inr?.change_pct} detail="Currency quote unavailable" icon={DollarSign} unavailable={snapshot?.macro?.usd_inr?.price == null} />
      <BannerKpi label="Crude oil · WTI" value={snapshot?.macro?.crude_oil?.price == null ? '—' : `$${fmt(snapshot.macro.crude_oil.price, 2)}`} change={snapshot?.macro?.crude_oil?.change_pct} detail="Open commodity prices" icon={Fuel} unavailable={snapshot?.macro?.crude_oil?.price == null} onClick={loadCommodities} />
    </section>

    <section className="grid items-start gap-5 xl:grid-cols-[minmax(0,1.6fr)_minmax(300px,1fr)]">
      <Panel eyebrow="Sectors" title="Sector heatmap" icon={BarChart3}
        description="Sector and index moves from the latest scan. Select a tile to open its investigation."
        right={sectors.length ? <span className="rounded-lg bg-slate-100 px-3 py-1.5 text-xs font-semibold text-slate-700"><span className="text-emerald-700">{sectorAdvancing} up</span> · <span className="text-rose-700">{sectorDeclining} down</span></span> : null}>
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">{sectors.map((item) => <HeatCell key={item.key || item.symbol} item={item} onClick={selectInstrument} />)}</div>
        {!sectors.length && !loading && <div className="rounded-xl border border-dashed border-slate-300 bg-slate-50 p-7 text-center text-sm text-slate-500">Sector prices are not available in the latest scan.</div>}
      </Panel>

      <Panel eyebrow="Coverage" title="Top movers" icon={TrendingUp} description="Largest day moves among Coverage List stocks in the latest scan.">
        <div className="grid gap-4">
          <div>
            <div className="mb-1 flex items-center gap-2 px-3 text-[11px] font-bold uppercase tracking-wider text-emerald-700"><TrendingUp size={13} /> Gainers</div>
            {gainers.length ? gainers.map((item) => <MoverRow key={`g-${item.symbol}`} item={item} onClick={selectInstrument} />) : <p className="px-3 py-3 text-sm text-slate-500">No gainers in this scan.</p>}
          </div>
          <div className="border-t border-slate-100 pt-4">
            <div className="mb-1 flex items-center gap-2 px-3 text-[11px] font-bold uppercase tracking-wider text-rose-700"><TrendingDown size={13} /> Losers</div>
            {losers.length ? losers.map((item) => <MoverRow key={`l-${item.symbol}`} item={item} onClick={selectInstrument} />) : <p className="px-3 py-3 text-sm text-slate-500">No losers in this scan.</p>}
          </div>
        </div>
      </Panel>
    </section>

    <section className="grid items-start gap-5 xl:grid-cols-2">
      <Panel eyebrow="Signals" title="Unusual movements" icon={Zap} description="Moves that crossed the configured thresholds in the latest scan.">
        <div className="mb-4 flex flex-wrap items-center gap-2">
          <div className="relative min-w-[180px] flex-1">
            <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
            <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search signals" aria-label="Search signals" className="w-full rounded-lg border border-slate-200 bg-white py-2 pl-9 pr-3 text-sm outline-none focus:border-navy-900 focus:ring-2 focus:ring-navy-900/10" />
          </div>
          <div role="group" aria-label="Filter signals" className="flex gap-1 rounded-lg bg-slate-100 p-1">
            {[['all', 'All'], ['sector', 'Sectors'], ['stock', 'Stocks'], ['index', 'Indices']].map(([key, label]) => (
              <button key={key} type="button" aria-pressed={filter === key} onClick={() => setFilter(key)} className={`rounded-md px-3 py-1.5 text-xs font-semibold transition ${filter === key ? 'bg-white text-navy-900 shadow-sm' : 'text-slate-600 hover:text-navy-900'}`}>{label}</button>
            ))}
          </div>
        </div>
        <div className="space-y-2">
          {signals.slice(0, 30).map((item, index) => <SignalRow key={`${item.kind}-${item.symbol}-${index}`} item={item} onClick={selectInstrument} />)}
          {!signals.length && <div className="rounded-xl bg-slate-50 p-6 text-center text-sm text-slate-500">{snapshot ? 'No unusual movements match these filters.' : 'Waiting for the first market scan.'}</div>}
        </div>
      </Panel>

      <Panel eyebrow="Events" title="Recent movements" icon={CalendarClock} description="Previously recorded signals, newest first.">
        <div className="space-y-1">
          {recentEvents.slice(0, 14).map((item) => (
            <button key={item.event_id} type="button" onClick={() => selectInstrument(item)} className="flex w-full items-center justify-between gap-3 rounded-xl px-3 py-2.5 text-left transition hover:bg-slate-50">
              <span className="min-w-0"><span className="block truncate text-sm font-semibold text-navy-900">{item.name || item.symbol}</span><span className="block truncate text-xs text-slate-500">{time(item.timestamp || item.last_seen)}{item.sector ? ` · ${item.sector}` : ''}</span></span>
              <span className={`shrink-0 text-sm font-bold tabular-nums ${Number(item.change_pct ?? item.change_5m_pct ?? 0) >= 0 ? 'text-emerald-700' : 'text-rose-700'}`}>{pct(item.change_pct ?? item.change_5m_pct)}</span>
            </button>
          ))}
          {!recentEvents.length && <div className="rounded-xl bg-slate-50 p-6 text-center text-sm text-slate-500">No recorded movements yet.</div>}
        </div>
      </Panel>
    </section>

    <Panel eyebrow="Coverage" title="Coverage List stocks" icon={Activity} description="Every Coverage List company in the latest scan. Select a tile for its price chart, news and filings.">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 xl:grid-cols-8">{stocks.map((item) => <HeatCell key={`${item.exchange || 'NSE'}-${item.symbol}`} item={item} onClick={selectInstrument} dense />)}</div>
      {!stocks.length && !loading && <div className="mt-2 rounded-xl border border-dashed border-slate-300 bg-slate-50 p-7 text-center text-sm text-slate-500">No Coverage List stock quotes are available in the current scan.</div>}
      <div className="mt-4 flex flex-wrap items-center gap-4 text-xs text-slate-500">
        <span className="flex items-center gap-1.5"><i className="h-2.5 w-2.5 rounded-sm bg-rose-500" />Decliners</span>
        <span className="flex items-center gap-1.5"><i className="h-2.5 w-2.5 rounded-sm bg-slate-300" />Flat or unavailable</span>
        <span className="flex items-center gap-1.5"><i className="h-2.5 w-2.5 rounded-sm bg-emerald-600" />Advancers</span>
      </div>
    </Panel>

    <footer className="flex flex-wrap items-center justify-between gap-2 border-t border-slate-200 pt-4 text-xs text-slate-500">
      <span>Scan cadence target: 60 seconds · Last scan: {time(snapshot?.as_of)}</span>
      <span>{snapshot?.market_data_note || 'Quotes are indicative. Verify against the exchange before acting.'}</span>
    </footer>

    {selected && <InvestigationModal item={selected} payload={investigation} loading={investigationLoading} error={investigationError} onClose={closeInvestigation} onChooseRelated={selectInstrument} />}
    {commoditiesOpen && <CommoditiesModal payload={commodities} loading={commoditiesLoading} error={commoditiesError} onClose={() => setCommoditiesOpen(false)} onRefresh={loadCommodities} />}
  </main>;
}
