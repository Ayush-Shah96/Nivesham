import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
  ArrowLeft, BarChart3, Building2, CalendarDays, ExternalLink, FileText,
  LoaderCircle, Newspaper, RefreshCw, Search, Share2, Wallet, TrendingDown,
  TrendingUp, Clock3, Briefcase,
} from 'lucide-react';
import { api } from '../lib/api';

const TTL = 10 * 60 * 1000;
const RECENT_DAYS = 3;
const companyMemory = new Map();
const searchMemory = new Map();
const textOf = (value) => String(value ?? '').trim();
const isMeaningful = (value) => value != null && value !== '' && value !== '—' && value !== '-' && value !== 'null';
const cacheFresh = (entry) => entry && Date.now() - entry.at < TTL;
const fmt = (value, digits = 2) => value == null || value === '' || !Number.isFinite(Number(value))
  ? 'Unavailable'
  : Number(value).toLocaleString('en-IN', { maximumFractionDigits: digits });

function dateValue(item) {
  return item?.published_at || item?.publishedAt || item?.broadcastDateTime || item?.an_dt || item?.date || item?.eventDate || item?.meetingDate || null;
}

function parseDate(value) {
  if (!value) return null;
  const raw = textOf(value);
  if (!raw) return null;
  // Parse YYYY-MM-DD as a local calendar date to avoid UTC day-boundary shifts.
  const isoDay = raw.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (isoDay) {
    const parsed = new Date(Number(isoDay[1]), Number(isoDay[2]) - 1, Number(isoDay[3]));
    return Number.isNaN(parsed.getTime()) ? null : parsed;
  }
  const parsed = new Date(raw);
  if (!Number.isNaN(parsed.getTime())) return parsed;
  const indian = raw.match(/^(\d{1,2})[- ]([A-Za-z]{3})[- ](\d{2,4})/);
  if (indian) {
    const parsedMonth = new Date(`${indian[2]} 1 2000`).getMonth();
    const year = Number(indian[3]) < 100 ? 2000 + Number(indian[3]) : Number(indian[3]);
    const result = new Date(year, parsedMonth, Number(indian[1]));
    return Number.isNaN(result.getTime()) ? null : result;
  }
  return null;
}

function isWithinRecentWindow(item) {
  const parsed = parseDate(dateValue(item));
  if (!parsed) return false;
  const today = new Date();
  const todayStart = new Date(today.getFullYear(), today.getMonth(), today.getDate());
  const start = new Date(todayStart);
  start.setDate(start.getDate() - (RECENT_DAYS - 1));
  const day = new Date(parsed.getFullYear(), parsed.getMonth(), parsed.getDate());
  return day >= start && day <= todayStart;
}

function humanDate(value) {
  const parsed = parseDate(value);
  return parsed ? parsed.toLocaleString('en-IN', { dateStyle: 'medium', ...(String(value).includes(':') ? { timeStyle: 'short' } : {}) }) : textOf(value);
}

function displayValue(label, value) {
  if (!isMeaningful(value) || (typeof value === 'object' && value !== null)) return 'Unavailable';
  if (typeof value === 'number' || (typeof value === 'string' && /^[-+]?\d[\d,]*(\.\d+)?%?$/.test(value.trim()))) {
    const isPercent = /%/.test(String(value)) || /%|change|return|holding|margin/i.test(label);
    const numeric = Number(String(value).replace(/,/g, '').replace(/%/g, ''));
    if (!Number.isFinite(numeric)) return textOf(value);
    if (/volume|quantity|shares/i.test(label)) return fmt(numeric, 0);
    if (/₹|price|revenue|income|profit|ebitda|market cap/i.test(label)) return `₹${fmt(numeric)}`;
    return `${fmt(numeric)}${isPercent ? '%' : ''}`;
  }
  return textOf(value);
}

function SourceLink({ href, children = 'Open source' }) {
  if (!href) return null;
  return <a href={href} target="_blank" rel="noreferrer" className="inline-flex shrink-0 items-center gap-1 text-[11px] font-semibold text-blue-700 transition hover:text-blue-900 hover:underline">{children}<ExternalLink size={12}/></a>;
}

function Metric({ label, value, tone = 'default', icon: Icon }) {
  const shown = textOf(value) || 'Unavailable';
  const toneClass = tone === 'positive' ? 'text-emerald-700' : tone === 'negative' ? 'text-rose-700' : 'text-slate-950';
  return <div className="min-w-0 rounded-xl border border-slate-200/90 bg-white p-3 shadow-sm shadow-slate-900/[0.025] transition hover:border-blue-200 hover:shadow-md">
    <div className="flex items-center justify-between gap-2"> <p className="truncate text-[10px] font-semibold uppercase tracking-[0.09em] text-slate-500">{label}</p>{Icon && <Icon size={14} className="shrink-0 text-slate-400"/>}</div>
    <p className={`mt-2 truncate text-base font-bold tabular-nums ${toneClass}`} title={shown}>{shown}</p>
  </div>;
}

function RecordCard({ item, kind = 'event' }) {
  const title = textOf(item?.title || item?.subject || item?.purpose || item?.name || item?.companyName || item?.period || item?.symbol)
    || (kind === 'news' ? 'Related news item' : kind === 'financial' ? 'Financial-result filing' : 'Company disclosure');
  const date = dateValue(item);
  const metrics = item?.metrics && typeof item.metrics === 'object' && !Array.isArray(item.metrics)
    ? Object.entries(item.metrics).filter(([, value]) => isMeaningful(value)).slice(0, 6) : [];
  const summary = textOf(item?.summary || item?.description || item?.snippet || item?.purpose || item?.remarks || item?.status);
  const href = item?.url || item?.source_url || item?.sourceUrl;
  const badge = textOf(item?.kind || item?.category || item?.source || (kind === 'news' ? 'Related news' : kind === 'financial' ? 'Financial results' : 'Company filing'));
  return <article className="group rounded-xl border border-slate-200 bg-white p-3.5 transition duration-150 hover:-translate-y-px hover:border-blue-200 hover:shadow-md hover:shadow-blue-950/[0.04] sm:p-4">
    <div className="flex items-start justify-between gap-3">
      <div className="min-w-0 flex-1">
        <div className="mb-1.5 flex flex-wrap items-center gap-1.5">
          <span className="rounded-full bg-slate-100 px-2 py-1 text-[9px] font-semibold text-slate-600">{badge}</span>
          {date && <span className="inline-flex items-center gap-1 text-[10px] text-slate-500"><Clock3 size={11}/>{humanDate(date)}</span>}
          {kind === 'news' && item?.source && <span className="text-[10px] text-slate-500">· {textOf(item.source)}</span>}
        </div>
        <h3 className="text-sm font-semibold leading-5 text-slate-900">{title}</h3>
        {summary && summary !== title && <p className="mt-1.5 line-clamp-3 text-xs leading-5 text-slate-600">{summary}</p>}
      </div>
      <SourceLink href={href}>{kind === 'news' ? 'Read news' : 'Open filing'}</SourceLink>
    </div>
    {metrics.length > 0 && <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-3">{metrics.map(([label, value]) => <div key={label} className="rounded-lg bg-slate-50 px-2.5 py-2"><p className="text-[9px] font-semibold uppercase tracking-wide text-slate-500">{label}</p><p className="mt-1 text-xs font-semibold text-slate-800">{displayValue(label, value)}</p></div>)}</div>}
    {item?.period && <p className="mt-2 text-[10px] text-slate-500">Period: {textOf(item.period)}</p>}
  </article>;
}

function ResearchTableView({ tables = [] }) {
  const usable = tables.filter((table) => Array.isArray(table?.rows) && table.rows.length > 1);
  if (!usable.length) return null;
  return <div className="mt-4 space-y-4">
    {usable.map((table, tableIndex) => {
      const rows = table.rows;
      const headers = Array.isArray(rows[0]) ? rows[0] : [];
      const bodyRows = rows.slice(1);
      return <section key={`${table.title || 'table'}-${tableIndex}`} className="overflow-hidden rounded-xl border border-slate-200 bg-white">
        <div className="border-b border-slate-100 bg-slate-50 px-3 py-2.5"><h3 className="text-xs font-bold text-slate-800">{textOf(table.title) || `Source table ${tableIndex + 1}`}</h3><p className="mt-0.5 text-[9px] text-slate-500">{bodyRows.length.toLocaleString('en-IN')} records · QuarterMark reported data</p></div>
        <div className="max-h-[560px] overflow-auto">
          <table className="w-full min-w-max border-separate border-spacing-0 text-left text-[11px]">
            <thead className="sticky top-0 z-20 bg-slate-100 text-[9px] font-bold uppercase tracking-wide text-slate-600">
              <tr>{headers.map((header, colIndex) => <th key={`${header}-${colIndex}`} className={`sticky top-0 border-b border-slate-200 px-3 py-2.5 whitespace-nowrap ${colIndex === 0 ? 'left-0 z-30 min-w-[180px] bg-slate-100' : 'min-w-[105px] text-right'}`}>{textOf(header) || `Column ${colIndex + 1}`}</th>)}</tr>
            </thead>
            <tbody>{bodyRows.map((row, rowIndex) => <tr key={`${tableIndex}-${rowIndex}`} className="odd:bg-white even:bg-slate-50/70 hover:bg-blue-50/60">
              {headers.map((header, colIndex) => {
                const value = row?.[colIndex] ?? '';
                const numeric = /^\(?[-+₹$]?[\d,.]+%?\)?$/.test(textOf(value));
                return <td key={`${rowIndex}-${colIndex}`} className={`border-b border-slate-100 px-3 py-2.5 tabular-nums ${colIndex === 0 ? 'sticky left-0 z-10 max-w-[280px] min-w-[180px] bg-inherit font-semibold text-slate-800' : `min-w-[105px] ${numeric ? 'text-right' : 'text-right'} text-slate-700`}`} title={textOf(value)}>{textOf(value) || '—'}</td>;
              })}
            </tr>)}</tbody>
          </table>
        </div>
      </section>;
    })}
  </div>;
}

function ResearchSection({ section, index, kind = 'event' }) {
  const icons = [Building2, BarChart3, Wallet, CalendarDays, Share2, Newspaper, Building2];
  const Icon = icons[index] || FileText;
  const metrics = Object.entries(section?.metrics || {}).filter(([, value]) => isMeaningful(value));
  const rawItems = Array.isArray(section?.items) ? section.items : [];
  const shouldFilterRecent = ['event', 'news', 'concall'].includes(kind);
  const items = (shouldFilterRecent ? rawItems.filter(isWithinRecentWindow) : rawItems).slice(0, kind === 'news' ? 15 : kind === 'financial' ? 100 : kind === 'ownership' ? 100 : 100);
  const tables = Array.isArray(section?.tables) ? section.tables : [];
  const hasTables = tables.some((table) => Array.isArray(table?.rows) && table.rows.length > 1);
  const text = textOf(section?.text).split('\n').map((line) => line.trim()).filter(Boolean)[0] || 'Source-reported information is shown below.';
  const hasContent = metrics.length || items.length || hasTables;
  const defaultEmpty = shouldFilterRecent ? `No records with a verifiable publication date from the last ${RECENT_DAYS} days.` : 'No structured records were returned by the source.';
  return <section className="scroll-mt-5 overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm shadow-slate-900/[0.025]">
    <div className="flex flex-wrap items-start justify-between gap-3 border-b border-slate-100 px-4 py-4 sm:px-5">
      <div className="flex min-w-0 items-start gap-3"><div className="rounded-xl bg-blue-50 p-2.5 text-blue-700"><Icon size={18}/></div><div className="min-w-0"><div className="flex flex-wrap items-center gap-2"><h2 className="text-sm font-bold text-slate-950">{section?.section || section?.title || 'Research section'}</h2>{shouldFilterRecent && <span className="rounded-full bg-blue-50 px-2 py-0.5 text-[9px] font-semibold text-blue-700">Last 3 days</span>}</div><p className="mt-1 max-w-3xl text-[11px] leading-5 text-slate-500">{text}</p></div></div>
      <SourceLink href={section?.url}>{kind === 'news' ? 'View news source' : 'Open QuarterMark page'}</SourceLink>
    </div>
    <div className="p-4 sm:p-5">
      {metrics.length > 0 && <div className="mb-4 grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">{metrics.slice(0, 12).map(([label, value]) => <Metric key={label} label={label} value={displayValue(label, value)}/>)}</div>}
      {items.length > 0 && <div className="grid gap-2.5 xl:grid-cols-2">{items.map((item, itemIndex) => <RecordCard key={`${item?.url || item?.source_url || item?.title || item?.symbol || 'record'}-${itemIndex}`} item={item} kind={kind}/>)}</div>}
      {!items.length && !metrics.length && !hasTables && <div className={`rounded-xl border border-dashed p-4 text-xs leading-5 ${section?.available ? 'border-slate-200 bg-slate-50 text-slate-500' : 'border-amber-200 bg-amber-50/70 text-amber-900'}`}>{section?.reason || defaultEmpty}</div>}
      {hasTables && <ResearchTableView tables={tables}/>}
      {section?.reason && hasContent && <p className="mt-3 text-[10px] text-amber-700">{section.reason}</p>}
    </div>
  </section>;
}

function ConcallResearchSection({ section }) {
  const calls = Array.isArray(section?.calls) ? section.calls : [];
  const [activeTab, setActiveTab] = useState('summary');
  const [activePeriod, setActivePeriod] = useState(calls[0]?.period || 'latest');
  const selectedCall = calls.find((call) => call.period === activePeriod) || calls[0] || null;
  const document = activeTab === 'transcript' ? selectedCall?.transcript : selectedCall?.summary;
  const fallbackText = textOf(section?.text);
  const contentText = typeof document?.text === 'string' ? document.text : '';
  const available = Boolean(document?.available && contentText.trim());
  return <section className="scroll-mt-5 overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm shadow-slate-900/[0.025]">
    <div className="flex flex-wrap items-start justify-between gap-3 border-b border-slate-100 px-4 py-4 sm:px-5">
      <div className="flex min-w-0 items-start gap-3"><div className="rounded-xl bg-blue-50 p-2.5 text-blue-700"><Share2 size={18}/></div><div><div className="flex flex-wrap items-center gap-2"><h2 className="text-sm font-bold text-slate-950">Concall Summary & Transcript</h2><span className="rounded-full bg-blue-50 px-2 py-0.5 text-[9px] font-semibold text-blue-700">QuarterMark</span></div><p className="mt-1 max-w-3xl text-[11px] leading-5 text-slate-500">Separate management summary and full call transcript, organized by reporting quarter.</p></div></div>
      <SourceLink href={document?.url || section?.url}>Open QuarterMark source</SourceLink>
    </div>
    <div className="p-4 sm:p-5">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div className="inline-flex rounded-xl border border-slate-200 bg-slate-50 p-1" role="tablist" aria-label="Concall document type">
          <button type="button" role="tab" aria-selected={activeTab === 'summary'} onClick={() => setActiveTab('summary')} className={`rounded-lg px-4 py-2 text-xs font-semibold transition ${activeTab === 'summary' ? 'bg-white text-blue-700 shadow-sm' : 'text-slate-500 hover:text-slate-800'}`}>Concall Summary</button>
          <button type="button" role="tab" aria-selected={activeTab === 'transcript'} onClick={() => setActiveTab('transcript')} className={`rounded-lg px-4 py-2 text-xs font-semibold transition ${activeTab === 'transcript' ? 'bg-white text-blue-700 shadow-sm' : 'text-slate-500 hover:text-slate-800'}`}>Full Transcript</button>
        </div>
        {calls.length > 1 && <label className="flex items-center gap-2 text-[10px] font-semibold text-slate-500">Quarter <select value={selectedCall?.period || activePeriod} onChange={(event) => setActivePeriod(event.target.value)} className="rounded-lg border border-slate-300 bg-white px-3 py-2 text-xs text-slate-800">{calls.map((call) => <option key={call.period} value={call.period}>{call.label || call.period}</option>)}</select></label>}
      </div>
      {available ? <>
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2"><div><h3 className="text-sm font-bold text-slate-900">{document.title || (activeTab === 'summary' ? 'Concall summary' : 'Full transcript')}</h3><p className="mt-1 text-[10px] text-slate-500">{selectedCall?.label || 'Latest available call'} · Scraped from QuarterMark</p></div><SourceLink href={document.url}>Open document</SourceLink></div>
        {document.tables?.length > 0 && <ResearchTableView tables={document.tables}/>}
        <article className={`rounded-xl border border-slate-200 bg-slate-50/60 p-4 text-xs leading-6 text-slate-700 ${activeTab === 'transcript' ? 'max-h-[780px] overflow-y-auto' : ''}`}>
          <div className="whitespace-pre-wrap break-words">{contentText}</div>
        </article>
      </> : <div className="rounded-xl border border-dashed border-amber-200 bg-amber-50/70 p-4 text-xs leading-5 text-amber-900">{document?.reason || (activeTab === 'summary' ? 'No readable QuarterMark summary was found for this period.' : 'No readable full transcript was found for this period.')}{!calls.length && fallbackText && <p className="mt-2 whitespace-pre-wrap">{fallbackText}</p>}</div>}
    </div>
  </section>;
}

export default function StockScreener() {
  const navigate = useNavigate();
  const { symbol: routeSymbol } = useParams();
  const symbol = textOf(routeSymbol).toUpperCase();
  const [query, setQuery] = useState('');
  const [results, setResults] = useState([]);
  const [searchLoading, setSearchLoading] = useState(false);
  const [searchError, setSearchError] = useState('');
  const [company, setCompany] = useState(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState('');
  const [refreshTick, setRefreshTick] = useState(0);
  const forceRefreshRef = useRef({ search: false, company: false });

  const refreshCompany = useCallback(() => {
    forceRefreshRef.current.company = true;
    companyMemory.delete(symbol);
    setRefreshTick((tick) => tick + 1);
  }, [symbol]);

  useEffect(() => {
    if (symbol) return undefined;
    let active = true;
    const cleanQuery = query.trim();
    const memoryKey = cleanQuery.toLowerCase();
    const forceRefresh = forceRefreshRef.current.search;
    forceRefreshRef.current.search = false;
    if (!forceRefresh && cacheFresh(searchMemory.get(memoryKey))) {
      setResults(searchMemory.get(memoryKey).data.results || []);
      setSearchError('');
      setSearchLoading(false);
      return undefined;
    }
    const timer = window.setTimeout(async () => {
      setSearchLoading(true);
      setSearchError('');
      try {
        const data = await api.stockScreenerSearch({ q: cleanQuery, limit: 100, refresh: forceRefresh });
        if (!active) return;
        const nextResults = Array.isArray(data?.results) ? data.results : [];
        setResults(nextResults);
        setSearchError(['unavailable', 'warming'].includes(data?.status) || (data?.status === 'partial' && nextResults.length === 0) ? (data.reason || 'QuarterMark company search is temporarily unavailable.') : '');
        if (data?.status !== 'unavailable') searchMemory.set(memoryKey, { at: Date.now(), data });
      } catch (error) {
        if (active) { setSearchError(error?.message || 'Could not search the QuarterMark company list.'); setResults([]); }
      } finally { if (active) setSearchLoading(false); }
    }, cleanQuery ? 220 : 30);
    return () => { active = false; window.clearTimeout(timer); };
  }, [query, symbol, refreshTick]);

  useEffect(() => {
    if (!symbol) { setCompany(null); setDetailError(''); setDetailLoading(false); return undefined; }
    let active = true;
    const forceRefresh = forceRefreshRef.current.company;
    forceRefreshRef.current.company = false;
    const cached = companyMemory.get(symbol);
    if (!forceRefresh && cacheFresh(cached)) {
      setCompany(cached.data); setDetailError(''); setDetailLoading(false);
      return () => { active = false; };
    }
    setDetailLoading(true);
    setDetailError('');
    setCompany(null);
    api.stockScreenerCompany(symbol, { refresh: forceRefresh })
      .then((data) => {
        if (!active) return;
        // Normalize common API wrapper shapes; do not let a transport envelope render as a company.
        const payload = data?.data && typeof data.data === 'object' && !data.symbol ? data.data : data;
        if (payload && typeof payload === 'object' && (payload.symbol || payload.sections || payload.company_name)) setCompany(payload);
        else throw new Error('The Stock Screener API did not return a normalized company profile.');
        if (payload?.status !== 'unavailable') companyMemory.set(symbol, { at: Date.now(), data: payload });
        else setDetailError(payload.reason || 'QuarterMark company data is unavailable.');
      })
      .catch((error) => { if (active) setDetailError(error?.message || 'Could not load company research.'); })
      .finally(() => { if (active) setDetailLoading(false); });
    return () => { active = false; };
  }, [symbol, refreshTick]);

  const sections = useMemo(() => {
    const payloadSections = company?.sections || {};
    return [
      { section: payloadSections.overview, key: 'overview', label: 'Overview', kind: 'overview' },
      { section: payloadSections.financials, key: 'financials', label: 'Financials', kind: 'financial' },
      { section: payloadSections.ownership, key: 'ownership', label: 'Shareholding pattern', kind: 'ownership' },
      { section: payloadSections.peers, key: 'peers', label: 'Peers', kind: 'peers' },
      { section: payloadSections.events, key: 'events', label: 'Announcements & events', kind: 'event' },
      { section: payloadSections.concall_summary, key: 'concall_summary', label: 'Concall Summary & Transcript', kind: 'concall' },
      { section: payloadSections.related_news, key: 'related_news', label: 'Related news', kind: 'news' },
    ];
  }, [company]);

  if (symbol) {
    const companyName = textOf(company?.company_name || company?.quote?.company_name || symbol);
    const quote = company?.quote || {};
    const rawMetrics = company?.metrics || quote?.metrics || {};
    const get = (...keys) => {
      for (const key of keys) {
        if (isMeaningful(quote?.[key])) return quote[key];
        if (isMeaningful(rawMetrics?.[key])) return rawMetrics[key];
      }
      return null;
    };
    const lastPrice = get('last_price', 'Last price (₹)');
    const changePct = get('change_pct', 'Change (%)');
    const previousClose = get('previous_close', 'Previous close (₹)');
    const dayHigh = get('high', 'Day high (₹)');
    const dayLow = get('low', 'Day low (₹)');
    const marketCap = get('Market cap (₹ Cr)', 'market_cap');
    const pe = get('P/E', 'pe');
    const volume = get('volume', 'totalTradedVolume');
    const kpis = [
      { label: 'Last traded price', value: lastPrice == null ? 'Unavailable' : `₹${fmt(lastPrice)}`, icon: BarChart3 },
      { label: 'Day change', value: changePct == null ? 'Unavailable' : `${Number(changePct) > 0 ? '+' : ''}${fmt(changePct)}%`, tone: changePct == null ? 'default' : Number(changePct) >= 0 ? 'positive' : 'negative', icon: Number(changePct) >= 0 ? TrendingUp : TrendingDown },
      { label: 'Previous close', value: previousClose == null ? 'Unavailable' : `₹${fmt(previousClose)}`, icon: BarChart3 },
      { label: 'Day range', value: dayHigh == null && dayLow == null ? 'Unavailable' : `₹${fmt(dayLow)} – ₹${fmt(dayHigh)}`, icon: BarChart3 },
      { label: 'Market cap', value: marketCap == null ? 'Unavailable' : `₹${fmt(marketCap)} Cr`, icon: Building2 },
      { label: 'P/E ratio', value: pe == null ? 'Unavailable' : fmt(pe), icon: BarChart3 },
      { label: 'Traded volume', value: volume == null ? 'Unavailable' : fmt(volume, 0), icon: Briefcase },
    ];
    return <main className="mx-auto w-full max-w-[1500px] space-y-4 px-3 py-5 sm:px-5 lg:px-7">
      <header className="overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm">
        <div className="flex flex-wrap items-start justify-between gap-4 p-4 sm:p-5">
          <div className="flex min-w-0 items-start gap-3">
            <button type="button" onClick={() => navigate('/stock-screener')} aria-label="Back to company search" className="mt-0.5 rounded-lg border border-slate-200 p-2 text-slate-600 transition hover:bg-slate-50"><ArrowLeft size={16}/></button>
            <div className="min-w-0"><div className="flex flex-wrap items-center gap-2 text-[10px] font-bold uppercase tracking-[0.16em] text-blue-700"><span>QuarterMark company analysis</span><span className="rounded-full bg-slate-100 px-2 py-1 tracking-normal text-slate-500">{symbol}</span></div><h1 className="mt-1 text-xl font-bold tracking-tight text-slate-950 sm:text-2xl">{companyName}</h1><p className="mt-1 text-[11px] text-slate-500">Company snapshot, trading KPIs, filings and recent research events.</p></div>
          </div>
          <div className="flex flex-wrap items-center gap-2"><SourceLink href={company?.nse_url || `https://www.nseindia.com/get-quotes/equity?symbol=${encodeURIComponent(symbol)}`}>Open NSE quote</SourceLink><SourceLink href={company?.quartermark_url || `https://quartermark.in/companies/${encodeURIComponent(symbol)}/overview`}>Open QuarterMark research</SourceLink><button type="button" onClick={refreshCompany} disabled={detailLoading} className="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 bg-white px-3 py-2 text-[11px] font-semibold text-slate-700 transition hover:bg-slate-50 disabled:opacity-50"><RefreshCw size={13} className={detailLoading ? 'animate-spin' : ''}/>Refresh data</button></div>
        </div>
        <div className="grid grid-cols-2 gap-2 border-t border-slate-100 bg-slate-50/70 p-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-7">{kpis.map((kpi) => <Metric key={kpi.label} {...kpi}/>)}</div>
        {quote?.timestamp && <div className="flex flex-wrap items-center justify-between gap-2 px-4 py-2 text-[10px] text-slate-500"><span>Quote timestamp: {textOf(quote.timestamp)}</span><span>News and event window: last {RECENT_DAYS} calendar days</span></div>}
      </header>
      {detailLoading && !company && <section className="flex items-center gap-2 rounded-2xl border border-blue-100 bg-blue-50 p-5 text-sm text-blue-900"><LoaderCircle className="animate-spin" size={17}/>Loading QuarterMark company research for {symbol}…</section>}
      {detailError && <section className="rounded-2xl border border-amber-200 bg-amber-50 p-4 text-xs leading-5 text-amber-900">{detailError}</section>}
      {company && <div className="space-y-4">{sections.map(({ section, key, label, kind }, index) => key === 'concall_summary' ? <ConcallResearchSection key={key} section={section || { section: label, available: false, reason: 'QuarterMark did not return a readable summary or transcript.' }}/> : <ResearchSection key={key} section={section || { section: label, available: false, reason: 'The source did not return this section.' }} index={index} kind={kind}/>)}</div>}
      <p className="pb-3 text-[10px] text-slate-400">QuarterMark provides company metrics and research sections; dated event and news cards link to their original sources. News, announcements and dated events are restricted to the last three calendar days. Periodic financial and ownership filings show the latest available source records because they are not daily news feeds. Missing values remain unavailable.</p>
    </main>;
  }

  return <main className="mx-auto w-full max-w-[1500px] space-y-5 px-3 py-5 sm:px-5 lg:px-7">
    <header><div className="text-[10px] font-bold uppercase tracking-[0.18em] text-blue-700">NIVESHAM · Company research</div><h1 className="mt-1 text-2xl font-bold tracking-tight text-slate-950">Stock Screener</h1><p className="mt-1 max-w-2xl text-xs leading-5 text-slate-500">Search the full QuarterMark company directory and open analyst-friendly company profiles with financial results, shareholding, peers, earnings-call summaries and full transcripts. Company source links open the NSE quote page.</p></header>
    <section className="rounded-2xl border border-slate-200 bg-white p-3 shadow-sm sm:p-4">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2"><label className="block text-[11px] font-semibold text-slate-700" htmlFor="stock-screener-search">Search company name or ticker</label><span className="text-[10px] text-slate-500">QuarterMark company directory · 10-minute temporary cache</span></div>
      <div className="flex flex-col gap-2 sm:flex-row"><div className="relative min-w-0 flex-1"><Search className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" size={17}/><input id="stock-screener-search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="e.g. LAURUS LABS, RELIANCE, TCS, HDFCBANK" className="w-full rounded-xl border border-slate-300 bg-white py-3 pl-10 pr-3 text-sm outline-none transition focus:border-blue-500 focus:ring-2 focus:ring-blue-100"/></div><button type="button" onClick={() => { searchMemory.delete(query.trim().toLowerCase()); forceRefreshRef.current.search = true; setRefreshTick((tick) => tick + 1); }} className="inline-flex items-center justify-center gap-2 rounded-xl bg-slate-900 px-4 py-3 text-xs font-semibold text-white transition hover:bg-slate-700"><Search size={14}/>Search companies</button></div>
      <div className="mt-3">{searchLoading ? <div className="flex items-center gap-2 p-4 text-xs text-slate-500"><LoaderCircle className="animate-spin" size={15}/>Searching QuarterMark company directory…</div> : searchError ? <div className="rounded-lg bg-amber-50 p-3 text-xs text-amber-800">{searchError}</div> : results.length ? <div className="overflow-x-auto rounded-xl border border-slate-200"><table className="w-full min-w-[760px] text-left"><thead className="bg-slate-50 text-[10px] uppercase tracking-wide text-slate-500"><tr><th className="px-3 py-3">Company</th><th className="px-3 py-3">Symbol / ISIN</th><th className="px-3 py-3 text-right">Last available price</th><th className="px-3 py-3 text-right">Change</th><th className="px-3 py-3">Source</th></tr></thead><tbody>{results.map((row) => <tr key={`${row.symbol}-${row.isin || ''}`} className="border-t border-slate-100 transition hover:bg-blue-50/40"><td className="px-3 py-3"><button type="button" onClick={() => navigate(`/stock-screener/${encodeURIComponent(row.symbol)}`)} className="text-left"><span className="block text-xs font-bold text-slate-900 hover:text-blue-700">{row.name || row.symbol}</span><span className="mt-0.5 block text-[10px] text-slate-500">{row.segment || 'Listed equity'}</span></button></td><td className="px-3 py-3"><span className="block text-xs font-semibold text-slate-800">{row.symbol}</span><span className="block text-[10px] text-slate-500">{row.isin || 'ISIN unavailable'}</span></td><td className="px-3 py-3 text-right text-xs tabular-nums">{row.price == null ? 'Unavailable' : `₹${fmt(row.price)}`}<span className="mt-0.5 block text-[9px] text-slate-400">{row.price_as_of || ''}</span></td><td className={`px-3 py-3 text-right text-xs font-semibold tabular-nums ${row.change_pct == null ? 'text-slate-400' : Number(row.change_pct) >= 0 ? 'text-emerald-700' : 'text-rose-700'}`}>{row.change_pct == null ? 'Unavailable' : `${Number(row.change_pct) > 0 ? '+' : ''}${fmt(row.change_pct)}%`}</td><td className="px-3 py-3"><SourceLink href={row.nse_url || `https://www.nseindia.com/get-quotes/equity?symbol=${encodeURIComponent(row.symbol || '')}`}>NSE quote</SourceLink></td></tr>)}</tbody></table><div className="border-t border-slate-100 px-3 py-2 text-[10px] text-slate-500">Showing {results.length} matches from the QuarterMark company directory. Source links open NSE quote pages.</div></div> : <div className="rounded-lg bg-slate-50 p-4 text-xs text-slate-500">{query.trim() ? 'No companies matched this search. Try the QuarterMark ticker or a shorter company-name query.' : 'Type a company name or ticker to search the QuarterMark company directory.'}</div>}</div>
    </section>
    <section className="grid gap-3 md:grid-cols-3"><article className="rounded-xl border border-slate-200 bg-white p-4"><Building2 className="text-blue-700" size={19}/><h3 className="mt-3 text-xs font-bold text-slate-900">Overview & KPIs</h3><p className="mt-1 text-[11px] leading-5 text-slate-500">Price, day range, market cap, valuation and company identity when reported.</p></article><article className="rounded-xl border border-slate-200 bg-white p-4"><BarChart3 className="text-blue-700" size={19}/><h3 className="mt-3 text-xs font-bold text-slate-900">Financials & ownership</h3><p className="mt-1 text-[11px] leading-5 text-slate-500">Structured financials, ownership and concall data from QuarterMark when available.</p></article><article className="rounded-xl border border-slate-200 bg-white p-4"><Newspaper className="text-blue-700" size={19}/><h3 className="mt-3 text-xs font-bold text-slate-900">Recent events & news</h3><p className="mt-1 text-[11px] leading-5 text-slate-500">Announcements, board meetings, concall disclosures and Google News stories from the last three days.</p></article></section>
  </main>;
}
