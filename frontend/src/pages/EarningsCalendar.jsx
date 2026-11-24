import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { CalendarDays, ChevronLeft, ChevronRight, Download, ExternalLink, RefreshCw, Search } from 'lucide-react';
import { api } from '../lib/api';

const ANALYST_STYLES = {
  raj: { chip: 'bg-blue-100 text-blue-800', event: 'bg-blue-50 text-blue-900 border-blue-200', row: 'bg-blue-50/40 hover:bg-blue-50', border: 'border-l-blue-600', dot: 'bg-blue-600' },
  jainam: { chip: 'bg-violet-100 text-violet-800', event: 'bg-violet-50 text-violet-900 border-violet-200', row: 'bg-violet-50/40 hover:bg-violet-50', border: 'border-l-violet-600', dot: 'bg-violet-600' },
  aashil: { chip: 'bg-emerald-100 text-emerald-800', event: 'bg-emerald-50 text-emerald-900 border-emerald-200', row: 'bg-emerald-50/40 hover:bg-emerald-50', border: 'border-l-emerald-600', dot: 'bg-emerald-600' },
  romil: { chip: 'bg-amber-100 text-amber-800', event: 'bg-amber-50 text-amber-900 border-amber-200', row: 'bg-amber-50/40 hover:bg-amber-50', border: 'border-l-amber-600', dot: 'bg-amber-600' },
};
const ANALYSTS = ['Raj', 'Jainam', 'Aashil', 'Romil'];
const toISO = (date) => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
const startOfMonth = (date) => new Date(date.getFullYear(), date.getMonth(), 1);
const keyAnalyst = (name) => String(name || '').toLowerCase().replace(/[^a-z]/g, '') === 'ashil' ? 'aashil' : String(name || '').toLowerCase().replace(/[^a-z]/g, '');
const formatDay = (date) => date.toLocaleDateString('en-IN', { weekday: 'short', day: 'numeric', month: 'short' });
let xlsxLoader = null;
function loadSheetJs() {
  if (window.XLSX) return Promise.resolve(window.XLSX);
  if (xlsxLoader) return xlsxLoader;
  xlsxLoader = new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = 'https://cdnjs.cloudflare.com/ajax/libs/xlsx/0.18.5/xlsx.full.min.js';
    script.async = true;
    script.onload = () => window.XLSX ? resolve(window.XLSX) : reject(new Error('SheetJS loaded without XLSX global'));
    script.onerror = () => reject(new Error('Could not load the Excel exporter. Check your internet connection.'));
    document.head.appendChild(script);
  });
  return xlsxLoader;
}

async function downloadWorkbook(scheduleRows, pendingRows) {
  const XLSX = await loadSheetJs();
  const workbook = XLSX.utils.book_new();
  const schedule = scheduleRows.map((event) => ({ Date: event.date || 'Pending', Company: event.company, Symbol: event.symbol || '', Analyst: event.analyst || 'Unassigned', Type: event.type, 'Call time': event.time || '', Source: event.source, 'Source URL': event.url }));
  const pending = pendingRows.map((event) => ({ Company: event.company, Symbol: event.symbol || '', Analyst: event.analyst || 'Unassigned', Type: event.type, Status: event.pending_label || 'Date pending', Source: event.source, 'Source URL': event.url }));
  XLSX.utils.book_append_sheet(workbook, XLSX.utils.json_to_sheet(schedule), 'Confirmed Schedule');
  XLSX.utils.book_append_sheet(workbook, XLSX.utils.json_to_sheet(pending), 'Date Pending');
  XLSX.writeFile(workbook, `NIVESHAM_Earnings_Calendar_${new Date().toISOString().slice(0, 10)}.xlsx`);
}

export default function EarningsCalendar() {
  const today = useMemo(() => new Date(), []);
  const [month, setMonth] = useState(() => startOfMonth(new Date()));
  const [events, setEvents] = useState([]);
  const [analysts, setAnalysts] = useState(() => new Set(ANALYSTS.map(keyAnalyst)));
  const [type, setType] = useState('All');
  const [search, setSearch] = useState('');
  const [selectedDay, setSelectedDay] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [lastLoaded, setLastLoaded] = useState(null);

  const load = useCallback(async (forceRefresh = false) => {
    setLoading(true); setError('');
    const start = new Date(month.getFullYear(), month.getMonth(), 1);
    start.setDate(start.getDate() - 7);
    const end = new Date(month.getFullYear(), month.getMonth() + 1, 14);
    const todayStart = new Date(today); todayStart.setDate(todayStart.getDate() - 30);
    const todayEnd = new Date(today); todayEnd.setDate(todayEnd.getDate() + 14);
    const rangeStart = start < todayStart ? start : todayStart;
    const rangeEnd = end > todayEnd ? end : todayEnd;
    try {
      const result = await api.earningsCalendar({ startDate: toISO(rangeStart), endDate: toISO(rangeEnd), forceRefresh });
      setEvents(Array.isArray(result?.events) ? result.events : []);
      setLastLoaded(new Date());
    } catch (e) {
      setError(e?.message || 'Could not load earnings-calendar events.');
    } finally {
      setLoading(false);
    }
  }, [month, today]);

  useEffect(() => { load(false); }, [load]);

  const filtered = useMemo(() => events.filter((event) => {
    const analystOkay = analysts.size === 0 || analysts.has(keyAnalyst(event.analyst));
    const typeOkay = type === 'All' || event.type === type;
    const query = search.trim().toLowerCase();
    const searchOkay = !query || `${event.company} ${event.symbol || ''} ${event.analyst || ''}`.toLowerCase().includes(query);
    return analystOkay && typeOkay && searchOkay;
  }), [events, analysts, type, search]);

  const byDate = useMemo(() => {
    const map = new Map();
    filtered.forEach((event) => {
      if (!map.has(event.date)) map.set(event.date, []);
      map.get(event.date).push(event);
    });
    for (const list of map.values()) list.sort((a, b) => String(a.type).localeCompare(String(b.type)) || a.company.localeCompare(b.company));
    return map;
  }, [filtered]);

  const monthGrid = useMemo(() => {
    const first = startOfMonth(month);
    const mondayOffset = (first.getDay() + 6) % 7;
    const start = new Date(first); start.setDate(first.getDate() - mondayOffset);
    const days = [];
    for (let index = 0; index < 42; index += 1) {
      const day = new Date(start); day.setDate(start.getDate() + index);
      days.push(day);
      if (index >= 27 && day.getMonth() !== month.getMonth() && day.getDay() === 0) break;
    }
    return days;
  }, [month]);

  const next14 = useMemo(() => {
    const from = toISO(today);
    const end = new Date(today); end.setDate(end.getDate() + 14);
    return filtered.filter((event) => event.date && event.date >= from && event.date <= toISO(end)).sort((a, b) => a.date.localeCompare(b.date));
  }, [filtered, today]);
  const pendingEvents = useMemo(() => filtered.filter((event) => !event.date), [filtered]);

  const monthLabel = month.toLocaleDateString('en-IN', { month: 'long', year: 'numeric' });
  const countResults = filtered.filter((e) => e.type === 'Results').length;
  const countCalls = filtered.filter((e) => e.type === 'Concall').length;

  const toggleAnalyst = (name) => setAnalysts((old) => {
    const next = new Set(old);
    const key = keyAnalyst(name);
    if (next.has(key)) next.delete(key); else next.add(key);
    return next;
  });

  const openSource = (event) => {
    if (event?.url && /^https?:\/\//i.test(event.url)) window.open(event.url, '_blank', 'noopener,noreferrer');
  };

  return (
    <div className="mx-auto w-full max-w-[1600px] space-y-4 p-4 md:p-6">
      <div className="flex flex-wrap items-start gap-3">
        <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-navy-900 text-white"><CalendarDays size={22} /></div>
        <div className="min-w-0 flex-1">
          <div className="text-[11px] font-bold uppercase tracking-[0.16em] text-slate-500">Coverage Calendar</div>
          <h1 className="text-2xl font-bold text-slate-900">Earnings Results & Concalls</h1>
          <p className="mt-1 max-w-3xl text-sm text-slate-500">Only active, approved Coverage List companies are shown. Sources: Screener.in upcoming concalls and upcoming results. Collection runs every six days.</p>
        </div>
        <button onClick={() => load(true)} disabled={loading} className="inline-flex items-center gap-2 rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm font-semibold text-slate-700 hover:bg-slate-50 disabled:opacity-50"><RefreshCw size={15} className={loading ? 'animate-spin' : ''} /> Refresh</button>
      </div>

      <div className="grid grid-cols-3 gap-3">
        <Stat label="Total Events" value={filtered.length} />
        <Stat label="Results" value={countResults} />
        <Stat label="Concalls" value={countCalls} />
      </div>

      <div className="flex flex-wrap items-center gap-2 rounded-2xl border border-slate-200 bg-white p-3">
        <span className="mr-1 text-xs font-semibold text-slate-500">Analyst</span>
        {ANALYSTS.map((name) => {
          const style = ANALYST_STYLES[keyAnalyst(name)] || ANALYST_STYLES.raj;
          const active = analysts.has(keyAnalyst(name));
          return <button key={name} onClick={() => toggleAnalyst(name)} className={`rounded-full px-3 py-1.5 text-xs font-semibold transition ${style.chip} ${active ? '' : 'opacity-30'}`}>{name}</button>;
        })}
        <span className="mx-1 hidden h-6 border-l border-slate-200 sm:block" />
        <div className="inline-flex overflow-hidden rounded-lg border border-slate-200">
          {['All', 'Results', 'Concall'].map((value) => <button key={value} onClick={() => setType(value)} className={`px-3 py-1.5 text-xs font-semibold ${type === value ? 'bg-navy-900 text-white' : 'bg-white text-slate-600 hover:bg-slate-50'}`}>{value === 'Concall' ? 'Concalls' : value}</button>)}
        </div>
        <div className="relative ml-auto min-w-[190px] flex-1 sm:max-w-xs"><Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400"/><input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search covered companies…" className="w-full rounded-lg border border-slate-200 bg-slate-50 py-2 pl-9 pr-3 text-xs outline-none focus:border-blue-500" /></div>
        <button onClick={() => downloadWorkbook(filtered, pendingEvents).catch((e) => setError(e.message || 'Excel export failed'))} className="inline-flex items-center gap-2 rounded-lg bg-slate-900 px-3 py-2 text-xs font-semibold text-white hover:bg-slate-700"><Download size={14}/> Export Excel</button>
      </div>

      {error && <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">{error}</div>}
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1fr)_320px]">
        <section className="overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm">
          <div className="flex items-center justify-between border-b border-slate-200 px-4 py-3">
            <button aria-label="Previous month" onClick={() => setMonth((m) => new Date(m.getFullYear(), m.getMonth() - 1, 1))} className="rounded-lg border border-slate-200 p-2 hover:bg-slate-50"><ChevronLeft size={18}/></button>
            <h2 className="text-base font-bold text-slate-900">{monthLabel}</h2>
            <button aria-label="Next month" onClick={() => setMonth((m) => new Date(m.getFullYear(), m.getMonth() + 1, 1))} className="rounded-lg border border-slate-200 p-2 hover:bg-slate-50"><ChevronRight size={18}/></button>
          </div>
          <div className="grid grid-cols-7 border-b border-slate-200 bg-slate-50">{['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].map((day) => <div key={day} className="py-2 text-center text-[10px] font-bold uppercase tracking-wider text-slate-500">{day}</div>)}</div>
          <div className="grid grid-cols-7">
            {monthGrid.map((day) => {
              const key = toISO(day); const dayEvents = byDate.get(key) || []; const isToday = toISO(day) === toISO(today); const isMonth = day.getMonth() === month.getMonth();
              return <button key={key} onClick={() => setSelectedDay(day)} className={`min-h-[104px] border-b border-r border-slate-100 p-1.5 text-left transition hover:bg-blue-50 sm:min-h-[120px] ${!isMonth ? 'bg-slate-50/70 text-slate-300' : 'bg-white'} ${isToday ? 'bg-red-50 ring-1 ring-inset ring-red-200' : ''}`}>
                <span className={`mb-1 flex h-5 w-5 items-center justify-center rounded-full text-[11px] font-bold ${isToday ? 'bg-red-600 text-white' : isMonth ? 'text-slate-600' : 'text-slate-300'}`}>{day.getDate()}</span>
                <div className="space-y-1">
                  {dayEvents.slice(0, 3).map((event) => { const analystStyle = ANALYST_STYLES[keyAnalyst(event.analyst)] || { event: 'bg-slate-100 text-slate-800 border-slate-200', border: 'border-l-slate-500' }; return <span key={event.id} onClick={(e) => { e.stopPropagation(); openSource(event); }} title={`${event.company} · ${event.type}${event.time ? ` · ${event.time}` : ''}`} className={`block cursor-pointer truncate rounded border px-1 py-0.5 text-[9px] font-semibold leading-4 ${analystStyle.event} border-l-2 ${analystStyle.border}`}>{event.type === 'Concall' ? '☎ ' : '▣ '}{event.company}{event.time ? ` ${event.time}` : ''}</span>; })}
                  {dayEvents.length > 3 && <span className="block px-1 text-[9px] text-slate-500">+{dayEvents.length - 3} more</span>}
                </div>
              </button>;
            })}
          </div>
          <div className="flex flex-wrap items-center justify-between gap-2 px-4 py-2 text-[11px] text-slate-500"><span>{loading ? 'Loading events…' : `${filtered.length} events for filtered coverage companies`}</span><span>{lastLoaded ? `Updated ${lastLoaded.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' })}` : 'Awaiting first load'}</span></div>
        </section>

        <aside className="space-y-4">
          <section className="overflow-hidden rounded-2xl border border-slate-200 bg-white">
            <header className="border-b border-slate-200 bg-slate-50 px-4 py-3 text-sm font-bold text-slate-800">Next 14 Days</header>
            {next14.length ? next14.slice(0, 30).map((event) => <EventRow key={event.id} event={event} onClick={openSource}/>) : <p className="p-4 text-xs text-slate-500">No upcoming events for the current filters.</p>}
          </section>
          <section className="overflow-hidden rounded-2xl border border-slate-200 bg-white">
            <header className="border-b border-slate-200 bg-slate-50 px-4 py-3 text-sm font-bold text-slate-800">Date Pending</header>
            {pendingEvents.length ? pendingEvents.map((event) => <EventRow key={event.id} event={event} onClick={openSource}/>) : <p className="p-4 text-xs leading-5 text-slate-500">No pending results or concall dates found for the current coverage filters.</p>}
          </section>
          <button onClick={() => downloadWorkbook(filtered, pendingEvents).catch((e) => setError(e.message || 'Excel export failed'))} className="inline-flex w-full items-center justify-center gap-2 rounded-xl bg-slate-900 px-4 py-3 text-sm font-semibold text-white hover:bg-slate-700"><Download size={15}/> Download Excel Workbook</button>
          <section className="rounded-2xl border border-slate-200 bg-white p-4">
            <div className="text-xs font-bold text-slate-700">Analyst legend</div>
            <div className="mt-3 grid grid-cols-2 gap-2">{ANALYSTS.map((name) => <div key={name} className="flex items-center gap-2 text-xs text-slate-600"><span className={`h-2.5 w-2.5 rounded-full ${ANALYST_STYLES[keyAnalyst(name)]?.dot || 'bg-slate-500'}`}/>{name}</div>)}</div>
            <p className="mt-3 text-[11px] leading-5 text-slate-400">Calendar scrape cadence: every 6 days. Refresh fetches the latest stored schedule without starting an extra scrape.</p>
          </section>
        </aside>
      </div>

      {selectedDay && <DayModal day={selectedDay} events={byDate.get(toISO(selectedDay)) || []} close={() => setSelectedDay(null)} openSource={openSource}/>}
    </div>
  );
}

function Stat({ label, value }) { return <div className="rounded-xl border border-slate-200 bg-white px-4 py-3"><div className="text-2xl font-bold tabular-nums text-slate-900">{value}</div><div className="mt-1 text-[11px] font-semibold uppercase tracking-wider text-slate-500">{label}</div></div>; }
function EventRow({ event, onClick }) { const style = ANALYST_STYLES[keyAnalyst(event.analyst)] || { chip: 'bg-slate-100 text-slate-700', event: 'bg-slate-50 text-slate-800 border-slate-200', row: 'bg-white hover:bg-slate-50', border: 'border-l-slate-400' }; return <button onClick={() => onClick(event)} className={`flex w-full items-start gap-3 border-b border-l-4 border-slate-100 px-4 py-3 text-left last:border-b-0 ${style.border} ${style.row}`}><div className="min-w-[72px] text-[11px] font-semibold text-slate-500">{event.date ? formatDay(new Date(`${event.date}T00:00:00`)) : (event.pending_label || 'Date pending')}</div><div className="min-w-0 flex-1"><div className="truncate text-xs font-bold text-slate-800">{event.company}</div><div className="mt-1 flex flex-wrap items-center gap-1.5"><span className={`rounded border px-1.5 py-0.5 text-[9px] font-bold ${style.event}`}>{event.type}</span><span className={`rounded px-1.5 py-0.5 text-[9px] font-semibold ${style.chip}`}>{event.analyst || 'Unassigned'}</span>{event.time && <span className="text-[10px] text-slate-500">{event.time}</span>}</div></div><ExternalLink size={13} className="mt-1 shrink-0 text-slate-400"/></button>; }
function DayModal({ day, events, close, openSource }) { return <div role="dialog" aria-modal="true" className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/40 p-4" onMouseDown={(e) => { if (e.target === e.currentTarget) close(); }}><div className="max-h-[85vh] w-full max-w-xl overflow-y-auto rounded-2xl bg-white shadow-2xl"><div className="flex items-center justify-between border-b border-slate-200 px-5 py-4"><h2 className="text-base font-bold text-slate-900">{day.toLocaleDateString('en-IN', { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' })}</h2><button onClick={close} className="rounded-lg border border-slate-200 px-3 py-1.5 text-xs font-semibold text-slate-600">Close</button></div>{events.length ? events.map((event) => <EventRow key={event.id} event={event} onClick={(item) => openSource(item)}/>) : <p className="p-5 text-sm text-slate-500">No events on this date for the active filters.</p>}</div></div>; }
