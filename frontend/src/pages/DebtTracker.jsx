import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertCircle, ArrowDown, ArrowUp, BarChart3, Check, CheckCircle2, Pencil,
  ChevronDown, Clock3, Download, FileSpreadsheet,
  Filter, Loader2, Plus, Search, ShieldAlert, UploadCloud, X,
} from 'lucide-react';
import {
  Bar, BarChart, CartesianGrid, Cell, Legend, Pie, PieChart, ResponsiveContainer,
  Tooltip, XAxis, YAxis,
} from 'recharts';
import { api } from '../lib/api';

const COLORS = ['#163b66', '#2674b8', '#14877b', '#e49b37', '#7662b5', '#cc665d', '#6c8f54', '#7c8796'];
const ACCEPT = '.xlsx,.xlsm,.xltx,.xltm';
const BASE_COLUMNS = [
  { key: 'fund', label: 'Fund Name as per Broker' },
  { key: 'paper', label: 'Paper Name' },
  { key: 'groupExposure', label: 'GROUP EXPOSURE' },
  { key: 'maturity', label: 'Paper Maturity' },
  { key: 'fundMaturity', label: 'Fund Maturity' },
  { key: 'marketValue', label: 'Paper Amount in Cr', numeric: true, sum: true, digits: 0 },
  { key: 'holding', label: '% of Holding (Fund Level)', numeric: true, sum: true, digits: 4 },
  { key: 'broker', label: 'Broker' },
  { key: 'instrument', label: 'ZCB / MLD / NCD' },
  { key: 'exposure', label: 'Prop Mkt Value', numeric: true, sum: true, digits: 0 },
  { key: 'category', label: 'Category' },
];
const SHEET4_COLUMNS = [
  { key: 'groupExposure', label: 'GROUP EXPOSURE (Issuer)' },
  { key: 'parentGroup', label: 'Parent Group' },
  { key: 'paper', label: 'Paper Name' },
  { key: 'isin', label: 'ISIN' },
  { key: 'fund', label: 'Fund Name as per Broker' },
  { key: 'broker', label: 'Broker' },
  { key: 'maturity', label: 'Paper Maturity' },
  { key: 'holding', label: '% of Holding (Fund Level)', numeric: true, sum: true, digits: 4 },
  { key: 'total', label: 'Prop Mkt Value (Cr)', numeric: true, sum: true, digits: 0 },
  { key: 'cumulative', label: 'Cumulative Paper Exposure (Cr)', numeric: true, sum: true, digits: 0 },
  { key: 'fundCount', label: 'Fund Count', numeric: true, sum: true, digits: 0 },
  { key: 'ratingChange', label: 'Rating Change' },
];
const RECONCILIATION_COLUMNS = [
  { key: 'fund', label: 'Fund' }, { key: 'paper', label: 'Paper' }, { key: 'issuer', label: 'Issuer' }, { key: 'isin', label: 'ISIN' },
  { key: 'brokerA', label: 'Broker A' }, { key: 'amountA', label: 'Paper Amount A (Cr)', numeric: true, digits: 2 },
  { key: 'brokerB', label: 'Broker B' }, { key: 'amountB', label: 'Paper Amount B (Cr)', numeric: true, digits: 2 },
  { key: 'differenceCr', label: 'Difference (Cr)', numeric: true, sum: true, digits: 2 },
  { key: 'higherBroker', label: 'Higher Amount Reported By' }, { key: 'differencePct', label: 'Difference (%)', numeric: true, digits: 2 },
  { key: 'status', label: 'Status' }, { key: 'amountBasis', label: 'Amount Basis' }, { key: 'sourceA', label: 'Source A' }, { key: 'sourceB', label: 'Source B' },
];
const SHEET8_COLUMNS = [
  { key: 'group', label: 'Parent Group' },
  { key: 'exposure', label: 'Exposure (Cr)', numeric: true, sum: true, digits: 0 },
  { key: 'percentage', label: 'Percentage', numeric: true, percent: true, sum: true },
  { key: 'rating', label: 'Rating' },
  { key: 'paperCount', label: 'Paper Count', numeric: true, sum: true, digits: 0 },
  { key: 'fundCount', label: 'Fund Count', numeric: true, sum: true, digits: 0 },
  { key: 'added', label: 'Added' },
  { key: 'exited', label: 'Exited' },
];
const GROUP_EXPOSURE_COLUMNS = [
  { key: 'group', label: 'Parent Group' },
  { key: 'exposure', label: 'Exposure (Cr)', numeric: true, sum: true, digits: 2 },
  { key: 'percentage', label: 'Portfolio Share', numeric: true, percent: true },
  { key: 'paperCount', label: 'No. of Papers', numeric: true },
  { key: 'fundCount', label: 'No. of Funds', numeric: true },
  { key: 'classification', label: 'Classification' },
  { key: 'rating', label: 'Rating' },
];
const CATEGORY_COLUMNS = [
  { key: 'category', label: 'Category' },
  { key: 'exposure', label: 'Exposure (Cr)', numeric: true, sum: true, digits: 0 },
  { key: 'percentage', label: 'Percentage', numeric: true, percent: true, sum: true },
  { key: 'limit', label: 'Limit', numeric: true, percent: true },
  { key: 'status', label: 'Status' },
];

function fmt(value, digits = 2) {
  const n = Number(value);
  if (!Number.isFinite(n)) return '—';
  return new Intl.NumberFormat('en-IN', { minimumFractionDigits: 0, maximumFractionDigits: digits }).format(n);
}
function cr(value, digits = 2) { return `₹${fmt(value, digits)} Cr`; }
function pct(value, digits = 2) {
  const n = Number(value);
  return Number.isFinite(n) ? `${fmt(n * 100, digits)}%` : '—';
}
function dateLabel(value) {
  if (!value) return '—';
  const d = new Date(String(value).length === 10 ? `${value}T00:00:00` : value);
  return Number.isNaN(d.getTime()) ? String(value) : d.toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' });
}
function getError(error) {
  return error?.response?.data?.detail || error?.detail || error?.message || 'Unable to process the workbooks.';
}
function toIsoDate(value) {
  if (!value) return '';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toISOString().slice(0, 10);
}
function rowFromDetail(row) {
  return {
    fund: row.source_scheme || row.fund || '', paper: row.paper || '',
    groupExposure: row.issuer || 'Needs Mapping', parentGroup: row.group || 'Others',
    maturity: toIsoDate(row.maturity), fundMaturity: toIsoDate(row.fund_maturity),
    marketValue: row.market_value_crore ?? '', holding: Number(row.allocation || 0) * 100,
    broker: row.broker || row.source?.file || '', instrument: row.position_level === 'issuer' ? '' : (row.asset_type || ''),
    exposure: Number(row.exposure || 0), category: row.reported_rating || row.rating || row.category || '',
    rating: row.reported_rating || row.rating || '', issuer: row.issuer || 'Needs Mapping', isin: row.isin || '',
  };
}
function buildSheet4Rows(detailRows) {
  const keyFor = (row) => {
    if (row.isin) return `isin:${String(row.isin).trim().toUpperCase()}`;
    const issuer = normalizeReviewText(row.issuer || '');
    const paper = normalizeReviewText(row.paper || '');
    const maturity = String(row.maturity || '').slice(0, 10);
    return row.position_level === 'issuer' ? `issuer:${issuer || paper}` : `paper:${paper}|issuer:${issuer}|maturity:${maturity}`;
  };
  const totals = new Map(); const funds = new Map();
  (detailRows || []).forEach((row) => {
    const key = keyFor(row); totals.set(key, (totals.get(key) || 0) + Number(row.exposure || 0));
    if (!funds.has(key)) funds.set(key, new Set());
    funds.get(key).add(row.fund || row.source_scheme || '');
  });
  return (detailRows || []).map((row, index) => {
    const key = keyFor(row);
    return {
      _rowIndex: index, groupExposure: row.issuer || 'Needs Mapping',
      parentGroup: row.group || 'Unmapped / Review', paper: row.paper || '',
      isin: row.isin || '', fund: row.source_scheme || row.fund || '',
      broker: row.broker || row.source?.file || '', maturity: toIsoDate(row.maturity),
      holding: Number(row.allocation || 0) * 100, total: Number(row.exposure || 0),
      cumulative: totals.get(key) || 0, fundCount: funds.get(key)?.size || 0,
      ratingChange: row.rating_change || 'NO',
      issuer: row.issuer || '', position_level: row.position_level || 'security',
      rating: row.reported_rating || row.rating || '', category: row.category || '',
    };
  });
}

function Button({ children, onClick, variant = 'primary', disabled = false, className = '', type = 'button' }) {
  const styles = variant === 'primary'
    ? 'bg-[#183c67] text-white hover:bg-[#102e50]'
    : variant === 'quiet'
      ? 'border border-slate-200 bg-white text-slate-700 hover:bg-slate-50'
      : 'border border-transparent bg-transparent text-slate-600 hover:bg-slate-100';
  return <button type={type} disabled={disabled} onClick={onClick} className={`inline-flex items-center justify-center gap-2 rounded-lg px-3.5 py-2.5 text-sm font-semibold transition disabled:cursor-not-allowed disabled:opacity-50 ${styles} ${className}`}>{children}</button>;
}
function Card({ children, className = '' }) { return <section className={`rounded-xl border border-slate-200 bg-white shadow-sm ${className}`}>{children}</section>; }
function SectionTitle({ title, hint, action }) {
  return <div className="flex flex-wrap items-end justify-between gap-3"><div><h2 className="text-base font-bold text-slate-900">{title}</h2>{hint && <p className="mt-1 text-xs text-slate-500">{hint}</p>}</div>{action}</div>;
}
function Pill({ children, tone = 'slate' }) {
  const tones = { slate: 'bg-slate-100 text-slate-700', green: 'bg-emerald-50 text-emerald-700', red: 'bg-rose-50 text-rose-700', amber: 'bg-amber-50 text-amber-800', blue: 'bg-blue-50 text-blue-700' };
  return <span className={`inline-flex items-center rounded-full px-2.5 py-1 text-[11px] font-bold ${tones[tone] || tones.slate}`}>{children}</span>;
}
function KPI({ label, value, sub, icon: Icon, tone = 'blue' }) {
  const tones = { blue: 'bg-blue-50 text-blue-700', green: 'bg-emerald-50 text-emerald-700', amber: 'bg-amber-50 text-amber-700', violet: 'bg-violet-50 text-violet-700' };
  return <Card className="p-4"><div className="flex items-start justify-between gap-3"><div className="min-w-0"><div className="text-xs font-semibold text-slate-500">{label}</div><div className="mt-2 text-2xl font-bold tracking-tight text-slate-900">{value}</div><div className="mt-1 text-[11px] text-slate-400">{sub}</div></div><span className={`grid h-9 w-9 shrink-0 place-items-center rounded-lg ${tones[tone]}`}><Icon size={18}/></span></div></Card>;
}

function ColumnFilter({ column, values, selected, onApply }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [draft, setDraft] = useState(values);
  const [menuPosition, setMenuPosition] = useState(null);
  const rootRef = useRef(null);
  const matches = values.filter((value) => (value === '' ? '(Blanks)' : String(value)).toLowerCase().includes(query.trim().toLowerCase()));
  const active = selected != null && selected.length < values.length;

  useEffect(() => {
    if (open) setDraft(selected == null ? values : [...selected]);
  }, [open, selected, values]);

  useEffect(() => {
    if (!open) return undefined;
    const closeOnOutsideClick = (event) => {
      if (!rootRef.current?.contains(event.target)) setOpen(false);
    };
    document.addEventListener('mousedown', closeOnOutsideClick);
    return () => document.removeEventListener('mousedown', closeOnOutsideClick);
  }, [open]);

  const toggleValue = (value, checked) => {
    setDraft((current) => {
      if (checked) return current.includes(value) ? current : [...current, value];
      return current.filter((item) => item !== value);
    });
  };

  const allVisibleSelected = matches.length > 0 && matches.every((value) => draft.includes(value));
  const toggleVisibleSelection = () => {
    setDraft((current) => {
      const visibleSet = new Set(matches);
      const everyVisibleSelected = matches.length > 0 && matches.every((value) => current.includes(value));
      return everyVisibleSelected
        ? current.filter((value) => !visibleSet.has(value))
        : [...new Set([...current, ...matches])];
    });
  };

  const clearSelection = () => setDraft([]);

  const clearFilter = () => {
    onApply(null);
    setDraft(values);
    setQuery('');
    setOpen(false);
  };

  const apply = () => {
    const next = draft.length >= values.length ? null : draft;
    onApply(next);
    setOpen(false);
  };

  return <div ref={rootRef} className="relative inline-flex" onMouseDown={(event) => event.stopPropagation()}>
    <button type="button" onClick={(event) => { if (open) { setOpen(false); return; } const rect = event.currentTarget.getBoundingClientRect(); setMenuPosition({ top: Math.max(8, Math.min(rect.bottom + 4, window.innerHeight - 380)), left: Math.max(8, Math.min(rect.right - 288, window.innerWidth - 296)) }); setOpen(true); }} title={`Filter ${column.label}`} aria-label={`Filter ${column.label}`} className={`ml-1 inline-flex h-6 w-6 items-center justify-center rounded-md ${active ? 'bg-blue-100 text-blue-800' : 'text-slate-500 hover:bg-slate-200 hover:text-slate-800'}`}>
      <Filter size={13}/>
      {active && <span className="absolute -right-0.5 -top-0.5 h-2 w-2 rounded-full bg-blue-600 ring-2 ring-white"/>}
    </button>
    {open && <div className="fixed z-[100] w-72 rounded-xl border border-slate-200 bg-white p-3 text-left font-normal text-slate-700 shadow-2xl" style={menuPosition ? { top: menuPosition.top, left: menuPosition.left } : undefined} onMouseDown={(event) => event.stopPropagation()}>
      <div className="mb-2 flex items-center justify-between gap-2"><span className="text-xs font-bold text-slate-800">Filter: {column.label}</span><button type="button" onClick={() => setOpen(false)} className="rounded p-1 text-slate-400 hover:bg-slate-100"><X size={14}/></button></div>
      <div className="relative"><Search size={13} className="absolute left-2.5 top-2.5 text-slate-400"/><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search filter values..." className="w-full rounded-lg border border-slate-200 py-2 pl-8 pr-2 text-xs outline-none focus:border-blue-400"/></div>
      <div className="my-2 flex flex-wrap items-center justify-between gap-1 text-[11px]">
        <button type="button" onClick={toggleVisibleSelection} disabled={!matches.length} className="rounded px-2 py-1 font-semibold text-blue-700 hover:bg-blue-50 disabled:cursor-not-allowed disabled:opacity-40">
          {allVisibleSelected ? 'Deselect all' : 'Select all'}{query ? ' shown' : ''}
        </button>
        <button type="button" onClick={clearSelection} disabled={!draft.length} className="rounded px-2 py-1 font-semibold text-slate-600 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-40">Select none</button>
        <button type="button" onClick={clearFilter} className="rounded px-2 py-1 font-semibold text-slate-600 hover:bg-slate-100">Clear filter</button>
      </div>
      <div className="max-h-64 overflow-auto border-y border-slate-100 py-1">
        {matches.map((value, index) => <div key={`${value}-${index}`} className="flex items-center gap-2 rounded px-1.5 py-1 hover:bg-slate-50">
          <label className="flex min-w-0 flex-1 cursor-pointer items-center gap-2 py-1 text-xs">
            <input type="checkbox" checked={draft.includes(value)} onChange={(event) => toggleValue(value, event.target.checked)} className="rounded border-slate-300 text-blue-700"/>
            <span className="truncate" title={value}>{value === '' ? '(Blanks)' : value}</span>
          </label>
          <button type="button" onClick={() => { onApply([value]); setOpen(false); }} className="shrink-0 rounded px-2 py-1 text-[10px] font-bold text-blue-700 hover:bg-blue-50">Only</button>
        </div>)}
        {!matches.length && <div className="px-2 py-4 text-center text-xs text-slate-400">No matching values</div>}
      </div>
      <div className="mt-2 flex items-center justify-between text-[10px] text-slate-500"><span className="font-semibold">{draft.length} of {values.length} selected</span><span>{draft.length === 0 ? 'No values selected' : (active ? 'Filter active' : 'All values')}</span></div>
      <div className="mt-3 flex justify-end gap-2 border-t border-slate-100 pt-2"><button type="button" onClick={() => setOpen(false)} className="rounded-lg px-3 py-1.5 text-xs font-semibold text-slate-600 hover:bg-slate-100">Cancel</button><button type="button" onClick={apply} className="rounded-lg bg-[#183c67] px-3 py-1.5 text-xs font-semibold text-white hover:bg-[#102e50]">Apply</button></div>
    </div>}
  </div>;
}

function spreadsheetRowKey(row) {
  if (row?._rowIndex != null) return String(row._rowIndex);
  return [row?.isin, row?.paper, row?.fund, row?.maturity].map((value) => String(value ?? '')).join('::');
}

function SpreadsheetTable({ title, hint, columns, rows, initialSort, downloadAction, onEditGroup, editGroupKey = 'group', empty = 'No rows found.', highlightRowKey = '', initialFilters = {}, filterRequestId = 0, rowClassName }) {
  const [filters, setFilters] = useState(initialFilters || {});
  const [sort, setSort] = useState(initialSort || { key: columns[0]?.key, dir: 'asc' });

  useEffect(() => {
    setFilters(initialFilters || {});
  }, [filterRequestId]);
  const filterValues = useMemo(() => Object.fromEntries(columns.map((column) => [
    column.key,
    [...new Set((rows || []).map((row) => String(row[column.key] ?? '')))]
      .sort((a, b) => a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' })),
  ])), [rows, columns]);
  const filtered = useMemo(() => (rows || []).filter((row) => columns.every((column) => {
    const selected = filters[column.key];
    return selected == null || selected.includes(String(row[column.key] ?? ''));
  })).sort((a, b) => {
    const col = columns.find((item) => item.key === sort.key);
    const av = a[sort.key]; const bv = b[sort.key];
    if (av == null || av === '') return 1;
    if (bv == null || bv === '') return -1;
    const result = col?.numeric ? Number(av) - Number(bv) : String(av).localeCompare(String(bv), undefined, { numeric: true, sensitivity: 'base' });
    return result * (sort.dir === 'desc' ? -1 : 1);
  }), [rows, columns, filters, sort]);
  const toggleSort = (key) => setSort((old) => old.key === key ? { key, dir: old.dir === 'asc' ? 'desc' : 'asc' } : { key, dir: 'asc' });
  const activeCount = Object.values(filters).filter((value) => value != null).length;
  const sums = useMemo(() => Object.fromEntries(columns.filter((column) => column.sum).map((column) => [
    column.key,
    filtered.reduce((sum, row) => sum + (Number(row[column.key]) || 0), 0),
  ])), [filtered, columns]);

  useEffect(() => {
    if (!highlightRowKey) return;
    const element = document.getElementById(`debt-spreadsheet-row-${encodeURIComponent(highlightRowKey)}`);
    if (element) element.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }, [highlightRowKey, filtered.length]);

  return <Card className="relative overflow-visible">
    <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 p-4">
      <div><h2 className="text-base font-bold text-slate-900">{title}</h2><p className="mt-1 text-xs text-slate-500">{hint} · {filtered.length.toLocaleString('en-IN')} rows</p></div>
      <div className="flex flex-wrap items-center gap-2">{activeCount > 0 && <Button variant="quiet" onClick={() => setFilters({})}><X size={13}/>Clear all filters ({activeCount})</Button>}{downloadAction}</div>
    </div>
    <div className="h-[min(68vh,720px)] min-h-[360px] overflow-auto overscroll-contain">
      <table className="min-w-full border-separate border-spacing-0 text-left text-xs">
        <thead className="sticky top-0 z-30"><tr>{columns.map((column) => <th key={column.key} className="sticky top-0 z-30 whitespace-nowrap border-b border-r border-slate-200 bg-slate-100 px-3 py-2.5 font-bold text-slate-700">
          <div className="flex items-center justify-between gap-2">
            <button type="button" onClick={() => toggleSort(column.key)} className="inline-flex items-center gap-1.5 text-left">{column.label}{sort.key === column.key && (sort.dir === 'asc' ? <ArrowUp size={12}/> : <ArrowDown size={12}/>)}</button>
            <ColumnFilter column={column} values={filterValues[column.key] || []} selected={filters[column.key]} onApply={(value) => setFilters((old) => { const next = { ...old }; if (value == null) delete next[column.key]; else next[column.key] = value; return next; })}/>
          </div>
        </th>)}</tr></thead>
        <tbody>{filtered.map((row, index) => {
          const rowKey = spreadsheetRowKey(row);
          const highlighted = Boolean(highlightRowKey) && rowKey === highlightRowKey;
          const customRowClass = rowClassName ? rowClassName(row) : '';
          return <tr id={`debt-spreadsheet-row-${encodeURIComponent(rowKey)}`} key={`${rowKey}-${index}`} className={`${customRowClass || (highlighted ? 'bg-amber-100 ring-1 ring-inset ring-amber-400' : (index % 2 ? 'bg-slate-50/45' : 'bg-white'))} hover:bg-blue-50/60`}>
            {columns.map((column) => <td key={column.key} className="whitespace-nowrap border-b border-r border-slate-100 px-3 py-2.5 text-slate-700">
              {column.key === editGroupKey && onEditGroup && (row.issuer || row.isin) ? <button type="button" title="Edit this paper's parent-group mapping" onClick={() => onEditGroup(row)} className="inline-flex items-center gap-1.5 rounded px-1 py-0.5 text-left hover:bg-blue-50 hover:text-blue-800"><span>{row[column.key] || ''}</span><Pencil size={11} className="text-slate-400"/></button> : column.key === 'status' && row.status ? <Pill tone={row.status === 'Mismatch' ? 'red' : 'green'}>{row.status}</Pill> : column.percent && row[column.key] !== '' && row[column.key] != null ? pct(row[column.key]) : column.numeric && row[column.key] !== '' && row[column.key] != null ? fmt(row[column.key], column.digits ?? (column.key === 'holding' ? 4 : 2)) : (row[column.key] ?? '—')}
            </td>)}
          </tr>;
        })}</tbody>
        {columns.some((column) => column.sum) && <tfoot className="z-30"><tr className="bg-slate-100 font-bold text-slate-900">{columns.map((column, index) => <td key={column.key} className={`sticky bottom-0 whitespace-nowrap border-t border-r border-slate-300 bg-slate-100 px-3 py-3 ${index === 0 ? 'left-0 z-40 text-left' : 'z-30 text-right'}`}>{index === 0 ? 'GRAND TOTAL' : column.sum ? fmt(sums[column.key], column.digits ?? 2) : ''}</td>)}</tr></tfoot>}
      </table>
      {!filtered.length && <div className="p-8 text-center text-sm text-slate-400">{empty}</div>}
    </div>
    <div className="flex flex-wrap items-center justify-between gap-3 border-t border-slate-200 px-4 py-3 text-xs text-slate-500"><span>{filtered.length.toLocaleString('en-IN')} rows · {activeCount ? `${activeCount} column filters active` : 'No column filters'} · Total row reflects all filtered rows</span><span>Scroll to view all rows · Filter and sort from each column header</span></div>
  </Card>;
}

function UploadModal({ open, onClose, onGenerate, busy, status, error }) {
  const [mapping, setMapping] = useState(null);
  const [holdings, setHoldings] = useState(null);
  const [brokers, setBrokers] = useState([]);
  const [brokerError, setBrokerError] = useState('');
  useEffect(() => { if (!open) { setMapping(null); setHoldings(null); setBrokers([]); setBrokerError(''); } }, [open]);
  if (!open) return null;
  const filesReady = Boolean(mapping && holdings && brokers.length);
  const tile = (label, hint, file, setFile, number) => <label className="flex cursor-pointer flex-col gap-3 rounded-xl border border-dashed border-slate-300 bg-slate-50 p-4 hover:border-blue-400 hover:bg-blue-50/30">
    <div className="flex items-center justify-between"><span className="text-sm font-bold text-slate-800">{number}. {label}</span><FileSpreadsheet size={19} className="text-slate-500"/></div>
    <span className="text-xs leading-5 text-slate-500">{hint}</span>
    <span className="truncate rounded-lg bg-white px-3 py-2 text-xs font-semibold text-slate-600 ring-1 ring-slate-200">{file?.name || 'Choose Excel file'}</span>
    <input type="file" accept={ACCEPT} className="hidden" onChange={(event) => setFile(event.target.files?.[0] || null)} />
  </label>;
  return <div className="fixed inset-0 z-[100] flex items-center justify-center bg-slate-950/45 p-4" onMouseDown={(event) => { if (event.target === event.currentTarget && !busy) onClose(); }}>
    <div className="w-full max-w-3xl rounded-2xl bg-white shadow-2xl">
      <div className="flex items-center justify-between border-b border-slate-200 px-6 py-4"><div><h2 className="text-lg font-bold text-slate-900">New quarterly Debt Exposure</h2><p className="mt-1 text-xs text-slate-500">Upload the previous-quarter report, MProfit holdings and one or more broker workbooks.</p></div><button type="button" disabled={busy} onClick={onClose} className="rounded-lg p-2 text-slate-400 hover:bg-slate-100"><X size={18}/></button></div>
      <div className="grid gap-3 p-6 md:grid-cols-3">
        {tile('Previous-quarter report', 'June / last quarter workbook containing BASE, Sheet6 and Sheet8.', mapping, setMapping, 1)}
        {tile('Portfolio holdings', 'MProfit Portfolio Summary Report. The report date is read from this file.', holdings, setHoldings, 2)}
        <label className="flex cursor-pointer flex-col gap-3 rounded-xl border border-dashed border-slate-300 bg-slate-50 p-4 hover:border-blue-400 hover:bg-blue-50/30">
          <div className="flex items-center justify-between"><span className="text-sm font-bold text-slate-800">3. Broker holdings</span><FileSpreadsheet size={19} className="text-slate-500"/></div>
          <span className="text-xs leading-5 text-slate-500">Select up to 12 supported broker workbooks, including 360, Kotak, ASK and ICICI. Overlapping schemes are reconciled before exposure is calculated.</span>
          <span className="rounded-lg bg-white px-3 py-2 text-xs font-semibold text-slate-600 ring-1 ring-slate-200">{brokers.length ? `${brokers.length} broker file${brokers.length === 1 ? '' : 's'} selected` : 'Choose one or more Excel files'}</span>
          <input type="file" accept={ACCEPT} multiple className="hidden" onChange={(event) => { const selected = Array.from(event.target.files || []); if (selected.length > 12) { setBrokerError('You can upload a maximum of 12 broker workbooks in one run.'); return; } setBrokerError(''); setBrokers(selected); }}/>
          {brokers.length > 0 && <span className="text-[11px] text-slate-500">{brokers.map((file) => file.name).join(' · ')}</span>}
        </label>
      </div>
      {brokerError && <div className="mx-6 -mt-2 mb-4 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">{brokerError}</div>}
      {brokers.length > 0 && <div className="mx-6 -mt-2 mb-4 flex flex-wrap gap-2">{brokers.map((file, index) => <span key={`${file.name}-${index}`} className="inline-flex max-w-full items-center gap-1.5 rounded-full bg-blue-50 px-2.5 py-1 text-[11px] font-medium text-blue-800"><span className="max-w-[260px] truncate">{file.name}</span><button type="button" disabled={busy} onClick={() => setBrokers((items) => items.filter((_, itemIndex) => itemIndex !== index))} aria-label={`Remove ${file.name}`} className="rounded-full p-0.5 hover:bg-blue-100"><X size={12}/></button></span>)}</div>}
      <div className="mx-6 mb-4 flex items-start gap-2 rounded-lg bg-blue-50 px-3 py-2.5 text-xs leading-5 text-blue-800"><AlertCircle size={15} className="mt-0.5 shrink-0"/>The report date is detected from the MProfit workbook. Broker formats are standardized, overlapping scheme feeds are resolved, and only one provider feed per MProfit fund enters the exposure calculation.</div>
      {error && <div className="mx-6 mb-4 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2.5 text-xs text-rose-700">{error}</div>}
      <div className="flex flex-wrap items-center justify-between gap-3 border-t border-slate-200 px-6 py-4"><div className="text-xs text-slate-500">{status || 'Ready'}{filesReady ? ` · ${brokers.length + 2} files selected` : ' · Select the mapping, holdings and at least one broker file'}</div><div className="flex gap-2"><Button variant="quiet" disabled={busy} onClick={onClose}>Cancel</Button><Button disabled={!filesReady || busy} onClick={() => onGenerate({ mappingFile: mapping, holdingsFile: holdings, brokerFiles: brokers })}>{busy ? <><Loader2 size={15} className="animate-spin"/>Processing</> : <><UploadCloud size={15}/>Generate report</>}</Button></div></div>
    </div>
  </div>;
}

function ReviewModal({ issue, suggestions, onClose, onApprove, onOpenSheet4, hasSheet4Row, busy, canRerun }) {
  const [target, setTarget] = useState('');
  const [scheme, setScheme] = useState('');
  useEffect(() => { setTarget(''); setScheme(''); }, [issue]);
  if (!issue) return null;
  const isFund = /fund not linked|fund not matched/i.test(issue.issue_type || '');
  const options = (suggestions || []).filter((item) => item.portfolio_fund === issue.fund).sort((a, b) => b.confidence - a.confidence);
  const selected = options.find((item) => item.broker_scheme === scheme) || options[0];
  return <div className="fixed inset-0 z-[90] flex items-center justify-center bg-slate-950/40 p-4" onMouseDown={(event) => { if (event.target === event.currentTarget && !busy) onClose(); }}>
    <div className="w-full max-w-xl rounded-2xl bg-white shadow-2xl">
      <div className="flex items-center justify-between border-b border-slate-200 px-5 py-4"><div><Pill tone={issue.severity === 'REVIEW' ? 'amber' : 'blue'}>{issue.severity || 'INFO'}</Pill><h3 className="mt-2 text-base font-bold text-slate-900">{issue.issue_type || 'Review item'}</h3></div><button onClick={onClose} className="rounded-lg p-2 text-slate-400 hover:bg-slate-100"><X size={18}/></button></div>
      <div className="space-y-3 p-5 text-sm">
        {issue.fund && <div><div className="text-[11px] font-semibold uppercase text-slate-400">Fund / scheme</div><div className="mt-1 font-semibold text-slate-800">{issue.fund}</div></div>}
        {issue.paper && <div><div className="text-[11px] font-semibold uppercase text-slate-400">Paper</div><div className="mt-1 font-semibold text-slate-800">{issue.paper}</div></div>}
        {issue.issuer && <div><div className="text-[11px] font-semibold uppercase text-slate-400">Issuer</div><div className="mt-1 text-slate-700">{issue.issuer}</div></div>}
        {issue.exposure != null && <div><div className="text-[11px] font-semibold uppercase text-slate-400">Exposure affected</div><div className="mt-1 font-semibold text-slate-800">{cr(issue.exposure)}</div></div>}
        {issue.source && <div><div className="text-[11px] font-semibold uppercase text-slate-400">Source</div><div className="mt-1 text-xs text-slate-600">{issue.source}</div></div>}
        <div className="rounded-lg bg-slate-50 p-3 text-xs leading-5 text-slate-700">{issue.reason || issue.action || 'Review the source row and confirm the correct treatment.'}</div>
        {isFund && options.length > 0 && <div className="space-y-2"><label className="text-xs font-bold text-slate-700">Choose the broker scheme that matches this fund</label><select value={scheme || options[0].broker_scheme} onChange={(event) => setScheme(event.target.value)} className="w-full rounded-lg border border-slate-200 px-3 py-2.5 text-sm">{options.map((item) => <option key={item.broker_scheme} value={item.broker_scheme}>{item.broker_scheme} · {Math.round(item.confidence * 100)}% match</option>)}</select><p className="text-[11px] text-slate-500">Saving this creates a reusable fund alias.</p></div>}
        {!isFund && /bond not assigned|group conflict/i.test(issue.issue_type || '') && <div className="space-y-2"><label className="text-xs font-bold text-slate-700">Correct parent group</label><input value={target} onChange={(event) => setTarget(event.target.value)} placeholder="Enter approved group name" className="w-full rounded-lg border border-slate-200 px-3 py-2.5 text-sm"/><p className="text-[11px] text-slate-500">This issuer-to-group mapping will be saved and used in subsequent quarterly runs.</p></div>}
        {issue.action && <p className="text-xs text-slate-500">Suggested action: {issue.action}</p>}
        {!hasSheet4Row && <p className="rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs text-slate-500">No matching Sheet4 paper row was found. This may be a fund-level or workbook-level issue.</p>}
      </div>
      <div className="flex flex-wrap justify-between gap-2 border-t border-slate-200 px-5 py-4"><div className="flex flex-wrap gap-2"><Button variant="quiet" onClick={onClose}>Close</Button>{hasSheet4Row && <Button variant="quiet" onClick={onOpenSheet4}>Open matching row in Sheet4</Button>}</div>{isFund && options.length > 0 ? <Button disabled={busy || !selected} onClick={() => onApprove({ mappingType: 'fund_alias', sourceKey: selected.broker_scheme, targetKey: issue.fund })}>{busy ? 'Saving…' : 'Save fund mapping'}</Button> : /bond not assigned|group conflict/i.test(issue.issue_type || '') ? <Button disabled={busy || !target.trim()} onClick={() => onApprove({ mappingType: 'issuer_group', sourceKey: issue.issuer || issue.paper, targetKey: target.trim() })}>{busy ? 'Saving…' : 'Approve group mapping'}</Button> : null}</div>
      {canRerun && <div className="px-5 pb-4 text-[11px] text-emerald-700">Approved mappings can be applied by regenerating with the same previous-quarter, MProfit and broker workbooks.</div>}
    </div>
  </div>;
}

function GroupEditModal({ row, onClose, onSave, busy }) {
  const [group, setGroup] = useState('');
  useEffect(() => { setGroup(''); }, [row]);
  if (!row) return null;
  const currentGroup = row.parentGroup || row._group || row.group || '';
  return <div className="fixed inset-0 z-[95] flex items-center justify-center bg-slate-950/40 p-4" onMouseDown={(event) => { if (event.target === event.currentTarget && !busy) onClose(); }}>
    <div className="w-full max-w-lg rounded-2xl bg-white shadow-2xl">
      <div className="flex items-center justify-between border-b border-slate-200 px-5 py-4"><div><h3 className="text-base font-bold text-slate-900">Change borrower group</h3><p className="mt-1 text-xs text-slate-500">This correction will be saved and applied to the quarter.</p></div><button type="button" disabled={busy} onClick={onClose} className="rounded-lg p-2 text-slate-400 hover:bg-slate-100"><X size={18}/></button></div>
      <div className="space-y-3 p-5 text-sm">
        <div><div className="text-[11px] font-semibold uppercase text-slate-400">Paper</div><div className="mt-1 font-semibold text-slate-800">{row.paper || '—'}</div></div>
        <div><div className="text-[11px] font-semibold uppercase text-slate-400">Issuer</div><div className="mt-1 text-slate-700">{row.issuer || '—'}</div></div>
        {row.isin && <div><div className="text-[11px] font-semibold uppercase text-slate-400">ISIN</div><div className="mt-1 text-slate-700">{row.isin}</div></div>}
        <div><div className="text-[11px] font-semibold uppercase text-slate-400">Current group</div><div className="mt-1 text-slate-700">{currentGroup || 'Unmapped / Review'}</div></div>
        <label className="block"><span className="text-xs font-bold text-slate-700">Correct parent group</span><input autoFocus value={group} onChange={(event) => setGroup(event.target.value)} placeholder="Enter the approved group name" className="mt-1.5 w-full rounded-lg border border-slate-200 px-3 py-2.5 text-sm outline-none focus:border-blue-400"/></label>
        <p className="text-[11px] leading-5 text-slate-500">{row.isin ? 'The mapping will be attached to this ISIN only.' : 'No ISIN is available, so the mapping will be attached to this issuer name.'} The report will be recalculated after saving.</p>
      </div>
      <div className="flex justify-end gap-2 border-t border-slate-200 px-5 py-4"><Button variant="quiet" disabled={busy} onClick={onClose}>Cancel</Button><Button disabled={busy || !group.trim()} onClick={() => onSave({ row, group: group.trim() })}>{busy ? 'Saving and recalculating…' : 'Save and recalculate'}</Button></div>
    </div>
  </div>;
}

function BaseEditModal({ open, rows, onClose, onSave, busy, error }) {
  const [draft, setDraft] = useState([]);
  const [validation, setValidation] = useState('');
  useEffect(() => {
    if (open) {
      setDraft((rows || []).map((row) => ({ ...row })));
      setValidation('');
    }
  }, [open, rows]);
  if (!open) return null;
  const updateCell = (rowIndex, key, value) => {
    setDraft((current) => current.map((row, index) => index === rowIndex ? { ...row, [key]: value } : row));
  };
  const save = async () => {
    for (let index = 0; index < draft.length; index += 1) {
      const row = draft[index];
      if (!String(row.fund || '').trim()) { setValidation(`Row ${index + 1}: Fund Name is required.`); return; }
      if (!String(row.paper || '').trim()) { setValidation(`Row ${index + 1}: Paper Name is required.`); return; }
      for (const key of ['marketValue', 'holding', 'exposure']) {
        if (row[key] === '' && key === 'marketValue') continue;
        if (row[key] === '' || !Number.isFinite(Number(row[key]))) { setValidation(`Row ${index + 1}: ${key === 'marketValue' ? 'Paper Amount' : key === 'holding' ? 'Holding' : 'Exposure'} must be numeric.`); return; }
      }
      if (Number(row.holding) < -100.5 || Number(row.holding) > 100.5) { setValidation(`Row ${index + 1}: Holding must be between -100.5% and 100.5%.`); return; }
      for (const key of ['maturity', 'fundMaturity']) {
        if (row[key] && !/^\d{4}-\d{2}-\d{2}$/.test(String(row[key]))) { setValidation(`Row ${index + 1}: ${key === 'maturity' ? 'Paper Maturity' : 'Fund Maturity'} must be a valid date.`); return; }
      }
    }
    setValidation('');
    await onSave(draft);
  };
  const fieldType = (key) => ['maturity', 'fundMaturity'].includes(key) ? 'date' : ['marketValue', 'holding', 'exposure'].includes(key) ? 'number' : 'text';
  return <div className="fixed inset-0 z-[130] flex items-center justify-center bg-slate-950/60 p-2 md:p-4" onMouseDown={(event) => { if (event.target === event.currentTarget && !busy) onClose(); }}>
    <div className="flex h-[94vh] w-[98vw] max-w-[1900px] flex-col overflow-hidden rounded-2xl bg-white shadow-2xl">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 px-5 py-4">
        <div><h2 className="text-lg font-bold text-slate-900">Edit BASE</h2><p className="mt-1 text-xs text-slate-500">Excel-style cell editing · {draft.length.toLocaleString('en-IN')} rows · changes refresh the report summaries and saved workbook</p></div>
        <button type="button" disabled={busy} onClick={onClose} className="rounded-lg p-2 text-slate-400 hover:bg-slate-100"><X size={18}/></button>
      </div>
      {(validation || error) && <div role="alert" className="mx-5 mt-3 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">{validation || error}</div>}
      <div className="min-h-0 flex-1 overflow-auto">
        <table className="w-max min-w-full border-separate border-spacing-0 text-xs">
          <thead className="sticky top-0 z-20 bg-slate-100"><tr>{BASE_COLUMNS.map((column) => <th key={column.key} className="whitespace-nowrap border-b border-r border-slate-200 px-2.5 py-3 text-left font-bold text-slate-700">{column.label}</th>)}</tr></thead>
          <tbody>{draft.map((row, rowIndex) => <tr key={row._rowIndex ?? rowIndex} className={rowIndex % 2 ? 'bg-slate-50/60' : 'bg-white'}>
            {BASE_COLUMNS.map((column) => <td key={column.key} className="border-b border-r border-slate-100 p-1">
              <input aria-label={`${column.label}, row ${rowIndex + 1}`} type={fieldType(column.key)} step={column.key === 'holding' ? '0.0001' : 'any'} value={row[column.key] ?? ''} onChange={(event) => updateCell(rowIndex, column.key, event.target.value)} disabled={busy} className={`w-[150px] rounded-md border border-slate-200 bg-white px-2 py-1.5 text-xs text-slate-800 outline-none focus:border-blue-400 focus:ring-2 focus:ring-blue-100 ${['marketValue', 'holding', 'exposure'].includes(column.key) ? 'text-right tabular-nums' : ''} ${['fund', 'paper', 'groupExposure'].includes(column.key) ? 'w-[220px]' : ''}`}/>
            </td>)}
          </tr>)}</tbody>
        </table>
        {!draft.length && <div className="p-10 text-center text-sm text-slate-400">There are no BASE rows to edit.</div>}
      </div>
      <div className="flex flex-wrap items-center justify-between gap-3 border-t border-slate-200 bg-slate-50 px-5 py-3">
        <span className="text-xs text-slate-500">All 11 BASE columns are editable. Numeric and date cells are validated before saving.</span>
        <div className="flex gap-2"><Button variant="quiet" disabled={busy} onClick={onClose}>Cancel</Button><Button disabled={busy || !draft.length} onClick={() => void save()}>{busy ? <><Loader2 size={14} className="animate-spin"/>Saving BASE…</> : <><Check size={14}/>Save changes</>}</Button></div>
      </div>
    </div>
  </div>;
}

function parentMembershipStatus(row) {
  if (row?.membershipStatus) return String(row.membershipStatus);
  const opening = Number(row?.opening || 0);
  const closing = Number(row?.closing || 0);
  if (opening <= 1e-12 && closing > 1e-12) return 'Added';
  if (opening > 1e-12 && closing <= 1e-12) return 'Exited';
  return 'Continuing';
}

function parentTrendStatus(row) {
  if (row?.trend) return String(row.trend);
  if (row?.status && !['Added', 'Exited', 'Continuing'].includes(String(row.status))) return String(row.status);
  const change = Number(row?.change || 0);
  return change > 1e-9 ? 'Increased' : (change < -1e-9 ? 'Reduced' : 'Unchanged');
}

function Overview({ data, onSelectGroup, onSelectRating }) {
  const kpis = data?.kpis || {};
  const groups = (data?.topGroups || []).filter((row) => !row.special).sort((a, b) => Number(b.exposure) - Number(a.exposure));
  const categories = [...(data?.categories || [])].sort((a, b) => Number(b.exposure) - Number(a.exposure));
  const groupSummary = data?.quarterlySummary?.parentGroups || {};
  const parentChanges = groupSummary.rows?.length ? groupSummary.rows : (data?.groupChanges || []);
  const addedParentGroups = parentChanges.filter((row) => parentMembershipStatus(row) === 'Added').sort((a, b) => Number(b.closing) - Number(a.closing));
  const exitedParentGroups = parentChanges.filter((row) => parentMembershipStatus(row) === 'Exited').sort((a, b) => Number(b.opening) - Number(a.opening));
  const reducedParentGroups = parentChanges.filter((row) => parentMembershipStatus(row) === 'Continuing' && parentTrendStatus(row) === 'Reduced').sort((a, b) => Math.abs(Number(b.change)) - Math.abs(Number(a.change)));
  const changedParentGroups = parentChanges.filter((row) => Math.abs(Number(row.change) || 0) > 0.000001).sort((a, b) => Math.abs(Number(b.change)) - Math.abs(Number(a.change)));
  const ratingData = useMemo(() => {
    const totals = new Map();
    (data?.detailRows || []).forEach((row) => {
      const rawRating = String(row.reported_rating || row.rating || '').trim();
      const category = String(row.category || '').trim();
      const filterKey = rawRating ? 'rating' : 'category';
      const filterValue = rawRating || category;
      const name = filterValue || 'Unrated / Special';
      const key = `${filterKey}::${filterValue}`;
      const current = totals.get(key) || { name, value: 0, filterKey, filterValue };
      current.value += Number(row.exposure || 0);
      totals.set(key, current);
    });
    return [...totals.values()].sort((a, b) => b.value - a.value);
  }, [data]);
  const topData = groups.slice(0, 12).map((row) => ({ name: row.group, exposure: Number(row.exposure) || 0 }));
  const biggest = groups[0];
  const groupExposureRows = groups.map((row) => ({
    group: row.group, exposure: row.exposure, percentage: row.percentage,
    paperCount: row.paperCount, fundCount: row.fundCount,
    classification: row.category || '—', rating: row.rating || '—',
  }));
  return <div className="space-y-5">
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <KPI label="Total portfolio" value={cr(kpis.totalInvestment ?? kpis.totalExposure, 2)} sub="Family debt portfolio value" icon={BarChart3}/>
      <KPI label="Invested in debt" value={cr(kpis.debtExposure, 2)} sub="All debt exposure excluding cash" icon={FileSpreadsheet} tone="green"/>
      <KPI label="Invested in cash" value={cr(kpis.cashExposure, 2)} sub="Cash & cash equivalent" icon={FileSpreadsheet} tone="violet"/>
      <KPI label="Biggest group" value={biggest?.group || '—'} sub={biggest ? cr(biggest.exposure, 2) : 'No mapped exposure'} icon={BarChart3} tone="amber"/>
      <KPI label="Total funds" value={fmt(kpis.totalFunds, 0)} sub="Distinct portfolio funds" icon={FileSpreadsheet}/>
      <KPI label="Total groups" value={fmt(kpis.totalParentGroups ?? kpis.totalGroups, 0)} sub="Distinct parent concentration groups" icon={ShieldAlert} tone="violet"/>
      <KPI label="Total papers" value={fmt(kpis.totalPapers, 0)} sub="Distinct securities / ISINs" icon={FileSpreadsheet} tone="green"/>
    </div>

    <div className="grid gap-4 xl:grid-cols-2">
      <Card className="p-4"><SectionTitle title="Parent-group exposure" hint="Top consolidated borrower groups · select a bar to open matching BASE rows"/><div className="mt-3 h-[340px]">{topData.length ? <ResponsiveContainer width="100%" height="100%"><BarChart data={topData} layout="vertical" margin={{ left: 24, right: 24, top: 5, bottom: 5 }}><CartesianGrid strokeDasharray="3 3" horizontal={false}/><XAxis type="number" tickFormatter={(value) => fmt(value, 0)} fontSize={10}/><YAxis type="category" dataKey="name" width={145} tick={{ fontSize: 10 }}/><Tooltip formatter={(value) => cr(value, 2)}/><Bar dataKey="exposure" fill="#235b8a" radius={[0, 4, 4, 0]} cursor="pointer" onClick={(entry) => { const name = entry?.name || entry?.payload?.name; if (name) onSelectGroup(name); }}/></BarChart></ResponsiveContainer> : <div className="grid h-full place-items-center text-sm text-slate-400">No group data</div>}</div></Card>
      <Card className="p-4"><SectionTitle title="Credit rating-wise exposure" hint="Select a rating slice to open matching securities in BASE"/><div className="mt-3 h-[340px]">{ratingData.length ? <ResponsiveContainer width="100%" height="100%"><PieChart><Pie data={ratingData} dataKey="value" nameKey="name" innerRadius={65} outerRadius={112} paddingAngle={2} cursor="pointer" onClick={(entry) => { const value = entry?.payload || entry; if (value?.filterKey) onSelectRating(value.filterKey, value.filterValue); }}>{ratingData.map((entry, index) => <Cell key={`${entry.filterKey}-${entry.name}`} fill={COLORS[index % COLORS.length]} cursor="pointer"/>)}</Pie><Tooltip formatter={(value) => cr(value, 2)}/><Legend verticalAlign="bottom" height={56} wrapperStyle={{ fontSize: 11 }}/></PieChart></ResponsiveContainer> : <div className="grid h-full place-items-center text-sm text-slate-400">No rating data</div>}</div></Card>
    </div>

    <SpreadsheetTable title="Group-wise Exposure" hint="All consolidated borrower groups, portfolio share, fund and paper counts, and classification" columns={GROUP_EXPOSURE_COLUMNS} rows={groupExposureRows} initialSort={{ key: 'exposure', dir: 'desc' }} empty="No parent-group exposure rows are available."/>

    <Card className="p-4"><SectionTitle title="Credit Rating-wise Exposure" hint="Full-width rating/category exposure and portfolio share"/><div className="mt-3 max-h-[min(48vh,560px)] overflow-auto"><table className="w-full table-fixed text-left text-[11px] sm:text-xs"><thead className="sticky top-0 z-10 bg-slate-100 text-slate-600"><tr><th className="w-[38%] px-3 py-2 font-bold">Rating / category</th><th className="w-[22%] px-3 py-2 text-right font-bold">Exposure (Cr)</th><th className="w-[20%] px-3 py-2 text-right font-bold">Portfolio share</th><th className="w-[20%] px-3 py-2 text-right font-bold">Limit</th></tr></thead><tbody>{categories.map((row) => <tr key={row.category} className="border-b border-slate-100"><td className="truncate px-3 py-2 font-semibold text-slate-700" title={row.category}>{row.category}</td><td className="px-3 py-2 text-right tabular-nums">{fmt(row.exposure, 3)}</td><td className="px-3 py-2 text-right tabular-nums">{pct(row.percentage)}</td><td className="px-3 py-2 text-right tabular-nums">{row.limit == null ? '—' : `${fmt(row.limit * 100, 0)}%`}</td></tr>)}</tbody></table>{!categories.length && <div className="p-8 text-center text-xs text-slate-400">No rating categories available.</div>}</div></Card>

    {(addedParentGroups.length || exitedParentGroups.length || reducedParentGroups.length) ? <Card className="overflow-hidden"><div className="border-b border-slate-200 p-4"><SectionTitle title="Added, Exited & Reduced Groups" hint="Only group movement categories with actual changes are displayed"/></div>
      <div className={`grid gap-3 p-4 ${[addedParentGroups.length, exitedParentGroups.length, reducedParentGroups.length].filter(Boolean).length === 1 ? 'sm:grid-cols-1' : [addedParentGroups.length, exitedParentGroups.length, reducedParentGroups.length].filter(Boolean).length === 2 ? 'sm:grid-cols-2' : 'sm:grid-cols-3'}`}>
        {addedParentGroups.length > 0 && <div className="rounded-lg bg-emerald-50 p-3"><div className="flex items-center justify-between"><span className="text-xs font-bold text-emerald-800">Added</span><Pill tone="green">{addedParentGroups.length}</Pill></div><div className="mt-2 max-h-48 space-y-2 overflow-auto">{addedParentGroups.length ? addedParentGroups.map((row) => <div key={row.key || row.group} className="flex items-start justify-between gap-2 text-xs"><span className="font-semibold text-emerald-950">{row.group}</span><span className="shrink-0 font-bold text-emerald-800">+{fmt(row.closing, 3)}</span></div>) : <p className="text-xs text-emerald-800/70">None</p>}</div><div className="mt-2 border-t border-emerald-200 pt-2 text-[11px] font-bold text-emerald-900">Total +{fmt(groupSummary.addedExposure ?? addedParentGroups.reduce((sum, row) => sum + Number(row.closing || 0), 0), 3)} Cr</div></div>}
        {exitedParentGroups.length > 0 && <div className="rounded-lg bg-rose-50 p-3"><div className="flex items-center justify-between"><span className="text-xs font-bold text-rose-800">Exited</span><Pill tone="red">{exitedParentGroups.length}</Pill></div><div className="mt-2 max-h-48 space-y-2 overflow-auto">{exitedParentGroups.length ? exitedParentGroups.map((row) => <div key={row.key || row.group} className="flex items-start justify-between gap-2 text-xs"><span className="font-semibold text-rose-950">{row.group}</span><span className="shrink-0 font-bold text-rose-800">−{fmt(row.opening, 3)}</span></div>) : <p className="text-xs text-rose-800/70">None</p>}</div><div className="mt-2 border-t border-rose-200 pt-2 text-[11px] font-bold text-rose-900">Total −{fmt(groupSummary.exitedExposure ?? exitedParentGroups.reduce((sum, row) => sum + Number(row.opening || 0), 0), 3)} Cr</div></div>}
        {reducedParentGroups.length > 0 && <div className="rounded-lg bg-amber-50 p-3"><div className="flex items-center justify-between"><span className="text-xs font-bold text-amber-800">Reduced</span><Pill tone="amber">{reducedParentGroups.length}</Pill></div><div className="mt-2 max-h-48 space-y-2 overflow-auto">{reducedParentGroups.length ? reducedParentGroups.map((row) => <div key={row.key || row.group} className="flex items-start justify-between gap-2 text-xs"><span className="font-semibold text-amber-950">{row.group}</span><span className="shrink-0 font-bold text-rose-700">−{fmt(Math.abs(Number(row.change || 0)), 3)}</span></div>) : <p className="text-xs text-amber-800/70">None</p>}</div><div className="mt-2 border-t border-amber-200 pt-2 text-[11px] font-bold text-amber-900">Total −{fmt(groupSummary.grossReduction ?? reducedParentGroups.reduce((sum, row) => sum + Math.abs(Number(row.change || 0)), 0), 3)} Cr</div></div>}
      </div>
      <div className="flex flex-wrap gap-2 border-t border-slate-200 bg-slate-50 px-4 py-3 text-[11px] text-slate-600"><Pill>{fmt(groupSummary.currentCount ?? groups.length, 0)} active borrower groups in movement comparison</Pill><Pill>{fmt(groupSummary.continuingCount, 0)} continuing</Pill><Pill tone="green">{fmt(groupSummary.increasedCount, 0)} increased</Pill><Pill tone="amber">{fmt(groupSummary.reducedCount ?? reducedParentGroups.length, 0)} reduced</Pill></div>
    </Card> : null}

    <Card className="p-4"><SectionTitle title="Parent-group holding changes" hint="Opening exposure, closing exposure, absolute change and movement status"/><div className="mt-3 max-h-[430px] overflow-auto"><table className="w-full table-fixed text-left text-[11px] sm:text-xs"><thead className="sticky top-0 z-10 bg-slate-50 text-slate-500"><tr><th className="w-[30%] px-3 py-2.5">Parent group</th><th className="w-[17%] px-3 py-2.5 text-right">Opening (Cr)</th><th className="w-[17%] px-3 py-2.5 text-right">Closing (Cr)</th><th className="w-[18%] px-3 py-2.5 text-right">Change (Cr)</th><th className="w-[18%] px-3 py-2.5">Status</th></tr></thead><tbody>{changedParentGroups.map((row) => { const change = Number(row.change) || 0; const positive = change >= 0; return <tr key={row.key || row.group} className="border-b border-slate-100"><td className="truncate px-3 py-2.5 font-semibold text-slate-800" title={row.group}>{row.group}</td><td className="px-3 py-2.5 text-right tabular-nums">{fmt(row.opening, 3)}</td><td className="px-3 py-2.5 text-right tabular-nums">{fmt(row.closing, 3)}</td><td className={`px-3 py-2.5 text-right font-bold tabular-nums ${positive ? 'text-emerald-700' : 'text-rose-700'}`}>{positive ? '+' : '−'}{fmt(Math.abs(change), 3)}</td><td className="px-3 py-2.5"><Pill tone={parentMembershipStatus(row) === 'Added' ? 'green' : parentMembershipStatus(row) === 'Exited' ? 'red' : parentTrendStatus(row) === 'Reduced' ? 'red' : 'green'}>{parentMembershipStatus(row) === 'Continuing' ? parentTrendStatus(row) : parentMembershipStatus(row)}</Pill></td></tr>; })}</tbody></table>{!changedParentGroups.length && <p className="py-6 text-center text-xs text-slate-400">No change in parent-group holdings.</p>}</div></Card>


  </div>;
}

function normalizeReviewText(value) {
  return String(value || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
}

function findReviewSheet4Row(issue, rows) {
  if (!issue || !rows?.length) return null;
  const paper = normalizeReviewText(issue.paper);
  const issuer = normalizeReviewText(issue.issuer);
  const fund = normalizeReviewText(issue.fund);
  if (!paper && !issuer && !fund) return null;
  let candidates = rows;

  if (paper && !['unallocated broker balance', ''].includes(paper)) {
    candidates = candidates.filter((row) => {
      const candidate = normalizeReviewText(row.paper);
      return candidate === paper || candidate.includes(paper) || paper.includes(candidate);
    });
    if (!candidates.length) return null;
  }
  if (issuer) {
    const issuerMatches = candidates.filter((row) => {
      const candidate = normalizeReviewText(row.issuer || row.group);
      return candidate === issuer || candidate.includes(issuer) || issuer.includes(candidate);
    });
    if (issuerMatches.length) candidates = issuerMatches;
    else if (!paper) return null;
  }
  if (fund) {
    const fundMatches = candidates.filter((row) => normalizeReviewText(row.fund) === fund);
    if (fundMatches.length) candidates = fundMatches;
    else if (!paper && !issuer) return null;
  }
  if (!candidates.length) return null;
  return candidates[0];
}

export default function DebtTracker() {
  const [data, setData] = useState(null);
  const [jobs, setJobs] = useState([]);
  const [jobId, setJobId] = useState('');
  const [tab, setTab] = useState('Overview');
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [reviewIssue, setReviewIssue] = useState(null);
  const [groupEditRow, setGroupEditRow] = useState(null);
  const [sheet4HighlightKey, setSheet4HighlightKey] = useState('');
  const [mappingBusy, setMappingBusy] = useState(false);
  const [error, setError] = useState('');
  const [status, setStatus] = useState('Ready');
  const [currentFiles, setCurrentFiles] = useState(null);
  const [focusGroup, setFocusGroup] = useState('');
  const [baseFilterRequest, setBaseFilterRequest] = useState({ id: 0, filters: {} });
  const [baseParentGroupFilter, setBaseParentGroupFilter] = useState('');
  const [showHistory, setShowHistory] = useState(false);
  const [baseEditOpen, setBaseEditOpen] = useState(false);
  const [reconciliationBroker, setReconciliationBroker] = useState('All Brokers');

  const loadJobs = useCallback(async () => {
    try {
      const result = await api.debtTrackerJobs();
      const list = result?.jobs || [];
      setJobs(list);
      const latest = list.find((item) => item.status === 'complete');
      if (latest) {
        const report = await api.debtTrackerJobData(latest.id);
        setData(report); setJobId(latest.id);
      }
    } catch (e) { setError(getError(e)); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void loadJobs(); }, [loadJobs]);

  const download = async (sheet = '') => {
    if (!jobId) return;
    try {
      const blob = sheet ? await api.downloadDebtTrackerSheet(jobId, sheet) : await api.downloadDebtTracker(jobId);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a'); anchor.href = url; anchor.download = sheet ? `${sheet}-Debt-Exposure.xlsx` : 'Debt-Exposure-Quarterly-Report.xlsx';
      document.body.appendChild(anchor); anchor.click(); anchor.remove(); URL.revokeObjectURL(url);
    } catch (e) { setError(getError(e)); }
  };

  const waitForJob = async (id) => {
    const start = Date.now();
    while (Date.now() - start < 20 * 60 * 1000) {
      const job = await api.debtTrackerJob(id);
      if (job.status === 'complete') return api.debtTrackerJobData(id);
      if (job.status === 'failed') throw new Error(job.error || 'The report could not be generated.');
      setStatus('Calculating exposure…');
      await new Promise((resolve) => setTimeout(resolve, 1500));
    }
    throw new Error('The report is still processing. Open History to check the run.');
  };

  const generate = async (files = currentFiles) => {
    if (!files?.mappingFile || !files?.holdingsFile || !files?.brokerFiles?.length) {
      setError('Upload the previous-quarter report, MProfit holdings and one or more broker workbooks.'); return;
    }
    setError(''); setBusy(true); setStatus('Uploading and validating workbooks…');
    try {
      const created = await api.generateDebtTracker({
        mappingFile: files.mappingFile, holdingsFile: files.holdingsFile,
        brokerFiles: files.brokerFiles, reportDate: '', baselineDate: '',
      });
      const id = created?.job?.id;
      if (!id) throw new Error('The processing job was not created.');
      setJobId(id); setStatus('Calculating paper and group exposure…');
      const report = await waitForJob(id);
      setData(report); setTab('Overview'); setUploadOpen(false); setReviewIssue(null); setSheet4HighlightKey('');
      setCurrentFiles(files); setStatus('Quarterly report generated');
      const list = await api.debtTrackerJobs(); setJobs(list?.jobs || []);
    } catch (e) { setError(getError(e)); setStatus('Generation failed'); }
    finally { setBusy(false); }
  };

  const saveMapping = async ({ mappingType, sourceKey, targetKey }) => {
    setMappingBusy(true); setError('');
    try {
      await api.saveDebtTrackerMapping({ mappingType, sourceKey, targetKey });
      setReviewIssue(null);
      if (currentFiles) {
        setStatus('Mapping saved · recalculating this quarter…');
        await generate(currentFiles);
      } else {
        setStatus('Mapping saved for the next quarterly run');
      }
    } catch (e) { setError(getError(e)); }
    finally { setMappingBusy(false); }
  };

  const saveGroupEdit = async ({ row, group }) => {
    setMappingBusy(true); setError('');
    try {
      const validIsin = /^[A-Z]{2}[A-Z0-9]{9}[0-9]$/i.test(String(row.isin || '').trim());
      const mappingType = validIsin ? 'security_group' : 'issuer_group';
      const sourceKey = validIsin ? row.isin : (row.issuer || row.paper);
      if (!sourceKey) throw new Error('This row has no issuer or ISIN to attach a mapping to.');
      await api.saveDebtTrackerMapping({ mappingType, sourceKey, targetKey: group });
      setGroupEditRow(null);
      if (currentFiles) await generate(currentFiles);
      else setStatus('Group mapping saved for the next quarterly run');
    } catch (e) { setError(getError(e)); }
    finally { setMappingBusy(false); }
  };

  const saveBaseEdits = async (editedRows) => {
    if (!jobId || !data?.detailRows) { setError('Open a saved report before editing BASE.'); return false; }
    setMappingBusy(true); setError(''); setStatus('Validating and saving BASE edits…');
    try {
      const payload = editedRows.map((row, index) => ({
        ...data.detailRows[index],
        fund: data.detailRows[index].fund || row.fund, source_scheme: String(row.fund || '').trim(),
        paper: String(row.paper || '').trim(), issuer: String(row.groupExposure || '').trim(),
        maturity: row.maturity || null, fund_maturity: row.fundMaturity || null,
        market_value_crore: row.marketValue === '' ? null : Number(row.marketValue),
        allocation: Number(row.holding) / 100, broker: String(row.broker || '').trim(),
        asset_type: String(row.instrument || '').trim(), exposure: Number(row.exposure),
        reported_rating: String(row.category || '').trim(), rating: String(row.category || '').trim(),
        category: String(row.category || '').trim() || 'Others',
      }));
      const updated = await api.saveDebtTrackerBase(jobId, payload);
      setData(updated); setBaseEditOpen(false); setBaseParentGroupFilter('');
      setBaseFilterRequest((current) => ({ id: current.id + 1, filters: {} }));
      setStatus('BASE updated · workbook and dependent summaries refreshed');
      return true;
    } catch (e) { setError(getError(e)); setStatus('BASE save failed'); return false; }
    finally { setMappingBusy(false); }
  };

  const openJob = async (id) => {
    setBusy(true); setError(''); setStatus('Loading report…');
    try { const report = await api.debtTrackerJobData(id); setData(report); setJobId(id); setTab('Overview'); setSheet4HighlightKey(''); setShowHistory(false); setStatus('Saved report opened'); }
    catch (e) { setError(getError(e)); }
    finally { setBusy(false); }
  };

  const detailRows = data?.detailRows || [];
  const reconciliationRows = data?.brokerReconciliation || [];
  const reconciliationSummary = data?.brokerReconciliationSummary || data?.dataQuality?.providerSourceSummary || {};
  const providerDiagnostics = reconciliationSummary.providerFileDiagnostics || [];
  const historicalFallbackFunds = reconciliationSummary.fundsWithoutBrokerFeed || [];
  const reconciliationBrokers = useMemo(() => {
    const fromRows = reconciliationRows.flatMap((row) => [row.brokerA, row.brokerB]);
    const fromFiles = providerDiagnostics.map((item) => item.broker);
    return [...new Set([...fromRows, ...fromFiles].filter((name) => name && !['historical base', 'mprofit', 'broker source'].includes(String(name).trim().toLowerCase())))].sort((a, b) => String(a).localeCompare(String(b)));
  }, [reconciliationRows, providerDiagnostics]);
  const visibleReconciliationRows = useMemo(() => {
    const filtered = reconciliationBroker === 'All Brokers' ? reconciliationRows : reconciliationRows.filter((row) => row.brokerA === reconciliationBroker || row.brokerB === reconciliationBroker);
    return filtered.slice().sort((a, b) => Number(b.differenceCr || 0) - Number(a.differenceCr || 0));
  }, [reconciliationRows, reconciliationBroker]);
  const baseRows = useMemo(() => detailRows.map((row, index) => ({ ...rowFromDetail(row), _rowIndex: index })), [detailRows]);
  const visibleBaseRows = useMemo(() => baseParentGroupFilter ? baseRows.filter((row) => String(row.parentGroup || '').toLowerCase() === baseParentGroupFilter.toLowerCase()) : baseRows, [baseRows, baseParentGroupFilter]);
  const sheet4Rows = useMemo(() => buildSheet4Rows(detailRows), [detailRows]);
  const sheet8Rows = useMemo(() => (data?.sheet8Preview || []).slice(1).filter((row) => row[0] && String(row[0]).toLowerCase() !== 'grand total').map((row) => ({ group: row[0], exposure: row[1] ?? 0, percentage: Number(row[2] || 0) * (Number(row[2] || 0) > 1 ? 0.01 : 1), rating: row[3] || '', paperCount: row[4] ?? 0, fundCount: row[5] ?? 0, added: row[6] || '', exited: row[7] || '' })), [data]);
  const categoryRows = useMemo(() => (data?.sheet8LimitPreview || []).slice(1).filter((row) => row[0] && String(row[0]).toLowerCase() !== 'grand total').map((row) => ({ category: row[0], exposure: Number(row[1] || 0), percentage: Number(row[2] || 0) * (Number(row[2] || 0) > 1 ? 0.01 : 1), limit: row[3] == null ? null : Number(row[3]), status: row[4] || '' })), [data]);
  const reviewSheet4Target = useMemo(() => findReviewSheet4Row(reviewIssue, sheet4Rows), [reviewIssue, sheet4Rows]);
  const reviewRows = data?.reviewIssues || [];
  const metadata = data?.metadata || {};

  const openBaseFilter = (columnKey, value) => {
    if (value == null) return;
    setFocusGroup('');
    setBaseParentGroupFilter('');
    setBaseFilterRequest((current) => ({
      id: current.id + 1,
      filters: { [columnKey]: [String(value)] },
    }));
    setTab('BASE');
  };
  const openGroup = (group) => { setBaseParentGroupFilter(group); setBaseFilterRequest((current) => ({ id: current.id + 1, filters: {} })); setTab('BASE'); };
  const openRating = (filterKey, value) => openBaseFilter(filterKey, value);
  const clearBaseChartFilter = () => { setBaseParentGroupFilter(''); setBaseFilterRequest((current) => ({ id: current.id + 1, filters: {} })); };
  const visibleSheet4 = useMemo(() => {
    if (!focusGroup) return sheet4Rows;
    const key = focusGroup.toLowerCase();
    return sheet4Rows.filter((row) => String(row.parentGroup || '').toLowerCase() === key || String(row.groupExposure || '').toLowerCase() === key);
  }, [sheet4Rows, focusGroup]);

  const tabNames = ['Overview', 'BASE', 'Sheet4', 'Sheet8', 'Broker Reconciliation', 'Review'];
  if (loading) return <div className="flex min-h-[50vh] items-center justify-center gap-2 text-sm text-slate-500"><Loader2 size={18} className="animate-spin"/>Loading Debt Tracker…</div>;

  return <div className="mx-auto max-w-[1700px] space-y-5 px-3 pb-10 md:px-6">
    <div className="flex flex-wrap items-end justify-between gap-3">
      <div><div className="text-[11px] font-bold uppercase tracking-[.16em] text-[#235b8a]">Quarterly portfolio report</div><h1 className="mt-1 text-2xl font-bold tracking-tight text-slate-900">Debt Exposure</h1><p className="mt-1 text-sm text-slate-500">Fund-level holdings, underlying papers, borrower groups and quarterly changes.</p></div>
      <div className="flex flex-wrap gap-2"><Button variant="quiet" onClick={() => setShowHistory((v) => !v)}><Clock3 size={15}/>History</Button><Button onClick={() => { setError(''); setUploadOpen(true); }}><Plus size={15}/>Upload / New run</Button>{data && <Button variant="quiet" onClick={() => void download()}><Download size={15}/>Download report</Button>}</div>
    </div>
    {error && <div className="flex items-start gap-2 rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700"><AlertCircle size={16} className="mt-0.5 shrink-0"/><span>{error}</span><button className="ml-auto" onClick={() => setError('')}><X size={15}/></button></div>}
    {showHistory && <Card className="p-4"><SectionTitle title="Previous runs" hint="Open a saved quarterly report"/><div className="mt-3 divide-y divide-slate-100">{jobs.filter((item) => item.status === 'complete').map((item) => <div key={item.id} className="flex flex-wrap items-center justify-between gap-3 py-2.5"><div><div className="text-sm font-semibold text-slate-800">{dateLabel(item.completed_at || item.created_at)} <span className="ml-2 text-xs font-normal text-slate-400">{item.input_files?.holdings || ''}</span></div></div><Button variant="quiet" onClick={() => void openJob(item.id)} disabled={busy}>Open report</Button></div>)}{!jobs.length && <p className="py-4 text-sm text-slate-400">No saved reports.</p>}</div></Card>}
    {!data ? <Card className="flex min-h-[300px] flex-col items-center justify-center p-8 text-center"><div className="grid h-14 w-14 place-items-center rounded-2xl bg-blue-50 text-blue-700"><FileSpreadsheet size={26}/></div><h2 className="mt-4 text-lg font-bold text-slate-900">Start a quarterly exposure report</h2><p className="mt-2 max-w-md text-sm leading-6 text-slate-500">Upload the previous-quarter report, MProfit holdings and one or more broker workbooks. The system will build BASE, Sheet4 and Sheet8 and compare the current quarter with the previous BASE.</p><Button className="mt-5" onClick={() => setUploadOpen(true)}><UploadCloud size={16}/>Upload Excel files</Button></Card> : <>
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-slate-200 bg-white px-4 py-2.5 text-xs"><div className="flex flex-wrap items-center gap-2"><Pill tone={reviewRows.some((item) => item.severity === 'REVIEW') ? 'amber' : 'green'}>{reviewRows.some((item) => item.severity === 'REVIEW') ? 'Review required' : 'Calculated'}</Pill><span className="text-slate-600">As of <b className="text-slate-800">{dateLabel(metadata.reportDate)}</b></span><span className="text-slate-400">Previous quarter: {dateLabel(metadata.baselineDate)}</span></div><div className="text-slate-500">{fmt(kpiValue(data, 'totalExposure'), 2)} Cr total exposure</div></div>
      <div className="flex gap-1 overflow-x-auto border-b border-slate-200">{tabNames.map((name) => <button key={name} onClick={() => { setTab(name); if (name !== 'Sheet4') setFocusGroup(''); }} className={`whitespace-nowrap border-b-2 px-4 py-3 text-sm font-semibold transition ${tab === name ? 'border-[#235b8a] text-[#163b66]' : 'border-transparent text-slate-500 hover:text-slate-800'}`}>{name}{name === 'Review' && reviewRows.filter((item) => item.severity === 'REVIEW').length > 0 ? <span className="ml-2 rounded-full bg-amber-100 px-2 py-0.5 text-[10px] text-amber-800">{reviewRows.filter((item) => item.severity === 'REVIEW').length}</span> : null}</button>)}</div>
      {tab === 'Overview' && <Overview data={data} onSelectGroup={openGroup} onSelectRating={openRating}/>}
      {tab === 'BASE' && <div className="space-y-3">
        {(Object.keys(baseFilterRequest.filters).length > 0 || baseParentGroupFilter) && <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-blue-100 bg-blue-50 px-3 py-2 text-xs text-blue-800"><span>Overview selection: <b>{baseParentGroupFilter ? 'Parent Group' : (baseFilterRequest.filters.rating ? 'Rating' : (baseFilterRequest.filters.category ? 'Category' : 'Filter'))} = {baseParentGroupFilter || (Object.values(baseFilterRequest.filters)[0] || []).map((value) => value === '' ? '(Blanks)' : value).join(', ')}</b></span><button type="button" onClick={clearBaseChartFilter} className="font-bold underline">Clear chart selection</button></div>}
        <SpreadsheetTable title="BASE" hint="Unified 11-column BASE · one selected source feed per MProfit fund · monetary values shown in whole crore" columns={BASE_COLUMNS} rows={visibleBaseRows} initialSort={{ key: 'groupExposure', dir: 'asc' }} onEditGroup={setGroupEditRow} editGroupKey="groupExposure" initialFilters={baseFilterRequest.filters} filterRequestId={baseFilterRequest.id} downloadAction={<><Button variant="quiet" onClick={() => { setError(''); setBaseEditOpen(true); }} disabled={!detailRows.length || busy || mappingBusy}><Pencil size={14}/>Edit BASE</Button><Button variant="quiet" onClick={() => void download('BASE')}><Download size={14}/>BASE Excel</Button></>} />
      </div>}
      {tab === 'Sheet4' && <div className="space-y-3">{focusGroup && <div className="flex items-center justify-between rounded-lg border border-blue-100 bg-blue-50 px-3 py-2 text-xs text-blue-800">Showing group: <b>{focusGroup}</b><button onClick={() => setFocusGroup('')} className="font-bold underline">Clear selection</button></div>}<SpreadsheetTable title="Sheet4" hint="One row per paper × fund · cumulative paper exposure includes all distinct funds holding that paper" columns={SHEET4_COLUMNS} rows={visibleSheet4} initialSort={{ key: 'groupExposure', dir: 'asc' }} highlightRowKey={sheet4HighlightKey} downloadAction={<Button variant="quiet" onClick={() => void download('Sheet4')}><Download size={14}/>Sheet4 Excel</Button>}/></div>}
      {tab === 'Sheet8' && <div className="space-y-4">
        <SpreadsheetTable title="Sheet8 · Parent-group exposure" hint="Consolidated parent groups · filter and sort the promoter summary" columns={SHEET8_COLUMNS} rows={sheet8Rows} initialSort={{ key: 'exposure', dir: 'desc' }} downloadAction={<Button variant="quiet" onClick={() => void download('Sheet8')}><Download size={14}/>Sheet8 Excel</Button>}/>
        <SpreadsheetTable title="Category limits" hint="Credit / risk category exposure and permitted portfolio limits" columns={CATEGORY_COLUMNS} rows={categoryRows} initialSort={{ key: 'exposure', dir: 'desc' }}/>
      </div>}
      {tab === 'Broker Reconciliation' && <div className="space-y-3">
        <Card className="p-4"><div className="flex flex-wrap items-end justify-between gap-3"><div className="min-w-0 flex-1"><SectionTitle title="Broker-to-broker reconciliation" hint="Only the same security within the same fund is compared. ISIN takes priority; normalized paper + issuer is used only when ISIN is unavailable."/><p className="mt-2 text-xs leading-5 text-slate-500">Duplicate rows are aggregated per broker. Each pair uses one common basis: provider client Amount where both sides publish it, otherwise provider market value where both publish it, otherwise MProfit fund value × allocation. MProfit is not compared as a broker. Differences of ₹{fmt(reconciliationSummary.differenceToleranceCr ?? 2, 0)} Cr or less are ignored; larger differences appear first.</p></div><div className="min-w-[190px]"><label htmlFor="debt-reconciliation-broker" className="mb-1 block text-[11px] font-semibold text-slate-500">Broker filter</label><select id="debt-reconciliation-broker" value={reconciliationBroker} onChange={(event) => setReconciliationBroker(event.target.value)} className="w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-semibold text-slate-700"><option value="All Brokers">All Brokers</option>{reconciliationBrokers.map((broker) => <option key={broker} value={broker}>{broker}</option>)}</select></div></div><div className="mt-3 flex flex-wrap gap-2"><Pill tone="blue">{reconciliationBrokers.length} brokers detected</Pill><Pill tone="amber">{visibleReconciliationRows.filter((row) => row.status === 'Mismatch').length} material differences</Pill><Pill>{visibleReconciliationRows.length} comparisons shown</Pill><Pill>{fmt(reconciliationSummary.withinTolerancePairCount || 0, 0)} within tolerance ignored</Pill></div>
          {providerDiagnostics.length > 0 && <div className="mt-4 overflow-x-auto rounded-lg border border-slate-200"><div className="border-b border-slate-200 bg-slate-50 px-3 py-2 text-[11px] font-bold uppercase tracking-wide text-slate-600">Uploaded broker file diagnostics</div><table className="w-full min-w-[760px] text-left text-[11px]"><thead className="bg-white text-slate-500"><tr><th className="px-3 py-2">Broker / File</th><th className="px-3 py-2 text-right">Parsed rows</th><th className="px-3 py-2 text-right">Matched funds</th><th className="px-3 py-2 text-right">Security rows</th><th className="px-3 py-2 text-right">Issuer rows</th><th className="px-3 py-2">Diagnosis</th></tr></thead><tbody>{providerDiagnostics.map((item) => <tr key={`${item.file}-${item.broker}`} className="border-t border-slate-100"><td className="max-w-[260px] px-3 py-2"><div className="font-semibold text-slate-800">{item.broker || 'Unknown broker'}</div><div className="truncate text-slate-500" title={item.file}>{item.file}</div></td><td className="px-3 py-2 text-right tabular-nums">{fmt(item.parsedPositionCount, 0)}</td><td className="px-3 py-2 text-right tabular-nums">{fmt(item.matchedFundCount, 0)} / {fmt(item.parsedSchemeCount, 0)}</td><td className="px-3 py-2 text-right tabular-nums">{fmt(item.securityPositionCount, 0)}</td><td className="px-3 py-2 text-right tabular-nums">{fmt(item.issuerPositionCount, 0)}</td><td className="max-w-[320px] px-3 py-2 text-slate-600">{item.note}</td></tr>)}</tbody></table></div>}
          {historicalFallbackFunds.length > 0 && <p className="mt-3 text-[11px] leading-5 text-amber-800">Historical BASE is used only where no valid broker feed matched. {historicalFallbackFunds.length} MProfit fund(s) currently use this fallback: {historicalFallbackFunds.slice(0, 8).join('; ')}{historicalFallbackFunds.length > 8 ? `; +${historicalFallbackFunds.length - 8} more` : ''}.</p>}
          {(reconciliationSummary.issuerLevelRowsNotComparable || 0) > 0 && <p className="mt-2 text-[11px] leading-5 text-slate-500">{fmt(reconciliationSummary.issuerLevelRowsNotComparable, 0)} issuer-level rows (including ICICI matrix-style data) cannot be reconciled as individual securities because they do not contain ISIN/security-level details.</p>}
        </Card>
        <SpreadsheetTable title="Broker Reconciliation" hint="Only material same-fund, same-security broker differences above ₹2 Cr · largest difference first" columns={RECONCILIATION_COLUMNS} rows={visibleReconciliationRows} initialSort={{ key: 'differenceCr', dir: 'desc' }} rowClassName={(row) => row.status === 'Mismatch' ? 'bg-rose-50/80' : ''} downloadAction={<Button variant="quiet" onClick={() => void download('Broker Reconciliation')}><Download size={14}/>Reconciliation Excel</Button>} empty="No same-security broker differences above ₹2 Cr were found for this filter. Issuer-only files are shown in the diagnostics above but are not compared as individual securities."/>
      </div>}
      {tab === 'Review' && <Card className="overflow-hidden"><div className="border-b border-slate-200 p-4"><SectionTitle title="Review queue" hint="Open an item to inspect source evidence and save an approved mapping" action={<Pill tone={reviewRows.some((item) => item.severity === 'REVIEW') ? 'amber' : 'green'}>{reviewRows.filter((item) => item.severity === 'REVIEW').length} decisions needed</Pill>}/><p className="mt-2 text-xs leading-5 text-slate-500">Approvals are saved for future calculations. After approving mappings, regenerate this quarter with the same previous-quarter, MProfit and broker workbooks to apply them.</p></div><div className="divide-y divide-slate-100">{reviewRows.map((issue, index) => <button key={`${issue.issue_type}-${issue.fund}-${issue.paper}-${index}`} onClick={() => setReviewIssue(issue)} className="flex w-full flex-wrap items-center justify-between gap-3 px-4 py-3 text-left transition hover:bg-slate-50"><div className="min-w-0"><div className="flex flex-wrap items-center gap-2"><Pill tone={issue.severity === 'REVIEW' ? 'amber' : 'blue'}>{issue.severity || 'INFO'}</Pill><span className="text-sm font-semibold text-slate-800">{issue.issue_type}</span></div><div className="mt-1 truncate text-xs text-slate-500">{issue.fund || issue.issuer || issue.paper || issue.source || 'Source review'}{issue.paper ? ` · ${issue.paper}` : ''}</div></div><div className="flex shrink-0 items-center gap-3"><span className="text-xs font-semibold text-slate-600">{issue.exposure == null ? '' : cr(issue.exposure)}</span><ChevronDown size={15} className="text-slate-400"/></div></button>)}{!reviewRows.length && <div className="flex flex-col items-center p-10 text-center"><CheckCircle2 size={28} className="text-emerald-600"/><h3 className="mt-3 text-sm font-bold text-slate-800">No review items</h3><p className="mt-1 text-xs text-slate-500">All parsed funds and securities have been classified.</p></div>}</div>{currentFiles && reviewRows.length > 0 && <div className="flex flex-wrap items-center justify-between gap-3 border-t border-slate-200 bg-slate-50 px-4 py-3"><span className="text-xs text-slate-600">Approved mappings are stored in your account.</span><Button disabled={busy} onClick={() => void generate(currentFiles)}><Check size={15}/>Regenerate with approved mappings</Button></div>}</Card>}
      {tab === 'Review' && <div className="space-y-4">
        {(data?.aiMappingReview?.items || []).length > 0 && <Card className="overflow-hidden"><div className="border-b border-slate-200 p-4"><SectionTitle title="AI-assisted identity research" hint="Groq suggestions are advisory only; verify the evidence before approving" action={<Pill tone="amber">Not approved</Pill>}/><p className="mt-2 text-xs text-slate-500">AI never changes an issuer or fund mapping automatically. Approving a suggestion saves it as an explicit rule and recalculates the report.</p></div><div className="divide-y divide-slate-100">{(data.aiMappingReview.items || []).map((item, index) => <div key={`${item.kind}-${item.sourceName}-${index}`} className="flex flex-wrap items-start justify-between gap-3 p-4"><div className="min-w-0 flex-1"><div className="flex flex-wrap items-center gap-2"><Pill tone="blue">{item.kind || 'issuer'}</Pill><b className="text-sm text-slate-800">{item.sourceName}</b></div><p className="mt-1 text-xs text-slate-600">Suggested identity: {item.suggestedIdentity || '—'} · Parent group: {item.suggestedParentGroup || '—'}</p><p className="mt-1 text-xs leading-5 text-slate-500">{item.rationale || 'No rationale supplied.'}</p><p className="mt-1 text-[11px] text-amber-700">Evidence to verify: {item.evidenceNeeded || 'Confirm against issuer filings / scheme identifiers.'}</p></div><Button disabled={mappingBusy || busy || !(item.kind === 'issuer' ? item.suggestedParentGroup : item.suggestedIdentity)} onClick={() => void saveMapping({mappingType: item.kind === 'fund' ? 'fund_alias' : 'issuer_group', sourceKey: item.kind === 'fund' ? item.suggestedIdentity : item.sourceName, targetKey: item.kind === 'fund' ? item.sourceName : item.suggestedParentGroup})}><Check size={14}/>Approve mapping</Button></div>)}</div></Card>}
        {(data?.dataQuality?.stateSdlDecisionRequired || []).length > 0 && <Card className="p-4"><SectionTitle title="State development loan decisions" hint="Broker records these as Government of India; confirm whether to keep them under GOI or identify the borrowing state." action={<Pill tone="amber">Decision required</Pill>}/><p className="mt-2 text-xs leading-5 text-slate-600">These papers currently remain under the broker-reported Government of India issuer and are not automatically reassigned from the paper description.</p><div className="mt-3 flex flex-wrap gap-2">{data.dataQuality.stateSdlDecisionRequired.map((name) => <span key={name} className="rounded-md bg-slate-100 px-2 py-1 text-xs text-slate-700">{name} SDL</span>)}</div></Card>}
      </div>}
    </>}
    {busy && <div className="text-[11px] text-slate-400">{status}</div>}
    <UploadModal open={uploadOpen} onClose={() => !busy && setUploadOpen(false)} onGenerate={(files) => { setCurrentFiles(files); void generate(files); }} busy={busy} status={status} error={error}/>
    <ReviewModal issue={reviewIssue} suggestions={data?.mappingSuggestions || []} onClose={() => setReviewIssue(null)} onApprove={saveMapping} onOpenSheet4={() => { if (reviewSheet4Target) { setSheet4HighlightKey(spreadsheetRowKey(reviewSheet4Target)); setFocusGroup(''); setTab('Sheet4'); setReviewIssue(null); } }} hasSheet4Row={Boolean(reviewSheet4Target)} busy={mappingBusy} canRerun={Boolean(currentFiles)}/>
    <GroupEditModal row={groupEditRow} onClose={() => setGroupEditRow(null)} onSave={saveGroupEdit} busy={mappingBusy || busy}/>
    <BaseEditModal open={baseEditOpen} rows={baseRows} onClose={() => !mappingBusy && setBaseEditOpen(false)} onSave={saveBaseEdits} busy={mappingBusy || busy} error={error}/>
  </div>;
}

function kpiValue(data, key) { return Number(data?.kpis?.[key] || 0); }
