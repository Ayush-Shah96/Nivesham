import React from 'react';
import { AlertCircle, CheckCircle2, Loader2 } from 'lucide-react';

export function PageHeader({ eyebrow, title, description, action }) {
  return (
    <header className="mb-8 flex flex-col gap-5 border-b border-slate-200/80 pb-6 md:flex-row md:items-end md:justify-between">
      <div className="min-w-0">
        {eyebrow && (
          <div className="mb-2 inline-flex items-center gap-2 text-[11px] font-bold uppercase tracking-[.18em] text-amber-700">
            <span className="h-px w-6 bg-amber-600/60" />
            {eyebrow}
          </div>
        )}
        <h1 className="font-display text-3xl font-bold tracking-tight text-navy-900 md:text-[2.25rem]">{title}</h1>
        {description && <p className="mt-2 max-w-2xl text-[15px] leading-relaxed text-slate-600">{description}</p>}
      </div>
      {action}
    </header>
  );
}

export function Card({ children, className = '' }) {
  return (
    <div className={`rounded-2xl border border-slate-200/80 bg-white shadow-[0_1px_2px_rgba(15,27,45,.04),0_12px_32px_-14px_rgba(15,27,45,.12)] ${className}`}>
      {children}
    </div>
  );
}

export function Loading() {
  return (
    <div className="flex min-h-40 items-center justify-center text-sm text-slate-500">
      <Loader2 className="mr-2 animate-spin" size={18} />
      Loading data…
    </div>
  );
}

export function ErrorBox({ error }) {
  return (
    <div className="flex items-start gap-3 rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-700">
      <AlertCircle size={18} className="mt-0.5 shrink-0" />
      <div>
        <b>We couldn’t load this section</b>
        <div className="mt-1">{error}</div>
      </div>
    </div>
  );
}

export function Empty({ title = 'Nothing here yet', text = 'There is no information for this view yet.' }) {
  return (
    <div className="p-10 text-center text-sm text-slate-500">
      <div className="mx-auto mb-3 grid h-11 w-11 place-items-center rounded-full bg-amber-50 text-amber-700">
        <CheckCircle2 size={18} />
      </div>
      <div className="font-semibold text-slate-700">{title}</div>
      <p className="mt-1">{text}</p>
    </div>
  );
}

export function Badge({ children, tone = 'slate' }) {
  const tones = {
    slate: 'bg-slate-100 text-slate-700',
    green: 'bg-emerald-50 text-emerald-700',
    amber: 'bg-amber-50 text-amber-800',
    red: 'bg-red-50 text-red-700',
    blue: 'bg-blue-50 text-blue-700',
  };
  return (
    <span className={`inline-flex rounded-full px-2.5 py-1 text-[11px] font-bold ${tones[tone] || tones.slate}`}>
      {children}
    </span>
  );
}

export function Table({ columns, rows, renderActions }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead className="border-b border-slate-200 bg-slate-50/80">
          <tr>
            {columns.map((c) => (
              <th key={c} className="whitespace-nowrap px-5 py-3 text-[11px] font-bold uppercase tracking-wide text-slate-500">{c}</th>
            ))}
            {renderActions && <th className="px-5 py-3" />}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {rows.map((r, i) => (
            <tr key={r.id ?? i} className="transition hover:bg-amber-50/40">
              {columns.map((c) => (
                <td key={c} className="whitespace-nowrap px-5 py-4">{r.__render?.[c] ?? r[c] ?? '—'}</td>
              ))}
              {renderActions && <td className="px-5 py-4">{renderActions(r)}</td>}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
