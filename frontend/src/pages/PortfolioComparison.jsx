import React, { useEffect, useMemo, useState } from 'react';
import {
  Archive,
  Clock3,
  Download,
  FileText,
  History,
  RefreshCw,
  UploadCloud,
} from 'lucide-react';
import { api } from '../lib/api';
import { Badge, Card, Empty, ErrorBox, Loading, PageHeader } from '../components/UI';

const monthNow = () => {
  const date = new Date();
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}`;
};

const DEFAULT_PMS_OPTIONS = [
  'NDPMS',
  '2Point2 Capital',
  'Sageone CORE',
  '360 One Pipe Fund',
  'Sageone Small',
  'Molecule',
  'Noveshaay',
  '91 Capital',
  'Persistence',
  'Xponent',
  'Bastion',
  'EMkay',
];

const pmsOptionKey = (value) => {
  const key = String(value || '').toLowerCase().replace(/[^a-z0-9]/g, '');
  const aliases = {
    '2point2capital': '2p2',
    '360onepipefund': '360onepipefund',
    '360onepipe': '360onepipefund',
    '360pipe': '360onepipefund',
    'sageonesmall': 'sageonesmallcap',
    'noveshaay': 'niveshaay',
  };
  return aliases[key] || key;
};

const mergePmsOptions = (registry) => {
  const options = [...DEFAULT_PMS_OPTIONS];
  const knownKeys = new Set(options.map(pmsOptionKey));
  (registry || []).forEach((item) => {
    const name = String(item?.pms_name || '').trim();
    const key = pmsOptionKey(name);
    if (name && key && !knownKeys.has(key)) {
      options.push(name);
      knownKeys.add(key);
    }
  });
  return options;
};

const number = (value, maximumFractionDigits = 2) => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
  return new Intl.NumberFormat('en-IN', { maximumFractionDigits }).format(Number(value));
};

const pct = (value) => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
  return `${Math.round(Number(value))}%`;
};

const formatDuration = (seconds) => {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  return [hours, minutes, secs].map((v) => String(v).padStart(2, '0')).join(':');
};

const elapsedFrom = (startAt, endAt = null) => {
  if (!startAt) return null;
  const start = new Date(startAt).getTime();
  if (!Number.isFinite(start)) return null;
  const end = endAt ? new Date(endAt).getTime() : Date.now();
  if (!Number.isFinite(end)) return null;
  return Math.max(0, Math.floor((end - start) / 1000));
};

const toneMap = {
  NEW: 'green',
  BOUGHT: 'green',
  SOLD: 'red',
  EXITED: 'red',
  HELD: 'slate',
  MEASUREMENT_CHANGED: 'amber',
  ACTIVE: 'green',
  INACTIVE: 'slate',
  ERROR: 'red',
  queued: 'blue',
  processing: 'blue',
  completed: 'green',
  completed_with_errors: 'amber',
  failed: 'red',
  processed: 'green',
  failed_file: 'red',
  NOT_RECEIVED: 'amber',
};

const tone = (status) => toneMap[status] || 'slate';


function normalizeReportPayload(payload) {
  let candidate = payload;
  const seen = new Set();

  for (let depth = 0; depth < 6 && candidate && typeof candidate === 'object' && !seen.has(candidate); depth += 1) {
    seen.add(candidate);
    if (Array.isArray(candidate.sheets)) {
      return {
        ...candidate,
        summary: candidate.summary || {},
        sheets: candidate.sheets.map((sheet, index) => ({
          ...sheet,
          name: sheet.name || sheet.title || `Sheet ${index + 1}`,
          type: sheet.type || (String(sheet.name || '').toLowerCase().includes('summary') ? 'summary' : 'table'),
          columns: Array.isArray(sheet.columns) ? sheet.columns : [],
          rows: Array.isArray(sheet.rows) ? sheet.rows : [],
        })),
      };
    }

    candidate = candidate.data || candidate.report || candidate.result || candidate.payload || null;
  }

  return null;
}

function StatusBadge({ status }) {
  return <Badge tone={tone(status)}>{status}</Badge>;
}

function FloatingJobTimer({ job, tick }) {
  void tick;
  if (!job || !['queued', 'processing'].includes(job.status)) return null;

  const elapsed = elapsedFrom(job.created_at)
    ?? (job.total_elapsed_seconds ?? job.processing_elapsed_seconds ?? 0);
  const queued = job.status === 'queued';

  return (
    <div
      role="status"
      aria-live="polite"
      aria-label={`${queued ? 'Portfolio processing queued' : 'Portfolio processing in progress'}, elapsed ${formatDuration(elapsed)}`}
      className="fixed bottom-4 right-4 z-[100] flex items-center gap-2 rounded-full border border-slate-200 bg-white/95 px-3 py-2 text-xs font-semibold text-slate-700 shadow-lg backdrop-blur"
    >
      {queued ? (
        <Clock3 size={14} className="shrink-0 text-amber-600" />
      ) : (
        <span className="inline-flex h-3.5 w-3.5 shrink-0 items-center justify-center">
          <RefreshCw size={13} className="animate-spin text-emerald-600" />
        </span>
      )}
      <span>{queued ? 'Queued' : 'Processing'}</span>
      <span className="tabular-nums text-navy-900">{formatDuration(elapsed)}</span>
    </div>
  );
}

function SummarySheet({ sheet, summary }) {
  const rows = sheet?.rows || [];
  const biggest = summary?.biggest_change_pms;
  const least = summary?.least_change_pms;
  const pmsChangeLabel = (item) => item?.pms_name
    ? `${item.pms_name}${item.average_abs_change_pct == null ? '' : ` (${pct(item.average_abs_change_pct)} avg.)`}`
    : 'N/A — no comparable holdings';
  const overview = [
    ['Total PMS', number(summary?.total_pms ?? summary?.active_pms ?? 0, 0)],
    ['New Stocks Entry Count', number(summary?.new_stock_entries ?? summary?.new_holdings ?? 0, 0)],
    ['Old Stocks Removal Count', number(summary?.old_stock_removals ?? summary?.exited_holdings ?? 0, 0)],
    ['Total Holdings', number(summary?.total_holdings ?? summary?.holding_count ?? 0, 0)],
    ['Biggest Change PMS', pmsChangeLabel(biggest)],
    ['Least Change PMS', pmsChangeLabel(least)],
  ];
  return (
    <div className="space-y-4 bg-white p-4">
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {overview.map(([label, value]) => (
          <div key={label} className="rounded-lg border border-slate-200 bg-slate-50 p-3">
            <div className="text-[10px] font-bold uppercase tracking-wide text-slate-500">{label}</div>
            <div className="mt-1 break-words text-sm font-bold text-navy-900">{value}</div>
          </div>
        ))}
      </div>
      <div className="overflow-x-auto">
        <div className="min-w-[1160px] overflow-hidden border border-slate-300">
          <div className="bg-[#245A87] px-4 py-3 text-center text-sm font-bold text-white">
            PMS SCRIP-WISE CHANGES TRACKER
          </div>
          <div className="grid grid-cols-[130px_1fr_90px_1fr_90px_1fr_110px] border-b border-slate-300 text-xs font-bold">
            <div className="border-r border-slate-300 px-3 py-2 text-center">{sheet?.report_date || ''}</div>
            <div className="col-span-2 border-r border-slate-300 bg-slate-100 px-3 py-2 text-center">BOUGHT / ADDED</div>
            <div className="col-span-2 border-r border-slate-300 bg-slate-100 px-3 py-2 text-center">SOLD / REDUCED</div>
            <div className="col-span-2 bg-slate-100 px-3 py-2 text-center">BASELINE HOLDINGS</div>
            <div className="bg-[#245A87] px-3 py-2 text-center text-white">PMS</div>
            <div className="bg-[#245A87] px-3 py-2 text-center text-white">SCRIP</div>
            <div className="bg-[#245A87] px-3 py-2 text-center text-white">% Change</div>
            <div className="bg-[#245A87] px-3 py-2 text-center text-white">SCRIP</div>
            <div className="bg-[#245A87] px-3 py-2 text-center text-white">% Change</div>
            <div className="bg-[#245A87] px-3 py-2 text-center text-white">SCRIP</div>
            <div className="bg-[#245A87] px-3 py-2 text-center text-white">HOLDING</div>
          </div>
          {rows.map((row, index) => {
            if (row.blank) return <div key={`blank-${index}`} className="h-4 border-b border-slate-200 bg-white" />;
            if (row.section) {
              return (
                <div key={`section-${index}`} className="grid grid-cols-[130px_1fr_90px_1fr_90px_1fr_110px] border-b border-slate-300 bg-white font-semibold">
                  <div className="px-3 py-2">{row.pms}{row.is_excluded_pms ? ' — NOT RECEIVED' : ''}</div>
                  <div className="col-span-6 px-3 py-2" />
                </div>
              );
            }
            const baselineValue = row.baseline_value == null
              ? ''
              : row.baseline_measurement_type === 'PERCENTAGE'
                ? `${number(row.baseline_value, 2)}%`
                : number(row.baseline_value, 0);
            return (
              <div key={`row-${index}`} className="grid grid-cols-[130px_1fr_90px_1fr_90px_1fr_110px] border-b border-slate-200 text-sm">
                <div className="px-3 py-2" />
                <div className="px-3 py-2">{row.bought_security || ''}</div>
                <div className={`px-3 py-2 text-right ${row.bought_security ? 'bg-[#C6EFCE] text-[#006100]' : ''}`}>{row.bought_security ? pct(row.bought_pct) : ''}</div>
                <div className="px-3 py-2">{row.sold_security || ''}</div>
                <div className={`px-3 py-2 text-right ${row.sold_security ? 'bg-[#FFC7CE] text-[#9C0006]' : ''}`}>{row.sold_security ? pct(row.sold_pct) : ''}</div>
                <div className="px-3 py-2">{row.baseline_security || ''}</div>
                <div className={`px-3 py-2 text-right ${row.baseline_security ? 'bg-[#DDEBF7] text-[#245A87]' : ''}`}>{baselineValue}</div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function HistoryValue({ value, row, label }) {
  if (value === null || value === undefined) return '—';
  const metric = row.value_measurements?.[label] || row.measurement_type;
  return metric === 'PERCENTAGE' ? `${number(value, 2)}%` : number(value, 0);
}

function DiffValue({ row }) {
  if (row.diff === null || row.diff === undefined) return '—';
  if (row.measurement_type === 'PERCENTAGE') {
    return `${number(row.diff, 2)}%`;
  }
  return number(row.diff, 0);
}

function HistoryTable({ sheet, title }) {
  const columns = sheet?.columns || [];
  const monthColumns = columns.slice(1, -2);
  const rows = sheet?.rows || [];
  const latestMonth = sheet?.report_month;

  return (
    <div className="max-h-[72vh] overflow-auto">
      <div className="min-w-max">
        {title ? <div className="sticky left-0 border-b border-slate-200 bg-white px-4 py-2 text-xs font-semibold text-slate-600">{title}</div> : null}
        <table className="w-full border-collapse text-sm">
          <thead className="sticky top-0 z-20 bg-white text-slate-900">
            <tr>
              {columns.map((column, index) => (
                <th
                  key={column}
                  className={`border border-slate-300 bg-white px-3 py-2 text-center text-xs font-bold ${index === 0 ? 'sticky left-0 z-30 min-w-[260px] text-left' : 'min-w-[105px]'}`}
                >
                  {column}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, index) => (
              <tr key={row.security_name || index} className="hover:bg-slate-50">
                <td className="sticky left-0 z-10 border border-slate-200 bg-white px-3 py-2 font-medium">{row.security_name}</td>
                {monthColumns.map((month) => (
                  <td key={month} className={`border border-slate-200 px-3 py-2 text-right tabular-nums ${month === latestMonth ? 'bg-[#FFF200]' : ''}`}>
                    <HistoryValue value={row.values?.[month]} row={row} label={month} />
                  </td>
                ))}
                <td className={`border border-slate-200 px-3 py-2 text-right font-semibold tabular-nums ${Number(row.diff) < 0 ? 'text-red-600' : Number(row.diff) > 0 ? 'text-emerald-700' : ''}`}>
                  <DiffValue row={row} />
                </td>
                <td className={`border border-slate-200 px-3 py-2 text-right font-semibold tabular-nums ${Number(row.diff_pct) < 0 ? 'text-red-600' : Number(row.diff_pct) > 0 ? 'text-emerald-700' : ''}`}>
                  {pct(row.diff_pct)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {!rows.length ? <div className="p-5"><Empty title="No holding history" text="No successfully parsed appraisal history is available for this worksheet." /></div> : null}
      </div>
    </div>
  );
}

function AllPmsSheet({ sheet }) {
  return <HistoryTable sheet={sheet} title="Consolidated holdings history across all PMS" />;
}


function RegistrySheet({ sheet }) {
  const columns = sheet?.columns || [];
  const rows = sheet?.rows || [];
  return (
    <div className="overflow-auto">
      <table className="w-full min-w-[900px] border-collapse text-sm">
        <thead className="sticky top-0 bg-[#245A87] text-white">
          <tr>{columns.map((column) => <th key={column} className="border border-slate-300 px-3 py-2 text-left text-xs font-bold">{column}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index} className="hover:bg-slate-50">
              {columns.map((column) => (
                <td key={column} className="border border-slate-200 px-3 py-2">{column === 'Status' ? <StatusBadge status={row[column]} /> : (row[column] ?? '—')}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {!rows.length ? <div className="p-5"><Empty title="No PMS registry entries" text="The registry is empty for this comparison report." /></div> : null}
    </div>
  );
}

function PmsDetailSheet({ sheet }) {
  return <HistoryTable sheet={sheet} title={sheet?.comparison_note || ''} />;
}


const PMS_HOLDINGS_CHANGE_TAB = '__pms_holdings_change__';

function PmsHoldingsChangeView({ sheets }) {
  const pmsSheets = (sheets || []).filter((sheet) => sheet?.type === 'pms');

  const displayChange = (row) => {
    if (row?.diff === null || row?.diff === undefined || Number.isNaN(Number(row.diff))) {
      if (row?.status === 'MEASUREMENT_CHANGED') return 'Not comparable';
      if (row?.status === 'BASELINE') return 'Baseline';
      return '—';
    }
    const value = Number(row.diff);
    if (Math.abs(value) < 1e-12) return 'No change';
    const formatted = row.measurement_type === 'PERCENTAGE'
      ? `${number(Math.abs(value), 2)} pp`
      : number(Math.abs(value), 0);
    return `${value > 0 ? '+' : '−'}${formatted}`;
  };

  if (!pmsSheets.length) {
    return <div className="p-5"><Empty title="No PMS holdings available" text="No individual PMS holding sheets were included in this report." /></div>;
  }

  return (
    <div className="space-y-5 bg-white p-4">
      <p className="text-xs text-slate-500">
        Change is calculated against each PMS's previous available appraisal. Quantity changes are shown as units; allocation changes are shown in percentage points.
      </p>
      {pmsSheets.map((sheet) => {
        const rows = sheet.rows || [];
        return (
          <section key={sheet.name} className="overflow-hidden rounded-xl border border-slate-200">
            <div className="flex flex-wrap items-center justify-between gap-2 bg-slate-50 px-4 py-3">
              <h3 className="text-sm font-bold text-navy-900">{sheet.display_name || sheet.name}</h3>
              <span className="text-[11px] text-slate-500">{rows.length} {rows.length === 1 ? 'stock' : 'stocks'}</span>
            </div>
            {rows.length ? (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[440px] border-collapse text-sm">
                  <thead className="bg-[#245A87] text-white">
                    <tr>
                      <th className="border border-slate-300 px-3 py-2 text-left text-xs font-semibold">Stock Name</th>
                      <th className="border border-slate-300 px-3 py-2 text-right text-xs font-semibold">Holding Change</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((row, index) => {
                      const value = row?.diff;
                      const changed = value !== null && value !== undefined && Number(value) !== 0;
                      const positive = changed && Number(value) > 0;
                      const negative = changed && Number(value) < 0;
                      return (
                        <tr key={`${row.security_name || 'stock'}-${row.isin || index}`} className="hover:bg-slate-50">
                          <td className="border border-slate-200 px-3 py-2 font-medium text-slate-800">{row.security_name || 'Unnamed security'}</td>
                          <td className={`border border-slate-200 px-3 py-2 text-right font-semibold tabular-nums ${positive ? 'text-emerald-700' : negative ? 'text-red-600' : 'text-slate-500'}`}>
                            {displayChange(row)}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className="px-4 py-4 text-sm text-slate-500">
                {String(sheet.comparison_note || '').toLowerCase().includes('excluded')
                  ? 'Marked as not received / excluded for this appraisal.'
                  : 'No stock holdings available for this PMS in the selected report.'}
              </div>
            )}
          </section>
        );
      })}
    </div>
  );
}

function GenericSheet({ sheet }) {
  const columns = sheet?.columns || [];
  const rows = sheet?.rows || [];

  return (
    <div className="max-h-[70vh] overflow-auto">
      <table className="w-full min-w-[700px] border-collapse text-sm">
        <thead className="sticky top-0 bg-[#245A87] text-white">
          <tr>{columns.map((column) => <th key={column} className="border border-slate-300 px-3 py-2 text-left text-xs font-bold">{column}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index} className="hover:bg-slate-50">
              {columns.map((column, columnIndex) => {
                const value = Array.isArray(row) ? row[columnIndex] : row?.[column];
                return <td key={column} className="border border-slate-200 px-3 py-2">{value === null || value === undefined || value === '' ? '—' : String(value)}</td>;
              })}
            </tr>
          ))}
        </tbody>
      </table>
      {!rows.length ? <div className="p-5"><Empty title="This worksheet is empty" text="The worksheet exists in the report but contains no rows." /></div> : null}
    </div>
  );
}

function DataQualitySheet({ sheet }) {
  return (
    <div className="space-y-2 p-5">
      {(sheet?.rows || []).length ? (sheet.rows || []).map((row, index) => (
        <div key={index} className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">{row.Issue}</div>
      )) : <Empty title="No data-quality issues" text="All detected PMS files completed successfully for this run." />}
    </div>
  );
}

function reportSheetsForRole(sheets, role) {
  const normalizedRole = String(role || '').trim().toUpperCase();
  if (normalizedRole === 'ADMIN') return sheets;

  if (normalizedRole === 'ANALYST') {
    // Analysts get the executive Summary plus the aggregated PMS Holdings
    // Change view. Individual PMS sheets are data sources for that view, not
    // tabs that analysts can open directly.
    return sheets.filter((sheet) => sheet?.type === 'summary');
  }

  if (normalizedRole === 'INVESTMENT') {
    // Investment users can inspect the consolidated and individual PMS
    // holdings, but not the registry and internal data-quality worksheets.
    return sheets.filter((sheet) => ['summary', 'all_pms', 'pms'].includes(sheet?.type));
  }

  // Unknown or missing roles receive the least-privileged report view.
  return sheets.filter((sheet) => sheet?.type === 'summary');
}

function ReportViewer({ report, user }) {
  const sheets = report?.sheets || [];
  const role = String(user?.role || '').trim().toUpperCase();
  const canViewPmsHoldings = ['ADMIN', 'ANALYST'].includes(role);
  const visibleSheets = useMemo(() => reportSheetsForRole(sheets, role), [sheets, role]);
  const [activeSheet, setActiveSheet] = useState('Summary');
  const isPmsHoldingsChangeTab = canViewPmsHoldings && activeSheet === PMS_HOLDINGS_CHANGE_TAB;
  const pmsHoldingsSourceSheets = sheets.filter((sheet) => sheet?.type === 'pms');
  const hasAllPmsSheet = visibleSheets.some((sheet) => sheet.type === 'all_pms');
  const insertHoldingsChangeAfter = role === 'ANALYST' || !hasAllPmsSheet ? 'summary' : 'all_pms';

  useEffect(() => {
    if (isPmsHoldingsChangeTab) return;
    if (visibleSheets.length && !visibleSheets.some((sheet) => sheet.name === activeSheet)) {
      const summarySheet = visibleSheets.find((sheet) => sheet.type === 'summary');
      setActiveSheet((summarySheet || visibleSheets[0]).name);
    }
  }, [visibleSheets, activeSheet, isPmsHoldingsChangeTab]);

  const selected = isPmsHoldingsChangeTab
    ? null
    : visibleSheets.find((sheet) => sheet.name === activeSheet) || visibleSheets[0];
  if (!report || (!selected && !isPmsHoldingsChangeTab)) return <Empty title="Report not available" text="The batch is complete, but no report JSON was returned." />;

  return (
    <Card>
      <div className="border-b border-slate-200 px-5 pt-5">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <div className="text-xs font-bold uppercase tracking-wide text-slate-500">Generated report</div>
            <h2 className="mt-1 text-lg font-bold text-navy-900">PMS Report · {report.summary?.report_date || report.summary?.comparison_month}</h2>
            <p className="mt-1 text-xs text-slate-500">{isPmsHoldingsChangeTab ? 'All PMS security-level holding changes for this reporting period.' : (selected?.comparison_note || 'The complete workbook is stored server-side; this viewer renders the saved sheet data in the browser.')}</p>
          </div>
          <Badge tone="green">Saved</Badge>
        </div>
        <div className="mt-5 flex gap-1 overflow-x-auto pb-0">
          {visibleSheets.map((sheet) => (
            <React.Fragment key={sheet.name}>
              <button
                type="button"
                onClick={() => setActiveSheet(sheet.name)}
                className={`whitespace-nowrap rounded-t-xl border border-b-0 px-3.5 py-2.5 text-xs font-semibold ${activeSheet === sheet.name ? 'border-slate-200 bg-white text-navy-900' : 'border-transparent text-slate-500 hover:bg-slate-50'}`}
              >
                {sheet.display_name || sheet.name}
              </button>
              {canViewPmsHoldings && sheet.type === insertHoldingsChangeAfter ? (
                <button
                  type="button"
                  onClick={() => setActiveSheet(PMS_HOLDINGS_CHANGE_TAB)}
                  className={`whitespace-nowrap rounded-t-xl border border-b-0 px-3.5 py-2.5 text-xs font-semibold ${isPmsHoldingsChangeTab ? 'border-slate-200 bg-white text-navy-900' : 'border-transparent text-slate-500 hover:bg-slate-50'}`}
                >
                  PMS Holdings Change
                </button>
              ) : null}
            </React.Fragment>
          ))}
        </div>
      </div>
      {isPmsHoldingsChangeTab ? <PmsHoldingsChangeView sheets={pmsHoldingsSourceSheets} /> : null}
      {selected?.type === 'summary' ? <SummarySheet sheet={selected} summary={report.summary} /> : null}
      {selected?.type === 'all_pms' ? <AllPmsSheet sheet={selected} /> : null}
      {selected?.type === 'registry' ? <RegistrySheet sheet={selected} /> : null}
      {selected?.type === 'pms' ? <PmsDetailSheet sheet={selected} /> : null}
      {selected?.type === 'data_quality' ? <DataQualitySheet sheet={selected} /> : null}
      {selected && !['summary', 'all_pms', 'registry', 'pms', 'data_quality'].includes(selected.type) ? <GenericSheet sheet={selected} /> : null}
    </Card>
  );
}

export default function PortfolioComparison({ user }) {
  const [tab, setTab] = useState('current');
  const [month, setMonth] = useState(monthNow());
  const [file, setFile] = useState(null);
  const [history, setHistory] = useState([]);
  const [job, setJob] = useState(null);
  const [report, setReport] = useState(null);
  const [historyReport, setHistoryReport] = useState(null);
  const [loading, setLoading] = useState(true);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState('');
  const [clockTick, setClockTick] = useState(0);
  const [reportCache, setReportCache] = useState({});
  const [reportLoading, setReportLoading] = useState(false);
  const [reportLoadError, setReportLoadError] = useState('');
  const [reportBatchId, setReportBatchId] = useState('');
  const [fileInputKey, setFileInputKey] = useState(0);
  const [historySelection, setHistorySelection] = useState(null);
  const [pmsOptions, setPmsOptions] = useState(DEFAULT_PMS_OPTIONS);
  const [excludedPms, setExcludedPms] = useState([]);

  const loadReportForBatch = async (batchId, comparisonMonth, excludedPms = []) => {
    if (!batchId) throw new Error('No completed portfolio batch is selected.');
    setReportLoadError('');

    const cached = normalizeReportPayload(reportCache[batchId]);
    if (cached?.sheets?.length) {
      setReport(cached);
      setReportBatchId(batchId);
      return cached;
    }

    setReportLoading(true);
    let reportError = null;
    try {
      let normalized = null;
      try {
        normalized = normalizeReportPayload(await api.portfolioReportJson(batchId));
      } catch (err) {
        reportError = err;
      }

      // Older batches or an unavailable JSON object can still be previewed from
      // the backend's normalized month-comparison endpoint.
      if (!normalized && comparisonMonth) {
        try {
          normalized = normalizeReportPayload(await api.portfolioComparison({ month: comparisonMonth, excludedPms }));
        } catch (err) {
          reportError = err;
        }
      }

      if (!normalized || !normalized.sheets.length) {
        throw new Error(reportError?.message || 'The server did not return worksheet data for this report.');
      }

      setReport(normalized);
      setReportBatchId(batchId);
      setReportCache((previous) => ({ ...previous, [batchId]: normalized }));
      return normalized;
    } catch (err) {
      const message = err?.message || 'Unable to load the Excel preview.';
      setReportLoadError(message);
      throw new Error(message);
    } finally {
      setReportLoading(false);
    }
  };

  const loadAll = async () => {
    // The job list is the critical path. History is refreshed independently so
    // a slow historical query cannot block upload or the processing indicator.
    const jobsData = await api.portfolioJobs({ limit: 30 });
    const nextJobs = jobsData?.jobs || [];
    void api.portfolioRegistry()
      .then((registryData) => setPmsOptions(mergePmsOptions(registryData?.registry || [])))
      .catch(() => setPmsOptions(DEFAULT_PMS_OPTIONS));
    void api.portfolioHistory({ limit: 24 })
      .then((historyData) => setHistory(historyData?.history || []))
      .catch(() => { /* History is non-blocking; retry on manual refresh. */ });

    const live = nextJobs.find((item) => ['queued', 'processing'].includes(item.status));
    if (live) {
      setJob(live);
      return;
    }

    const latestCompleted = nextJobs.find((item) => ['completed', 'completed_with_errors'].includes(item.status) && item.report_available);
    if (latestCompleted) {
      setJob(latestCompleted);
      setMonth(latestCompleted.comparison_month);
      try {
        await loadReportForBatch(latestCompleted.id, latestCompleted.comparison_month, latestCompleted.excluded_pms || []);
      } catch {
        setReport(null);
        setReportBatchId('');
      }
    }
  };

  useEffect(() => {
    let alive = true;
    loadAll()
      .catch((err) => alive && setError(err.message || 'Unable to load Portfolio Manager.'))
      .finally(() => alive && setLoading(false));
    return () => { alive = false; };
  }, []);

  useEffect(() => {
    if (!job?.id || !['queued', 'processing'].includes(job.status)) return undefined;
    let alive = true;
    let timeout;
    let consecutiveFailures = 0;

    const poll = async () => {
      try {
        const next = await api.portfolioJob(job.id);
        consecutiveFailures = 0;
        if (!alive) return;
        setError('');
        setJob(next);
        if (['completed', 'completed_with_errors'].includes(next.status)) {
          if (next.report_available) {
            try {
              await loadReportForBatch(next.id, next.comparison_month, next.excluded_pms || []);
              if (!alive) return;
              setHistoryReport(null);
              setHistorySelection(null);
              setTab('preview');
            } catch (err) {
              if (alive) setError(err.message || 'The batch completed, but its report preview could not be loaded.');
            }
          }
          try {
            const updatedHistory = await api.portfolioHistory({ limit: 24 });
            if (alive) setHistory(updatedHistory?.history || []);
          } catch {
            // The saved report preview remains usable even if history refresh fails.
          }
          return;
        }
        if (next.status === 'failed') return;
      } catch (err) {
        consecutiveFailures += 1;
        // A single transient DB stall should not look like a failed batch.
        if (alive && consecutiveFailures >= 3) {
          setError(`Processing is continuing, but the status check has failed ${consecutiveFailures} times. ${err.message || ''}`.trim());
        }
      }
      if (alive) timeout = window.setTimeout(poll, 3000);
    };

    timeout = window.setTimeout(poll, 700);
    return () => {
      alive = false;
      window.clearTimeout(timeout);
    };
  }, [job?.id, job?.status]);

  useEffect(() => {
    if (!job?.id || !['queued', 'processing'].includes(job.status)) return undefined;
    const interval = window.setInterval(() => setClockTick((value) => value + 1), 1000);
    return () => window.clearInterval(interval);
  }, [job?.id, job?.status]);

  const onFileChange = (event) => {
    const selected = event.target.files?.[0] || null;
    setError('');
    if (selected && !selected.name.toLowerCase().endsWith('.zip')) {
      setFile(null);
      setError('Select one monthly ZIP file.');
      return;
    }
    setFile(selected);
  };

  const upload = async () => {
    if (!file) {
      setError('Select the Portfolio Appraisal ZIP first.');
      return;
    }
    setUploading(true);
    setError('');
    setReport(null);
    setReportBatchId('');
    setReportLoadError('');
    setHistoryReport(null);
    setHistorySelection(null);
    try {
      const response = await api.portfolioUpload({ comparisonMonth: month, file, excludedPms });
      setJob({
        id: response.batch_id,
        status: response.status || 'queued',
        comparison_month: response.comparison_month || month,
        created_at: new Date().toISOString(),
        report_available: false,
        source_count: 0,
        parsed_rows: 0,
      });
      setFile(null);
      setExcludedPms([]);
      setFileInputKey((value) => value + 1);
      setTab('current');
      // Refresh the history tab in the background; never hold up the live job.
      void api.portfolioHistory({ limit: 24 })
        .then((historyData) => setHistory(historyData?.history || []))
        .catch(() => {});
    } catch (err) {
      setError(err.message || 'Portfolio ZIP upload failed.');
    } finally {
      setUploading(false);
    }
  };

  const refresh = async () => {
    setError('');
    try {
      await loadAll();
      if (job?.id && reportCache[job.id]) setReport(reportCache[job.id]);
    } catch (err) {
      setError(err.message || 'Unable to refresh Portfolio Manager.');
    }
  };

  const openCurrentPreview = async () => {
    setTab('preview');
    setError('');
    if (normalizeReportPayload(report)?.sheets?.length && reportBatchId === job?.id) return;
    if (job?.id && ['completed', 'completed_with_errors'].includes(job.status) && job.report_available) {
      try {
        await loadReportForBatch(job.id, job.comparison_month, job.excluded_pms || []);
      } catch (err) {
        setError(err.message || 'Unable to load the current report preview.');
      }
    }
  };

  const loadHistoryReport = async (historyItem) => {
    setError('');
    try {
      const saved = await loadReportForBatch(historyItem.id, historyItem.comparison_month, historyItem.excluded_pms || []);
      setHistoryReport(saved);
      setHistorySelection(historyItem);
      setMonth(historyItem.comparison_month);
      setTab('history');
    } catch (err) {
      setError(err.message || 'Unable to load the saved historical report.');
    }
  };

  const downloadReport = async (batchId, comparisonMonth) => {
    try {
      const blob = await api.downloadPortfolioReport(batchId);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = `PMS_Report_${comparisonMonth}.xlsx`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      setError(err.message || 'Report download failed.');
    }
  };

  const currentSummary = report?.summary || {};
  const beginAnotherUpload = () => {
    setFile(null);
    setExcludedPms([]);
    setFileInputKey((value) => value + 1);
    setError('');
    setTab('current');
  };
  if (loading) return <Loading />;

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Portfolio Analytics"
        title="Adaptive PMS Portfolio Manager"
        description="Upload one ZIP for each appraisal period. The system identifies each PMS report (PDF or Excel), preserves its full holdings history, and compares the latest report with the previous available report for that PMS."
        action={
          <button type="button" onClick={refresh} className="inline-flex items-center gap-2 rounded-xl border border-slate-200 bg-white px-3.5 py-2.5 text-sm font-semibold text-slate-700 shadow-sm hover:bg-slate-50">
            <RefreshCw size={16} /> Refresh
          </button>
        }
      />

      {error ? <ErrorBox error={error} /> : null}

      <div className="flex items-center gap-1 rounded-xl border border-slate-200 bg-slate-50 p-1">
        <button type="button" onClick={() => setTab('current')} className={`inline-flex items-center gap-2 rounded-lg px-4 py-2.5 text-sm font-semibold ${tab === 'current' ? 'bg-white text-navy-900 shadow-sm' : 'text-slate-500 hover:text-slate-700'}`}>
          <FileText size={16} /> Current Appraisal
        </button>
        <button type="button" onClick={openCurrentPreview} className={`inline-flex items-center gap-2 rounded-lg px-4 py-2.5 text-sm font-semibold ${tab === 'preview' ? 'bg-white text-navy-900 shadow-sm' : 'text-slate-500 hover:text-slate-700'}`}>
          <FileText size={16} /> Excel Preview
        </button>
        <button type="button" onClick={() => setTab('history')} className={`inline-flex items-center gap-2 rounded-lg px-4 py-2.5 text-sm font-semibold ${tab === 'history' ? 'bg-white text-navy-900 shadow-sm' : 'text-slate-500 hover:text-slate-700'}`}>
          <History size={16} /> Report History
        </button>
      </div>

      {tab === 'current' ? (
        <>
          <Card>
            <div className="border-b border-slate-200 px-5 py-4">
              <div className="flex items-center gap-2">
                <UploadCloud size={18} className="text-navy-700" />
                <div>
                  <h2 className="font-bold text-navy-900">Upload monthly Portfolio ZIP</h2>
                  <p className="mt-1 text-xs text-slate-500">No fixed naming format is required. PMS identity is inferred from the file name, ZIP folder name or report title. Each PMS report can be PDF or Excel; use one report per PMS and include its name somewhere in the file name or report where possible.</p>
                </div>
              </div>
            </div>
            <div className="grid gap-4 px-5 py-5 md:grid-cols-[180px_1fr_auto] md:items-end">
              <label className="block">
                <span className="mb-2 block text-xs font-semibold uppercase tracking-wide text-slate-500">Appraisal / reporting month</span>
                <input type="month" value={month} onChange={(event) => setMonth(event.target.value)} className="w-full rounded-xl border border-slate-200 bg-white px-3 py-2.5 text-sm outline-none focus:border-navy-500" />
              </label>
              <label className="block">
                <span className="mb-2 block text-xs font-semibold uppercase tracking-wide text-slate-500">Portfolio Appraisal ZIP</span>
                <input key={fileInputKey} type="file" accept=".zip" onChange={onFileChange} className="block w-full rounded-xl border border-dashed border-slate-300 bg-slate-50 px-3 py-2.5 text-sm" />
              </label>
              <button type="button" onClick={upload} disabled={uploading} className="inline-flex items-center justify-center gap-2 rounded-xl bg-navy-700 px-4 py-2.5 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-60">
                <Archive size={16} /> {uploading ? 'Uploading…' : 'Start Processing'}
              </button>
            </div>
            <div className="border-t border-slate-100 px-5 py-4">
              <div className="mb-3">
                <h3 className="text-sm font-bold text-navy-900">PMS appraisal reports not received</h3>
                <p className="mt-1 text-xs text-slate-500">
                  Select any PMS that has not shared its appraisal for this month. Selected PMSs are ignored even when a matching file is present in the ZIP; the workbook will show the fund name with no holdings or buy/sell changes.
                </p>
              </div>
              <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
                {pmsOptions.map((pmsName) => (
                  <label key={pmsOptionKey(pmsName)} className="flex cursor-pointer items-center gap-2 rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-700 hover:bg-slate-50">
                    <input
                      type="checkbox"
                      checked={excludedPms.includes(pmsName)}
                      onChange={(event) => setExcludedPms((current) => (
                        event.target.checked
                          ? [...current, pmsName]
                          : current.filter((item) => item !== pmsName)
                      ))}
                      className="h-4 w-4 rounded border-slate-300 text-navy-700 focus:ring-navy-600"
                    />
                    <span>{pmsName}</span>
                  </label>
                ))}
              </div>
              {excludedPms.length ? (
                <div className="mt-3 text-xs font-medium text-amber-700">
                  {excludedPms.length} PMS marked as not received: {excludedPms.join(', ')}
                </div>
              ) : null}
            </div>
            <div className="border-t border-slate-100 px-5 py-3 text-xs text-slate-500">
              The generated workbook keeps all available reporting months as columns. DIFF and DIFF % compare the selected report with that PMS's immediately preceding available appraisal report, even when a PMS skips a month. Individual PMS tabs show the complete security history, not only large changes. Percentage displays are rounded to whole percentages without decimal places.
            </div>
          </Card>

          {job && ['completed', 'completed_with_errors'].includes(job.status) && job.report_available ? (
            <Card>
              <div className="flex flex-wrap items-center justify-between gap-4 px-5 py-4">
                <div className="flex items-center gap-3">
                  <div className="grid h-10 w-10 place-items-center rounded-xl bg-emerald-50 text-emerald-700"><FileText size={18} /></div>
                  <div>
                    <div className="text-sm font-bold text-navy-900">Report ready · {job.comparison_month}</div>
                    <div className="mt-1 text-xs text-slate-500">{job.pms_count || 0} PMS detected · {job.error_count || 0} parsing errors</div>
                  </div>
                </div>
                <button type="button" onClick={openCurrentPreview} className="inline-flex items-center gap-2 rounded-xl border border-slate-200 bg-white px-3.5 py-2.5 text-sm font-semibold text-slate-700 hover:bg-slate-50"><FileText size={16} /> Preview Excel</button>
              </div>
            </Card>
          ) : null}

          {report ? (
            <Card>
              <div className="grid gap-3 p-5 sm:grid-cols-2 lg:grid-cols-3">
                {[
                  ['Total PMS', number(currentSummary.total_pms ?? currentSummary.active_pms ?? 0, 0), 'Active in this month'],
                  ['New Stocks Entry Count', number(currentSummary.new_stock_entries ?? currentSummary.new_holdings ?? 0, 0), 'New holding entries'],
                  ['Old Stocks Removal Count', number(currentSummary.old_stock_removals ?? currentSummary.exited_holdings ?? 0, 0), 'Fully exited holdings'],
                  ['Total Holdings', number(currentSummary.total_holdings ?? currentSummary.holding_count ?? 0, 0), 'Unique holdings across active PMS'],
                  ['Biggest Change PMS', currentSummary.biggest_change_pms?.pms_name || 'N/A', currentSummary.biggest_change_pms?.average_abs_change_pct == null ? 'No comparable holdings' : `${pct(currentSummary.biggest_change_pms.average_abs_change_pct)} average change`],
                  ['Least Change PMS', currentSummary.least_change_pms?.pms_name || 'N/A', currentSummary.least_change_pms?.average_abs_change_pct == null ? 'No comparable holdings' : `${pct(currentSummary.least_change_pms.average_abs_change_pct)} average change`],
                ].map(([label, value, detail]) => (
                  <div key={label} className="rounded-xl bg-slate-50 p-4">
                    <div className="text-[10px] font-bold uppercase tracking-wide text-slate-500">{label}</div>
                    <div className="mt-1 break-words text-lg font-bold text-navy-900">{value}</div>
                    <div className="mt-1 text-xs text-slate-500">{detail}</div>
                  </div>
                ))}
              </div>
            </Card>
          ) : null}

        </>
      ) : tab === 'preview' ? (
        <>
          {reportLoading ? (
            <Card><div className="flex items-center gap-3 p-6"><RefreshCw size={18} className="animate-spin text-navy-700" /><div><div className="text-sm font-semibold text-navy-900">Loading workbook preview…</div><div className="mt-1 text-xs text-slate-500">Retrieving worksheet data from the saved report.</div></div></div></Card>
          ) : report && reportBatchId === job?.id ? (
            <ReportViewer key={reportBatchId || report.summary?.comparison_month || 'current-preview'} report={report} user={user} />
          ) : (
            <Card>
              <div className="p-6">
                <Empty title="Excel preview is not available yet" text={reportLoadError || 'Upload a monthly ZIP and wait for processing to finish. The report can then be previewed here before download.'} />
                {job?.id && ['completed', 'completed_with_errors'].includes(job.status) && job.report_available ? (
                  <div className="mt-4 text-center"><button type="button" onClick={openCurrentPreview} className="inline-flex items-center gap-2 rounded-xl bg-navy-700 px-4 py-2.5 text-sm font-semibold text-white"><RefreshCw size={15} /> Retry Preview</button></div>
                ) : null}
              </div>
            </Card>
          )}
          {report && reportBatchId === job?.id && job?.id && ['completed', 'completed_with_errors'].includes(job.status) ? (
            <Card>
              <div className="flex flex-wrap items-center justify-between gap-4 p-5">
                <div>
                  <h2 className="font-bold text-navy-900">What would you like to do next?</h2>
                  <p className="mt-1 text-xs text-slate-500">Review the worksheet tabs above, then download the Excel workbook or upload another monthly ZIP to run a new comparison.</p>
                </div>
                <div className="flex flex-wrap gap-2">
                  <button type="button" onClick={() => downloadReport(job.id, report.summary?.comparison_month || job.comparison_month)} className="inline-flex items-center gap-2 rounded-xl bg-navy-700 px-4 py-2.5 text-sm font-semibold text-white hover:bg-navy-800"><Download size={16} /> Download Excel</button>
                  <button type="button" onClick={beginAnotherUpload} className="inline-flex items-center gap-2 rounded-xl border border-slate-200 bg-white px-4 py-2.5 text-sm font-semibold text-slate-700 hover:bg-slate-50"><UploadCloud size={16} /> Upload Another ZIP</button>
                </div>
              </div>
            </Card>
          ) : null}
        </>
      ) : (
        <>
          <Card>
            <div className="border-b border-slate-200 px-5 py-4">
              <div className="flex items-center gap-2"><History size={17} /><div><h2 className="font-bold text-navy-900">Appraisal report history</h2><p className="mt-1 text-xs text-slate-500">Every completed appraisal period remains available. Re-running a period creates a new version while the old version stays in the database.</p></div></div>
            </div>
            {history.length ? (
              <div className="divide-y divide-slate-100">
                {history.map((item, index) => (
                  <div key={item.id} className="flex flex-wrap items-center justify-between gap-4 px-5 py-4 hover:bg-slate-50">
                    <div>
                      <div className="flex items-center gap-2 text-sm font-bold text-navy-900">
                        {item.comparison_month}
                        {index === 0 ? <Badge tone="blue">Latest</Badge> : null}
                      </div>
                      <div className="mt-1 text-xs text-slate-500">
                        {item.active_pms || 0} active PMS · {item.inactive_pms || 0} inactive · {item.error_count || 0} errors · total {formatDuration(item.total_elapsed_seconds || 0)}
                      </div>
                    </div>
                    <div className="flex items-center gap-2">
                      <StatusBadge status={item.status} />
                      {item.report_available ? <button type="button" onClick={() => loadHistoryReport(item)} className="rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs font-semibold text-slate-700">Preview report</button> : null}
                    </div>
                  </div>
                ))}
              </div>
            ) : <Empty title="No report history yet" text="Completed appraisal ZIP uploads will appear here." />}
          </Card>

          {historyReport ? (
            <>
              <ReportViewer key={historySelection?.id || historyReport.summary?.comparison_month || 'history-preview'} report={historyReport} user={user} />
              <Card>
                <div className="flex flex-wrap items-center justify-between gap-4 p-5">
                  <div>
                    <h2 className="font-bold text-navy-900">Historical workbook previewed</h2>
                    <p className="mt-1 text-xs text-slate-500">You can now download the reviewed workbook or return to the current month.</p>
                  </div>
                  <div className="flex gap-2">
                    {historySelection?.id ? <button type="button" onClick={() => downloadReport(historySelection.id, historySelection.comparison_month)} className="inline-flex items-center gap-2 rounded-xl bg-navy-700 px-4 py-2.5 text-sm font-semibold text-white"><Download size={16} /> Download Excel</button> : null}
                    <button type="button" onClick={() => setTab('current')} className="rounded-xl border border-slate-200 bg-white px-4 py-2.5 text-sm font-semibold text-slate-700">Back to Current Month</button>
                  </div>
                </div>
              </Card>
            </>
          ) : null}
        </>
      )}
      <FloatingJobTimer job={job} tick={clockTick} />
    </div>
  );
}