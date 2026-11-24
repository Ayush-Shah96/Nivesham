import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Activity,
  ArrowDownToLine,
  ArrowLeft,
  ArrowUpFromLine,
  Clock3,
  Loader2,
  Plus,
  RefreshCw,
  Trash2,
  X,
} from 'lucide-react';
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { PageHeader } from '../components/UI';
import api from '../lib/api';

const number = (value) => {
  if (value === null || value === undefined || value === '') return '—';
  const n = Number(value);
  return Number.isFinite(n)
    ? new Intl.NumberFormat('en-IN', { maximumFractionDigits: 2 }).format(n)
    : String(value);
};

const tradeLabel = (trade) => (trade === 'export' ? 'Export' : 'Import');

const formatMonth = (value) => {
  const date = new Date(`${value}-01T00:00:00`);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleDateString('en-IN', { month: 'short', year: 'numeric' });
};

const formatDateTime = (value, compact = false) => {
  if (!value) return 'Not available';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return 'Not available';
  return date.toLocaleString('en-IN', compact
    ? { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }
    : { day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' });
};

const cleanPeriodLabel = (value, fallback) => {
  const label = String(value || fallback || '').trim();
  return label.replace(/\s*\(([RF])\)\s*$/i, '').replace(/\s+/g, '-');
};

function TradeHistoryModal({ code, commodity, tradeType, onClose }) {
  const [payload, setPayload] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [collection, setCollection] = useState(null);

  const load = useCallback(async (refresh = false, quiet = false) => {
    if (!quiet) setLoading(true);
    if (refresh) setError('');
    try {
      const response = await api.tradestatCommodityHistory(code, { refresh, tradeType });
      const nextPayload = response?.data?.sector_payload || {};
      if (response?.data) setPayload(nextPayload);
      setCollection(response || null);
      if (response?.error) setError(response.error);
      const directionRows = nextPayload?.[tradeType === 'export' ? 'exports' : 'imports'] || [];
      if (!directionRows.length && response?.data?.capture_completeness?.status === 'failed') {
        setError(response?.error || 'No historical observations are available for this commodity.');
      }
    } catch (err) {
      if (!quiet) setError(err?.message || 'Unable to load historical trade data.');
    } finally {
      if (!quiet) setLoading(false);
    }
  }, [code, tradeType]);

  useEffect(() => { load(false); }, [load]);
  useEffect(() => {
    if (!collection?.refresh_in_progress) return undefined;
    const timer = window.setTimeout(() => load(false, true), 5000);
    return () => window.clearTimeout(timer);
  }, [collection?.refresh_in_progress, load]);

  const rows = useMemo(() => {
    const source = payload?.[tradeType === 'export' ? 'exports' : 'imports'] || [];
    return source
      .filter((item) => item?.trade_value !== null && item?.trade_value !== undefined)
      .map((item) => ({
        period: item.period,
        value: Number(item.trade_value),
        previousYear: item.previous_year_value == null ? null : Number(item.previous_year_value),
        currentYear: item.current_year_value == null ? Number(item.trade_value) : Number(item.current_year_value),
        growth: item.growth_percent == null
          ? (item.previous_year_value != null && Number(item.previous_year_value) !== 0 && item.current_year_value != null
            ? ((Number(item.current_year_value) / Number(item.previous_year_value)) - 1) * 100
            : null)
          : Number(item.growth_percent),
        unit: item.unit || '',
      }))
      .sort((a, b) => a.period.localeCompare(b.period));
  }, [payload, tradeType]);

  const units = [...new Set(rows.map((row) => row.unit).filter(Boolean))];
  const unitsConsistent = units.length <= 1;
  const yoyChartRows = rows.filter((row) => row.growth != null && Number.isFinite(row.growth));
  const unitLabel = units.length === 1 ? units[0] : units.length > 1 ? 'Multiple units' : '—';

  return (
    <div className="fixed inset-0 z-[80] flex items-center justify-center bg-slate-950/45 p-3 sm:p-6" role="dialog" aria-modal="true" aria-label={`Trade history for ${code}`}>
      <div className="flex max-h-[94vh] w-full max-w-6xl flex-col overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-2xl">
        <div className="flex items-start justify-between gap-3 border-b border-slate-100 px-5 py-4">
          <div>
            <div className="text-[11px] font-bold uppercase tracking-wider text-blue-700">HS Code {code}</div>
            <h2 className="mt-1 text-lg font-bold text-slate-900">{commodity}</h2>
            <p className="mt-1 text-xs text-slate-500">{tradeLabel(tradeType)} · Last 12 months</p>
          </div>
          <button onClick={onClose} className="rounded-lg p-2 text-slate-500 hover:bg-slate-100" aria-label="Close"><X size={18} /></button>
        </div>

        <div className="min-h-[420px] overflow-y-auto p-5">
          {(loading || (collection?.refresh_in_progress && !rows.length)) ? (
            <div className="flex min-h-72 items-center justify-center gap-2 text-sm text-slate-500">
              <Loader2 className="animate-spin" size={18} />
              {loading ? 'Loading monthly data…' : 'Collecting monthly data…'}
            </div>
          ) : rows.length ? <>
            <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
              <span className="text-xs text-slate-500">Unit: {unitLabel}</span>
              <div className="flex items-center gap-2">
                {collection?.refresh_in_progress ? <span className="text-[11px] text-amber-700">Updating…</span> : null}
                <button onClick={() => load(true)} className="inline-flex items-center gap-2 rounded-lg border border-slate-200 px-3 py-2 text-xs font-semibold text-slate-700 hover:bg-slate-50">
                  <RefreshCw size={13} /> Refresh
                </button>
              </div>
            </div>

            <div className="mb-5 rounded-xl border border-slate-200 bg-slate-50/50 p-3">
              <div className="mb-2 text-xs font-semibold text-slate-700">Monthly {tradeLabel(tradeType).toLowerCase()} value</div>
              {unitsConsistent ? (
                <div className="h-[330px] w-full">
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={rows} margin={{ top: 10, right: 16, left: 4, bottom: 24 }}>
                      <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                      <XAxis dataKey="period" tickFormatter={formatMonth} tick={{ fontSize: 11 }} />
                      <YAxis tick={{ fontSize: 11 }} tickFormatter={number} />
                      <Tooltip labelFormatter={formatMonth} formatter={(value) => [number(value), 'Trade value']} />
                      <Legend />
                      <Bar dataKey="value" name="Trade value" fill={tradeType === 'export' ? '#0f766e' : '#2563eb'} radius={[4, 4, 0, 0]} />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              ) : <div className="flex h-32 items-center justify-center text-xs text-amber-800">The monthly observations use different units and cannot be combined into one chart.</div>}
            </div>

            <div className="mb-5 rounded-xl border border-slate-200 bg-slate-50/50 p-3">
              <div className="mb-2 text-xs font-semibold text-slate-700">Monthly year-on-year growth</div>
              {!unitsConsistent ? (
                <div className="flex h-32 items-center justify-center text-xs text-amber-800">The growth chart is hidden because the units differ.</div>
              ) : yoyChartRows.length ? (
                <div className="h-[340px] w-full">
                  <ResponsiveContainer width="100%" height="100%">
                    <LineChart data={yoyChartRows} margin={{ top: 10, right: 16, left: 4, bottom: 24 }}>
                      <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                      <XAxis dataKey="period" tickFormatter={formatMonth} tick={{ fontSize: 11 }} />
                      <YAxis tick={{ fontSize: 11 }} tickFormatter={(value) => `${number(value)}%`} />
                      <Tooltip labelFormatter={formatMonth} formatter={(value) => [`${number(value)}%`, 'YoY growth']} />
                      <Line type="monotone" dataKey="growth" name="YoY growth" stroke="#7c3aed" strokeWidth={2.5} dot={{ r: 3 }} />
                    </LineChart>
                  </ResponsiveContainer>
                </div>
              ) : <div className="flex h-32 items-center justify-center text-xs text-slate-500">No comparable prior-year values are available for these months.</div>}
            </div>

            <div className="overflow-x-auto rounded-xl border border-slate-200">
              <table className="min-w-full text-left text-xs">
                <thead className="bg-slate-50 text-[10px] font-bold uppercase tracking-wide text-slate-500">
                  <tr><th className="px-3 py-2.5">Month</th><th className="px-3 py-2.5">Trade value</th><th className="px-3 py-2.5">Previous year</th><th className="px-3 py-2.5">Current year</th><th className="px-3 py-2.5">YoY %</th></tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {rows.map((row) => (
                    <tr key={row.period}>
                      <td className="whitespace-nowrap px-3 py-2.5">{formatMonth(row.period)}</td>
                      <td className="px-3 py-2.5">{number(row.value)}</td>
                      <td className="px-3 py-2.5">{number(row.previousYear)}</td>
                      <td className="px-3 py-2.5">{number(row.currentYear)}</td>
                      <td className="px-3 py-2.5">{row.growth == null ? '—' : `${number(row.growth)}%`}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </> : (
            <div className="flex min-h-72 items-center justify-center rounded-xl border border-dashed border-slate-300 text-sm text-slate-500">
              {error || 'No monthly observations are available for this HS code.'}
            </div>
          )}
          {error && rows.length ? <div className="mt-3 text-xs text-amber-800">{error}</div> : null}
        </div>
      </div>
    </div>
  );
}

export default function SectorTradestat() {
  const [activeView, setActiveView] = useState('export');
  const [tradeType, setTradeType] = useState('export');
  const [watchlists, setWatchlists] = useState({ imports: [], exports: [] });
  const [report, setReport] = useState(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState('');
  const [hsCode, setHsCode] = useState('');
  const [commodity, setCommodity] = useState('');
  const [adding, setAdding] = useState(false);
  const [selected, setSelected] = useState(null);

  const activeWatchlist = tradeType === 'export' ? watchlists.exports : watchlists.imports;

  const load = useCallback(async ({ refresh = false, quiet = false } = {}) => {
    if (!quiet && !refresh) setLoading(true);
    if (refresh) setRefreshing(true);
    if (!quiet) setError('');
    try {
      if (quiet) {
        const monitor = await api.tradestatMonitor();
        if (monitor?.watchlists) {
          setWatchlists({
            imports: monitor.watchlists.imports || [],
            exports: monitor.watchlists.exports || [],
          });
        }
        setReport(monitor?.report || null);
        if (monitor?.report?.error) setError(monitor.report.error);
      } else {
        const [listResult, monitorResult] = await Promise.allSettled([
          api.tradestatWatchlist(),
          api.tradestatMonitor({ refresh }),
        ]);
        const list = listResult.status === 'fulfilled' ? listResult.value : null;
        const monitor = monitorResult.status === 'fulfilled' ? monitorResult.value : null;
        if (list || monitor?.watchlists) {
          setWatchlists({
            imports: list?.imports || monitor?.watchlists?.imports || [],
            exports: list?.exports || monitor?.watchlists?.exports || [],
          });
        }
        if (monitor?.report) {
          setReport(monitor.report);
          if (monitor.report.error) setError(monitor.report.error);
        }
        const failures = [listResult, monitorResult]
          .filter((result) => result.status === 'rejected')
          .map((result) => result.reason?.message || 'Request failed');
        if (failures.length) setError(failures.join(' · '));
        if (!list && !monitor) throw new Error(failures.join(' · ') || 'Unable to load TradeStat data.');
      }
    } catch (err) {
      if (!quiet) setError(err?.message || 'Unable to load TradeStat data.');
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    if (!report?.refresh_in_progress) return undefined;
    const timer = window.setTimeout(() => load({ quiet: true }), 5000);
    return () => window.clearTimeout(timer);
  }, [report?.refresh_in_progress, load]);

  const payload = report?.data?.sector_payload || {};
  const importRows = useMemo(() => payload.monitor_rows_by_trade?.import || [], [payload]);
  const exportRows = useMemo(() => payload.monitor_rows_by_trade?.export || [], [payload]);
  const directionStatuses = report?.direction_status || payload.direction_status || {};
  const lastAttempts = report?.last_attempt || {};

  const lastScrapedAt = useMemo(() => {
    const candidates = [
      lastAttempts.import?.attempted_at,
      lastAttempts.export?.attempted_at,
      directionStatuses.import?.refreshed_at,
      directionStatuses.export?.refreshed_at,
      report?.data?.refreshed_at,
    ].filter(Boolean);
    const parsed = candidates
      .map((value) => ({ value, time: new Date(value).getTime() }))
      .filter((item) => Number.isFinite(item.time))
      .sort((a, b) => b.time - a.time);
    return parsed[0]?.value || null;
  }, [lastAttempts, directionStatuses, report?.data?.refreshed_at]);

  const add = async (event) => {
    event.preventDefault();
    if (!hsCode.trim()) return;
    setAdding(true);
    setError('');
    try {
      const result = await api.tradestatAddWatchlist(hsCode, commodity, tradeType);
      setWatchlists((current) => ({
        ...current,
        [`${tradeType}s`]: result?.items || current[`${tradeType}s`],
      }));
      setHsCode('');
      setCommodity('');
      await load({ refresh: true });
    } catch (err) {
      setError(err?.message || 'Unable to add this HS code.');
    } finally {
      setAdding(false);
    }
  };

  const remove = async (code) => {
    try {
      const result = await api.tradestatRemoveWatchlist(code, tradeType);
      setWatchlists((current) => ({ ...current, [`${tradeType}s`]: result?.items || [] }));
      await load({ refresh: true });
    } catch (err) {
      setError(err?.message || 'Unable to remove this HS code.');
    }
  };

  const watchedCount = watchlists.imports.length + watchlists.exports.length;
  const importUnitSet = [...new Set(importRows.map((row) => row['Source unit']).filter(Boolean))];
  const exportUnitSet = [...new Set(exportRows.map((row) => row['Source unit']).filter(Boolean))];
  const unitFor = (units) => units.length === 1 ? units[0] : units.length > 1 ? 'Multiple units' : '—';
  const activeTableRows = activeView === 'import' ? importRows : exportRows;
  const activeUnits = activeView === 'import' ? importUnitSet : exportUnitSet;
  const firstRow = activeTableRows[0] || {};
  const previousLabel = cleanPeriodLabel(firstRow['Previous Year Label'], 'Previous year');
  const currentLabel = cleanPeriodLabel(firstRow['Current Year Label'], 'Current year');

  const kpis = [
    { key: 'export', label: 'Export Commodities', value: number(exportRows.length), icon: ArrowUpFromLine, view: 'export' },
    { key: 'import', label: 'Import Commodities', value: number(importRows.length), icon: ArrowDownToLine, view: 'import' },
    { key: 'watchlist', label: 'Watchlist', value: number(watchedCount), icon: Activity, view: 'watchlist' },
    { key: 'last', label: 'Last Scraped', value: formatDateTime(lastScrapedAt, true), icon: Clock3, view: null },
  ];

  const viewTitle = {
    watchlist: 'Watched Commodities',
    import: 'Import Commodities',
    export: 'Export Commodities',
  }[activeView];

  return (
    <div className="space-y-5 pb-8">
      <PageHeader
        eyebrow="NIVESHAM · TRADE INTELLIGENCE"
        title="TradeStat – Commodity Monitor"
        description="Track watched commodities, import and export values, and year-on-year growth."
        action={(
          <button
            onClick={() => load({ refresh: true })}
            disabled={refreshing}
            className="inline-flex items-center gap-2 rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm font-semibold text-slate-700 shadow-sm hover:bg-slate-50 disabled:opacity-50"
          >
            <RefreshCw size={15} className={refreshing ? 'animate-spin' : ''} /> Refresh
          </button>
        )}
      />

      <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
        {kpis.map((item) => {
          const Icon = item.icon;
          const active = activeView === item.view;
          const tileClass = `min-w-0 rounded-xl border bg-white p-4 text-left shadow-sm ${item.view ? 'cursor-pointer transition hover:-translate-y-0.5 hover:border-blue-300 hover:shadow-md focus:outline-none focus:ring-2 focus:ring-blue-500' : 'cursor-default'} ${active ? 'border-blue-500 ring-1 ring-blue-200' : 'border-slate-200'}`;
          const content = (
            <>
              <div className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wide text-slate-500">
                <Icon size={15} className="shrink-0 text-blue-700" />
                <span>{item.label}</span>
              </div>
              <div className={`mt-2 truncate font-bold text-slate-900 ${item.key === 'last' ? 'text-base' : 'text-2xl'}`} title={item.key === 'last' ? formatDateTime(lastScrapedAt) : String(item.value)}>
                {loading && item.key !== 'last' ? '—' : item.value}
              </div>
            </>
          );
          return item.view ? (
            <button key={item.key} type="button" onClick={() => setActiveView(item.view)} aria-pressed={active} className={tileClass}>
              {content}
            </button>
          ) : (
            <div key={item.key} className={tileClass} aria-label={`${item.label}: ${item.value}`}>
              {content}
            </div>
          );
        })}
      </div>

      {error && activeView !== 'import' && activeView !== 'export' ? (
        <div className="rounded-xl border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">{error}</div>
      ) : null}

      {activeView === 'overview' ? (
        <div className="rounded-xl border border-dashed border-slate-300 bg-white px-5 py-12 text-center">
          <div className="text-sm font-semibold text-slate-700">Select a KPI to open its details</div>
          <p className="mt-1 text-xs text-slate-500">Open your watchlist, view import or export commodities, or check the latest scrape time.</p>
        </div>
      ) : (
        <section className="space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-center gap-2">
              <button onClick={() => setActiveView('export')} className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-semibold text-slate-600 hover:bg-slate-50">
                <ArrowLeft size={14} /> Back
              </button>
              <h2 className="text-base font-bold text-slate-900">{viewTitle}</h2>
            </div>
            {activeView === 'watchlist' ? null : (
              <span className="text-xs text-slate-500">Unit: {unitFor(activeUnits)}</span>
            )}
          </div>

          {activeView === 'watchlist' ? (
            <div className="space-y-4">
              <div className="flex flex-wrap items-center gap-2">
                <button onClick={() => setTradeType('import')} className={`rounded-lg px-4 py-2 text-xs font-semibold ${tradeType === 'import' ? 'bg-blue-700 text-white' : 'border border-slate-200 bg-white text-slate-600 hover:bg-slate-50'}`}>
                  Import ({watchlists.imports.length}/30)
                </button>
                <button onClick={() => setTradeType('export')} className={`rounded-lg px-4 py-2 text-xs font-semibold ${tradeType === 'export' ? 'bg-blue-700 text-white' : 'border border-slate-200 bg-white text-slate-600 hover:bg-slate-50'}`}>
                  Export ({watchlists.exports.length}/30)
                </button>
              </div>

              <form onSubmit={add} className="rounded-xl border border-slate-200 bg-white p-3 shadow-sm">
                <div className="grid grid-cols-1 items-end gap-3 md:grid-cols-[minmax(150px,0.75fr)_minmax(220px,1.5fr)_auto]">
                  <label className="block text-xs font-semibold text-slate-600">
                    HS Code (2, 4, 6 or 8 digits)
                    <input
                      value={hsCode}
                      onChange={(event) => setHsCode(event.target.value.replace(/\D/g, '').slice(0, 8))}
                      inputMode="numeric"
                      placeholder="e.g. 260600"
                      className="mt-1.5 w-full rounded-lg border border-slate-200 px-3 py-2.5 text-sm outline-none focus:border-blue-500"
                    />
                  </label>
                  <label className="block text-xs font-semibold text-slate-600">
                    Commodity description (optional)
                    <input
                      value={commodity}
                      onChange={(event) => setCommodity(event.target.value)}
                      placeholder="e.g. Aluminium ores and concentrates"
                      className="mt-1.5 w-full rounded-lg border border-slate-200 px-3 py-2.5 text-sm outline-none focus:border-blue-500"
                    />
                  </label>
                  <button
                    disabled={adding || ![2, 4, 6, 8].includes(hsCode.length)}
                    className="inline-flex items-center justify-center gap-2 rounded-lg bg-blue-700 px-5 py-2.5 text-xs font-semibold text-white shadow-sm hover:bg-blue-800 disabled:opacity-50"
                  >
                    {adding ? <Loader2 size={14} className="animate-spin" /> : <Plus size={14} />}
                    Add {tradeLabel(tradeType)} commodity
                  </button>
                </div>
              </form>

              <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
                <div className="flex items-center justify-between gap-3 border-b border-slate-100 px-4 py-3">
                  <h3 className="text-sm font-semibold text-slate-900">{tradeLabel(tradeType)} watchlist</h3>
                  <span className="text-xs text-slate-500">{activeWatchlist.length} of 30 commodities</span>
                </div>
                {activeWatchlist.length ? (
                  <div className="divide-y divide-slate-100">
                    {activeWatchlist.map((item) => (
                      <div key={`${tradeType}-${item.hs_code}`} className="flex items-center justify-between gap-3 px-4 py-3 hover:bg-slate-50/70">
                        <div className="flex min-w-0 items-center gap-4">
                          <span className="w-20 shrink-0 text-xs font-semibold text-slate-700">{item.hs_code}</span>
                          <span className="truncate text-sm text-slate-800">{item.commodity || `HS ${item.hs_code}`}</span>
                        </div>
                        <button onClick={() => remove(item.hs_code)} className="shrink-0 rounded-lg p-2 text-slate-400 hover:bg-red-50 hover:text-red-600" title="Remove" aria-label={`Remove HS ${item.hs_code}`}>
                          <Trash2 size={15} />
                        </button>
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="p-8 text-center text-sm text-slate-500">No commodities added to the {tradeLabel(tradeType).toLowerCase()} watchlist yet.</div>
                )}
              </div>
            </div>
          ) : null}

          {activeView === 'import' || activeView === 'export' ? (
            <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
              {activeTableRows.length ? (
                <div className="overflow-x-auto">
                  <table className="min-w-full text-left text-xs">
                    <thead className="bg-slate-50 text-[10px] font-bold uppercase tracking-wide text-slate-600">
                      <tr>
                        <th className="whitespace-nowrap px-4 py-3">HS Code</th>
                        <th className="min-w-[220px] px-4 py-3">Commodity</th>
                        <th className="whitespace-nowrap px-4 py-3">{previousLabel}</th>
                        <th className="whitespace-nowrap px-4 py-3">{currentLabel}</th>
                        <th className="whitespace-nowrap px-4 py-3">YoY %</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100">
                      {activeTableRows.map((row, index) => (
                        <tr
                          key={`${activeView}-${row.HSCode}-${index}`}
                          onClick={() => setSelected({ code: String(row.HSCode), commodity: row.Commodity || `HS ${row.HSCode}`, tradeType: activeView })}
                          onKeyDown={(event) => {
                            if (event.key === 'Enter' || event.key === ' ') {
                              event.preventDefault();
                              setSelected({ code: String(row.HSCode), commodity: row.Commodity || `HS ${row.HSCode}`, tradeType: activeView });
                            }
                          }}
                          tabIndex={0}
                          className="cursor-pointer transition hover:bg-blue-50/60 focus:bg-blue-50"
                        >
                          <td className="whitespace-nowrap px-4 py-3 font-semibold text-slate-800">{row.HSCode}</td>
                          <td className="px-4 py-3 text-slate-700">{row.Commodity}</td>
                          <td className="whitespace-nowrap px-4 py-3 text-slate-700">{number(row['Previous Year'])}</td>
                          <td className="whitespace-nowrap px-4 py-3 text-slate-700">{number(row['Current Year'])}</td>
                          <td className="whitespace-nowrap px-4 py-3 text-slate-700">{row['% Growth'] == null ? '—' : `${number(row['% Growth'])}%`}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <div className="p-10 text-center">
                  <div className="text-sm font-semibold text-slate-700">No {tradeLabel(activeView).toLowerCase()} commodities available</div>
                  <p className="mt-1 text-xs text-slate-500">Add HS codes to this list to display the commodities here.</p>
                  <button
                    onClick={() => { setTradeType(activeView); setActiveView('watchlist'); }}
                    className="mt-3 rounded-lg bg-blue-700 px-3 py-2 text-xs font-semibold text-white hover:bg-blue-800"
                  >
                    Manage {tradeLabel(activeView).toLowerCase()} list
                  </button>
                </div>
              )}
            </div>
          ) : null}

        </section>
      )}

      {report?.refresh_in_progress ? (
        <div className="flex items-center gap-2 rounded-lg border border-blue-100 bg-blue-50/60 p-3 text-xs text-blue-900">
          <Loader2 size={14} className="animate-spin" /> Updating trade observations.
        </div>
      ) : null}

      {selected ? (
        <TradeHistoryModal
          code={selected.code}
          commodity={selected.commodity}
          tradeType={selected.tradeType}
          onClose={() => setSelected(null)}
        />
      ) : null}
    </div>
  );
}
