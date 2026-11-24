import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Activity, BarChart3, CarFront, ChevronDown, RefreshCw, RotateCcw, Table2, Zap } from "lucide-react";
import {
  Area, AreaChart, Bar, BarChart, CartesianGrid, Cell, Legend, Line, LineChart, Pie, PieChart,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import api from "../lib/api";

const COLORS = ["#2563eb", "#0f766e", "#d97706", "#7c3aed", "#db2777", "#0891b2", "#65a30d", "#ea580c", "#64748b"];
const CATEGORY_OPTIONS = ["2W", "4W", "3W", "Tractor", "Commercial Vehicle", "EV", "Bus"];
const FUEL_OPTIONS = ["Petrol", "CNG", "Diesel", "Hybrid", "EV"];
const PARIVAHAN_URL = "https://analytics.parivahan.gov.in/analytics/publicdashboard/vahan";
const REPORT_CHART_TITLES = {
  ev: ["Monthly EV Registrations in India", "EV Share of Total Vehicle Registrations", "Top EV Makers — 2026 YTD", "EV Registrations by Vehicle Segment", "EV Penetration by State"],
  two_wheeler: ["Monthly Two-Wheeler Registrations in India", "Top Two-Wheeler Makers — 2026 YTD", "Top Electric Two-Wheeler Makers — 2026 YTD", "Two-Wheeler Fuel Mix — 2026 YTD"],
  four_wheeler: ["Monthly Car Registrations in India", "Top Car Makers — 2026 YTD", "Car Fuel Mix — 2026 YTD", "Electric Car Makers — 2026 YTD"],
  commercial_vehicle: ["Monthly Commercial Vehicle Registrations", "Leading Commercial Vehicle Manufacturers", "Commercial Vehicle Market Share — 2026 YTD", "Commercial Vehicle Fuel Mix — 2026 YTD", "Commercial Vehicle Registrations by State"],
  statewise: ["Top States by Vehicle Registrations — 2026 YTD", "Monthly Registrations — Top States", "National Monthly Registrations — Year over Year"],
};
const REPORT_TABLE_TITLES = {
  ev: ["Top EV Manufacturers in India — 2026 YTD"],
  two_wheeler: ["Top Two-Wheeler Brands by Registrations — 2026 YTD"],
  four_wheeler: ["Top Car Manufacturers by Registrations — 2026 YTD"],
  commercial_vehicle: ["Commercial Vehicle Manufacturer Leaderboard", "Commercial Vehicle Market Share — 2026 YTD", "Commercial Vehicle Registrations by Fuel — 2026 YTD", "Top States by Commercial Vehicle Registrations"],
  statewise: ["State-wise Vehicle Registrations — All States and UTs"],
};
const isGenericTitle = (value) => !String(value || "").trim() || /^(?:source\s+)?(?:chart|table)(?:\s*#?\s*\d+)?$/i.test(String(value).trim());
const GRANULARITIES = [
  { label: "Monthly", key: "monthly" },
  { label: "Quarterly", key: "quarterly" },
  { label: "Yearly", key: "yearly" },
];
const fmt = (value) => {
  const number = Number(value);
  return Number.isFinite(number)
    ? new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 }).format(number)
    : "—";
};
const fmtPct = (value) => {
  const number = Number(value);
  return Number.isFinite(number) ? `${number >= 0 ? "+" : ""}${number.toFixed(2)}%` : "—";
};
const periodSortValue = (value) => {
  const text = String(value || "").trim();
  let match = text.match(/^(\d{4})-(\d{2})$/);
  if (match) return Number(match[1]) * 100 + Number(match[2]);
  match = text.match(/^(\d{4})-Q([1-4])$/i);
  if (match) return Number(match[1]) * 10 + Number(match[2]);
  match = text.match(/^(\d{4})$/);
  if (match) return Number(match[1]) * 100;
  return null;
};
const formatPeriodLabel = (value) => {
  const text = String(value || "").trim();
  let match = text.match(/^(\d{4})-(\d{2})$/);
  if (match) {
    const date = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, 1));
    return new Intl.DateTimeFormat("en", { month: "short", year: "2-digit", timeZone: "UTC" }).format(date);
  }
  match = text.match(/^(\d{4})-Q([1-4])$/i);
  if (match) return `Q${match[2]} ${match[1]}`;
  return text;
};
const formatCompactCount = (value) => {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  if (Math.abs(number) >= 10000000) return `${(number / 10000000).toFixed(number % 10000000 === 0 ? 0 : 1)}Cr`;
  if (Math.abs(number) >= 100000) return `${(number / 100000).toFixed(number % 100000 === 0 ? 0 : 1)}L`;
  if (Math.abs(number) >= 1000) return `${(number / 1000).toFixed(number % 1000 === 0 ? 0 : 1)}K`;
  return fmt(number);
};

function Panel({ title, subtitle, icon: Icon = Activity, action, children }) {
  return (
    <section className="min-w-0 overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
      <div className="flex items-start justify-between gap-3 border-b border-slate-100 px-4 py-3">
        <div className="flex min-w-0 items-start gap-3">
          <div className="mt-0.5 rounded-lg bg-blue-50 p-2 text-blue-700"><Icon size={16} /></div>
          <div className="min-w-0">
            <h3 className="text-sm font-semibold text-slate-900">{title}</h3>
            {subtitle && <p className="mt-0.5 text-xs text-slate-500">{subtitle}</p>}
          </div>
        </div>
        {action}
      </div>
      {children}
    </section>
  );
}

function Kpi({ label, value, icon: Icon = Activity, loading }) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white px-4 py-3 shadow-sm">
      <div className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wide text-slate-500">
        <Icon size={14} />{label}
      </div>
      <div className={`mt-2 min-h-8 text-2xl font-bold tracking-tight text-slate-900 ${loading ? "animate-pulse" : ""}`}>{value ?? "—"}</div>
    </div>
  );
}

function ChartPanel({ title, data, type = "bar", horizontal = false, vertical = false }) {
  const rows = (data || []).filter((item) => Number.isFinite(Number(item.value)) && Number(item.value) >= 0);
  return (
    <Panel title={title} icon={BarChart3}>
      <div className="p-3" style={{ height: horizontal ? Math.max(300, Math.min(760, rows.length * 30 + 76)) : vertical ? Math.max(390, Math.min(590, rows.length * 24 + 150)) : 310 }}>
        {rows.length ? (
          <ResponsiveContainer width="100%" height="100%">
            {type === "pie" ? (
              <PieChart>
                <Tooltip formatter={(value) => fmt(value)} />
                <Legend verticalAlign="bottom" height={42} wrapperStyle={{ fontSize: 10 }} />
                <Pie data={rows} dataKey="value" nameKey="name" outerRadius={100} label={({ name, percent }) => `${name} ${(percent * 100).toFixed(1)}%`}>
                  {rows.map((row, index) => <Cell key={`${row.name}-${index}`} fill={COLORS[index % COLORS.length]} />)}
                </Pie>
              </PieChart>
            ) : horizontal ? (
              <BarChart data={rows} layout="vertical" margin={{ top: 8, right: 20, left: 12, bottom: 8 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                <XAxis type="number" tick={{ fontSize: 10 }} tickFormatter={formatCompactCount} domain={[0, "auto"]} tickCount={6} allowDecimals={false} />
                <YAxis type="category" dataKey="name" width={220} tick={{ fontSize: 10 }} interval={0} />
                <Tooltip formatter={(value) => fmt(value)} />
                <Bar dataKey="value" name="Registrations" radius={[0, 4, 4, 0]}>
                  {rows.map((row, index) => <Cell key={`${row.name}-${index}`} fill={COLORS[index % COLORS.length]} />)}
                </Bar>
              </BarChart>
            ) : (
              <BarChart data={rows} margin={{ top: 8, right: 16, left: 8, bottom: vertical ? 104 : 54 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                <XAxis
                  dataKey="name"
                  tick={{ fontSize: vertical ? 9 : 10 }}
                  tickFormatter={vertical ? (value) => {
                    const label = String(value || "");
                    return label.length > 22 ? `${label.slice(0, 21)}…` : label;
                  } : undefined}
                  angle={vertical ? -48 : -22}
                  textAnchor="end"
                  interval={0}
                  height={vertical ? 108 : 64}
                  tickMargin={8}
                />
                <YAxis tick={{ fontSize: 10 }} tickFormatter={formatCompactCount} domain={[0, "auto"]} tickCount={6} allowDecimals={false} width={68} />
                <Tooltip labelFormatter={(label) => label} formatter={(value) => fmt(value)} />
                <Bar dataKey="value" name="Registrations" radius={[4, 4, 0, 0]}>
                  {rows.map((row, index) => <Cell key={`${row.name}-${index}`} fill={COLORS[index % COLORS.length]} />)}
                </Bar>
              </BarChart>
            )}
          </ResponsiveContainer>
        ) : (
          <div className="flex h-full items-center justify-center text-sm text-slate-400">No records for the selected filters.</div>
        )}
      </div>
    </Panel>
  );
}

function SourceChartPanel({ chart, title }) {
  const sourceSeries = Array.isArray(chart?.series) ? chart.series : [];
  const palette = COLORS;
  const seriesKeys = sourceSeries.map((_, index) => `series_${index}`);
  const merged = new Map();
  sourceSeries.forEach((series, seriesIndex) => {
    (series.data || []).forEach((point) => {
      const label = String(point.label ?? "").trim();
      const value = Number(point.value);
      if (!label || !Number.isFinite(value)) return;
      if (!merged.has(label)) merged.set(label, { name: label });
      merged.get(label)[seriesKeys[seriesIndex]] = value;
    });
  });
  const rows = Array.from(merged.values());
  const periodValues = rows.map((row) => periodSortValue(row.name));
  const hasOnlyPeriods = rows.length > 0 && periodValues.every((value) => value !== null);
  if (hasOnlyPeriods) rows.sort((left, right) => periodSortValue(left.name) - periodSortValue(right.name));
  const kind = String(chart?.kind || "bar").toLowerCase();
  return (
    <Panel title={title || chart?.title || "Registration Trend"} icon={BarChart3}>
      <div className="h-[330px] p-3">
        {rows.length ? (
          <ResponsiveContainer width="100%" height="100%">
            {kind === "pie" ? (
              <PieChart>
                <Tooltip formatter={(value) => fmt(value)} />
                <Legend verticalAlign="bottom" height={42} wrapperStyle={{ fontSize: 10 }} />
                <Pie data={rows.map((row) => ({ name: row.name, value: row[seriesKeys[0]] }))} dataKey="value" nameKey="name" outerRadius={105} label={({ name, percent }) => `${name} ${(percent * 100).toFixed(1)}%`}>
                  {rows.map((row, index) => <Cell key={`${row.name}-${index}`} fill={palette[index % palette.length]} />)}
                </Pie>
              </PieChart>
            ) : kind === "line" || kind === "area" ? (
              kind === "area" ? (
                <AreaChart data={rows} margin={{ top: 12, right: 14, left: 6, bottom: 42 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                  <XAxis dataKey="name" tick={{ fontSize: 10 }} tickFormatter={hasOnlyPeriods ? formatPeriodLabel : undefined} angle={-25} textAnchor="end" interval="preserveStartEnd" minTickGap={28} tickMargin={8} height={62} />
                  <YAxis tick={{ fontSize: 10 }} tickFormatter={formatCompactCount} domain={[0, "auto"]} tickCount={6} allowDecimals={false} width={62} />
                  <Tooltip labelFormatter={(label) => hasOnlyPeriods ? formatPeriodLabel(label) : label} formatter={(value) => fmt(value)} />
                  <Legend wrapperStyle={{ fontSize: 10 }} />
                  {sourceSeries.map((series, index) => <Area key={seriesKeys[index]} type="monotone" dataKey={seriesKeys[index]} name={series.name} stroke={palette[index % palette.length]} fill={palette[index % palette.length]} fillOpacity={0.12} connectNulls />)}
                </AreaChart>
              ) : (
                <LineChart data={rows} margin={{ top: 12, right: 14, left: 6, bottom: 42 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                  <XAxis dataKey="name" tick={{ fontSize: 10 }} tickFormatter={hasOnlyPeriods ? formatPeriodLabel : undefined} angle={-25} textAnchor="end" interval="preserveStartEnd" minTickGap={28} tickMargin={8} height={62} />
                  <YAxis tick={{ fontSize: 10 }} tickFormatter={formatCompactCount} domain={[0, "auto"]} tickCount={6} allowDecimals={false} width={62} />
                  <Tooltip labelFormatter={(label) => hasOnlyPeriods ? formatPeriodLabel(label) : label} formatter={(value) => fmt(value)} />
                  <Legend wrapperStyle={{ fontSize: 10 }} />
                  {sourceSeries.map((series, index) => <Line key={seriesKeys[index]} type="monotone" dataKey={seriesKeys[index]} name={series.name} stroke={palette[index % palette.length]} strokeWidth={2} dot={false} connectNulls />)}
                </LineChart>
              )
            ) : (
              <BarChart data={rows} margin={{ top: 8, right: 12, left: 8, bottom: 58 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                <XAxis dataKey="name" tick={{ fontSize: 10 }} angle={-24} textAnchor="end" interval={0} height={68} />
                <YAxis tick={{ fontSize: 10 }} tickFormatter={(value) => value >= 10000000 ? `${(value / 10000000).toFixed(1)}Cr` : value >= 100000 ? `${(value / 100000).toFixed(1)}L` : fmt(value)} />
                <Tooltip formatter={(value) => fmt(value)} />
                <Legend wrapperStyle={{ fontSize: 10 }} />
                {sourceSeries.map((series, index) => <Bar key={seriesKeys[index]} dataKey={seriesKeys[index]} name={series.name} fill={palette[index % palette.length]} radius={[4, 4, 0, 0]} />)}
              </BarChart>
            )}
          </ResponsiveContainer>
        ) : <div className="flex h-full items-center justify-center text-sm text-slate-400">No chart data is available yet.</div>}
      </div>
    </Panel>
  );
}

function DataTable({ title, columns, rows, leftColumnCount = 2, emptyMakerLabel = null }) {
  const getValue = (row, column) => {
    if (Object.prototype.hasOwnProperty.call(row || {}, column)) return row[column];
    const wanted = String(column).toLowerCase();
    const matchingKey = Object.keys(row || {}).find((key) => String(key).toLowerCase() === wanted);
    return matchingKey ? row[matchingKey] : undefined;
  };
  const rowMaker = (row) => getValue(row, "Maker") ?? getValue(row, "maker") ?? getValue(row, "Manufacturer");

  return (
    <Panel title={title} icon={Table2}>
      <div className="max-h-[430px] overflow-auto">
        <table className="min-w-full border-separate border-spacing-0 text-xs">
          <thead className="sticky top-0 z-10 bg-slate-50">
            <tr>{columns.map((column, index) => (
              <th key={column} className={`whitespace-nowrap border-b border-slate-200 px-3 py-2.5 font-semibold text-slate-600 ${index < leftColumnCount ? "text-left" : "text-right"}`}>{formatPeriodLabel(column)}</th>
            ))}</tr>
          </thead>
          <tbody>
            {rows?.length ? rows.map((row, rowIndex) => (
              <tr key={`${rowMaker(row) || row.Period || row.State || "row"}-${rowIndex}`} className="hover:bg-blue-50/40">
                {columns.map((column, columnIndex) => {
                  let value = getValue(row, column);
                  if (String(column).toLowerCase() === "maker" && (value == null || value === "") && emptyMakerLabel) value = emptyMakerLabel;
                  const isGrowthColumn = /growth|rate|%|mom|qoq|yoy/i.test(column);
                  let display;
                  if (value == null || value === "") {
                    display = "—";
                  } else if (value && typeof value === "object" && !Array.isArray(value)) {
                    const entries = Object.entries(value);
                    display = (
                      <div className="flex min-w-[144px] flex-col items-end gap-0.5 text-[10px] leading-4">
                        {entries.map(([metric, rate]) => (
                          <span key={metric} className="whitespace-nowrap">
                            <span className="font-semibold text-slate-500">{metric.replace(/\s*\(%\)/, "")}:</span> {rate == null ? "—" : fmtPct(rate)}
                          </span>
                        ))}
                      </div>
                    );
                  } else {
                    display = isGrowthColumn ? fmtPct(value) : typeof value === "number" ? fmt(value) : value;
                  }
                  return <td key={column} className={`whitespace-nowrap border-b border-slate-100 px-3 py-2 ${columnIndex < leftColumnCount ? "text-left font-medium text-slate-800" : "text-right tabular-nums text-slate-700"}`}>{display}</td>;
                })}
              </tr>
            )) : (
              <tr><td colSpan={columns.length} className="p-6 text-center text-slate-400">No records for the selected filters.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}

export default function SectorAutomotive() {
  const [activeTab, setActiveTab] = useState("overview");
  const [timeline, setTimeline] = useState("monthly");
  const [category, setCategory] = useState("");
  const [fuel, setFuel] = useState("");
  const [makers, setMakers] = useState([]);
  const [makerSearch, setMakerSearch] = useState("");
  const [makerOptions, setMakerOptions] = useState([]);
  const [showMakerOptions, setShowMakerOptions] = useState(false);
  const [response, setResponse] = useState(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [refreshNonce, setRefreshNonce] = useState(0);
  const requestSequenceRef = useRef(0);

  const filters = useMemo(() => ({ category, fuel, makers, timeline }), [category, fuel, makers, timeline]);
  const tabs = [
    { key: "overview", label: "Overview", reportKey: "overview" },
    { key: "ev", label: "EV", reportKey: "ev" },
    { key: "two_wheeler", label: "2W", reportKey: "two_wheeler" },
    { key: "four_wheeler", label: "4W", reportKey: "four_wheeler" },
    { key: "commercial_vehicle", label: "Commercial Vehicle", reportKey: "commercial_vehicle" },
    { key: "statewise", label: "Statewise", reportKey: "statewise" },
  ];
  const activeTabMeta = tabs.find((tab) => tab.key === activeTab) || tabs[0];
  const isOverview = activeTab === "overview";

  // Overview uses its Parivahan filter contract. The five report tabs are
  // unfiltered and never inherit Overview filters.
  useEffect(() => {
    let active = true;
    let timer = null;
    let attempts = 0;
    const sequence = ++requestSequenceRef.current;
    const reportKey = activeTabMeta.reportKey;
    setResponse(null);
    setError("");
    setLoading(true);
    setRefreshing(false);

    const load = async (refresh) => {
      if (!active || sequence !== requestSequenceRef.current) return;
      if (attempts === 0) setRefreshing(Boolean(refresh));
      attempts += 1;
      try {
        const result = await api.sectorScreenerReport("automotive", reportKey, {
          refresh,
          filters: isOverview ? filters : {},
          timeline: isOverview ? timeline : "monthly",
        });
        if (!active || sequence !== requestSequenceRef.current) return;
        setResponse(result);
        setError(result?.error || result?.data?.warning || "");
        const pending = ["queued", "scraping"].includes(result?.status) || Boolean(result?.refresh_in_progress);
        if (pending && attempts < 50) {
          timer = window.setTimeout(() => load(false), 3000);
        } else {
          setLoading(false);
          setRefreshing(false);
          if (result?.status === "failed" && !result?.data) {
            setError(result?.error || `Unable to load the ${activeTabMeta.label} source report.`);
          }
        }
      } catch (err) {
        if (!active || sequence !== requestSequenceRef.current) return;
        setError(err?.message || `Unable to load the ${activeTabMeta.label} source report.`);
        setLoading(false);
        setRefreshing(false);
      }
    };

    load(true);
    return () => {
      active = false;
      if (timer) window.clearTimeout(timer);
    };
    // response intentionally excluded: receiving a snapshot must not restart scraping.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab, category, fuel, makers, timeline, refreshNonce]);

  useEffect(() => {
    const query = makerSearch.trim();
    if (!isOverview || query.length < 2) {
      setMakerOptions([]);
      return undefined;
    }
    let active = true;
    const timer = window.setTimeout(async () => {
      try {
        const result = await api.automotiveMakerOptions(query, 30);
        if (active) {
          const selected = new Set(makers.map((item) => item.toLowerCase()));
          setMakerOptions((Array.isArray(result?.options) ? result.options : []).filter((item) => !selected.has(String(item).toLowerCase())));
        }
      } catch {
        if (active) setMakerOptions([]);
      }
    }, 300);
    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [makerSearch, makers, isOverview]);

  const reportData = response?.data || null;
  const dashboard = isOverview ? reportData?.automotive_dashboard || null : null;
  const makerComparison = dashboard?.maker_comparison || null;
  const growthMeta = dashboard?.growth_by_timeline?.[timeline] || {};
  const overviewKpis = dashboard?.kpis || [];
  const totalKpi = overviewKpis.find((item) => item.key === "total-registrations");
  const growthKpi = overviewKpis.find((item) => item.key === "registration-growth");
  const evKpi = overviewKpis.find((item) => item.key === "ev-share");
  const growthValue = growthMeta.value ?? (timeline === "monthly" ? growthKpi?.value : null);
  const fuelData = (dashboard?.breakdowns?.fuel || []).map((item) => ({ name: item.label, value: item.value }));
  const categoryData = (dashboard?.breakdowns?.category || []).map((item) => ({ name: item.label, value: item.value }));
  const makerData = (dashboard?.top_makers || []).map((item) => ({ name: item.label, value: item.value }));
  const genericKpis = reportData?.kpis || reportData?.kpi_cards || [];
  const genericCharts = (reportData?.charts || []).map((chart, index) => ({
    ...chart,
    title: isGenericTitle(chart?.title)
      ? (REPORT_CHART_TITLES[activeTabMeta.reportKey]?.[index] || `${activeTabMeta.label} Registration Trend ${index + 1}`)
      : chart.title,
  }));
  const genericTables = (reportData?.tables || []).map((table, index) => ({
    ...table,
    title: isGenericTitle(table?.title)
      ? (REPORT_TABLE_TITLES[activeTabMeta.reportKey]?.[index] || `${activeTabMeta.label} Registration Details`)
      : table.title,
  }));

  const resetFilters = () => {
    setTimeline("monthly");
    setCategory("");
    setFuel("");
    setMakers([]);
    setMakerSearch("");
    setMakerOptions([]);
    setShowMakerOptions(false);
  };
  const applyMaker = (value) => {
    const selected = String(value || "").trim();
    if (!selected) return;
    setMakers((current) => {
      if (current.some((item) => item.toLowerCase() === selected.toLowerCase())) return current;
      return current.length < 10 ? [...current, selected] : current;
    });
    setMakerSearch("");
    setMakerOptions([]);
    setShowMakerOptions(false);
  };
  const refresh = useCallback(() => setRefreshNonce((value) => value + 1), []);

  return (
    <div className="space-y-5 pb-8">
      <div className="flex items-center justify-between gap-3">
        <h1 className="text-xl font-bold tracking-tight text-slate-900">Automotive</h1>
        <div className="flex items-center gap-3">
          <a href={PARIVAHAN_URL} target="_blank" rel="noreferrer" className="text-xs font-semibold text-blue-700 hover:text-blue-800">VAHAN dashboard ↗</a>
          <button onClick={refresh} disabled={refreshing || loading} aria-label="Refresh dashboard" title="Refresh dashboard" className="inline-flex items-center gap-2 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-semibold text-slate-700 shadow-sm transition hover:bg-slate-50 disabled:opacity-50">
            <RefreshCw size={14} className={refreshing ? "animate-spin" : ""} /> Refresh
          </button>
        </div>
      </div>

      <div className="flex min-w-0 gap-1 overflow-x-auto rounded-xl border border-slate-200 bg-white p-1.5">
        {tabs.map((tab) => (
          <button key={tab.key} type="button" onClick={() => setActiveTab(tab.key)}
            className={`shrink-0 rounded-lg px-4 py-2 text-xs font-semibold transition ${activeTab === tab.key ? "bg-slate-900 text-white shadow-sm" : "text-slate-600 hover:bg-slate-100"}`}>
            {tab.label}
          </button>
        ))}
      </div>

      {isOverview && (
        <section className="rounded-xl border border-slate-200 bg-white px-3 py-3 shadow-sm">
          <div className="mb-2 flex items-center justify-between gap-2">
            <span className="text-[11px] font-bold uppercase tracking-wide text-slate-500">Overview filters</span>
            <button onClick={resetFilters} className="inline-flex items-center gap-2 rounded-lg border border-slate-200 px-3 py-2 text-xs font-semibold text-slate-600 hover:bg-slate-50"><RotateCcw size={13} />Clear all filters</button>
          </div>
          <div className="flex flex-wrap items-end gap-3">
            <label className="flex flex-col gap-1 text-[11px] font-semibold text-slate-600">
              Timeline
              <select className="min-w-[145px] rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-medium text-slate-700 outline-none focus:border-blue-500" value={timeline} onChange={(event) => setTimeline(event.target.value)}>
                {GRANULARITIES.map((item) => <option key={item.key} value={item.key}>{item.label}</option>)}
              </select>
            </label>
            <label className="flex flex-col gap-1 text-[11px] font-semibold text-slate-600">
              Category
              <select className="min-w-[190px] rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-medium text-slate-700 outline-none focus:border-blue-500" value={category} onChange={(event) => setCategory(event.target.value)}>
                <option value="">All categories</option>
                {CATEGORY_OPTIONS.map((item) => <option key={item} value={item}>{item}</option>)}
              </select>
            </label>
            <label className="flex flex-col gap-1 text-[11px] font-semibold text-slate-600">
              Fuel
              <select className="min-w-[145px] rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-medium text-slate-700 outline-none focus:border-blue-500" value={fuel} onChange={(event) => setFuel(event.target.value)}>
                <option value="">All fuels</option>
                {FUEL_OPTIONS.map((item) => <option key={item} value={item}>{item}</option>)}
              </select>
            </label>
            <div className="relative flex min-w-[260px] max-w-full flex-1 flex-col gap-1 text-[11px] font-semibold text-slate-600">
              Makers <span className="font-normal text-slate-400">(optional · up to 10)</span>
              <div className="relative">
                <input className="w-full rounded-lg border border-slate-200 bg-white px-3 py-2 pr-8 text-xs font-medium text-slate-700 outline-none focus:border-blue-500"
                  value={makerSearch} placeholder="Search and add manufacturers…" onFocus={() => setShowMakerOptions(true)}
                  onChange={(event) => { setMakerSearch(event.target.value); setShowMakerOptions(true); }}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") {
                      event.preventDefault();
                      const exact = makerOptions.find((item) => item.toLowerCase() === makerSearch.trim().toLowerCase());
                      applyMaker(exact || makerOptions[0] || makerSearch);
                    }
                    if (event.key === "Escape") setShowMakerOptions(false);
                  }} />
                <ChevronDown size={14} className="pointer-events-none absolute right-3 top-2.5 text-slate-400" />
              </div>
              {showMakerOptions && makerOptions.length > 0 && (
                <div className="absolute left-0 top-full z-30 mt-1 max-h-60 w-full overflow-y-auto rounded-lg border border-slate-200 bg-white py-1 shadow-xl">
                  {makerOptions.map((item) => <button key={item} type="button" onMouseDown={(event) => event.preventDefault()} onClick={() => applyMaker(item)} className="block w-full px-3 py-2 text-left text-xs font-medium text-slate-700 hover:bg-blue-50">{item}</button>)}
                </div>
              )}
            </div>
          </div>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            {makers.length ? makers.map((item) => (
              <span key={item} className="inline-flex items-center gap-1.5 rounded-full border border-blue-100 bg-blue-50 px-2.5 py-1 text-[11px] font-semibold text-blue-800">
                {item}
                <button type="button" aria-label={`Remove ${item}`} onClick={() => setMakers((current) => current.filter((makerName) => makerName !== item))} className="text-blue-500 hover:text-blue-900">×</button>
              </span>
            )) : <span className="text-[11px] text-slate-400">All vehicle registrations (no maker filter applied).</span>}
            {makerSearch.trim().length >= 2 && makerOptions.length === 0 && showMakerOptions && makers.length < 10 && <span className="text-[10px] text-slate-400">Press Enter to use the exact maker name typed.</span>}
            {makers.length >= 10 && <span className="text-[10px] text-slate-400">Maximum 10 makers selected.</span>}
          </div>
        </section>
      )}

      {error && <div className="rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-xs text-rose-800"><b>Dashboard issue:</b> {error}</div>}
      {loading && !reportData && <div className="rounded-xl border border-slate-200 bg-white p-6 text-center text-sm text-slate-500">Loading {activeTabMeta.label} dashboard…</div>}

      {isOverview ? (
        <>
          <section>
            <div className="mb-3 flex items-center gap-2"><Activity size={16} className="text-blue-700" /><h2 className="text-sm font-bold text-slate-900">Key indicators</h2></div>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
              <Kpi label="Total Registrations" value={totalKpi?.display_value || (totalKpi?.value != null ? fmt(totalKpi.value) : "—")} icon={CarFront} loading={loading && !dashboard} />
              <Kpi label="Registration Growth" value={growthValue == null ? "—" : fmtPct(growthValue)} icon={BarChart3} loading={loading && !dashboard} />
              <Kpi label="EV Share" value={evKpi?.display_value || (evKpi?.value != null ? `${Number(evKpi.value).toFixed(2)}%` : "—")} icon={Zap} loading={loading && !dashboard} />
            </div>
          </section>

          <section className="space-y-3">
            <div>
              <div className="flex items-center gap-2"><Activity size={16} className="text-blue-700" /><h2 className="text-sm font-bold text-slate-900">Vehicle Registration Trend</h2></div>
              <p className="mt-1 text-xs text-slate-500">
                {makers.length ? `Separate registration series for ${makers.join(", ")} using the shared Overview filters.` : "All vehicle registrations by the selected timeline and filters. Add makers above to compare them as separate lines."}
              </p>
            </div>
            <SourceChartPanel chart={makerComparison?.chart || { kind: "line", series: [] }} title={`Vehicle Registrations — ${timeline === "monthly" ? "Monthly" : timeline === "quarterly" ? "Quarterly" : "Yearly"}`} />
            <DataTable
              title={`Registration Growth (%) — ${timeline === "monthly" ? "MoM / YoY" : timeline === "quarterly" ? "QoQ / YoY" : "YoY"}`}
              columns={makerComparison?.table?.columns || ["Maker"]}
              rows={makerComparison?.table?.rows || []}
              leftColumnCount={1}
              emptyMakerLabel={makers.length ? null : "All Vehicle Registrations"}
            />
            <DataTable
              title="Absolute Registrations — Maker-wise"
              columns={makerComparison?.absolute_table?.columns || ["Maker"]}
              rows={makerComparison?.absolute_table?.rows || []}
              leftColumnCount={1}
            />
            {Object.keys(makerComparison?.source_errors || {}).length > 0 && (
              <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
                Some selected makers did not return usable Parivahan registration history: {Object.keys(makerComparison.source_errors).join(", ")}.
              </div>
            )}
          </section>

          <section className="grid grid-cols-1 gap-4 xl:grid-cols-2">
            <ChartPanel title="Vehicle Registrations by Fuel Type" type="pie" data={fuelData} />
            <ChartPanel title="Vehicle Registration by Category" type="pie" data={categoryData} />
          </section>
          <section className="min-w-0">
            <ChartPanel title="Top 20 Makers by Registration Volume" data={makerData.slice(0, 20)} vertical />
            {makerData.length < 20 && (
              <p className="px-1 pt-2 text-[11px] text-slate-500">
                Parivahan's public maker-ranking endpoint is a Top 5 aggregate and currently returns {makerData.length} verified row{makerData.length === 1 ? "" : "s"}. The vertical chart can render up to 20 rows when the source supplies them; unavailable ranks are not estimated.
              </p>
            )}
          </section>
          {dashboard?.source_errors && Object.keys(dashboard.source_errors).length > 0 && (
            <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
              Some registration breakdowns are temporarily unavailable. Core dashboard values remain source-based.
            </div>
          )}
        </>
      ) : (
        <>
          {genericKpis.length > 0 && (
            <section>
              <div className="mb-3 flex items-center gap-2"><Activity size={16} className="text-blue-700" /><h2 className="text-sm font-bold text-slate-900">Key indicators</h2></div>
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
                {genericKpis.map((item, index) => <Kpi key={item.key || index} label={item.label} value={item.display_value || (item.value != null ? fmt(item.value) : "—")} icon={[CarFront, BarChart3, Zap, Activity][index % 4]} loading={loading} />)}
              </div>
            </section>
          )}
          {genericCharts.length > 0 && (
            <section className="grid grid-cols-1 gap-4 xl:grid-cols-2">
              {genericCharts.map((chart, index) => <SourceChartPanel key={chart.key || index} chart={chart} title={chart.title} />)}
            </section>
          )}
          {genericTables.length > 0 && (
            <section className="grid grid-cols-1 gap-4">
              {genericTables.map((table, index) => <DataTable key={table.key || index} title={table.title} columns={table.columns || []} rows={table.rows || []} />)}
            </section>
          )}

        </>
      )}
    </div>
  );
}