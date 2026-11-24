import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
  Activity, AlertCircle, BarChart3, CheckCircle2, ChevronDown, Clock3,
  Database, Layers3, Loader2, PieChart as PieIcon, RefreshCw, Search, Table2, TrendingUp,
} from 'lucide-react';
import {
  Area, AreaChart, Bar, BarChart, CartesianGrid, Cell, Legend, Line, LineChart,
  Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts';
import {
  ArrowLeftRight, BatteryCharging, Boxes, BrainCircuit, CarFront, ChevronRight,
  Cpu, FlaskConical, Fuel, Pill, Shield, Sun,
} from 'lucide-react';
import { PageHeader } from '../components/UI';
import api from '../lib/api';

const SECTORS = {
  pharma: { name: 'Pharma & Healthcare', description: 'Industry indicators, production, trade, pricing and healthcare market activity.', reports: [{ id: 'overview', label: 'Overview' }] },
  chemical: { name: 'Chemical', description: 'Capacity, production, exports, imports, product groups and state consumption.', reports: [{ id: 'overview', label: 'Overview' }] },
  semiconductor: { name: 'Semiconductors', description: 'Market size, manufacturing, trade, investment and technology trends.', reports: [{ id: 'overview', label: 'Overview' }] },
  defence: { name: 'Defence', description: 'Defence budget, production, exports, procurement and industry indicators.', reports: [{ id: 'overview', label: 'Overview' }] },
  automotive: { name: 'Automotive', description: 'Parivahan VAHAN registrations, fuel mix, vehicle categories, manufacturer and state rankings.', reports: [{ id: 'overview', label: 'Overview' }] },
  solar: { name: 'Solar', description: 'NISE DCR solar cell and module manufacturing indicators, charts and source views.', source_url: 'https://solardcrportal.nise.res.in/Summary/index', reports: [{ id: 'overview', label: 'Overview' }] },
  commodities: { name: 'Commodities', description: 'Energy, metals, agriculture and industrial commodity prices and performance.', reports: [{ id: 'overview', label: 'Overview' }] },
  tradestat: { name: 'TradeStat', description: 'HS-code watchlists, monthly imports, exports and historical trade trends.', reports: [{ id: 'overview', label: 'Commodity Trade Monitor' }] },
  lpg: { name: 'LPG / CNG', description: 'LPG and natural-gas production, imports, consumption, connections and sales.', reports: [{ id: 'overview', label: 'Overview' }] },
  gpu: { name: 'GPU / Compute', description: 'GPU rental pricing, compute benchmarks and AI infrastructure indices.', reports: [{ id: 'overview', label: 'Overview' }] },
  battery: { name: 'Battery Installation', description: 'Battery storage deployments, BESS projects, capacity, tenders and manufacturing.', reports: [{ id: 'overview', label: 'Overview' }] },
};


const SECTOR_CARDS = [
  { id: 'pharma', name: 'Pharma & Healthcare', description: 'Track pharmaceutical manufacturing, healthcare activity, pricing, production and trade indicators.', icon: Pill, tone: 'blue' },
  { id: 'chemical', name: 'Chemical', description: 'Explore chemical capacity, production, product groups, imports, exports and state consumption.', icon: FlaskConical, tone: 'violet' },
  { id: 'semiconductor', name: 'Semiconductors', description: 'Follow semiconductor manufacturing, investments, market activity and technology trends.', icon: Cpu, tone: 'cyan' },
  { id: 'defence', name: 'Defence', description: 'Monitor defence production, procurement, budgets, exports and industry developments.', icon: Shield, tone: 'slate' },
  { id: 'automotive', name: 'Automotive', description: 'Analyse vehicle registrations, fuel mix, EV adoption, manufacturers and state trends.', icon: CarFront, tone: 'amber' },
  { id: 'solar', name: 'Solar', description: 'Explore solar cell and module manufacturing, capacity and industry indicators.', icon: Sun, tone: 'orange' },
  { id: 'commodities', name: 'Commodities', description: 'Follow energy, metals, agriculture and industrial commodity markets.', icon: Boxes, tone: 'emerald' },
  { id: 'tradestat', name: 'TradeStat', description: 'Analyse monthly Indian imports and exports by HS code with commodity-level history.', icon: ArrowLeftRight, tone: 'indigo' },
  { id: 'lpg', name: 'LPG / CNG', description: 'Track gas production, imports, consumption, connections and sales trends.', icon: Fuel, tone: 'teal' },
  { id: 'gpu', name: 'GPU / Compute', description: 'Monitor GPU rental pricing, compute benchmarks and AI infrastructure indicators.', icon: BrainCircuit, tone: 'purple' },
  { id: 'battery', name: 'Battery Installation', description: 'Explore battery storage deployments, BESS projects, tenders and manufacturing.', icon: BatteryCharging, tone: 'green' },
];

const SECTOR_ICON_TONES = {
  blue: 'bg-blue-50 text-blue-700 group-hover:bg-blue-100',
  violet: 'bg-violet-50 text-violet-700 group-hover:bg-violet-100',
  cyan: 'bg-cyan-50 text-cyan-700 group-hover:bg-cyan-100',
  slate: 'bg-slate-100 text-slate-700 group-hover:bg-slate-200',
  amber: 'bg-amber-50 text-amber-700 group-hover:bg-amber-100',
  orange: 'bg-orange-50 text-orange-700 group-hover:bg-orange-100',
  emerald: 'bg-emerald-50 text-emerald-700 group-hover:bg-emerald-100',
  indigo: 'bg-indigo-50 text-indigo-700 group-hover:bg-indigo-100',
  teal: 'bg-teal-50 text-teal-700 group-hover:bg-teal-100',
  purple: 'bg-purple-50 text-purple-700 group-hover:bg-purple-100',
  green: 'bg-green-50 text-green-700 group-hover:bg-green-100',
};

export function SectorScreenerLanding() {
  const navigate = useNavigate();
  const [query, setQuery] = useState('');
  const visible = SECTOR_CARDS.filter((sector) => {
    const q = query.trim().toLowerCase();
    return !q || `${sector.name} ${sector.description}`.toLowerCase().includes(q);
  });
  return (
    <div className="space-y-7 pb-8">
      <header className="flex flex-col gap-5 md:flex-row md:items-end md:justify-between">
        <div className="min-w-0">
          <div className="mb-2 inline-flex items-center gap-2 text-[11px] font-bold uppercase tracking-[.18em] text-amber-700"><span className="h-px w-6 bg-amber-600/60" />NIVESHAM · Sector research</div>
          <h1 className="font-display text-3xl font-bold tracking-tight text-navy-900 md:text-[2.25rem]">Sector Screener</h1>
          <p className="mt-2 max-w-2xl text-[15px] leading-relaxed text-slate-600">Industry dashboards with key indicators, charts and data tables. Choose a sector to explore its reports.</p>
        </div>
        <div className="relative w-full md:w-72">
          <Search size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Find a sector"
            aria-label="Find a sector"
            className="w-full rounded-xl border border-slate-200 bg-white py-2.5 pl-9 pr-3 text-sm outline-none transition focus:border-navy-900 focus:ring-4 focus:ring-navy-900/10"
          />
        </div>
      </header>

      <p className="text-xs font-semibold text-slate-500">{visible.length} of {SECTOR_CARDS.length} sectors</p>

      {visible.length ? (
        <section className="grid grid-cols-1 gap-5 sm:grid-cols-2 xl:grid-cols-3">
          {visible.map((sector) => {
            const Icon = sector.icon;
            return (
              <button
                key={sector.id}
                type="button"
                onClick={() => navigate(`/sector-screener/${sector.id}`)}
                className="group flex min-h-[200px] flex-col rounded-2xl border border-slate-200/80 bg-white p-6 text-left shadow-[0_1px_2px_rgba(15,27,45,.04),0_12px_32px_-18px_rgba(15,27,45,.14)] transition duration-200 hover:-translate-y-0.5 hover:border-amber-300 focus:outline-none focus-visible:ring-4 focus-visible:ring-navy-900/10"
              >
                <div className="flex w-full items-start justify-between gap-3">
                  <span className={`grid h-12 w-12 place-items-center rounded-xl transition ${SECTOR_ICON_TONES[sector.tone]}`}>
                    <Icon size={24} strokeWidth={1.8} />
                  </span>
                  <span className="inline-flex items-center gap-1 text-xs font-semibold text-slate-400 transition group-hover:text-navy-900">
                    Open <ChevronRight size={16} className="transition group-hover:translate-x-0.5" />
                  </span>
                </div>
                <div className="mt-6 font-display text-xl font-bold tracking-tight text-navy-900">{sector.name}</div>
                <p className="mt-2 text-sm leading-relaxed text-slate-500">{sector.description}</p>
              </button>
            );
          })}
        </section>
      ) : (
        <div className="rounded-2xl border border-dashed border-slate-300 bg-white p-10 text-center text-sm text-slate-500">No sectors match “{query}”.</div>
      )}
    </div>
  );
}

const COLORS = ['#2563eb', '#0f766e', '#d97706', '#7c3aed', '#db2777', '#0891b2', '#65a30d', '#ea580c'];
const fmtNumber = (value) => {
  if (value === null || value === undefined || value === '') return '—';
  const n = Number(value);
  if (!Number.isFinite(n)) return String(value);
  return new Intl.NumberFormat('en-IN', { maximumFractionDigits: 2 }).format(n);
};
const safeSection = (value) => {
  const label = String(value || 'Overview').trim();
  return !label || /^(initial|http|rendered|network)$/i.test(label) ? 'Overview' : label.replace(/^filter-.*$/, 'Filtered data');
};

function Panel({ title, subtitle, icon: Icon = Activity, children, className = '' }) {
  return (
    <section className={`min-w-0 overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm ${className}`}>
      <div className="flex items-start gap-3 border-b border-slate-100 px-4 py-3">
        <div className="mt-0.5 rounded-lg bg-blue-50 p-2 text-blue-700"><Icon size={16} /></div>
        <div className="min-w-0">
          <h3 className="truncate text-sm font-semibold text-slate-900">{title}</h3>
          {subtitle && <p className="mt-0.5 text-xs text-slate-500">{subtitle}</p>}
        </div>
      </div>
      {children}
    </section>
  );
}

function KpiCard({ item, index }) {
  return (
    <div className="min-w-0 rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
      <div className="mb-2 flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wide text-slate-500">
        <span className="h-1.5 w-1.5 rounded-full" style={{ backgroundColor: COLORS[index % COLORS.length] }} />
        <span className="line-clamp-2">{item.label}</span>
      </div>
      <div className="break-words text-2xl font-bold tracking-tight text-slate-900">{item.display_value || fmtNumber(item.value)}</div>
      {item.change && <div className="mt-1 text-xs font-medium text-emerald-700">{item.change}</div>}
    </div>
  );
}

function toChartRows(series = []) {
  const rowsByLabel = new Map();
  series.forEach((s, index) => {
    (s.data || []).forEach((point) => {
      const label = String(point.label ?? '');
      if (!rowsByLabel.has(label)) rowsByLabel.set(label, { label });
      rowsByLabel.get(label)[`series_${index}`] = Number(point.value);
    });
  });
  return [...rowsByLabel.values()].slice(0, 300);
}

function ChartPanel({ chart }) {
  const series = Array.isArray(chart.series) ? chart.series : [];
  const rows = useMemo(() => toChartRows(series), [series]);
  const kind = chart.kind || 'bar';
  const firstSeries = series[0]?.data || [];
  const pieRows = firstSeries.map((point) => ({ name: point.label, value: Number(point.value) })).filter((point) => Number.isFinite(point.value));
  return (
    <Panel title={chart.title} subtitle={chart.unit || (chart.state && chart.state !== 'Overview' ? chart.state : '')} icon={kind === 'pie' ? PieIcon : BarChart3}>
      <div className="h-[330px] w-full p-3">
        {!rows.length ? (
          <div className="flex h-full items-center justify-center text-sm text-slate-400">No numeric series was exposed for this chart.</div>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            {kind === 'pie' ? (
              <PieChart>
                <Tooltip formatter={(value) => fmtNumber(value)} />
                <Legend />
                <Pie data={pieRows} dataKey="value" nameKey="name" outerRadius={112} labelLine={false} label={({ name, percent }) => `${name} ${(percent * 100).toFixed(1)}%`}>
                  {pieRows.map((entry, index) => <Cell key={`${entry.name}-${index}`} fill={COLORS[index % COLORS.length]} />)}
                </Pie>
              </PieChart>
            ) : kind === 'line' || kind === 'area' ? (
              <LineChart data={rows} margin={{ top: 8, right: 16, left: 4, bottom: 35 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                <XAxis dataKey="label" tick={{ fontSize: 10 }} angle={-22} textAnchor="end" height={55} interval="preserveStartEnd" />
                <YAxis tick={{ fontSize: 10 }} tickFormatter={fmtNumber} />
                <Tooltip formatter={(value) => fmtNumber(value)} />
                <Legend />
                {series.map((item, index) => kind === 'area'
                  ? <Area key={item.name} type="monotone" dataKey={`series_${index}`} name={item.name} stroke={COLORS[index % COLORS.length]} fill={COLORS[index % COLORS.length]} fillOpacity={0.12} connectNulls />
                  : <Line key={item.name} type="monotone" dataKey={`series_${index}`} name={item.name} stroke={COLORS[index % COLORS.length]} strokeWidth={2} dot={false} connectNulls />)}
              </LineChart>
            ) : (
              <BarChart data={rows} margin={{ top: 8, right: 16, left: 4, bottom: 45 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                <XAxis dataKey="label" tick={{ fontSize: 10 }} angle={-25} textAnchor="end" height={65} interval="preserveStartEnd" />
                <YAxis tick={{ fontSize: 10 }} tickFormatter={fmtNumber} />
                <Tooltip formatter={(value) => fmtNumber(value)} />
                <Legend />
                {series.map((item, index) => <Bar key={item.name} dataKey={`series_${index}`} name={item.name} fill={COLORS[index % COLORS.length]} radius={[4, 4, 0, 0]} maxBarSize={52} />)}
              </BarChart>
            )}
          </ResponsiveContainer>
        )}
      </div>
    </Panel>
  );
}

function DataTable({ table }) {
  const columns = table.columns || [];
  const rows = table.rows || [];
  return (
    <Panel title={table.title} subtitle={`${fmtNumber(table.row_count || rows.length)} records${table.truncated ? ' · first 10,000 displayed' : ''}`} icon={Table2}>
      <div className="max-h-[520px] overflow-auto">
        <table className="min-w-full border-separate border-spacing-0 text-xs">
          <thead className="sticky top-0 z-10 bg-slate-50">
            <tr>{columns.map((column, index) => <th key={`${column}-${index}`} className={`whitespace-nowrap border-b border-slate-200 px-3 py-2.5 font-semibold text-slate-600 ${index === 0 ? 'sticky left-0 z-20 bg-slate-50 text-left' : 'text-right'}`}>{column}</th>)}</tr>
          </thead>
          <tbody>
            {rows.map((row, rowIndex) => (
              <tr key={rowIndex} className="hover:bg-blue-50/40">
                {columns.map((column, colIndex) => {
                  const value = Array.isArray(row) ? row[colIndex] : row?.[column];
                  return <td key={`${rowIndex}-${colIndex}`} className={`whitespace-nowrap border-b border-slate-100 px-3 py-2 text-slate-700 ${String(column).toLowerCase().includes('growth') ? 'text-[10px] text-slate-500' : ''} ${colIndex === 0 ? 'sticky left-0 bg-white font-medium text-slate-900' : 'text-right tabular-nums'}`}>{value === null || value === undefined || value === '' ? '—' : String(value)}</td>;
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}


function AutomotiveDashboard({ dashboard }) {
  const [timeline, setTimeline] = useState('Monthly');
  const [category, setCategory] = useState('All');
  const [fuel, setFuel] = useState('All');
  const [maker, setMaker] = useState('All');
  const [evMode, setEvMode] = useState('All');
  const rows = dashboard?.rows || [];
  const options = dashboard?.filters || {};
  const isEv = (value) => /electric|\bev\b/i.test(String(value || ''));
  const timelineRows = useMemo(() => {
    const granularity = timeline.toLowerCase();
    const exact = rows.filter((row) => row.granularity === granularity);
    if (granularity === 'monthly' || exact.length) return exact;
    // Derive quarter/year rollups from monthly source observations only when
    // the source does not publish that granularity itself.
    const monthly = rows.filter((row) => row.granularity === 'monthly');
    const grouped = new Map();
    monthly.forEach((row) => {
      const text = String(row.period || '');
      let year = Number((text.match(/20\d{2}/) || [])[0]);
      let month = Number((text.match(/(?:^|[-/ ])(0?[1-9]|1[0-2])(?:$|[-/ ])/ ) || [])[1]);
      if (!month) {
        const names = ['jan','feb','mar','apr','may','jun','jul','aug','sep','oct','nov','dec'];
        const found = names.findIndex((name) => text.toLowerCase().includes(name));
        if (found >= 0) month = found + 1;
      }
      if (!year || !month) return;
      const period = granularity === 'quarterly' ? `Q${Math.ceil(month / 3)} ${year}` : String(year);
      const key = [period, row.category, row.fuel, row.maker, row.state].join('|');
      if (!grouped.has(key)) grouped.set(key, { ...row, period, granularity, registrations: 0 });
      grouped.get(key).registrations += Number(row.registrations || 0);
    });
    return [...grouped.values()];
  }, [rows, timeline]);
  const filtered = timelineRows.filter((row) => {
    if (category !== 'All' && row.category !== category) return false;
    if (fuel !== 'All' && row.fuel !== fuel) return false;
    if (maker !== 'All' && row.maker !== maker) return false;
    if (evMode === 'EV' && !isEv(row.fuel)) return false;
    if (evMode === 'Non-EV' && isEv(row.fuel)) return false;
    return true;
  });
  const orderedPeriods = [...new Set(filtered.map((row) => row.period).filter(Boolean))].sort((a, b) => {
    const parse = (value) => {
      const q = value.match(/Q([1-4])\s*(20\d{2})/i);
      if (q) return Number(q[2]) * 100 + Number(q[1]) * 3;
      const y = Number((value.match(/20\d{2}/) || [])[0]);
      const m = value.match(/(?:^|[-/ ])(0?[1-9]|1[0-2])(?:$|[-/ ])/);
      const names = ['jan','feb','mar','apr','may','jun','jul','aug','sep','oct','nov','dec'];
      const named = names.findIndex((name) => value.toLowerCase().includes(name));
      return y * 100 + (Number(m?.[1]) || (named >= 0 ? named + 1 : 0));
    };
    return parse(a) - parse(b);
  });
  const latestPeriod = orderedPeriods[orderedPeriods.length - 1];
  const summaryRows = latestPeriod ? filtered.filter((row) => row.period === latestPeriod) : filtered;
  const aggregate = (field, source = summaryRows) => {
    const map = new Map();
    source.forEach((row) => {
      const key = row[field] || 'Unknown';
      if (['Unknown', 'All', 'Total', 'All India'].includes(key)) return;
      map.set(key, (map.get(key) || 0) + Number(row.registrations || 0));
    });
    return [...map.entries()].sort((a, b) => b[1] - a[1]);
  };
  const total = summaryRows.reduce((sum, row) => sum + Number(row.registrations || 0), 0);
  const ev = summaryRows.filter((row) => isEv(row.fuel)).reduce((sum, row) => sum + Number(row.registrations || 0), 0);
  const evShare = total ? (ev / total) * 100 : null;
  const chartData = [
    { key: 'fuel', title: 'Fuel Mix', kind: 'pie', pairs: aggregate('fuel') },
    { key: 'category', title: 'Vehicle Registration by Category', kind: 'pie', pairs: aggregate('category') },
    { key: 'maker', title: 'Top 5 Makers by Registration Volume', kind: 'bar', pairs: aggregate('maker').slice(0, 5) },
    { key: 'state', title: 'Top 5 States by Registration Volume', kind: 'bar', pairs: aggregate('state').slice(0, 5) },
  ];
  const selectClass = 'rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-medium text-slate-700 outline-none focus:border-blue-500';
  const growthLabel = timeline === 'Monthly' ? 'MoM Growth' : timeline === 'Quarterly' ? 'QoQ Growth' : 'YoY Growth';
  const tableRows = useMemo(() => {
    const periods = orderedPeriods;
    const groups = new Map();
    filtered.filter((r) => !['Unknown', 'All', 'Total'].includes(r.maker)).forEach((r) => {
      const key = `${r.category}||${r.maker}`;
      if (!groups.has(key)) groups.set(key, { Category: r.category, Maker: r.maker });
      const target = groups.get(key);
      target[r.period] = (target[r.period] || 0) + Number(r.registrations || 0);
    });
    const result = [...groups.values()].map((item) => {
      const values = periods.map((p) => Number(item[p] || 0));
      const last = values[values.length - 1];
      const previous = values[values.length - 2];
      const growth = previous > 0 ? ((last - previous) / previous) * 100 : null;
      return { ...item, [growthLabel]: growth === null ? '—' : `${growth >= 0 ? '+' : ''}${growth.toFixed(1)}%` };
    });
    const byCategory = new Map();
    result.forEach((item) => {
      if (!byCategory.has(item.Category)) byCategory.set(item.Category, []);
      byCategory.get(item.Category).push(item);
    });
    return [...byCategory.values()].flatMap((items) => items.sort((a, b) => periods.reduce((s,p) => s + Number(b[p] || 0) - Number(a[p] || 0), 0)).slice(0, 5));
  }, [filtered, growthLabel]);
  const periods = orderedPeriods;
  const columns = ['Category', 'Maker', ...periods.slice(-24), growthLabel];
  return (
    <div className="space-y-5">
      <section className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
        <div className="mb-3 text-sm font-semibold text-slate-900">Registration filters</div>
        <div className="flex flex-wrap gap-2">
          <select className={selectClass} value={timeline} onChange={(e) => setTimeline(e.target.value)}><option>Monthly</option><option>Quarterly</option><option>Yearly</option></select>
          <select className={selectClass} value={category} onChange={(e) => setCategory(e.target.value)}><option value="All">All categories</option>{(options.category || []).map((x) => <option key={x}>{x}</option>)}</select>
          <select className={selectClass} value={evMode} onChange={(e) => setEvMode(e.target.value)}><option>All</option><option>EV</option><option>Non-EV</option></select>
          <select className={selectClass} value={fuel} onChange={(e) => setFuel(e.target.value)}><option value="All">All fuels</option>{(options.fuel || []).map((x) => <option key={x}>{x}</option>)}</select>
          <select className={selectClass} value={maker} onChange={(e) => setMaker(e.target.value)}><option value="All">All makers</option>{(options.maker || []).map((x) => <option key={x}>{x}</option>)}</select>
          <button className="rounded-lg border border-slate-200 px-3 py-2 text-xs font-semibold text-slate-600" onClick={() => { setTimeline('Monthly'); setCategory('All'); setFuel('All'); setMaker('All'); setEvMode('All'); }}>Reset</button>
        </div>
        <p className="mt-2 text-[11px] text-slate-400">Filters operate only on captured Parivahan registration records. Missing source dimensions are not inferred.</p>
      </section>
      <section>
        <div className="mb-3 text-sm font-bold text-slate-900">Key indicators</div>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <KpiCard item={{ label: 'Total Registrations', display_value: fmtNumber(total) }} index={0} />
          <KpiCard item={{ label: 'EV Share', display_value: evShare === null ? '—' : `${evShare.toFixed(2)}%` }} index={1} />
        </div>
      </section>
      <section className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        {chartData.map((item) => <ChartPanel key={item.key} chart={{ key: item.key, title: item.title, kind: item.kind, series: [{ name: item.title, data: item.pairs.map(([label, value]) => ({ label, value })) }] }} />)}
      </section>
      <DataTable table={{ key: 'automotive-registration', title: 'Vehicle Registrations — Top 5 Makers by Category', columns, rows: tableRows, row_count: tableRows.length }} />
      <div className="text-[11px] text-slate-400">Latest source period: {latestPeriod || dashboard?.latest_period || 'Not provided'} · {filtered.length.toLocaleString('en-IN')} registration records · {dashboard?.data_quality || ''}</div>
    </div>
  );
}

export default function SectorScreener() {
  const navigate = useNavigate();
  const { sectorId = 'pharma', reportKey } = useParams();
  const [catalog, setCatalog] = useState(null);
  const [response, setResponse] = useState(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState('');
  const [solarView, setSolarView] = useState('overview');
  const pollRef = useRef(null);

  useEffect(() => {
    let active = true;
    api.sectorScreenerCatalog().then((payload) => {
      if (!active || !Array.isArray(payload?.sectors)) return;
      const next = {};
      payload.sectors.forEach((item) => {
        next[item.key] = {
          name: item.name,
          description: item.description || '',
          source: item.source,
          source_url: item.source_url,
          reports: (item.reports || []).map((report) => ({ id: report.key, label: report.label })),
        };
      });
      if (active) setCatalog(next);
    }).catch(() => {});
    return () => { active = false; };
  }, []);

  const sectorCatalog = catalog || SECTORS;
  const sector = sectorCatalog[sectorId] || sectorCatalog.pharma || SECTORS.pharma;
  const activeReport = (sector.reports || [{ id: 'overview', label: 'Overview' }]).find((report) => report.id === (reportKey || 'overview')) || (sector.reports || [{ id: 'overview', label: 'Overview' }])[0];
  const load = useCallback(async ({ refresh = false, quiet = false } = {}) => {
    if (!quiet && !refresh) setLoading(true);
    if (refresh) setRefreshing(true);
    try {
      const result = await api.sectorScreenerReport(sectorId, activeReport.id, { refresh });
      setResponse(result);
      setError(result?.error || '');
    } catch (err) {
      setError(err?.message || 'Unable to load this sector report.');
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [sectorId, activeReport.id]);

  useEffect(() => {
    setResponse(null);
    setError('');
    setLoading(true);
    load();
    return () => { if (pollRef.current) window.clearInterval(pollRef.current); };
  }, [load]);

  const pending = response && ['queued', 'scraping'].includes(response.status);
  useEffect(() => {
    if (pollRef.current) window.clearInterval(pollRef.current);
    if (!pending) return undefined;
    let attempts = 0;
    pollRef.current = window.setInterval(() => {
      attempts += 1;
      if (attempts > 40) {
        window.clearInterval(pollRef.current);
        pollRef.current = null;
        return;
      }
      load({ quiet: true });
    }, 5000);
    return () => { if (pollRef.current) window.clearInterval(pollRef.current); };
  }, [pending, load]);

  const data = response?.data || {};
  const allKpis = data.kpis || [];
  const allCharts = data.charts || [];
  const allTables = data.tables || [];
  const solarViewLabel = solarView === 'cell'
    ? 'Solar Cell Manufacturer'
    : solarView === 'module'
      ? 'Solar Module Manufacturer'
      : 'Overview';
  const isInSolarView = (item) => {
    const state = String(item?.state || 'Overview').trim().toLowerCase();
    if (solarView === 'cell') return /solar\s+cell\s+manufacturer/.test(state);
    if (solarView === 'module') return /solar\s+module\s+manufacturer/.test(state);
    return !/solar\s+(?:cell|module)\s+manufacturer/.test(state);
  };
  // The shared collector captures the overview and both NISE manufacturer
  // drilldowns in one browser session. These controls switch the in-app view;
  // they never navigate the user to the external NISE website.
  const kpis = sectorId === 'solar' ? allKpis.filter(isInSolarView) : allKpis;
  const charts = sectorId === 'solar' ? allCharts.filter(isInSolarView) : allCharts;
  const tables = sectorId === 'solar' ? allTables.filter(isInSolarView) : allTables;
  const sections = useMemo(() => {
    const order = [];
    [...charts, ...tables].forEach((item) => {
      const name = safeSection(item.state);
      if (!order.includes(name)) order.push(name);
    });
    return order;
  }, [charts, tables]);

  const selectReport = (next) => navigate(`/sector-screener/${sectorId}/${next}`);
  const hasAnyData = Boolean(kpis.length || charts.length || tables.length);

  return (
    <div className="space-y-5 pb-8">
      <PageHeader
        eyebrow="NIVESHAM · INDUSTRY RESEARCH"
        title={sector.name}
        description={sector.description}
        action={(
          <button onClick={() => load({ refresh: true })} disabled={refreshing || loading} className="inline-flex items-center gap-2 rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm font-semibold text-slate-700 shadow-sm transition hover:bg-slate-50 disabled:opacity-50">
            <RefreshCw size={15} className={refreshing ? 'animate-spin' : ''} /> Refresh
          </button>
        )}
      />

      {sectorId === 'solar' && (
        <section className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
          <div className="mb-2 text-sm font-semibold text-slate-900">NISE DCR manufacturer view</div>
          <div className="flex flex-wrap gap-2" role="group" aria-label="Solar manufacturer view">
            {[
              { key: 'overview', label: 'Overview' },
              { key: 'cell', label: 'Solar Cell Manufacturer' },
              { key: 'module', label: 'Solar Module Manufacturer' },
            ].map((view) => (
              <button
                key={view.key}
                type="button"
                aria-pressed={solarView === view.key}
                onClick={() => setSolarView(view.key)}
                className={`rounded-lg border px-3 py-2 text-xs font-semibold transition ${solarView === view.key
                  ? 'border-blue-700 bg-blue-700 text-white shadow-sm'
                  : 'border-slate-200 bg-white text-slate-700 hover:border-blue-300 hover:bg-blue-50 hover:text-blue-800'}`}
              >
                {view.label}
              </button>
            ))}
          </div>
          <p className="mt-2 text-[11px] text-slate-400">
            Source views are captured by the backend and displayed here. Selecting a view does not leave this dashboard.
          </p>
        </section>
      )}

      {(sector.reports || []).length > 1 && (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-slate-200 bg-white px-4 py-3 shadow-sm">
          <div><div className="text-sm font-semibold text-slate-900">Report view</div><div className="mt-0.5 text-xs text-slate-500">Choose a report to explore.</div></div>
          <div className="relative min-w-[250px]">
            <select value={activeReport.id} onChange={(event) => selectReport(event.target.value)} className="w-full appearance-none rounded-lg border border-slate-300 bg-white px-3 py-2 pr-9 text-sm font-medium text-slate-800 outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-100">
              {sector.reports.map((report) => <option key={report.id} value={report.id}>{report.label}</option>)}
            </select>
            <ChevronDown size={15} className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-slate-500" />
          </div>
        </div>
      )}

      <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-slate-200 bg-white px-4 py-2.5 text-xs text-slate-500">
        <div className="flex items-center gap-2">
          {response?.status === 'ready' || response?.status === 'stale' ? <CheckCircle2 size={14} className="text-emerald-600" /> : <Loader2 size={14} className="animate-spin text-blue-600" />}
          <span>{data.refreshed_at ? `Last updated ${new Date(data.refreshed_at).toLocaleString('en-IN')}` : pending ? 'Collecting sector data…' : 'Waiting for report data'}</span>
        </div>
        <span className="inline-flex items-center gap-1"><Clock3 size={13} /> one-hour data cache</span>
      </div>

      {error && !hasAnyData && !pending && (
        <div className="flex items-start gap-2 rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900"><AlertCircle size={17} className="mt-0.5 shrink-0" /><div><div className="font-semibold">Report data could not be refreshed</div><p className="mt-1 text-xs leading-5">{error}</p></div></div>
      )}

      {loading && !response && (
        <div className="flex min-h-48 items-center justify-center gap-3 rounded-xl border border-slate-200 bg-white text-sm text-slate-500"><Loader2 size={18} className="animate-spin text-blue-600" /> Loading sector intelligence…</div>
      )}

      {pending && !hasAnyData && (
        <div className="flex items-center gap-3 rounded-xl border border-blue-100 bg-blue-50 p-5 text-sm text-blue-900"><Loader2 size={18} className="animate-spin" /><div><div className="font-semibold">Preparing this report</div><p className="mt-1 text-xs text-blue-700">The first capture is running in the background. This dashboard will update automatically.</p></div></div>
      )}

      {sectorId === 'automotive' && data.automotive_dashboard ? (
        <AutomotiveDashboard dashboard={data.automotive_dashboard} />
      ) : <>
      {!!kpis.length && (
        <section>
          <div className="mb-3 flex items-center gap-2"><Activity size={16} className="text-blue-700"/><h2 className="text-sm font-bold text-slate-900">Key indicators</h2><span className="text-xs text-slate-400">{kpis.length} metrics</span></div>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">{kpis.map((item, index) => <KpiCard key={`${item.label}-${item.display_value}`} item={item} index={index} />)}</div>
        </section>
      )}

      {sections.map((section) => {
        const sectionCharts = charts.filter((chart) => safeSection(chart.state) === section);
        const sectionTables = tables.filter((table) => safeSection(table.state) === section);
        return (
          <section key={section} className="space-y-3">
            {(sections.length > 1 || section !== 'Overview') && <div className="flex items-center gap-2 border-b border-slate-200 pb-2"><Layers3 size={16} className="text-blue-700"/><h2 className="text-base font-bold text-slate-900">{section}</h2></div>}
            {!!sectionCharts.length && <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">{sectionCharts.map((chart) => <ChartPanel key={chart.key} chart={chart} />)}</div>}
            {!!sectionTables.length && <div className="space-y-4">{sectionTables.map((table) => <DataTable key={table.key} table={table} />)}</div>}
          </section>
        );
      })}

      {!loading && !pending && !hasAnyData && !error && (
        <div className="rounded-xl border border-slate-200 bg-white p-8 text-center">
          <Database size={25} className="mx-auto text-slate-300" />
          <h3 className="mt-3 text-sm font-semibold text-slate-900">No structured data available for {sectorId === 'solar' ? solarViewLabel : 'this report'} yet</h3>
          <p className="mx-auto mt-1 max-w-xl text-xs leading-5 text-slate-500">The captured source view did not expose validated KPI values, chart datasets or table rows. No placeholder metrics have been created.</p>
          <button onClick={() => load({ refresh: true })} className="mt-4 inline-flex items-center gap-2 rounded-lg bg-blue-700 px-3 py-2 text-xs font-semibold text-white hover:bg-blue-800"><RefreshCw size={13}/> Try again</button>
        </div>
      )}
      </>}
    </div>
  );
}
