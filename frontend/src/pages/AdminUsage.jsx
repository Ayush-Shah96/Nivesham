import React, { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import {
  Activity,
  Check,
  CheckCircle2,
  Eye,
  KeyRound,
  LogIn,
  LogOut,
  RefreshCw,
  Save,
  ShieldCheck,
  Trash2,
  UserCheck,
  UserCog,
  UserPlus,
  Users,
} from "lucide-react";
import { Badge, Card, ErrorBox, Loading, PageHeader } from "../components/UI";
import { api } from "../lib/api";
import { UserSettingsPanel } from "./UserSettings";
import {
  ACCESS_APPS,
  defaultAccessForUser,
  permissionsForUser,
  readAppAccessStore,
  saveUserAppPermissions,
} from "../lib/appAccess";

const TABS = [
  { id: "profile", label: "User Settings", icon: UserCog },
  { id: "access", label: "App Access", icon: ShieldCheck },
  { id: "usage", label: "Usage & Sessions", icon: Activity },
  { id: "users", label: "Manage Users", icon: Users },
];

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString("en-IN", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function duration(minutes) {
  const value = Number(minutes || 0);
  if (!Number.isFinite(value)) return "0m";
  if (value < 60) return `${Math.round(value)}m`;
  const hours = Math.floor(value / 60);
  const mins = Math.round(value % 60);
  return `${hours}h ${mins}m`;
}

function TabButton({ active, icon: Icon, children, onClick }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`inline-flex items-center gap-2 rounded-xl px-3.5 py-2.5 text-sm font-semibold transition ${
        active
          ? "bg-navy-900 text-white shadow-sm"
          : "text-slate-600 hover:bg-slate-100 hover:text-navy-900"
      }`}
    >
      <Icon size={16} />
      {children}
    </button>
  );
}

function Metric({ icon: Icon, label, value, detail }) {
  return (
    <Card className="relative overflow-hidden p-5">
      <div className="absolute right-4 top-4 rounded-xl bg-slate-50 p-2.5 text-navy-700">
        <Icon size={18} />
      </div>
      <div className="text-[11px] font-bold uppercase tracking-[.14em] text-slate-500">{label}</div>
      <div className="mt-3 text-2xl font-extrabold tracking-tight text-navy-900">{value}</div>
      {detail ? <div className="mt-1 text-xs text-slate-500">{detail}</div> : null}
    </Card>
  );
}

function statusTone(active) {
  return active ? "green" : "slate";
}

export default function AdminUsage({ user }) {
  // `?tab=users` (used by the header "User Accounts" link) opens that tab directly.
  const [searchParams] = useSearchParams();
  const requestedTab = searchParams.get("tab");
  const [tab, setTab] = useState(() => (TABS.some((item) => item.id === requestedTab) ? requestedTab : "profile"));
  useEffect(() => {
    if (TABS.some((item) => item.id === requestedTab)) setTab(requestedTab);
  }, [requestedTab]);
  const [users, setUsers] = useState([]);
  const [accessDraft, setAccessDraft] = useState({});
  const [usage, setUsage] = useState(null);
  const [days, setDays] = useState(7);
  const [busy, setBusy] = useState(true);
  const [usageBusy, setUsageBusy] = useState(false);
  const [userBusy, setUserBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  const [editingUser, setEditingUser] = useState(null);
  const [newUser, setNewUser] = useState({
    username: "",
    password: "",
    role: "ANALYST",
  });

  const syncAccessDraft = useCallback((rows) => {
    const store = readAppAccessStore();
    const draft = {};
    rows.forEach((row) => {
      draft[String(row.id)] = permissionsForUser(row.id, store);
    });
    setAccessDraft(draft);
  }, []);

  // One backend request powers Users + App Access + Usage + Sessions.
  // The /admin/usage response already contains the complete user list, so the
  // settings page does not make a second /admin/users request during startup.
  const loadAdminData = useCallback(async ({ forceRefresh = false } = {}) => {
    if (user?.role !== "ADMIN") return;

    setUsageBusy(true);
    setBusy(false);
    setError("");

    try {
      const result = await api.adminUsage({ days, forceRefresh });
      const list = Array.isArray(result?.users) ? result.users : [];

      setUsage(result);
      setUsers(list);
      syncAccessDraft(list);
    } catch (err) {
      setError(err?.message || "Failed to load administration data");
    } finally {
      setUsageBusy(false);
    }
  }, [days, syncAccessDraft, user?.role]);

  const loadUsage = useCallback(async ({ forceRefresh = false } = {}) => {
    if (user?.role !== "ADMIN") return;

    setUsageBusy(true);
    setError("");

    try {
      const result = await api.adminUsage({ days, forceRefresh });
      const list = Array.isArray(result?.users) ? result.users : [];

      setUsage(result);
      setUsers(list);
      syncAccessDraft(list);
    } catch (err) {
      setError(err?.message || "Failed to refresh usage");
    } finally {
      setUsageBusy(false);
    }
  }, [days, syncAccessDraft, user?.role]);

  useEffect(() => {
    void loadAdminData();
  }, [loadAdminData]);

  useEffect(() => {
    if (tab !== "usage") return undefined;

    const timer = window.setInterval(() => {
      void loadUsage({ forceRefresh: true });
    }, 60_000);

    return () => window.clearInterval(timer);
  }, [loadUsage, tab]);

  const toggleAppAccess = (userId, appId) => {
    setSaved(false);
    setAccessDraft((prev) => {
      const current = prev[String(userId)] || defaultAccessForUser();
      return {
        ...prev,
        [String(userId)]: {
          ...current,
          [appId]: current[appId] === false,
        },
      };
    });
  };

  const saveAccess = () => {
    if (user?.role !== "ADMIN") return;
    setUserBusy(true);
    setError("");
    try {
      users.forEach((row) => {
        const permissions = accessDraft[String(row.id)] || defaultAccessForUser();
        if (!saveUserAppPermissions(row.id, permissions)) {
          throw new Error("Browser storage is unavailable. Access settings could not be saved.");
        }
      });
      setSaved(true);
    } catch (err) {
      setError(err?.message || "Failed to save access settings");
    } finally {
      setUserBusy(false);
    }
  };

  const refreshEverything = async () => {
    await loadAdminData({ forceRefresh: true });
  };

  const createUser = async (event) => {
    event.preventDefault();
    setUserBusy(true);
    setError("");
    try {
      await api.createUser(newUser);
      setNewUser({ username: "", password: "", role: "ANALYST" });
      await loadAdminData({ forceRefresh: true });
      setTab("users");
    } catch (err) {
      setError(err?.message || "Failed to create user");
    } finally {
      setUserBusy(false);
    }
  };

  const patchUser = async (row, patch) => {
    setUserBusy(true);
    setError("");
    try {
      await api.updateUser(row.id, patch);
      await loadAdminData({ forceRefresh: true });
      setEditingUser(null);
    } catch (err) {
      setError(err?.message || "Failed to update user");
    } finally {
      setUserBusy(false);
    }
  };

  const deactivateUser = async (row) => {
    if (!window.confirm(`Deactivate ${row.username}?`)) return;
    setUserBusy(true);
    setError("");
    try {
      await api.removeUser(row.id);
      await loadAdminData({ forceRefresh: true });
    } catch (err) {
      setError(err?.message || "Failed to deactivate user");
    } finally {
      setUserBusy(false);
    }
  };

  if (user?.role !== "ADMIN") return null;

  const usageUsers = Array.isArray(usage?.users) ? usage.users : [];
  const sessions = Array.isArray(usage?.sessions) ? usage.sessions : [];
  const metrics = usage?.metrics || {};

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow="Administration"
        title="Settings"
        description="Manage users, application access, authentication sessions, and platform usage from one admin workspace."
        action={
          <button
            type="button"
            onClick={() => void refreshEverything()}
            disabled={busy || usageBusy}
            className="inline-flex items-center gap-2 rounded-xl border border-slate-200 bg-white px-3.5 py-2.5 text-sm font-semibold text-slate-700 hover:bg-slate-50 disabled:opacity-50"
          >
            <RefreshCw size={15} className={busy || usageBusy ? "animate-spin" : ""} />
            Refresh
          </button>
        }
      />

      <Card className="overflow-hidden">
        <div className="flex flex-wrap gap-2 border-b border-slate-200 bg-slate-50/70 p-2">
          {TABS.map((item) => (
            <TabButton
              key={item.id}
              active={tab === item.id}
              icon={item.icon}
              onClick={() => setTab(item.id)}
            >
              {item.label}
            </TabButton>
          ))}
        </div>
      </Card>

      <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-slate-200 bg-white px-4 py-3 shadow-[0_4px_18px_rgba(15,23,42,.03)]">
        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-navy-900 text-xs font-bold text-white">
            {(user?.username || "A").slice(0, 2).toUpperCase()}
          </div>
          <div>
            <div className="text-sm font-bold text-navy-900">{user?.username || "Administrator"}</div>
            <div className="text-xs text-slate-500">Authenticated administrator · full administration access</div>
          </div>
        </div>
        <div className="flex items-center gap-2 text-xs font-semibold text-slate-500">
          <ShieldCheck size={14} className="text-emerald-600" />
          Authentication & access controls
        </div>
      </div>

      {error ? <ErrorBox error={error} /> : null}

      {tab === "profile" ? <UserSettingsPanel user={user} /> : null}

      {tab === "access" ? (
        <AccessTab
          users={users}
          draft={accessDraft}
          busy={busy || userBusy}
          saved={saved}
          onToggle={toggleAppAccess}
          onSave={saveAccess}
          currentUserId={user?.id}
        />
      ) : null}

      {tab === "usage" ? (
        <UsageTab
          days={days}
          setDays={setDays}
          data={usage}
          busy={usageBusy && !usage}
          users={usageUsers}
          sessions={sessions}
        />
      ) : null}

      {tab === "users" ? (
        <ManageUsersTab
          users={users}
          busy={busy || userBusy}
          editingUser={editingUser}
          setEditingUser={setEditingUser}
          newUser={newUser}
          setNewUser={setNewUser}
          onCreate={createUser}
          onPatch={patchUser}
          onDeactivate={deactivateUser}
          currentUserId={user?.id}
        />
      ) : null}
    </div>
  );
}

function AccessTab({ users, draft, busy, saved, onToggle, onSave, currentUserId }) {
  return (
    <div className="space-y-5">
      <Card>
        <div className="flex flex-col gap-3 border-b border-slate-200 px-5 py-4 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h2 className="font-bold text-navy-900">Application access</h2>
            <p className="mt-0.5 text-xs text-slate-500">
              Control which applications appear in each user's workspace. Role-based backend authorization remains the security boundary.
            </p>
          </div>
          <Badge tone="blue">{users.length} users</Badge>
        </div>

        <div className="h-[560px] overflow-auto p-5">
          {busy && !users.length ? (
            <Loading />
          ) : users.length ? (
            <div className="space-y-4">
              {users.map((row) => {
                const permissions = draft[String(row.id)] || defaultAccessForUser();
                const enabledCount = ACCESS_APPS.filter((app) => permissions[app.id] !== false).length;
                return (
                  <div
                    key={row.id}
                    className="rounded-2xl border border-slate-200 bg-white p-4 shadow-[0_4px_18px_rgba(15,23,42,.03)]"
                  >
                    <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-bold text-navy-900">{row.username}</span>
                        {row.id === currentUserId ? <Badge tone="green">You</Badge> : null}
                        <Badge tone={row.role === "ADMIN" ? "blue" : "slate"}>{row.role}</Badge>
                        <Badge tone={row.is_active ? "green" : "red"}>{row.is_active ? "Enabled" : "Disabled"}</Badge>
                      </div>
                      <span className="text-xs font-semibold text-slate-500">
                        {enabledCount} / {ACCESS_APPS.length} apps enabled
                      </span>
                    </div>

                    <div className="mt-4 grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
                      {ACCESS_APPS.map((app) => {
                        const enabled = permissions[app.id] !== false;
                        return (
                          <label
                            key={app.id}
                            className={`flex cursor-pointer items-center gap-3 rounded-xl border px-3 py-3 transition ${
                              enabled
                                ? "border-slate-200 bg-slate-50 hover:border-navy-200 hover:bg-white"
                                : "border-slate-200 bg-slate-100/70 opacity-75"
                            }`}
                          >
                            <input
                              type="checkbox"
                              checked={enabled}
                              onChange={() => onToggle(row.id, app.id)}
                              className="h-4 w-4 rounded border-slate-300 text-navy-900 focus:ring-navy-200"
                            />
                            <span className="min-w-0">
                              <span className="block truncate text-xs font-semibold text-slate-700">{app.label}</span>
                              <span className="mt-0.5 block text-[10px] text-slate-400">{app.group}</span>
                            </span>
                          </label>
                        );
                      })}
                    </div>
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="rounded-xl border border-dashed border-slate-300 p-10 text-center text-sm text-slate-500">No users found.</div>
          )}
        </div>

        <div className="flex flex-col gap-3 border-t border-slate-200 bg-slate-50/60 px-5 py-4 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex items-start gap-2 text-xs text-slate-500">
            <Eye size={15} className="mt-0.5 shrink-0" />
            <span>These settings control workspace visibility. Backend roles remain the actual security boundary.</span>
          </div>
          <div className="flex items-center gap-2">
            {saved ? (
              <span className="inline-flex items-center gap-1.5 text-xs font-semibold text-emerald-700">
                <CheckCircle2 size={15} /> Saved
              </span>
            ) : null}
            <button
              type="button"
              onClick={onSave}
              disabled={busy || !users.length}
              className="inline-flex items-center gap-2 rounded-xl bg-navy-900 px-4 py-2.5 text-sm font-semibold text-white hover:opacity-95 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <Save size={15} />
              {busy ? "Saving…" : "Save access settings"}
            </button>
          </div>
        </div>
      </Card>
    </div>
  );
}

function UsageTab({ days, setDays, data, busy, users, sessions }) {
  const [teamFilter, setTeamFilter] = useState("ALL");
  const metrics = data?.metrics || {};
  const rankedUsers = useMemo(
    () => [...users].sort((a, b) => String(a.username || "").localeCompare(String(b.username || ""))),
    [users],
  );

  // Build a seven-day app-usage matrix from the session telemetry returned by
  // /admin/usage. Session durations are attributed to the local calendar day
  // on which each session started, matching the available backend fields.
  const appUsage = useMemo(() => {
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const dateColumns = Array.from({ length: 7 }, (_, index) => {
      const date = new Date(today);
      date.setDate(today.getDate() - (6 - index));
      const key = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
      return {
        key,
        label: date.toLocaleDateString("en-IN", { weekday: "short", day: "2-digit", month: "short" }),
      };
    });
    const teamUsers = users.filter((row) => {
      const role = String(row.role || "").toUpperCase();
      return teamFilter === "ALL" || role === teamFilter;
    });
    const userRows = teamUsers.map((row) => ({
      ...row,
      dailyMinutes: Object.fromEntries(dateColumns.map(({ key }) => [key, 0])),
      appMinutes: {},
    }));
    const byId = new Map(userRows.map((row) => [String(row.id), row]));
    const byUsername = new Map(userRows.map((row) => [String(row.username || "").toLowerCase(), row]));

    sessions.forEach((session) => {
      const row = (session.user_id != null ? byId.get(String(session.user_id)) : null)
        || byUsername.get(String(session.username || "").toLowerCase());
      if (!row) return;
      const validDays = new Set(dateColumns.map((column) => column.key));
      const hasDailyUsageField = Boolean(session.daily_app_usage_seconds && typeof session.daily_app_usage_seconds === "object");
      const dailyUsage = hasDailyUsageField ? session.daily_app_usage_seconds : {};
      let hasWindowBucket = false;

      Object.entries(dailyUsage).forEach(([dayKey, byApp]) => {
        if (!validDays.has(dayKey) || !byApp || typeof byApp !== "object") return;
        Object.entries(byApp).forEach(([appNameRaw, secondsRaw]) => {
          const seconds = Math.max(0, Number(secondsRaw || 0));
          if (!Number.isFinite(seconds) || seconds <= 0) return;
          const minutes = seconds / 60;
          const appName = String(appNameRaw || "Workspace");
          row.dailyMinutes[dayKey] += minutes;
          row.appMinutes[appName] = (row.appMinutes[appName] || 0) + minutes;
          hasWindowBucket = true;
        });
      });

      // Legacy session counters have no stored date attribution. Preserve them
      // rather than dropping them, but only bucket the legacy baseline by login
      // date; all new tracking is shown from exact per-day/per-app buckets above.
      const legacySeconds = Math.max(0, Number(session.legacy_unbucketed_seconds || 0));
      const sessionDate = session.login_at ? new Date(session.login_at) : null;
      if (legacySeconds > 0 && sessionDate && !Number.isNaN(sessionDate.getTime())) {
        const key = `${sessionDate.getFullYear()}-${String(sessionDate.getMonth() + 1).padStart(2, "0")}-${String(sessionDate.getDate()).padStart(2, "0")}`;
        if (validDays.has(key)) {
          const minutes = legacySeconds / 60;
          const appName = String(session.current_app || "Legacy / Unattributed");
          row.dailyMinutes[key] += minutes;
          row.appMinutes[appName] = (row.appMinutes[appName] || 0) + minutes;
        }
      } else if (!hasWindowBucket && !hasDailyUsageField && sessionDate && !Number.isNaN(sessionDate.getTime())) {
        // Backwards compatibility with old API response shapes during deployment.
        const key = `${sessionDate.getFullYear()}-${String(sessionDate.getMonth() + 1).padStart(2, "0")}-${String(sessionDate.getDate()).padStart(2, "0")}`;
        if (validDays.has(key)) {
          const minutes = Math.max(0, Number(session.active_minutes || 0));
          const appName = String(session.current_app || session.app || "Workspace");
          row.dailyMinutes[key] += minutes;
          row.appMinutes[appName] = (row.appMinutes[appName] || 0) + minutes;
        }
      }
    });

    const getTopApp = (appMinutes) => Object.entries(appMinutes || {})
      .sort((a, b) => b[1] - a[1])[0]?.[0] || "—";
    const totals = Object.fromEntries(dateColumns.map(({ key }) => [key, 0]));
    const totalApps = {};
    userRows.forEach((row) => {
      dateColumns.forEach(({ key }) => { totals[key] += row.dailyMinutes[key] || 0; });
      Object.entries(row.appMinutes).forEach(([app, minutes]) => {
        totalApps[app] = (totalApps[app] || 0) + minutes;
      });
    });
    return {
      dateColumns,
      rows: userRows.sort((a, b) => a.username.localeCompare(b.username)),
      totals,
      topApp: getTopApp(totalApps),
      totalMinutes: Object.values(totals).reduce((sum, value) => sum + value, 0),
      getTopApp,
    };
  }, [users, sessions, teamFilter]);

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 text-sm font-bold text-navy-900">
            <Activity size={15} className="text-navy-700" />
            Usage & authentication overview
          </div>
          <div className="mt-0.5 text-xs text-slate-500">Login history, logout state, engaged time, current application, and session activity.</div>
        </div>
        <select
          value={days}
          onChange={(event) => setDays(Number(event.target.value))}
          className="rounded-xl border border-slate-200 bg-white px-3 py-2.5 text-sm font-semibold text-slate-700 outline-none focus:border-navy-700 focus:ring-2 focus:ring-navy-100"
        >
          <option value={7}>Last 7 days</option>
          <option value={30}>Last 30 days</option>
          <option value={90}>Last 90 days</option>
          <option value={365}>Last 365 days</option>
        </select>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Metric icon={Users} label="Total users" value={metrics.total_users ?? 0} />
        <Metric icon={UserCheck} label="Unique users" value={metrics.unique_users ?? 0} detail={`in selected ${days}-day window`} />
        <Metric icon={Activity} label="Active now" value={metrics.active_users_now ?? 0} detail="heartbeat within 3 min" />
        <Metric icon={LogIn} label="Sessions" value={metrics.sessions ?? 0} detail={`started in ${days} days`} />
      </div>

      {busy ? <Card><Loading /></Card> : null}

      <Card>
        <div className="flex flex-col gap-3 border-b border-slate-100 px-5 py-4 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h2 className="font-bold text-navy-900">App usage · last 7 days</h2>
            <p className="mt-0.5 text-xs text-slate-500">Daily tracked active time per member, plus the most-used app by total time for the selected team.</p>
          </div>
          <label className="flex items-center gap-2 text-xs font-semibold text-slate-600">
            Team
            <select
              value={teamFilter}
              onChange={(event) => setTeamFilter(event.target.value)}
              className="rounded-xl border border-slate-200 bg-white px-3 py-2.5 text-sm font-semibold text-slate-700 outline-none focus:border-navy-700 focus:ring-2 focus:ring-navy-100"
            >
              <option value="ALL">All teams</option>
              <option value="INVESTMENT">Investment</option>
              <option value="ANALYST">Analyst</option>
            </select>
          </label>
        </div>
        <div className="overflow-auto">
          <table className="min-w-[1050px] w-full text-left text-sm">
            <thead className="border-b border-slate-200 bg-slate-50">
              <tr>
                <th className="whitespace-nowrap px-4 py-3 text-[11px] font-bold uppercase tracking-wide text-slate-500">Team member</th>
                {appUsage.dateColumns.map((column) => (
                  <th key={column.key} className="whitespace-nowrap px-4 py-3 text-[11px] font-bold uppercase tracking-wide text-slate-500">{column.label}</th>
                ))}
                <th className="whitespace-nowrap px-4 py-3 text-[11px] font-bold uppercase tracking-wide text-slate-500">Maximum used app</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {appUsage.rows.map((row) => (
                <tr key={row.id} className="hover:bg-slate-50">
                  <td className="whitespace-nowrap px-4 py-3">
                    <div className="font-semibold text-navy-900">{row.username}</div>
                    <div className="text-[11px] text-slate-400">{row.role === "INVESTMENT" ? "Investment" : row.role === "ANALYST" ? "Analyst" : row.role}</div>
                  </td>
                  {appUsage.dateColumns.map((column) => (
                    <td key={column.key} className="whitespace-nowrap px-4 py-3 font-medium text-slate-700">{duration(row.dailyMinutes[column.key])}</td>
                  ))}
                  <td className="px-4 py-3 font-semibold text-slate-700">{appUsage.getTopApp(row.appMinutes)}</td>
                </tr>
              ))}
              <tr className="bg-slate-50 font-bold">
                <td className="whitespace-nowrap px-4 py-3 text-navy-900">TEAM TOTAL</td>
                {appUsage.dateColumns.map((column) => (
                  <td key={column.key} className="whitespace-nowrap px-4 py-3 text-navy-900">{duration(appUsage.totals[column.key])}</td>
                ))}
                <td className="px-4 py-3 text-navy-900">{appUsage.topApp}</td>
              </tr>
              {!appUsage.rows.length ? (
                <tr><td colSpan={appUsage.dateColumns.length + 2} className="px-4 py-8 text-center text-sm text-slate-500">No members found for this team.</td></tr>
              ) : null}
            </tbody>
          </table>
        </div>
        <div className="border-t border-slate-100 px-5 py-3 text-[11px] text-slate-400">
          Usage is displayed separately for each IST calendar day. No overall or cumulative active-time KPI is shown.
        </div>
      </Card>

      <Card>
        <div className="flex items-center justify-between border-b border-slate-100 px-5 py-4">
          <div>
            <h2 className="font-bold text-navy-900">User activity</h2>
            <p className="mt-0.5 text-xs text-slate-500">User status and authentication history. Usage time is shown only in the day-by-day table above.</p>
          </div>
          <Badge tone="blue">Admin only</Badge>
        </div>

        <div className="h-[420px] overflow-auto">
          <table className="min-w-[1180px] w-full text-left text-sm">
            <thead className="sticky top-0 z-10 border-b border-slate-200 bg-slate-50">
              <tr>
                {["User", "Role", "Status", "Sessions", "Last login", "Last logout", "Current state"].map((heading) => (
                  <th key={heading} className="whitespace-nowrap px-5 py-3 text-[11px] font-bold uppercase tracking-wide text-slate-500">{heading}</th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {rankedUsers.map((row) => (
                <tr key={row.id} className="transition hover:bg-slate-50">
                  <td className="px-5 py-3.5">
                    <div className="font-semibold text-navy-900">{row.username}</div>
                    <div className="text-[11px] text-slate-400">User #{row.id}</div>
                  </td>
                  <td className="px-5 py-3.5"><Badge tone={row.role === "ADMIN" ? "blue" : "slate"}>{row.role}</Badge></td>
                  <td className="px-5 py-3.5"><Badge tone={row.is_active ? "green" : "red"}>{row.is_active ? "Enabled" : "Disabled"}</Badge></td>
                  <td className="px-5 py-3.5 font-semibold">{row.sessions_count}</td>
                  <td className="whitespace-nowrap px-5 py-3.5 text-slate-600">{formatDate(row.last_login_at)}</td>
                  <td className="whitespace-nowrap px-5 py-3.5 text-slate-600">{formatDate(row.last_logout_at)}</td>
                  <td className="px-5 py-3.5">
                    {row.currently_active ? (
                      <div>
                        <Badge tone="green">Online</Badge>
                        <div className="mt-1 text-[11px] text-slate-500">{row.current_app || "Workspace"} · {duration(row.current_session_active_minutes)}</div>
                      </div>
                    ) : row.last_seen_at ? (
                      <div>
                        <Badge tone="slate">Offline</Badge>
                        <div className="mt-1 text-[11px] text-slate-500">Seen {formatDate(row.last_seen_at)}</div>
                      </div>
                    ) : (
                      <Badge tone="slate">Never logged in</Badge>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      <Card>
        <div className="flex items-center justify-between border-b border-slate-100 px-5 py-4">
          <div>
            <h2 className="font-bold text-navy-900">Recent sessions</h2>
            <p className="mt-0.5 text-xs text-slate-500">Individual session login/logout history; time spent is not totaled here.</p>
          </div>
          <div className="text-xs text-slate-400">Latest {Math.min(sessions.length, 500)}</div>
        </div>

        <div className="h-[420px] overflow-auto">
          <table className="min-w-[1050px] w-full text-left text-sm">
            <thead className="sticky top-0 z-10 border-b border-slate-200 bg-slate-50">
              <tr>
                {["User", "Login time", "Logout time", "Last seen", "App", "Status"].map((heading) => (
                  <th key={heading} className="whitespace-nowrap px-5 py-3 text-[11px] font-bold uppercase tracking-wide text-slate-500">{heading}</th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {sessions.map((row) => (
                <tr key={row.id} className="transition hover:bg-slate-50">
                  <td className="px-5 py-3.5"><div className="font-semibold text-navy-900">{row.username}</div><div className="text-[11px] text-slate-400">{row.role}</div></td>
                  <td className="whitespace-nowrap px-5 py-3.5">{formatDate(row.login_at)}</td>
                  <td className="whitespace-nowrap px-5 py-3.5">{formatDate(row.logout_at)}</td>
                  <td className="whitespace-nowrap px-5 py-3.5 text-slate-600">{formatDate(row.last_seen_at)}</td>
                  <td className="px-5 py-3.5 text-slate-600">{row.current_app || "Workspace"}</td>
                  <td className="px-5 py-3.5"><Badge tone={statusTone(row.active)}>{row.active ? "Online" : "Closed / stale"}</Badge></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}

function ManageUsersTab({ users, busy, editingUser, setEditingUser, newUser, setNewUser, onCreate, onPatch, onDeactivate, currentUserId }) {
  return (
    <div className="grid gap-5 xl:grid-cols-[320px_minmax(0,1fr)]">
      <Card className="h-fit overflow-hidden">
        <div className="border-b border-slate-100 bg-slate-50/70 px-5 py-4">
          <div className="flex items-center gap-2">
            <UserPlus size={17} className="text-navy-700" />
            <h2 className="font-bold text-navy-900">Add user</h2>
          </div>
          <p className="mt-1 text-xs text-slate-500">Create a new platform account and assign its initial role.</p>
        </div>
        <form onSubmit={onCreate} className="space-y-3 p-5">
          <label className="block text-xs font-semibold text-slate-600">
            Username
            <input
              value={newUser.username}
              onChange={(event) => setNewUser({ ...newUser, username: event.target.value })}
              placeholder="Username"
              minLength={2}
              className="mt-1.5 w-full rounded-xl border border-slate-200 bg-white px-3 py-2.5 text-sm outline-none focus:border-navy-700 focus:ring-2 focus:ring-navy-100"
              required
            />
          </label>
          <label className="block text-xs font-semibold text-slate-600">
            Temporary password
            <input
              type="password"
              value={newUser.password}
              onChange={(event) => setNewUser({ ...newUser, password: event.target.value })}
              placeholder="Minimum 6 characters"
              minLength={6}
              className="mt-1.5 w-full rounded-xl border border-slate-200 bg-white px-3 py-2.5 text-sm outline-none focus:border-navy-700 focus:ring-2 focus:ring-navy-100"
              required
            />
          </label>
          <label className="block text-xs font-semibold text-slate-600">
            Role
            <select
              value={newUser.role}
              onChange={(event) => setNewUser({ ...newUser, role: event.target.value })}
              className="mt-1.5 w-full rounded-xl border border-slate-200 bg-white px-3 py-2.5 text-sm outline-none focus:border-navy-700"
            >
              <option value="ANALYST">ANALYST</option>
              <option value="INVESTMENT">INVESTMENT</option>
              <option value="ADMIN">ADMIN</option>
            </select>
          </label>
          <button
            type="submit"
            disabled={busy}
            className="inline-flex w-full items-center justify-center gap-2 rounded-xl bg-navy-900 px-4 py-2.5 text-sm font-semibold text-white hover:opacity-95 disabled:opacity-50"
          >
            <UserPlus size={15} /> {busy ? "Creating…" : "Create user"}
          </button>
        </form>
      </Card>

      <Card className="overflow-hidden">
        <div className="flex items-center justify-between border-b border-slate-100 px-5 py-4">
          <div>
            <h2 className="font-bold text-navy-900">Manage users</h2>
            <p className="mt-0.5 text-xs text-slate-500">Update roles, change passwords, enable/disable accounts, or review application access.</p>
          </div>
          <Badge tone="blue">{users.length} accounts</Badge>
        </div>

        <div className="h-[560px] overflow-auto">
          <table className="min-w-[1050px] w-full text-left text-sm">
            <thead className="sticky top-0 z-10 border-b border-slate-200 bg-slate-50">
              <tr>
                {['User', 'Role', 'Status', 'Account actions', 'Security'].map((heading) => (
                  <th key={heading} className="whitespace-nowrap px-5 py-3 text-[11px] font-bold uppercase tracking-wide text-slate-500">{heading}</th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {users.map((row) => (
                <tr key={row.id} className="align-top transition hover:bg-slate-50">
                  <td className="px-5 py-4">
                    <div className="font-semibold text-navy-900">{row.username}</div>
                    <div className="mt-1 text-[11px] text-slate-400">User #{row.id}{row.id === currentUserId ? ' · You' : ''}</div>
                  </td>
                  <td className="px-5 py-4">
                    <select
                      value={row.role}
                      onChange={(event) => void onPatch(row, { role: event.target.value })}
                      disabled={busy}
                      className="rounded-lg border border-slate-200 bg-white px-2.5 py-2 text-xs font-semibold text-slate-700 outline-none focus:border-navy-700 disabled:opacity-50"
                    >
                      <option value="ANALYST">ANALYST</option>
                      <option value="INVESTMENT">INVESTMENT</option>
                      <option value="ADMIN">ADMIN</option>
                    </select>
                  </td>
                  <td className="px-5 py-4">
                    <div className="space-y-2">
                      <Badge tone={row.is_active ? "green" : "red"}>{row.is_active ? "Enabled" : "Disabled"}</Badge>
                      {row.id !== currentUserId ? (
                        <select
                          value={row.is_active ? "true" : "false"}
                          onChange={(event) => void onPatch(row, { is_active: event.target.value === "true" })}
                          disabled={busy}
                          className="block rounded-lg border border-slate-200 bg-white px-2.5 py-2 text-xs outline-none focus:border-navy-700 disabled:opacity-50"
                        >
                          <option value="true">Active</option>
                          <option value="false">Inactive</option>
                        </select>
                      ) : <div className="text-[11px] text-slate-400">Your admin account cannot be disabled here.</div>}
                    </div>
                  </td>
                  <td className="px-5 py-4">
                    {editingUser === row.id ? (
                      <div className="space-y-2">
                        <button
                          type="button"
                          onClick={() => setEditingUser(null)}
                          className="rounded-lg border border-slate-200 px-3 py-2 text-xs font-semibold text-slate-700 hover:bg-white"
                        >
                          Cancel
                        </button>
                      </div>
                    ) : (
                      <button
                        type="button"
                        onClick={() => setEditingUser(row.id)}
                        className="rounded-lg border border-slate-200 px-3 py-2 text-xs font-semibold text-slate-700 hover:bg-white"
                      >
                        Edit password
                      </button>
                    )}
                    {row.is_active && row.id !== currentUserId ? (
                      <button
                        type="button"
                        onClick={() => void onDeactivate(row)}
                        disabled={busy}
                        className="mt-2 inline-flex items-center gap-1.5 rounded-lg border border-red-200 px-3 py-2 text-xs font-semibold text-red-600 hover:bg-red-50 disabled:opacity-50"
                      >
                        <Trash2 size={13} /> Deactivate
                      </button>
                    ) : null}
                  </td>
                  <td className="px-5 py-4">
                    {editingUser === row.id ? (
                      <PasswordEditor row={row} busy={busy} onPatch={onPatch} />
                    ) : (
                      <div className="flex items-center gap-2 text-xs text-slate-500">
                        <KeyRound size={14} />
                        Password is hidden
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}

function PasswordEditor({ row, busy, onPatch }) {
  const [value, setValue] = useState("");
  return (
    <div className="min-w-[230px]">
      <input
        type="password"
        value={value}
        onChange={(event) => setValue(event.target.value)}
        placeholder="New password"
        minLength={6}
        className="w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs outline-none focus:border-navy-700"
      />
      <button
        type="button"
        onClick={() => {
          if (value.trim().length < 6) return;
          void onPatch(row, { password: value.trim() });
          setValue("");
        }}
        disabled={busy || value.trim().length < 6}
        className="mt-2 inline-flex items-center gap-1.5 rounded-lg bg-navy-900 px-3 py-2 text-xs font-semibold text-white disabled:opacity-50"
      >
        <Check size={13} /> Update password
      </button>
    </div>
  );
}