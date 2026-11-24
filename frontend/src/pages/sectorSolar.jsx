import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Activity, BarChart3, Factory, RefreshCw, Search, X } from "lucide-react";
import {
  Bar, BarChart, CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import { PageHeader } from "../components/UI";
import api from "../lib/api";

const COLORS = { cell: "#0f766e", module: "#2563eb" };
const fmt = (value, digits = 3) => {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  return Number.isFinite(number)
    ? new Intl.NumberFormat("en-IN", { maximumFractionDigits: digits }).format(number)
    : String(value);
};
const safeText = (value) => value === null || value === undefined || value === "" ? "—" : String(value);

function Panel({ title, subtitle, icon: Icon = Activity, children }) {
  return (
    <section className="min-w-0 overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
      <div className="flex items-start gap-3 border-b border-slate-100 px-4 py-3">
        <div className="mt-0.5 rounded-lg bg-blue-50 p-2 text-blue-700"><Icon size={16} /></div>
        <div className="min-w-0">
          <h3 className="text-sm font-semibold text-slate-900">{safeText(title)}</h3>
          {subtitle && <p className="mt-0.5 text-xs text-slate-500">{safeText(subtitle)}</p>}
        </div>
      </div>
      {children}
    </section>
  );
}

function ToggleGroup({ label, value, onChange, options }) {
  return (
    <div className="flex flex-col gap-1.5">
      <span className="text-[11px] font-semibold text-slate-600">{label}</span>
      <div className="inline-flex w-fit rounded-lg border border-slate-200 bg-slate-50 p-1" role="group" aria-label={label}>
        {options.map((option) => (
          <button
            key={option.value}
            type="button"
            aria-pressed={value === option.value}
            onClick={() => onChange(option.value)}
            className={`rounded-md px-3 py-2 text-xs font-semibold transition ${value === option.value
              ? "bg-blue-700 text-white shadow-sm"
              : "text-slate-600 hover:bg-white hover:text-slate-900"}`}
          >
            {option.label}
          </button>
        ))}
      </div>
    </div>
  );
}

function KpiCard({ label, item, unit, accent }) {
  const value = item?.display_value || (item?.value !== null && item?.value !== undefined ? fmt(item.value) : "Not available");
  const effectiveUnit = item?.unit || unit;
  const hasUnit = effectiveUnit && !String(value).toLowerCase().includes(String(effectiveUnit).toLowerCase());
  return (
    <div className="min-w-0 rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
      <div className="mb-2 flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wide text-slate-500">
        <span className="h-1.5 w-1.5 shrink-0 rounded-full" style={{ backgroundColor: accent }} />
        <span>{label}</span>
      </div>
      <div className="break-words text-2xl font-bold tracking-tight text-slate-900">{safeText(value)}</div>
      {hasUnit && <div className="mt-1 text-[10px] text-slate-500">{effectiveUnit}</div>}
      {item?.period_basis && <div className="mt-1 text-[10px] text-slate-400">{safeText(item.period_basis)}</div>}
    </div>
  );
}

function parsePeriod(value) {
  const text = String(value || "").trim();
  const yearMatch = text.match(/\b(20\d{2})\b/);
  let year = yearMatch ? Number(yearMatch[1]) : null;
  const monthYear = text.match(/\b(0?[1-9]|1[0-2])[-/.](20\d{2}|\d{2})\b/);
  let month = monthYear ? Number(monthYear[1]) : null;
  if (monthYear && year === null) year = monthYear[2].length === 4 ? Number(monthYear[2]) : 2000 + Number(monthYear[2]);
  if (!month) {
    const monthName = text.match(/\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\b/i);
    if (monthName) {
      month = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
        .indexOf(monthName[1].slice(0, 3).toLowerCase()) + 1;
    }
  }
  if (year === null) {
    const shortYear = text.match(/(?:[-/. ]|')(\d{2})\s*$/);
    const fiscal = text.match(/\bFY\s*'?([0-9]{2})\s*[-/]\s*[0-9]{2}\b/i);
    if (fiscal) year = 2000 + Number(fiscal[1]);
    else if (shortYear) year = 2000 + Number(shortYear[1]);
  }
  if (!month) {
    const iso = text.match(/20\d{2}[-/.](\d{1,2})/);
    if (iso) month = Number(iso[1]);
  }
  return { year, month: month && month >= 1 && month <= 12 ? month : null };
}

function chartRows(series = []) {
  const rows = new Map();
  series.forEach((item, index) => {
    (item.data || []).forEach((point) => {
      const label = safeText(point?.label);
      const value = Number(point?.value);
      if (label === "—" || !Number.isFinite(value)) return;
      if (!rows.has(label)) rows.set(label, { label });
      rows.get(label)[`series_${index}`] = value;
    });
  });
  return [...rows.values()];
}

function sortPeriodRows(rows) {
  return rows.sort((left, right) => {
    const a = parsePeriod(left.label);
    const b = parsePeriod(right.label);
    if (a.year && b.year && a.year !== b.year) return a.year - b.year;
    if (a.month && b.month && a.month !== b.month) return a.month - b.month;
    return left.label.localeCompare(right.label);
  });
}

function toYearlySeries(monthlySeries, explicitYearlySeries = [], unit = "MW") {
  const explicit = chartRows(explicitYearlySeries).filter((row) => parsePeriod(row.label).year && !parsePeriod(row.label).month);
  const explicitYears = new Set(explicit.map((row) => parsePeriod(row.label).year));
  const byYear = new Map();

  monthlySeries.forEach((series, seriesIndex) => {
    (series.data || []).forEach((point) => {
      const { year, month } = parsePeriod(point?.label);
      const value = Number(point?.value);
      if (!year || !month || !Number.isFinite(value) || explicitYears.has(year)) return;
      if (!byYear.has(year)) byYear.set(year, { months: new Set(), value: 0 });
      const record = byYear.get(year);
      record.months.add(month);
      record.value += value;
    });
  });

  const derived = [...byYear.entries()].map(([year, record]) => ({
    label: record.months.size === 12 ? String(year) : `${year} YTD`,
    value: record.value,
  }));
  const explicitValues = explicit.map((row) => ({ label: String(parsePeriod(row.label).year), value: Number(row.series_0) }));
  const combined = [...explicitValues, ...derived];
  return combined.length ? [{ name: `Manufactured (${unit})`, data: sortPeriodRows(combined) }] : [];
}

function yearsFromData(data, charts) {
  const years = new Set();
  const addYear = (value) => {
    const n = Number(value);
    if (Number.isInteger(n) && n >= 2000 && n <= 2099) years.add(String(n));
  };
  [data?.available_years, data?.selected_years, data?.filters?.years].forEach((items) => {
    (Array.isArray(items) ? items : []).forEach(addYear);
  });
  (charts || []).forEach((chart) => (chart?.series || []).forEach((series) => (series?.data || []).forEach((point) => {
    addYear(parsePeriod(point?.label).year);
    Object.keys(point?.by_year || {}).forEach(addYear);
  })));
  return [...years].sort((a, b) => Number(a) - Number(b));
}

function filterChartByYears(chart, selectedYears) {
  if (!chart || !selectedYears?.length) return chart || null;
  const wanted = new Set(selectedYears.map(String));
  return {
    ...chart,
    series: (chart.series || []).map((series) => ({
      ...series,
      data: (series.data || []).filter((point) => {
        const year = parsePeriod(point?.label).year;
        return year !== null && wanted.has(String(year));
      }),
    })),
  };
}

function filterManufacturerChartByYears(chart, selectedYears) {
  if (!chart || !selectedYears?.length) return chart || null;
  const wanted = new Set(selectedYears.map(String));
  return {
    ...chart,
    series: (chart.series || []).map((series) => ({
      ...series,
      data: (series.data || []).flatMap((point) => {
        const annual = point?.by_year;
        if (!annual || typeof annual !== "object") return [];
        const value = [...wanted].reduce((sum, year) => sum + (Number.isFinite(Number(annual[year])) ? Number(annual[year]) : 0), 0);
        return value > 0 ? [{ ...point, value }] : [];
      }),
    })),
  };
}

function productionTotalForYears(monthlyChart, yearlyChart, selectedYears) {
  const annual = new Map();
  const monthly = new Map();
  (yearlyChart?.series || []).forEach((series) => (series.data || []).forEach((point) => {
    const parsed = parsePeriod(point?.label);
    const value = Number(point?.value);
    if (parsed.year && !parsed.month && Number.isFinite(value)) annual.set(String(parsed.year), value);
  }));
  (monthlyChart?.series || []).forEach((series) => (series.data || []).forEach((point) => {
    const parsed = parsePeriod(point?.label);
    const value = Number(point?.value);
    if (parsed.year && parsed.month && Number.isFinite(value)) {
      const key = String(parsed.year);
      monthly.set(key, (monthly.get(key) || 0) + value);
    }
  }));
  const years = selectedYears?.length
    ? selectedYears.map(String)
    : [...new Set([...annual.keys(), ...monthly.keys()])];
  if (!years.length) return null;
  let total = 0;
  let found = false;
  years.forEach((year) => {
    if (annual.has(year)) {
      total += annual.get(year);
      found = true;
    } else if (monthly.has(year)) {
      total += monthly.get(year);
      found = true;
    }
  });
  return found ? Math.round((total + Number.EPSILON) * 1000) / 1000 : null;
}

function formatMonthlyLabel(value) {
  const { year, month } = parsePeriod(value);
  if (!year || !month) return String(value);
  const monthName = new Date(year, month - 1, 1).toLocaleString("en-IN", { month: "short" });
  return `${monthName} '${String(year).slice(-2)}`;
}

function ChartPanel({ title, subtitle, chart, noDataMessage, color = COLORS.cell }) {
  const series = Array.isArray(chart?.series) ? chart.series : [];
  const unit = chart?.unit || "MW";
  const rows = useMemo(() => sortPeriodRows(chartRows(series)), [series]);

  return (
    <Panel title={title} subtitle={subtitle || `Manufactured output (${unit})`} icon={BarChart3}>
      <div className="h-[340px] p-3">
        {rows.length ? (
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={rows} margin={{ top: 10, right: 12, left: 8, bottom: 52 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
              <XAxis dataKey="label" tick={{ fontSize: 10 }} tickFormatter={formatMonthlyLabel} angle={-25} textAnchor="end" interval="preserveStartEnd" height={66} />
              <YAxis tick={{ fontSize: 10 }} tickFormatter={fmt} />
              <Tooltip labelFormatter={formatMonthlyLabel} formatter={(value) => [`${fmt(value)} ${unit}`, "Manufactured"]} />
              {series.map((item, index) => (
                <Bar key={`${item.name}-${index}`} dataKey={`series_${index}`} name={safeText(item?.name)} fill={color} radius={[4, 4, 0, 0]} maxBarSize={54} />
              ))}
            </BarChart>
          </ResponsiveContainer>
        ) : (
          <div className="flex h-full min-h-[300px] items-center justify-center px-6 text-center text-sm text-slate-400">
            {noDataMessage || "The NISE source did not expose this manufacturing dataset."}
          </div>
        )}
      </div>
    </Panel>
  );
}

function MakerWisePanel({ product, manufacturers, selectedYears }) {
  const unit = product === "Module" ? "MWp" : "MW";
  const rows = useMemo(() => {
    const available = [...new Set((manufacturers || []).flatMap((item) => Object.keys(item?.yearly_values || {})))].sort((a, b) => Number(a) - Number(b));
    const years = selectedYears.length ? selectedYears : (available.length ? [available[available.length - 1]] : []);
    return (manufacturers || []).map((item) => {
      const values = item?.yearly_values || {};
      const value = years.reduce((sum, year) => sum + (Number.isFinite(Number(values[year])) ? Number(values[year]) : 0), 0);
      return { name: String(item?.name || "Unknown manufacturer"), value };
    }).filter((item) => item.value > 0).sort((a, b) => b.value - a.value);
  }, [manufacturers, selectedYears]);
  const yearsLabel = selectedYears.length ? selectedYears.join(", ") : "latest available year";
  return (
    <Panel title={`Solar ${product} Maker-wise Manufacturing`} subtitle={`Manufacturer production (${unit}) · ${yearsLabel}`} icon={Factory}>
      <div className="h-[420px] overflow-y-auto p-3">
        {rows.length ? (
          <ResponsiveContainer width="100%" height={Math.max(320, rows.length * 30)}>
            <BarChart data={rows} layout="vertical" margin={{ top: 8, right: 24, left: 12, bottom: 8 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
              <XAxis type="number" tick={{ fontSize: 10 }} tickFormatter={fmt} />
              <YAxis type="category" dataKey="name" width={190} tick={{ fontSize: 10 }} />
              <Tooltip formatter={(value) => [`${fmt(value)} ${unit}`, "Manufactured"]} />
              <Bar dataKey="value" name="Manufactured" fill={product === "Module" ? COLORS.module : COLORS.cell} radius={[0, 4, 4, 0]} maxBarSize={20} />
            </BarChart>
          </ResponsiveContainer>
        ) : (
          <div className="flex h-full min-h-[280px] items-center justify-center text-center text-sm text-slate-400">NISE has not returned manufacturer-wise production for the selected year(s).</div>
        )}
      </div>
    </Panel>
  );
}

const COMPANY_LINE_COLORS = ["#0f766e", "#2563eb", "#b45309", "#7c3aed", "#be123c", "#0891b2", "#4d7c0f", "#c2410c"];

function mergeCompanySeries(series = [], selectedYears = []) {
  const wanted = new Set((selectedYears || []).map(String));
  const rows = new Map();
  for (const item of series) {
    const key = String(item?.agency_id || "");
    for (const point of item?.data || []) {
      const label = String(point?.label || "");
      const year = parsePeriod(label).year;
      if (!label || (wanted.size && (!year || !wanted.has(String(year))))) continue;
      const value = Number(point?.value);
      if (!Number.isFinite(value)) continue;
      if (!rows.has(label)) rows.set(label, { label });
      rows.get(label)[key] = value;
    }
  }
  return sortPeriodRows([...rows.values()]);
}

function CompanyComparisonPanel({
  product, period, manufacturers, selectedIds, onToggle, onClear,
  search, onSearch, open, onOpenChange, series, loading, pending, selectedYears,
}) {
  const filtered = manufacturers.filter((item) => String(item.name || "").toLowerCase().includes(search.toLowerCase()));
  const chartRowsData = useMemo(() => mergeCompanySeries(series, selectedYears), [series, selectedYears]);
  const validSeries = (series || []).filter((item) => selectedIds.includes(String(item.agency_id)));
  const unit = product === "Module" ? "MWp" : "MW";
  return (
    <Panel
      title={`Solar ${product} Manufactured — Company Comparison`}
      subtitle={`Compare manufacturer production (${unit}) over ${period === "monthly" ? "months" : "years"}`}
      icon={BarChart3}
    >
      <div className="space-y-3 p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0 flex-1">
            <div className="mb-1 text-[11px] font-semibold text-slate-600">Select multiple manufacturers (up to 8)</div>
            <button
              type="button"
              aria-expanded={open}
              onClick={() => onOpenChange(!open)}
              className="flex w-full items-center justify-between gap-3 rounded-lg border border-slate-200 bg-white px-3 py-2 text-left text-xs font-medium text-slate-700 hover:bg-slate-50 sm:max-w-xl"
            >
              <span>{selectedIds.length ? `${selectedIds.length} manufacturer${selectedIds.length === 1 ? "" : "s"} selected` : "Choose manufacturers"}</span>
              <span className="text-slate-400">{open ? "Close" : "Select"}</span>
            </button>
            {open && (
              <div className="mt-2 max-h-72 overflow-hidden rounded-lg border border-slate-200 bg-white shadow-sm sm:max-w-xl">
                <div className="flex items-center gap-2 border-b border-slate-100 px-3 py-2">
                  <Search size={14} className="text-slate-400" />
                  <input
                    type="search"
                    value={search}
                    onChange={(event) => onSearch(event.target.value)}
                    placeholder="Search manufacturer"
                    className="min-w-0 flex-1 border-0 bg-transparent text-xs outline-none focus:ring-0"
                  />
                  <button type="button" onClick={onClear} className="text-[11px] font-semibold text-blue-700 hover:underline">Clear</button>
                  <button type="button" onClick={() => onOpenChange(false)} aria-label="Close manufacturer selector" className="rounded p-1 text-slate-400 hover:bg-slate-100"><X size={14} /></button>
                </div>
                <div className="max-h-56 overflow-y-auto p-2">
                  {filtered.map((item) => {
                    const id = String(item.agency_id);
                    const checked = selectedIds.includes(id);
                    const disabled = !checked && selectedIds.length >= 8;
                    return (
                      <label key={id} className={`flex cursor-pointer items-center gap-2 rounded-md px-2 py-2 text-xs ${disabled ? "cursor-not-allowed opacity-40" : "hover:bg-slate-50"}`}>
                        <input type="checkbox" checked={checked} disabled={disabled} onChange={() => onToggle(id)} className="h-3.5 w-3.5 rounded border-slate-300 text-blue-700 focus:ring-blue-500" />
                        <span className="min-w-0 flex-1 truncate text-slate-700">{item.name}</span>
                        <span className="text-[10px] text-slate-400">{fmt(item.latest_yearly_mw)} {unit}</span>
                      </label>
                    );
                  })}
                  {!filtered.length && <div className="px-3 py-5 text-center text-xs text-slate-400">No manufacturers match that search.</div>}
                </div>
              </div>
            )}
          </div>
          <div className="flex flex-wrap gap-1.5">
            {validSeries.map((item, index) => (
              <span key={item.agency_id} className="inline-flex items-center gap-1 rounded-full bg-slate-100 px-2 py-1 text-[10px] font-medium text-slate-700">
                <span className="h-2 w-2 rounded-full" style={{ backgroundColor: COMPANY_LINE_COLORS[index % COMPANY_LINE_COLORS.length] }} />
                {safeText(item.name)}
              </span>
            ))}
          </div>
        </div>

        {loading && <div className="text-xs text-blue-700">Loading selected manufacturer history…</div>}
        {!loading && pending && <div className="text-xs text-blue-700">Fetching missing monthly observations from NISE. Cached observations are shown as they arrive.</div>}
        {chartRowsData.length > 0 && validSeries.length > 0 ? (
          <div className="h-[380px] min-w-0">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={chartRowsData} margin={{ top: 12, right: 18, left: 8, bottom: 42 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
                <XAxis dataKey="label" tick={{ fontSize: 10 }} tickFormatter={period === "monthly" ? formatMonthlyLabel : undefined} angle={period === "monthly" ? -25 : 0} textAnchor={period === "monthly" ? "end" : "middle"} interval="preserveStartEnd" height={period === "monthly" ? 64 : 34} />
                <YAxis tick={{ fontSize: 10 }} tickFormatter={fmt} />
                <Tooltip labelFormatter={period === "monthly" ? formatMonthlyLabel : undefined} formatter={(value, name) => [`${fmt(value)} ${unit}`, name]} />
                <Legend wrapperStyle={{ fontSize: 11 }} />
                {validSeries.map((item, index) => (
                  <Line key={item.agency_id} type="monotone" dataKey={String(item.agency_id)} name={safeText(item.name)} stroke={COMPANY_LINE_COLORS[index % COMPANY_LINE_COLORS.length]} strokeWidth={2} dot={false} activeDot={{ r: 4 }} connectNulls={false} />
                ))}
              </LineChart>
            </ResponsiveContainer>
          </div>
        ) : (
          <div className="flex min-h-[300px] items-center justify-center rounded-lg bg-slate-50 px-6 text-center text-sm text-slate-400">
            {selectedIds.length ? (pending ? "Waiting for NISE company data…" : "No company observations are available for the selected period.") : "Select one or more manufacturers to load their production history and compare their trends."}
          </div>
        )}
      </div>
    </Panel>
  );
}

export default function SectorSolar() {
  const [period, setPeriod] = useState("monthly");
  const [product, setProduct] = useState("Cell");
  const [yearSelections, setYearSelections] = useState({ Cell: [], Module: [] });
  const [response, setResponse] = useState(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [refreshNonce, setRefreshNonce] = useState(0);
  const requestSequenceRef = useRef(0);
  const handledRefreshNonceRef = useRef(0);
  const [selectedCompanyIds, setSelectedCompanyIds] = useState([]);
  const [companySeries, setCompanySeries] = useState([]);
  const [companySeriesLoading, setCompanySeriesLoading] = useState(false);
  const [companySeriesPending, setCompanySeriesPending] = useState(false);
  const [companySearch, setCompanySearch] = useState("");
  const [companySelectorOpen, setCompanySelectorOpen] = useState(false);
  const companyRequestSequenceRef = useRef(0);

  // Only product selection changes the API key. Period and year selections
  // are local projections of the already-cached all-years source snapshot.
  const load = useCallback(async (refresh = false, active = () => true, sequence = requestSequenceRef.current, selectedProduct = "Cell") => {
    try {
      const result = await api.sectorScreenerReport("solar", "overview", {
        refresh,
        filters: { product: selectedProduct, period: "yearly", sourceFilters: {} },
      });
      if (!active() || sequence !== requestSequenceRef.current) return null;
      if (!result || typeof result !== "object") throw new Error("The NISE Solar API returned an empty or invalid response.");
      setResponse(result);
      const pending = ["queued", "scraping", "stale"].includes(String(result.status || "").toLowerCase()) || Boolean(result.refresh_in_progress);
      if (!pending && result.error && !result.data) {
        setError(String(result.error || result.last_attempt?.warning || "NISE Solar data collection failed."));
      } else if (result.status === "failed" && !result.data) {
        setError(String(result.last_attempt?.warning || "NISE Solar data collection failed."));
      } else {
        setError("");
      }
      return result;
    } catch (err) {
      if (active() && sequence === requestSequenceRef.current) setError(err?.message || "Unable to connect to the local Solar data service.");
      return null;
    }
  }, []);

  useEffect(() => {
    let active = true;
    let timer = null;
    let attempts = 0;
    const sequence = ++requestSequenceRef.current;
    const manualRefresh = refreshNonce !== handledRefreshNonceRef.current;
    handledRefreshNonceRef.current = refreshNonce;
    const existingProduct = String(response?.selected_filters?.product || response?.data?.selected_product || "").toLowerCase();
    const hasMatchingSnapshot = existingProduct === product.toLowerCase() && Boolean(response?.data);
    setLoading(!hasMatchingSnapshot);
    setRefreshing(false);
    setError("");

    const poll = async (force = false) => {
      if (!active || sequence !== requestSequenceRef.current) return;
      const result = await load(force, () => active, sequence, product);
      if (!active || sequence !== requestSequenceRef.current) return;
      attempts += 1;
      const pending = result && (["queued", "scraping", "stale"].includes(String(result.status || "").toLowerCase()) || Boolean(result.refresh_in_progress));
      const hasData = Boolean(result?.data);
      // Never block a usable cached report for a background refresh. When the
      // first worker warm has no snapshot yet, keep checking until it publishes.
      // A full historical collection spans multiple NISE year endpoints and
      // may take longer than the old six-poll window. Keep the initial warm-up
      // visible for up to the backend's 210-second scrape timeout.
      const maxForegroundAttempts = manualRefresh ? 50 : 55;
      if (pending && (!hasData || manualRefresh) && attempts < maxForegroundAttempts) {
        setLoading(!hasData && attempts < 6);
        setRefreshing(Boolean(manualRefresh && pending));
        timer = window.setTimeout(() => poll(false), manualRefresh ? 5000 : 4000);
      } else {
        setLoading(false);
        setRefreshing(false);
        if (pending && !hasData) {
          setError("No Solar source snapshot is available yet. The direct NISE collector is still warming this product. Check the backend/worker logs if this continues; dashboard period and year filters use the cached snapshot.");
        }
      }
    };

    poll(manualRefresh);
    return () => {
      active = false;
      if (timer) window.clearTimeout(timer);
    };
  }, [refreshNonce, product, load]);

  const refresh = useCallback(() => setRefreshNonce((value) => value + 1), []);

  // The worker refreshes the persistent product snapshot every five minutes.
  // Re-read the local API cache periodically so an open dashboard sees the new
  // snapshot without launching source collection from a dashboard read.
  useEffect(() => {
    let active = true;
    const timer = window.setInterval(() => {
      load(false, () => active, requestSequenceRef.current, product);
    }, 30_000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [product, load]);
  const responseProduct = String(response?.selected_filters?.product || response?.data?.selected_product || "").toLowerCase();
  const responseMatchesProduct = responseProduct === product.toLowerCase();
  const data = responseMatchesProduct ? (response?.data || null) : null;
  const kpis = Array.isArray(data?.kpis) ? data.kpis : [];
  const charts = Array.isArray(data?.charts) ? data.charts : [];
  const manufacturers = Array.isArray(data?.manufacturers)
    ? data.manufacturers.filter((item) => item?.agency_id && !String(item.agency_id).startsWith("name:"))
    : [];
  const prefix = product.toLowerCase();
  const kpiByKey = useMemo(() => new Map(kpis.map((item) => [item.key, item])), [kpis]);
  const monthlyChart = charts.find((item) => item?.key === `nise-${prefix}-monthwise-manufactured`);
  const yearlyChart = charts.find((item) => item?.key === `nise-${prefix}-yearwise-manufactured`);
  const availableYears = useMemo(() => yearsFromData(data, charts), [data, charts]);
  const selectedYears = yearSelections[product] || [];
  const updateSelectedYears = (values) => {
    const allowed = new Set(availableYears);
    const next = [...new Set((values || []).map(String).filter((value) => allowed.has(value)))].sort((a, b) => Number(a) - Number(b));
    setYearSelections((current) => ({ ...current, [product]: next }));
  };
  const timeChart = useMemo(() => {
    if (period === "yearly") {
      const unit = prefix === "module" ? "MWp" : "MW";
      const series = toYearlySeries(monthlyChart?.series || [], yearlyChart?.series || [], unit);
      return filterChartByYears({ ...(yearlyChart || {}), key: `nise-${prefix}-yearwise-manufactured`, unit, series }, selectedYears);
    }
    return filterChartByYears(monthlyChart || null, selectedYears);
  }, [period, yearlyChart, monthlyChart, prefix, selectedYears]);
  useEffect(() => {
    setSelectedCompanyIds([]);
    setCompanySeries([]);
    setCompanySelectorOpen(false);
    setCompanySearch("");
  }, [product]);

  useEffect(() => {
    let active = true;
    let timer = null;
    const sequence = ++companyRequestSequenceRef.current;
    if (!selectedCompanyIds.length) {
      setCompanySeries([]);
      setCompanySeriesLoading(false);
      setCompanySeriesPending(false);
      return () => { active = false; };
    }
    const loadCompanySeries = async () => {
      if (!active || sequence !== companyRequestSequenceRef.current) return;
      setCompanySeriesLoading(true);
      try {
        const result = await api.solarCompanySeries(product, selectedCompanyIds, period);
        if (!active || sequence !== companyRequestSequenceRef.current) return;
        setCompanySeries(Array.isArray(result?.series) ? result.series : []);
        setCompanySeriesPending(Boolean(result?.pending));
        setCompanySeriesLoading(false);
        if (result?.pending) timer = window.setTimeout(loadCompanySeries, 3000);
      } catch (err) {
        if (!active || sequence !== companyRequestSequenceRef.current) return;
        setCompanySeriesLoading(false);
        setCompanySeriesPending(false);
        setCompanySeries([]);
      }
    };
    loadCompanySeries();
    return () => {
      active = false;
      if (timer) window.clearTimeout(timer);
    };
  }, [product, period, selectedCompanyIds.join("|")]);

  const toggleCompany = (agencyId) => {
    setSelectedCompanyIds((current) => current.includes(agencyId)
      ? current.filter((value) => value !== agencyId)
      : (current.length < 8 ? [...current, agencyId] : current));
  };

  const manufacturerKpi = kpiByKey.get(`solar-${prefix}-manufacturers`);
  const sourceManufacturedKpi = kpiByKey.get(`solar-${prefix}-manufactured-mw`);
  const filteredProductionTotal = selectedYears.length
    ? productionTotalForYears(monthlyChart, yearlyChart, selectedYears)
    : null;
  const manufacturedKpi = !selectedYears.length
    ? sourceManufacturedKpi
    : filteredProductionTotal === null
      ? { ...(sourceManufacturedKpi || {}), value: null, display_value: "Not available", unit: prefix === "module" ? "MWp" : "MW", period_basis: "No production observations for selected years" }
      : {
        ...(sourceManufacturedKpi || {}),
        value: filteredProductionTotal,
        display_value: fmt(filteredProductionTotal),
        unit: prefix === "module" ? "MWp" : "MW",
        period_basis: `${selectedYears.join(", ")} source years`,
      };
  const partialComponents = Array.isArray(data?.solar_missing_components) ? data.solar_missing_components : [];
  const isPartialSnapshot = data?.status === "partial" || response?.status === "partial";
  const updatedLabel = data?.refreshed_at || data?.captured_at || response?.refreshed_at;
  const updatedAt = updatedLabel
    ? new Date(updatedLabel).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" })
    : "";
  const captureWarnings = [
    ...(Array.isArray(data?.capture_completeness?.warnings) ? data.capture_completeness.warnings : []),
    ...(Array.isArray(data?.data_quality_notes) ? data.data_quality_notes : []),
    ...(Array.isArray(response?.last_attempt?.capture_completeness?.warnings) && !data ? response.last_attempt.capture_completeness.warnings : []),
  ].filter(Boolean);
  const captureAudit = data?.capture_audit && typeof data.capture_audit === "object" ? data.capture_audit : {};
  const endpointDiagnostics = Array.isArray(captureAudit.endpoints) ? captureAudit.endpoints : [];
  const failedHttpRequests = Array.isArray(captureAudit.failed_requests) ? captureAudit.failed_requests : [];
  return (
    <div className="space-y-4 pb-8">
      <PageHeader title="Solar Dashboard" />

      {error && (
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900" role="alert">
          <span className="min-w-0 flex-1">{error}</span>
          <button type="button" onClick={refresh} className="rounded-md border border-amber-300 bg-white px-3 py-1.5 font-semibold hover:bg-amber-100">Retry</button>
        </div>
      )}

      <div className="flex flex-wrap items-end gap-5 rounded-xl border border-slate-200 bg-white px-4 py-3 shadow-sm">
        <ToggleGroup label="Period" value={period} onChange={setPeriod} options={[{ value: "monthly", label: "Monthly" }, { value: "yearly", label: "Yearly" }]} />
        <ToggleGroup
          label="Product"
          value={product}
          onChange={(nextProduct) => {
            setSelectedCompanyIds([]);
            setCompanySeries([]);
            setCompanySeriesPending(false);
            setProduct(nextProduct);
          }}
          options={[{ value: "Cell", label: "Solar Cell" }, { value: "Module", label: "Solar Module" }]}
        />
        <div className="flex min-w-[190px] flex-col gap-1.5">
          <span className="text-[11px] font-semibold text-slate-600">Reporting year(s)</span>
          <details className="relative">
            <summary className="flex cursor-pointer list-none items-center justify-between gap-3 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs text-slate-700 hover:bg-slate-50">
              <span>{selectedYears.length ? `${selectedYears.length} year(s) selected` : "All captured years"}</span>
              <span aria-hidden="true">▾</span>
            </summary>
            <div className="absolute left-0 top-full z-30 mt-1 max-h-64 min-w-full overflow-y-auto rounded-lg border border-slate-200 bg-white p-2 shadow-lg">
              {availableYears.map((year) => (
                <label key={year} className="flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-xs text-slate-700 hover:bg-slate-50">
                  <input type="checkbox" checked={selectedYears.includes(year)} onChange={(event) => updateSelectedYears(event.target.checked ? [...selectedYears, year] : selectedYears.filter((item) => item !== year))} className="h-3.5 w-3.5 rounded border-slate-300 text-blue-700 focus:ring-blue-500" />
                  {year}
                </label>
              ))}
              {selectedYears.length > 0 && <button type="button" onClick={() => updateSelectedYears([])} className="mt-1 w-full border-t border-slate-100 px-2 pt-2 text-left text-xs font-semibold text-blue-700">Clear selection</button>}
            </div>
          </details>
        </div>
        <div className="ml-auto flex items-center gap-2 pb-1 text-[10px] text-slate-400">
          {updatedAt && <span>Updated {updatedAt}</span>}
          <button type="button" onClick={refresh} disabled={refreshing} aria-label="Refresh Solar data" title="Refresh Solar data" className="rounded-md border border-slate-200 p-2 text-slate-600 hover:bg-slate-50 disabled:opacity-50"><RefreshCw size={14} className={refreshing ? "animate-spin" : ""} /></button>
        </div>
      </div>
      {loading && <div className="text-xs text-blue-700">Loading NISE Solar data…</div>}

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <KpiCard
          label={`No. of Solar ${product} Manufacturers`}
          item={manufacturerKpi}
          accent={COLORS[prefix]}
        />
        <KpiCard
          label={`Solar ${product} Manufactured`}
          item={manufacturedKpi}
          unit={product === "Module" ? "MWp" : "MW"}
          accent={COLORS[prefix]}
        />
      </div>

      <ChartPanel
        title={`Solar ${product} Manufactured ${period === "monthly" ? "Month-wise" : "Year-wise"}`}
        subtitle={`Manufacturing output (${product === "Module" ? "MWp" : "MW"})`}
        chart={timeChart}
        noDataMessage={period === "yearly"
          ? "NISE has not exposed annual totals or enough dated monthly observations to build this year-wise chart. Partial years are labelled YTD when available."
          : "NISE did not expose a month-wise manufactured-MW series for this product."}
        color={COLORS[prefix]}
      />

      <MakerWisePanel product={product} manufacturers={manufacturers} selectedYears={selectedYears} />

      <CompanyComparisonPanel
        product={product}
        period={period}
        manufacturers={manufacturers}
        selectedIds={selectedCompanyIds}
        onToggle={toggleCompany}
        onClear={() => setSelectedCompanyIds([])}
        search={companySearch}
        onSearch={setCompanySearch}
        open={companySelectorOpen}
        onOpenChange={setCompanySelectorOpen}
        series={companySeries}
        loading={companySeriesLoading}
        pending={companySeriesPending}
        selectedYears={selectedYears}
      />

    </div>
  );
}
