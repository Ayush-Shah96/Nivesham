import React, { useState } from 'react';
import { CheckCircle2, KeyRound, Palette, UserRound } from 'lucide-react';
import { Card, ErrorBox, PageHeader } from '../components/UI';
import { api } from '../lib/api';
import {
  DENSITIES,
  THEMES,
  applyDensity,
  applyTheme,
  getCurrentDensity,
  getCurrentTheme,
} from '../lib/theme';

const inputClass =
  'w-full rounded-xl border border-slate-200 bg-white px-3 py-2.5 text-sm outline-none focus:border-navy-700 focus:ring-2 focus:ring-navy-100';
const labelClass = 'mb-1 block text-[11px] font-bold uppercase tracking-[.1em] text-slate-500';

function CardHeader({ icon: Icon, title, subtitle }) {
  return (
    <div className="mb-5 flex items-start gap-3">
      <div className="grid h-10 w-10 shrink-0 place-items-center rounded-xl border border-slate-200 bg-slate-50 text-navy-700">
        <Icon size={18} />
      </div>
      <div>
        <h3 className="font-bold">{title}</h3>
        {subtitle && <p className="mt-0.5 text-xs text-slate-500">{subtitle}</p>}
      </div>
    </div>
  );
}

function AccountDetails({ user }) {
  const rows = [
    ['Username', user?.username],
    ['Role', user?.role],
    ['User ID', user?.id],
  ];
  return (
    <Card className="p-6">
      <CardHeader icon={UserRound} title="Account details" subtitle="Your sign-in identity." />
      <dl className="space-y-4 text-sm">
        {rows.map(([label, value]) => (
          <div key={label} className="flex justify-between border-b border-slate-100 pb-3">
            <dt className="text-slate-500">{label}</dt>
            <dd className="font-semibold">{value ?? '—'}</dd>
          </div>
        ))}
      </dl>
    </Card>
  );
}

function PasswordCard() {
  const [form, setForm] = useState({ current: '', next: '', confirm: '' });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [done, setDone] = useState(false);

  const update = (key) => (event) => setForm((prev) => ({ ...prev, [key]: event.target.value }));

  const submit = async (event) => {
    event.preventDefault();
    setError('');
    setDone(false);

    if (form.next.length < 8) {
      setError('New password must be at least 8 characters.');
      return;
    }
    if (form.next !== form.confirm) {
      setError('New password and confirmation do not match.');
      return;
    }
    if (form.next === form.current) {
      setError('New password must be different from the current password.');
      return;
    }

    setBusy(true);
    try {
      await api.changeOwnPassword(form.current, form.next);
      setForm({ current: '', next: '', confirm: '' });
      setDone(true);
    } catch (err) {
      setError(err?.message || 'Could not update password.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card className="p-6">
      <CardHeader icon={KeyRound} title="Change password" subtitle="Use at least 8 characters." />
      <form onSubmit={submit} className="space-y-4">
        <div>
          <label className={labelClass} htmlFor="pw-current">Current password</label>
          <input id="pw-current" type="password" autoComplete="current-password" required value={form.current} onChange={update('current')} className={inputClass} />
        </div>
        <div>
          <label className={labelClass} htmlFor="pw-new">New password</label>
          <input id="pw-new" type="password" autoComplete="new-password" required value={form.next} onChange={update('next')} className={inputClass} />
        </div>
        <div>
          <label className={labelClass} htmlFor="pw-confirm">Confirm new password</label>
          <input id="pw-confirm" type="password" autoComplete="new-password" required value={form.confirm} onChange={update('confirm')} className={inputClass} />
        </div>

        {error ? <ErrorBox error={error} /> : null}
        {done ? (
          <div className="flex items-center gap-2 rounded-xl border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-700">
            <CheckCircle2 size={16} /> Password updated.
          </div>
        ) : null}

        <button
          type="submit"
          disabled={busy}
          className="rounded-xl bg-navy-900 px-4 py-2.5 text-sm font-semibold text-white hover:opacity-95 disabled:opacity-50"
        >
          {busy ? 'Updating…' : 'Update password'}
        </button>
      </form>
    </Card>
  );
}

function AppearanceCard() {
  const [theme, setTheme] = useState(() => getCurrentTheme());
  const [density, setDensity] = useState(() => getCurrentDensity());

  return (
    <Card className="p-6">
      <CardHeader icon={Palette} title="Appearance" subtitle="Applies in this browser." />
      <div className="space-y-4">
        <div>
          <label className={labelClass} htmlFor="theme-select">Theme</label>
          <select id="theme-select" value={theme} onChange={(event) => setTheme(applyTheme(event.target.value))} className={inputClass}>
            {THEMES.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
          </select>
        </div>
        <div>
          <label className={labelClass} htmlFor="density-select">UI size</label>
          <select id="density-select" value={density} onChange={(event) => setDensity(applyDensity(event.target.value))} className={inputClass}>
            {DENSITIES.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
          </select>
        </div>
      </div>
    </Card>
  );
}

// Shared by every user (non-admin Settings page) and by the admin "User Settings" tab.
export function UserSettingsPanel({ user }) {
  return (
    <div className="grid gap-5 lg:grid-cols-2">
      <AccountDetails user={user} />
      <PasswordCard />
      <AppearanceCard />
    </div>
  );
}

export default function UserSettings({ user }) {
  return (
    <div className="space-y-6">
      <PageHeader eyebrow="Account" title="Settings" description="Your account details, password and display preferences." />
      <UserSettingsPanel user={user} />
    </div>
  );
}
