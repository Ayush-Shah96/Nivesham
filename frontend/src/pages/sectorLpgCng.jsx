import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { AlertCircle, Clock3, ExternalLink, Loader2, RefreshCw } from 'lucide-react';
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { PageHeader } from '../components/UI';
import api from '../lib/api';

const TABS = [
  { key: 'overview', label: 'Overview' },
  { key: 'production', label: 'Production' },
  { key: 'consumption', label: 'Consumption' },
];

const PETROLEUM_PRODUCTS = [
  'Crude Oil', 'LPG', 'MS', 'Naphtha', 'ATF', 'SKO', 'HSD', 'LOBS', 'Fuel Oil', 'Bitumen', 'Petcoke', 'Others',
];

const SERIES_COLORS = [
  '#2563eb', '#0f766e', '#d97706', '#7c3aed', '#dc2626', '#0891b2',
  '#65a30d', '#c026d3', '#ea580c', '#4f46e5', '#15803d', '#be123c',
];

const fmt = (value) => {
  if (value === null || value === undefined || value === '') return '—';
  const number = Number(value);
  return Number.isFinite(number)
    ? new Intl.NumberFormat('en-IN', { maximumFractionDigits: 0 }).format(number)
    : String(value);
};


function buildRows(series, mode) {
  const rows = new Map();
  (series || []).forEach((item, index) => {
    (item.data || []).forEach((point) => {
      const date = String(point.label || '');
      const fy = String(point.financial_year || '');
      const yearMatch = date.match(/(20\d{2})/);
      const monthMatch = date.match(/^[A-Za-z]{3}/);
      const rowKey = mode === 'yearly' ? fy : date;
      if (!rowKey) return;
      if (!rows.has(rowKey)) rows.set(rowKey, { label: rowKey, __sort: Number(yearMatch?.[1] || 0) * 100 + (monthMatch ? new Date(`${monthMatch[0]} 1, 2000`).getMonth() + 1 : 0) });
      const row = rows.get(rowKey);
      const value = Number(point.value);
      if (!Number.isFinite(value)) return;
      if (mode === 'yearly') row[`series_${index}`] = (row[`series_${index}`] || 0) + value;
      else row[`series_${index}`] = value;
    });
  });
  return [...rows.values()].sort((a, b) => a.__sort - b.__sort).map(({ __sort, ...row }) => row);
}

function ChartCard({ chart, mode }) {
  const series = useMemo(() => {
    const sourceSeries = chart.series || [];
    if (!['Imports', 'Exports'].includes(chart.title)) return sourceSeries;
    const byName = new Map(sourceSeries.map((item) => [item.name, item]));
    return PETROLEUM_PRODUCTS.map((name) => byName.get(name) || { name, data: [] });
  }, [chart]);
  const rows = useMemo(() => buildRows(series, mode), [series, mode]);
  return <section className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
    <div className="flex items-center justify-between gap-3 border-b border-slate-100 px-4 py-3">
      <div>
        <h2 className="text-sm font-semibold text-slate-900">{chart.title}</h2>
        <p className="mt-0.5 text-xs text-slate-500">X-axis: {mode === 'yearly' ? 'Financial years' : 'Months'} · Y-axis: {chart.unit || 'PPAC published values'}</p>
      </div>
      {chart.source_url && <a href={chart.source_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-[11px] font-semibold text-slate-500 hover:text-blue-700">PPAC <ExternalLink size={11}/></a>}
    </div>
    <div className="h-[360px] p-3">
      {!rows.length ? <div className="flex h-full items-center justify-center text-sm text-slate-400">No validated source observations are available.</div> : <ResponsiveContainer width="100%" height="100%">
        <LineChart data={rows} margin={{ top: 12, right: 18, left: 10, bottom: 32 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
          <XAxis dataKey="label" tick={{ fontSize: 10 }} angle={mode === 'yearly' ? 0 : -25} textAnchor={mode === 'yearly' ? 'middle' : 'end'} height={mode === 'yearly' ? 26 : 54} />
          <YAxis tick={{ fontSize: 10 }} tickFormatter={fmt} />
          <Tooltip formatter={(value) => fmt(value)} />
          <Legend wrapperStyle={{ fontSize: 10 }} />
          {series.map((item, index) => <Line key={item.name} type="monotone" dataKey={`series_${index}`} name={item.name} stroke={SERIES_COLORS[index % SERIES_COLORS.length]} strokeWidth={2} dot={false} connectNulls={false} />)}
        </LineChart>
      </ResponsiveContainer>}
    </div>
  </section>;
}

function LatestMetrics({ charts, section }) {
  const metrics = [];
  charts.filter((chart) => chart.section === section).forEach((chart) => {
    (chart.series || []).forEach((series) => {
      const latest = (series.data || [])[series.data?.length - 1];
      if (latest && Number.isFinite(Number(latest.value))) metrics.push({ title: chart.title, name: series.name, value: latest.value, unit: chart.unit, period: latest.label });
    });
  });
  return <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
    {metrics.slice(0, 8).map((item, index) => <div key={`${item.title}-${item.name}-${index}`} className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
      <div className="text-[10px] font-bold uppercase tracking-wide text-slate-500">{item.name}</div>
      <div className="mt-1 text-xl font-bold text-slate-900">{fmt(item.value)}</div>
      <div className="mt-1 text-[11px] text-slate-400">{item.title} · {item.period} · {item.unit}</div>
    </div>)}
  </div>;
}

export default function SectorLpgCng() {
  const [report, setReport] = useState(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState('');
  const [tab, setTab] = useState('overview');
  const [mode, setMode] = useState('monthly');

  const load = useCallback(async (refresh = false) => {
    if (refresh) setRefreshing(true); else setLoading(true);
    setError('');
    try {
      const result = await api.sectorScreenerReport('lpg', 'overview', { refresh });
      setReport(result);
      if (result?.error) setError(result.error);
    } catch (err) {
      setError(err?.message || 'Unable to load PPAC petroleum / natural-gas data.');
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    if (!report?.refresh_in_progress) return undefined;
    const timer = window.setTimeout(() => load(false), 3000);
    return () => window.clearTimeout(timer);
  }, [report?.refresh_in_progress, load]);

  const data = report?.data || {};
  const payload = data.sector_payload || {};
  const charts = useMemo(() => (data.charts || []).filter((chart) => chart?.section === tab), [data.charts, tab]);
  const sources = payload.sources || data.source_views || [];
  const status = report?.status || 'queued';

  return <div className="space-y-5 pb-8">
    <PageHeader
      eyebrow="NIVESHAM · INDUSTRY RESEARCH"
      title="Petroleum & Natural Gas Market Intelligence"
      description="PPAC source-backed imports/exports, LNG imports, production and domestic consumption. Only the required source datasets are collected."
      action={<button onClick={() => load(true)} disabled={loading || refreshing} className="inline-flex items-center gap-2 rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm font-semibold text-slate-700 shadow-sm hover:bg-slate-50 disabled:opacity-50"><RefreshCw size={15} className={refreshing ? 'animate-spin' : ''}/>Refresh PPAC</button>}
    />

    <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-slate-200 bg-white p-3 shadow-sm">
      <div className="flex flex-wrap gap-1">{TABS.map((item) => <button key={item.key} onClick={() => setTab(item.key)} className={`rounded-lg px-4 py-2 text-xs font-semibold transition ${tab === item.key ? 'bg-blue-700 text-white shadow-sm' : 'text-slate-600 hover:bg-slate-100'}`}>{item.label}</button>)}</div>
      <div className="flex items-center gap-1 rounded-lg bg-slate-100 p-1"><button onClick={() => setMode('monthly')} className={`rounded-md px-3 py-1.5 text-xs font-semibold ${mode === 'monthly' ? 'bg-white text-blue-700 shadow-sm' : 'text-slate-600'}`}>Monthly</button><button onClick={() => setMode('yearly')} className={`rounded-md px-3 py-1.5 text-xs font-semibold ${mode === 'yearly' ? 'bg-white text-blue-700 shadow-sm' : 'text-slate-600'}`}>Yearly</button></div>
    </div>

    <div className="flex items-center justify-between gap-3 rounded-xl border border-slate-200 bg-white p-3 shadow-sm text-xs text-slate-500"><span className="inline-flex items-center gap-2"><span className={`h-2 w-2 rounded-full ${status === 'ready' ? 'bg-emerald-500' : status === 'failed' ? 'bg-red-500' : 'bg-amber-500'}`}/>{data.refreshed_at ? `Last collected ${new Date(data.refreshed_at).toLocaleString('en-IN')}` : ['queued','scraping'].includes(status) ? 'Collecting targeted PPAC datasets…' : 'Waiting for source data'}</span><span className="inline-flex items-center gap-1 text-slate-400"><Clock3 size={13}/>Scheduled sector cache</span></div>

    {error && <div className="rounded-xl border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900"><AlertCircle size={16} className="mr-2 inline"/>{error}</div>}

    {loading && !report ? <div className="flex min-h-52 items-center justify-center gap-2 rounded-xl border border-slate-200 bg-white text-sm text-slate-500"><Loader2 size={18} className="animate-spin"/>Loading targeted PPAC data…</div> : <>
      <LatestMetrics charts={data.charts || []} section={tab} />
      <div className="grid gap-4 xl:grid-cols-2">{charts.map((chart) => <ChartCard key={chart.key} chart={chart} mode={mode}/>)}</div>
      {!charts.length && <div className="rounded-xl border border-dashed border-slate-300 bg-white p-12 text-center text-sm text-slate-500">No validated data is currently available for this tab. Refresh the PPAC collection to retry.</div>}
    </>}

    <section className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm"><div className="border-b border-slate-100 px-4 py-3"><h2 className="text-sm font-semibold text-slate-900">Source pages</h2><p className="mt-0.5 text-xs text-slate-500">Only the seven PPAC datasets required by this module are collected.</p></div><div className="grid gap-2 p-3 sm:grid-cols-2 xl:grid-cols-3">{sources.map((source) => <a key={source.url} href={source.url} target="_blank" rel="noreferrer" className="flex items-start justify-between gap-2 rounded-lg border border-slate-100 px-3 py-2.5 text-xs text-slate-700 hover:border-blue-200 hover:bg-blue-50/40"><span>{source.label}</span><ExternalLink size={12} className="mt-0.5 shrink-0 text-slate-400"/></a>)}</div></section>

    {!!payload.diagnostics?.length && <details className="rounded-xl border border-slate-200 bg-white p-4"><summary className="cursor-pointer text-xs font-semibold text-slate-600">Collection diagnostics ({payload.diagnostics.length})</summary><ul className="mt-2 space-y-1 text-[11px] text-slate-500">{payload.diagnostics.slice(0, 30).map((item, index) => <li key={index}>{item}</li>)}</ul></details>}
  </div>;
}
