import React, { useState } from 'react';
import { ArrowLeft, ArrowRight, LockKeyhole } from 'lucide-react';
import { Link } from 'react-router-dom';
import { api, getUserFromToken, setToken } from '../lib/api';
import Brand from '../components/Brand';

const HIGHLIGHTS = [
  'Market signals and unusual activity in one place',
  'Sector research, filings and news, classified for the desk',
  'Portfolio and debt exposure, consolidated across brokers',
];

export default function Login({ onLogin }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError('');
    try {
      const response = await api.login(username, password);
      setToken(response.access_token);
      const provisionalUser = getUserFromToken();
      if (!provisionalUser) throw new Error('Sign-in succeeded, but your session could not be started. Please try again.');
      // The freshly issued token already carries the identity; no second request needed.
      onLogin(provisionalUser);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  const inputClass =
    'w-full rounded-xl border border-[#d9dee7] bg-white px-4 py-3 text-[15px] text-[#0f1b2d] outline-none transition placeholder:text-[#9aa4b5] focus:border-[#0b1f3a] focus:ring-4 focus:ring-[#0b1f3a]/10';

  return (
    <div className="site-light min-h-screen bg-[#fbfaf7] font-sans text-[#0f1b2d]">
      <div className="grid min-h-screen lg:grid-cols-[1.05fr_.95fr]">
        {/* Brand panel */}
        <aside className="relative hidden overflow-hidden bg-[#0b1f3a] text-white lg:flex">
          <div className="pointer-events-none absolute -left-24 -top-24 h-96 w-96 rounded-full bg-[#e0b25a]/10 blur-3xl" />
          <div className="pointer-events-none absolute -bottom-32 right-0 h-[28rem] w-[28rem] rounded-full bg-[#1d3b5e]/60 blur-3xl" />

          <div className="relative flex w-full flex-col justify-between p-12 xl:p-16">
            <Link to="/" className="inline-flex w-fit items-center gap-2 text-sm text-white/70 hover:text-white">
              <ArrowLeft size={16} /> Back to home
            </Link>

            <div className="max-w-xl">
              <Brand tone="light" className="mb-10" />
              <p className="mb-4 text-xs font-bold uppercase tracking-[.22em] text-[#e0b25a]">Your research desk</p>
              <h1 className="font-display text-4xl font-bold leading-[1.15] tracking-tight xl:text-5xl">
                Everything your desk needs, in one calm workspace.
              </h1>
              <ul className="mt-9 space-y-4">
                {HIGHLIGHTS.map((text) => (
                  <li key={text} className="flex items-start gap-3 text-[15px] leading-relaxed text-white/80">
                    <span className="mt-2 h-1.5 w-1.5 shrink-0 rounded-full bg-[#e0b25a]" />
                    {text}
                  </li>
                ))}
              </ul>
            </div>

            <p className="text-xs text-white/50">© {new Date().getFullYear()} NIVESHAM</p>
          </div>
        </aside>

        {/* Form */}
        <div className="flex items-center justify-center p-6 sm:p-10">
          <div className="w-full max-w-md">
            <div className="mb-10 flex items-center justify-between lg:hidden">
              <Brand compact />
              <Link to="/" className="text-sm text-slate-500 hover:text-navy-900">Home</Link>
            </div>

            <form onSubmit={submit} className="rounded-3xl border border-[#e3e1da] bg-white p-8 shadow-[0_30px_70px_-30px_rgba(11,31,58,.25)] md:p-10">
              <div className="mb-8">
                <div className="text-xs font-bold uppercase tracking-[.18em] text-amber-700">Welcome back</div>
                <h2 className="font-display mt-2 text-3xl font-bold tracking-tight text-[#0b1f3a]">Sign in to NIVESHAM</h2>
                <p className="mt-2 text-sm leading-relaxed text-[#5b6679]">Use the username and password your administrator issued to you.</p>
              </div>

              {error && (
                <div role="alert" className="mb-6 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm font-medium text-red-700">
                  {error}
                </div>
              )}

              <label className="mb-5 block">
                <span className="mb-2 block text-sm font-semibold text-[#2b3649]">Username</span>
                <input
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  autoComplete="username"
                  required
                  className={inputClass}
                  placeholder="Your username"
                />
              </label>

              <label className="mb-7 block">
                <span className="mb-2 block text-sm font-semibold text-[#2b3649]">Password</span>
                <input
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  type="password"
                  autoComplete="current-password"
                  required
                  className={inputClass}
                  placeholder="Your password"
                />
              </label>

              <button
                type="submit"
                disabled={busy}
                className="flex w-full items-center justify-center gap-2 rounded-xl bg-[#0b1f3a] px-4 py-3.5 text-[15px] font-semibold text-white shadow-lg shadow-[#0b1f3a]/20 transition hover:bg-[#14304f] disabled:cursor-not-allowed disabled:opacity-60"
              >
                {busy ? 'Signing you in…' : 'Sign in'}
                {!busy && <ArrowRight size={17} />}
              </button>

              <p className="mt-6 text-center text-xs text-[#6b7689]">
                Forgot your password? Contact your administrator.
              </p>
            </form>

            <div className="mt-6 flex items-center justify-center gap-2 text-xs text-[#6b7689]">
              <LockKeyhole size={13} />
              <span>Protected sign-in</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
