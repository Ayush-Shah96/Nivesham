import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  ArrowDown, ArrowUp, Check, ChevronDown, ChevronRight, ExternalLink,
  Filter, RefreshCw, Search, SlidersHorizontal, X,
} from "lucide-react";
import { api } from "../lib/api";
import { Badge, Card, Empty, ErrorBox, PageHeader } from "../components/UI";

const PAGE_SIZE = 100;
const ACTIVITY_LABELS = {
  INSIDER_TRADING: "Promoter / Insider",
  BULK_DEAL: "Bulk / Block deals",
  QIP: "QIP",
  PREFERENTIAL_WARRANT: "Preferential / Warrant",
};
const COVERAGE_LABELS = { PO: "PO", HT: "HT", LT: "LT", OTHERS: "Others" };
const DEFAULT_FILTERS = {
  date: { min: "", max: "" },
  company: "",
  coverage: [],
  analyst: [],
  activity: [],
  person: "",
  action: [],
  quantity: { min: "", max: "" },
  price: { min: "", max: "" },
  value: { min: "", max: "" },
  cmp: { min: "", max: "" },
  change: { min: "", max: "" },
  holding: { min: "", max: "" },
  status: [],
};

function number(value, digits = 2) {
  if (value === null || value === undefined || value === "") return "—";
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed.toLocaleString("en-IN", { maximumFractionDigits: digits }) : String(value);
}

function currency(value) {
  if (value === null || value === undefined || value === "") return "—";
  const parsed = Number(value);
  return Number.isFinite(parsed) ? `₹${parsed.toLocaleString("en-IN", { maximumFractionDigits: 2 })}` : String(value);
}

function croreCurrency(value) {
  if (value === null || value === undefined || value === "") return "—";
  const parsed = Number(value);
  return Number.isFinite(parsed) ? `₹${(parsed / 10000000).toLocaleString("en-IN", { maximumFractionDigits: 2 })} Cr` : String(value);
}

function dateLabel(value) {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? String(value)
    : parsed.toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" });
}

function labelForActivity(row) {
  if (row.activity_type === "INSIDER_TRADING") return "Promoter / Insider";
  if (row.activity_type === "BULK_DEAL") return row.activity_sub_type === "BLOCK" ? "Block Deal" : "Bulk Deal";
  if (row.activity_type === "QIP") return "QIP";
  if (row.activity_type === "PREFERENTIAL_WARRANT") return row.activity_sub_type === "WARRANT" ? "Warrant Issue" : "Preferential Issue";
  return row.activity_type || "Other";
}

function StatusBadge({ status }) {
  const value = String(status || "PARTIAL").toUpperCase();
  const tone = value === "DETAILED" ? "green" : value === "LEGACY" ? "blue" : value === "PARTIAL" ? "amber" : "slate";
  return <Badge tone={tone}>{value}</Badge>;
}

function CoverageBadge({ mode }) {
  const value = String(mode || "OTHERS").toUpperCase();
  const styles = {
    PO: "border-violet-200 bg-violet-50 text-violet-700",
    HT: "border-blue-200 bg-blue-50 text-blue-700",
    LT: "border-cyan-200 bg-cyan-50 text-cyan-700",
    OTHERS: "border-slate-200 bg-slate-100 text-slate-600",
  };
  return <span className={`inline-flex rounded-md border px-2 py-1 text-[10px] font-bold ${styles[value] || styles.OTHERS}`}>{COVERAGE_LABELS[value] || "Others"}</span>;
}

function FilterMenu({ label, mode, value, options = [], onApply, hint }) {
  const triggerRef = useRef(null);
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState(value);
  const [position, setPosition] = useState({ top: 0, left: 0 });
  const active = mode === "multi"
    ? Array.isArray(value) && value.length > 0
    : mode === "range" || mode === "dateRange"
      ? Boolean(value?.min || value?.max)
      : Boolean(value);

  useEffect(() => {
    if (open) setDraft(value);
  }, [open, value]);

  const toggle = () => {
    if (open) {
      setOpen(false);
      return;
    }
    const rect = triggerRef.current?.getBoundingClientRect();
    if (rect) {
      const width = 276;
      const left = Math.max(10, Math.min(rect.left, window.innerWidth - width - 10));
      const top = Math.max(10, Math.min(rect.bottom + 7, window.innerHeight - 350));
      setPosition({ top, left });
    }
    setOpen(true);
  };

  const apply = () => {
    onApply(draft);
    setOpen(false);
  };
  const clear = () => {
    const empty = mode === "multi" ? [] : mode === "range" || mode === "dateRange" ? { min: "", max: "" } : "";
    setDraft(empty);
    onApply(empty);
    setOpen(false);
  };

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        onClick={toggle}
        title={`Filter ${label}`}
        aria-label={`Filter ${label}`}
        className={`ml-1 inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-md transition ${active ? "bg-blue-100 text-blue-700" : "text-slate-400 hover:bg-slate-100 hover:text-slate-700"}`}
      >
        <Filter size={12} />
      </button>
      {open && createPortal(
        <div className="fixed inset-0 z-[100]" onMouseDown={(event) => { if (event.target === event.currentTarget) setOpen(false); }}>
          <div
            className="absolute max-h-[min(420px,80vh)] w-[276px] overflow-y-auto rounded-xl border border-slate-200 bg-white p-3 shadow-2xl"
            style={{ top: position.top, left: position.left }}
            onMouseDown={(event) => event.stopPropagation()}
          >
            <div className="mb-3 flex items-center justify-between border-b border-slate-100 pb-2">
              <div className="text-xs font-bold text-slate-800">Filter {label}</div>
              <button type="button" onClick={() => setOpen(false)} className="rounded-md p-1 text-slate-400 hover:bg-slate-100" aria-label="Close filter"><X size={14} /></button>
            </div>
            {mode === "multi" && (
              <div className="space-y-2">
                {options.length ? options.map((option) => {
                  const selected = Array.isArray(draft) && draft.includes(option.value);
                  return (
                    <label key={option.value} className="flex cursor-pointer items-center gap-2 rounded-md px-1 py-1 text-xs text-slate-700 hover:bg-slate-50">
                      <input type="checkbox" checked={selected} onChange={(event) => {
                        const current = Array.isArray(draft) ? draft : [];
                        setDraft(event.target.checked ? [...current, option.value] : current.filter((item) => item !== option.value));
                      }} className="h-3.5 w-3.5 rounded border-slate-300 text-blue-600 focus:ring-blue-500" />
                      <span>{option.label}</span>
                    </label>
                  );
                }) : <div className="py-3 text-xs text-slate-400">No filter options available.</div>}
              </div>
            )}
            {mode === "range" && (
              <div className="space-y-3">
                {hint && <div className="text-[10px] text-slate-500">{hint}</div>}
                <div className="grid grid-cols-2 gap-2">
                  <label className="text-[10px] font-semibold text-slate-500">Minimum<input type="number" step="any" value={draft?.min || ""} onChange={(event) => setDraft((current) => ({ ...current, min: event.target.value }))} className="mt-1 w-full rounded-lg border border-slate-200 px-2 py-2 text-xs font-normal text-slate-800 outline-none focus:border-blue-400" placeholder="Min" /></label>
                  <label className="text-[10px] font-semibold text-slate-500">Maximum<input type="number" step="any" value={draft?.max || ""} onChange={(event) => setDraft((current) => ({ ...current, max: event.target.value }))} className="mt-1 w-full rounded-lg border border-slate-200 px-2 py-2 text-xs font-normal text-slate-800 outline-none focus:border-blue-400" placeholder="Max" /></label>
                </div>
              </div>
            )}
            {mode === "dateRange" && (
              <div className="grid grid-cols-1 gap-2">
                <label className="text-[10px] font-semibold text-slate-500">From<input type="date" value={draft?.min || ""} onChange={(event) => setDraft((current) => ({ ...current, min: event.target.value }))} className="mt-1 w-full rounded-lg border border-slate-200 px-2 py-2 text-xs font-normal text-slate-800" /></label>
                <label className="text-[10px] font-semibold text-slate-500">To<input type="date" value={draft?.max || ""} onChange={(event) => setDraft((current) => ({ ...current, max: event.target.value }))} className="mt-1 w-full rounded-lg border border-slate-200 px-2 py-2 text-xs font-normal text-slate-800" /></label>
              </div>
            )}
            {mode === "text" && (
              <input autoFocus value={draft || ""} onChange={(event) => setDraft(event.target.value)} placeholder={`Search ${label.toLowerCase()}…`} className="w-full rounded-lg border border-slate-200 px-3 py-2 text-xs outline-none focus:border-blue-400" />
            )}
            <div className="mt-4 flex items-center justify-between border-t border-slate-100 pt-3">
              <button type="button" onClick={clear} className="text-xs font-semibold text-slate-500 hover:text-slate-800">Clear filter</button>
              <button type="button" onClick={apply} className="inline-flex items-center gap-1 rounded-lg bg-slate-900 px-3 py-2 text-xs font-semibold text-white hover:bg-slate-700"><Check size={12} /> Apply</button>
            </div>
          </div>
        </div>,
        document.body,
      )}
    </>
  );
}

function SortHead({ label, field, sortBy, sortDir, onSort }) {
  const active = sortBy === field;
  return (
    <button type="button" onClick={() => onSort(field)} className="inline-flex items-center gap-1 whitespace-nowrap text-[10px] font-bold uppercase tracking-[0.08em] text-slate-600 transition hover:text-blue-700">
      {label}
      {active ? (sortDir === "asc" ? <ArrowUp size={11} /> : <ArrowDown size={11} />) : <span className="text-slate-300">↕</span>}
    </button>
  );
}

function HeaderCell({ label, field, filterMode, filterValue, options, hint, sortBy, sortDir, onSort, onFilter }) {
  return (
    <th className="sticky top-0 z-20 whitespace-nowrap border-b border-slate-200 bg-slate-50/95 px-3 py-3 backdrop-blur">
      <div className="flex items-center gap-0.5">
        {field ? <SortHead label={label} field={field} sortBy={sortBy} sortDir={sortDir} onSort={onSort} /> : <span className="text-[10px] font-bold uppercase tracking-[0.08em] text-slate-600">{label}</span>}
        {filterMode && <FilterMenu label={label} mode={filterMode} value={filterValue} options={options} hint={hint} onApply={onFilter} />}
      </div>
    </th>
  );
}

function DetailPanel({ row }) {
  const transactions = Array.isArray(row.details?.transactions) ? row.details.transactions : [];
  return (
    <div className="border-y border-blue-100 bg-blue-50/40 px-5 py-4">
      <div className="mb-3 flex items-center justify-between">
        <div className="text-[11px] font-bold uppercase tracking-wide text-slate-600">Combined transactions</div>
        <span className="rounded-full bg-white px-2.5 py-1 text-[10px] font-semibold text-slate-600 ring-1 ring-slate-200">{transactions.length} records</span>
      </div>
      <div className="overflow-x-auto rounded-xl border border-slate-200 bg-white shadow-sm">
        <table className="min-w-[960px] w-full text-left text-[11px]">
          <thead className="bg-slate-100 text-[10px] uppercase tracking-wide text-slate-500">
            <tr>{["Transaction date", "Person / Client", "Action", "Quantity", "Price", "Value (₹ Cr)", "Holding before", "Holding after", "Filing"].map((heading) => <th key={heading} className="px-3 py-2.5">{heading}</th>)}</tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {transactions.map((transaction, index) => (
              <tr key={transaction.record_id || index} className="hover:bg-blue-50/30">
                <td className="whitespace-nowrap px-3 py-2.5">{dateLabel(transaction.date)}</td>
                <td className="px-3 py-2.5">{transaction.person || row.person_allottee_client || "—"}</td>
                <td className="px-3 py-2.5">{transaction.action || row.action || "—"}</td>
                <td className="whitespace-nowrap px-3 py-2.5 tabular-nums">{number(transaction.quantity, 4)}</td>
                <td className="whitespace-nowrap px-3 py-2.5 tabular-nums">{currency(transaction.price)}</td>
                <td className="whitespace-nowrap px-3 py-2.5 font-semibold tabular-nums">{croreCurrency(transaction.value)}</td>
                <td className="whitespace-nowrap px-3 py-2.5 tabular-nums">{number(transaction.holding_before, 4)}</td>
                <td className="whitespace-nowrap px-3 py-2.5 tabular-nums">{number(transaction.holding_after, 4)}</td>
                <td className="px-3 py-2.5">{transaction.source_url ? <a href={transaction.source_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 font-semibold text-blue-700 hover:underline"><ExternalLink size={11} /> Open</a> : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default function Regulatory() {
  const [rows, setRows] = useState([]);
  const [filterOptions, setFilterOptions] = useState({ activities: [], actions: [], statuses: [], coverage_modes: [], analysts: [] });
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [page, setPage] = useState(1);
  const [hasNext, setHasNext] = useState(false);
  const [sortBy, setSortBy] = useState("date");
  const [sortDir, setSortDir] = useState("desc");
  const [expanded, setExpanded] = useState({});
  const [filters, setFilters] = useState(DEFAULT_FILTERS);

  useEffect(() => {
    let alive = true;
    api.regulatoryActivityFilterOptions().then((result) => {
      if (alive && result && typeof result === "object") setFilterOptions(result);
    }).catch(() => {
      // Static fallbacks keep the table usable if the optional facet request fails.
    });
    return () => { alive = false; };
  }, []);

  const loadPage = useCallback(async ({ showLoading = true, forceRefresh = false } = {}) => {
    if (showLoading) setLoading(true);
    setError("");
    try {
      const result = await api.regulatoryActivityEvents({
        company: filters.company,
        person: filters.person,
        activity_types: filters.activity.join(","),
        actions: filters.action.join(","),
        statuses: filters.status.join(","),
        coverage_modes: filters.coverage.join(","),
        analysts: filters.analyst.join(","),
        from_date: filters.date.min,
        to_date: filters.date.max,
        quantity_min: filters.quantity.min,
        quantity_max: filters.quantity.max,
        price_min: filters.price.min,
        price_max: filters.price.max,
        value_min_crore: filters.value.min,
        value_max_crore: filters.value.max,
        cmp_min: filters.cmp.min,
        cmp_max: filters.cmp.max,
        change_min: filters.change.min,
        change_max: filters.change.max,
        holding_min: filters.holding.min,
        holding_max: filters.holding.max,
        sort_by: sortBy,
        sort_dir: sortDir,
        limit: PAGE_SIZE,
        offset: (page - 1) * PAGE_SIZE,
        forceRefresh,
      });
      const next = Array.isArray(result) ? result : [];
      setRows(next);
      setHasNext(next.length === PAGE_SIZE);
      setExpanded({});
    } catch (caught) {
      setError(caught?.message || "Unable to load Regulatory Activity records.");
    } finally {
      if (showLoading) setLoading(false);
    }
  }, [filters, sortBy, sortDir, page]);

  useEffect(() => { void loadPage({ showLoading: true }); }, [loadPage]);

  const updateFilter = (key, value) => {
    setFilters((current) => ({ ...current, [key]: value }));
    setPage(1);
  };

  const clearFilters = () => {
    setFilters(DEFAULT_FILTERS);
    setPage(1);
  };

  const activeFilterCount = useMemo(() => Object.entries(filters).reduce((total, [key, value]) => {
    if (Array.isArray(value)) return total + (value.length ? 1 : 0);
    if (value && typeof value === "object") return total + (value.min || value.max ? 1 : 0);
    return total + (value ? 1 : 0);
  }, 0), [filters]);

  const refresh = async () => {
    if (refreshing) return;
    setRefreshing(true);
    setNotice("");
    setError("");
    try {
      const result = await api.syncRegulatoryActivity({ days: 7 });
      const failed = result?.failed_sources || [];
      setNotice(failed.length
        ? `Collection finished with source failures: ${failed.join(", ")}. Successfully extracted rows were retained.`
        : "All configured Regulatory Activity sources completed for the latest seven calendar days.");
      await loadPage({ showLoading: false, forceRefresh: true });
    } catch (caught) {
      setError(caught?.message || "Regulatory collection could not be started.");
    } finally {
      setRefreshing(false);
    }
  };

  const onSort = (field) => {
    if (sortBy === field) setSortDir((current) => current === "asc" ? "desc" : "asc");
    else { setSortBy(field); setSortDir("desc"); }
    setPage(1);
  };

  const options = useMemo(() => ({
    activities: (filterOptions.activities || []).map((value) => ({ value, label: ACTIVITY_LABELS[value] || value })),
    actions: (filterOptions.actions || []).map((value) => ({ value, label: value })),
    statuses: (filterOptions.statuses || []).map((value) => ({ value, label: value })),
    coverage: ["PO", "HT", "LT", "OTHERS"].map((value) => ({ value, label: COVERAGE_LABELS[value] })),
    analysts: (filterOptions.analysts || []).map((value) => ({ value, label: value || "Unassigned" })),
  }), [filterOptions]);

  const header = (label, field, key, mode, list = null, hint = null) => (
    <HeaderCell
      key={key}
      label={label}
      field={field}
      sortBy={sortBy}
      sortDir={sortDir}
      onSort={onSort}
      filterMode={mode}
      filterValue={filters[key]}
      options={list || []}
      hint={hint}
      onFilter={(value) => updateFilter(key, value)}
    />
  );

  const hasRows = rows.length > 0;
  const stockLinkActivities = new Set(["BULK_DEAL", "QIP", "PREFERENTIAL_WARRANT"]);

  return (
    <div className="space-y-4 pb-6">
      <PageHeader
        eyebrow="EXCHANGE DISCLOSURES"
        title="Regulatory Activity"
        description="Insider trades, bulk/block deals, QIP and preferential/warrant issues in one normalized activity view. Filter directly from each column heading."
        action={<button onClick={refresh} disabled={refreshing} className="inline-flex items-center gap-2 rounded-xl border border-slate-200 bg-white px-4 py-2 text-xs font-bold text-slate-700 shadow-sm transition hover:border-blue-200 hover:bg-blue-50 disabled:opacity-50"><RefreshCw size={14} className={refreshing ? "animate-spin" : ""} />{refreshing ? "Collecting…" : "Collect latest 7 days"}</button>}
      />

      {error && <ErrorBox error={error} />}
      {notice && <div className="rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-3 text-xs font-medium text-emerald-800">{notice}</div>}

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <div className="rounded-xl border border-slate-200 bg-white px-3 py-2.5 shadow-sm"><div className="text-[9px] font-bold uppercase tracking-wider text-slate-400">Displayed entries</div><div className="mt-1 text-lg font-bold text-slate-900 tabular-nums">{loading ? "…" : number(rows.length, 0)}</div></div>
        <div className="rounded-xl border border-slate-200 bg-white px-3 py-2.5 shadow-sm"><div className="text-[9px] font-bold uppercase tracking-wider text-slate-400">Active filters</div><div className="mt-1 text-lg font-bold text-blue-700 tabular-nums">{activeFilterCount}</div></div>
        <div className="rounded-xl border border-slate-200 bg-white px-3 py-2.5 shadow-sm"><div className="text-[9px] font-bold uppercase tracking-wider text-slate-400">Value display</div><div className="mt-1 text-lg font-bold text-slate-900">₹ Crores</div></div>
        <div className="flex items-center justify-between rounded-xl border border-slate-200 bg-white px-3 py-2.5 shadow-sm"><div><div className="text-[9px] font-bold uppercase tracking-wider text-slate-400">Filtering</div><div className="mt-1 text-xs font-semibold text-slate-700">Column headers</div></div><button type="button" onClick={clearFilters} disabled={!activeFilterCount} className="inline-flex items-center gap-1 rounded-lg border border-slate-200 px-2.5 py-1.5 text-[10px] font-bold text-slate-600 transition hover:bg-slate-50 disabled:opacity-40"><SlidersHorizontal size={11} /> Clear all</button></div>
      </div>

      <Card>
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-200 bg-gradient-to-r from-slate-50 to-white px-4 py-3">
          <div className="flex items-center gap-2"><span className="h-2 w-2 rounded-full bg-emerald-500" /><span className="text-[11px] font-bold uppercase tracking-wider text-slate-600">Regulatory event register</span></div>
          <div className="text-[10px] text-slate-400">Click <Filter size={10} className="mx-0.5 inline" /> in a heading to filter · Click heading label to sort</div>
        </div>

        {loading ? <div className="px-5 py-16 text-center text-sm text-slate-500"><RefreshCw size={18} className="mx-auto mb-2 animate-spin text-blue-500" />Loading regulatory events…</div> : hasRows ? (
          <div className="max-h-[72vh] overflow-auto">
            <table className="min-w-[2200px] w-full border-separate border-spacing-0 text-left text-xs">
              <thead><tr>
                <th className="sticky top-0 z-20 w-9 border-b border-slate-200 bg-slate-50/95 px-2 py-3" />
                {header("Date", "date", "date", "dateRange")}
                {header("Company", "company", "company", "text")}
                {header("Coverage", "coverage", "coverage", "multi", options.coverage)}
                {header("Analyst", "analyst", "analyst", "multi", options.analysts)}
                {header("Activity", "activity", "activity", "multi", options.activities)}
                {header("Person / Allottee / Client", null, "person", "text")}
                {header("Action", "action", "action", "multi", options.actions)}
                {header("Quantity", "quantity", "quantity", "range")}
                {header("Price (₹)", "price", "price", "range", null, "Minimum and maximum price per share in rupees.")}
                {header("Value (₹ Cr)", "value", "value", "range", null, "Enter the amount in INR Crores.")}
                {header("CMP (₹)", "cmp", "cmp", "range", null, "Current market price per share in rupees.")}
                {header("CMP Change (%)", "cmp_change", "change", "range", null, "Percentage change. You can enter negative values.")}
                {header("Holding Before", null, "holding", "range")}
                {header("Status", null, "status", "multi", options.statuses)}
                <th className="sticky top-0 z-20 border-b border-slate-200 bg-slate-50/95 px-3 py-3 text-[10px] font-bold uppercase tracking-[0.08em] text-slate-600">Stock / Filing</th>
              </tr></thead>
              <tbody>
                {rows.map((row, index) => {
                  const showStockPage = stockLinkActivities.has(row.activity_type);
                  const destination = showStockPage ? row.stock_url : row.source_url;
                  return (
                    <React.Fragment key={row.id}>
                      <tr className={`group transition-colors hover:bg-blue-50/60 ${index % 2 ? "bg-slate-50/50" : "bg-white"}`}>
                        <td className="border-b border-slate-100 px-2 py-3 align-middle">{Number(row.transaction_count) > 1 && Array.isArray(row.details?.transactions) ? <button type="button" onClick={() => setExpanded((current) => ({ ...current, [row.id]: !current[row.id] }))} className="rounded-md p-1 text-slate-400 transition hover:bg-blue-100 hover:text-blue-700" aria-label="Toggle combined transactions">{expanded[row.id] ? <ChevronDown size={14} /> : <ChevronRight size={14} />}</button> : null}</td>
                        <td className="whitespace-nowrap border-b border-slate-100 px-3 py-3.5 text-[11px] font-medium text-slate-600">{dateLabel(row.date)}</td>
                        <td className="border-b border-slate-100 px-3 py-3.5"><div className="font-bold text-slate-800">{showStockPage && row.stock_url ? <a href={row.stock_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1.5 hover:text-blue-700 hover:underline">{row.company || "—"}<ExternalLink size={10} className="text-slate-400" /></a> : row.company || "—"}</div><div className="mt-1 text-[10px] font-medium tracking-wide text-slate-400">{row.symbol || row.isin || "—"}</div></td>
                        <td className="border-b border-slate-100 px-3 py-3.5"><CoverageBadge mode={row.coverage_mode} /></td>
                        <td className="border-b border-slate-100 px-3 py-3.5 text-[11px] font-medium text-slate-700">{row.analyst || <span className="text-slate-400">Unassigned</span>}</td>
                        <td className="border-b border-slate-100 px-3 py-3.5"><div className="font-semibold text-slate-700">{labelForActivity(row)}</div><div className="mt-1 text-[10px] text-slate-400">{row.activity_sub_type || row.source_exchange || "NSE"}</div></td>
                        <td className="border-b border-slate-100 px-3 py-3.5"><div className="max-w-[210px] truncate text-[11px] text-slate-700" title={row.person_allottee_client || "Unspecified"}>{row.person_allottee_client || "—"}</div>{Number(row.transaction_count) > 1 && <div className="mt-1 text-[10px] font-bold text-blue-700">{number(row.transaction_count, 0)} transactions combined</div>}</td>
                        <td className="border-b border-slate-100 px-3 py-3.5"><span className="inline-flex rounded-md bg-slate-100 px-2 py-1 text-[10px] font-bold text-slate-700">{row.action || "—"}</span></td>
                        <td className="whitespace-nowrap border-b border-slate-100 px-3 py-3.5 text-[11px] font-semibold tabular-nums text-slate-800">{number(row.quantity, 4)}</td>
                        <td className="whitespace-nowrap border-b border-slate-100 px-3 py-3.5 text-[11px] font-semibold tabular-nums text-slate-800">{currency(row.price)}</td>
                        <td className="whitespace-nowrap border-b border-slate-100 px-3 py-3.5 text-[11px] font-bold tabular-nums text-slate-900">{croreCurrency(row.value)}</td>
                        <td className="whitespace-nowrap border-b border-slate-100 px-3 py-3.5 text-[11px] font-semibold tabular-nums text-slate-800">{currency(row.current_market_price)}</td>
                        <td className={`whitespace-nowrap border-b border-slate-100 px-3 py-3.5 text-[11px] font-semibold tabular-nums ${row.market_price_change_percent == null ? "text-slate-400" : Number(row.market_price_change_percent) >= 0 ? "text-emerald-700" : "text-rose-700"}`}>{row.market_price_change_percent == null ? "—" : `${Number(row.market_price_change_percent) > 0 ? "+" : ""}${number(row.market_price_change_percent)}%`}</td>
                        <td className="whitespace-nowrap border-b border-slate-100 px-3 py-3.5 text-[11px] tabular-nums text-slate-600">{number(row.holding_before, 4)}</td>
                        <td className="border-b border-slate-100 px-3 py-3.5"><StatusBadge status={row.status} /></td>
                        <td className="border-b border-slate-100 px-3 py-3.5">{destination ? <a href={destination} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 whitespace-nowrap rounded-lg border border-blue-200 bg-white px-2.5 py-1.5 text-[10px] font-bold text-blue-700 shadow-sm transition hover:border-blue-300 hover:bg-blue-50"><ExternalLink size={11} />{showStockPage ? "NSE Stock" : "View filing"}</a> : <span className="text-[10px] text-slate-400">Unavailable</span>}</td>
                      </tr>
                      {expanded[row.id] && Number(row.transaction_count) > 1 && <tr><td colSpan={16} className="p-0"><DetailPanel row={row} /></td></tr>}
                    </React.Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : <Empty title="No regulatory events found" text="Try adjusting or clearing the column filters, or collect the latest exchange disclosures." />}

        <div className="flex flex-wrap items-center justify-between gap-3 border-t border-slate-200 bg-slate-50/70 px-4 py-3">
          <div className="text-[10px] font-medium text-slate-500">Page {page} · {rows.length} entries on this page · value displayed in ₹ Crores</div>
          <div className="flex items-center gap-2"><button disabled={page <= 1} onClick={() => setPage((current) => Math.max(1, current - 1))} className="rounded-lg border border-slate-200 bg-white px-3 py-2 text-[11px] font-semibold text-slate-600 transition hover:bg-slate-100 disabled:opacity-40">Previous</button><button disabled={!hasNext} onClick={() => setPage((current) => current + 1)} className="rounded-lg border border-slate-200 bg-white px-3 py-2 text-[11px] font-semibold text-slate-600 transition hover:bg-slate-100 disabled:opacity-40">Next</button></div>
        </div>
      </Card>
    </div>
  );
}
