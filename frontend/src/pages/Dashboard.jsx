import React, { useEffect, useMemo, useState } from "react";
import {
  ArrowUpRight,
  BellRing,
  Bookmark,
  CalendarClock,
  Check,
  Circle,
  Clock3,
  ExternalLink,
  ListTodo,
  Plus,
  Trash2,
  X,
} from "lucide-react";

import { Card, PageHeader } from "../components/UI";

const TODO_KEY = (userId) => `ii_dashboard_todos_${userId || "guest"}`;
const REMINDER_KEY = (userId) => `ii_dashboard_reminders_${userId || "guest"}`;
// Keep the existing key so bookmarks saved by the previous Dashboard remain available.
const BOOKMARK_KEY = (userId) => `ii_dashboard_bookmarks_${userId || "guest"}`;

const DEFAULT_BOOKMARKS = [
  { id: "default-bse", name: "BSE", href: "https://www.bseindia.com/" },
  { id: "default-nse", name: "NSE", href: "https://www.nseindia.com/" },
  { id: "default-trendlyne", name: "Trendlyne", href: "https://trendlyne.com/" },
];

function readJson(key, fallback) {
  try {
    const value = JSON.parse(window.localStorage.getItem(key) || "null");
    return value ?? fallback;
  } catch {
    return fallback;
  }
}

function writeJson(key, value) {
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Keep the dashboard usable if browser storage is disabled or full.
  }
}

function greetingForNow() {
  const hour = new Date().getHours();
  if (hour < 12) return "Good morning";
  if (hour < 17) return "Good afternoon";
  return "Good evening";
}

function formatDateTime(value) {
  if (!value) return "No date set";
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "Invalid date";
  return date.toLocaleString("en-IN", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function reminderState(reminder) {
  if (reminder.done) return { label: "Completed", classes: "bg-emerald-50 text-emerald-700" };
  const when = new Date(reminder.when).getTime();
  if (!Number.isFinite(when)) return { label: "Date not set", classes: "bg-slate-100 text-slate-600" };
  if (when < Date.now()) return { label: "Overdue", classes: "bg-red-50 text-red-700" };
  if (when - Date.now() < 60 * 60 * 1000) return { label: "Due soon", classes: "bg-amber-50 text-amber-700" };
  return { label: "Upcoming", classes: "bg-blue-50 text-blue-700" };
}

function SectionCard({ title, subtitle, icon, children, className = "" }) {
  return (
    <Card className={`flex min-h-0 min-w-0 flex-col overflow-hidden ${className}`}>
      <div className="flex items-start gap-3 border-b border-slate-200 bg-slate-50/80 px-5 py-4">
        <div className="grid h-10 w-10 shrink-0 place-items-center rounded-xl border border-slate-200 bg-white text-slate-700">
          {icon}
        </div>
        <div className="min-w-0 flex-1">
          <h2 className="text-sm font-extrabold tracking-tight text-slate-950">{title}</h2>
          {subtitle && <p className="mt-1 text-xs text-slate-500">{subtitle}</p>}
        </div>
      </div>
      <div className="flex min-h-0 flex-1 flex-col p-4 sm:p-5">{children}</div>
    </Card>
  );
}

export default function Dashboard({ user }) {
  const userId = user?.id;
  const displayName = user?.username || user?.name || "there";

  const [todos, setTodos] = useState(() => {
    const saved = readJson(TODO_KEY(userId), []);
    return Array.isArray(saved) ? saved : [];
  });
  const [todoText, setTodoText] = useState("");

  const [reminders, setReminders] = useState(() => {
    const saved = readJson(REMINDER_KEY(userId), []);
    return Array.isArray(saved) ? saved : [];
  });
  const [reminderText, setReminderText] = useState("");
  const [reminderWhen, setReminderWhen] = useState("");

  const [bookmarks, setBookmarks] = useState(() => {
    const saved = readJson(BOOKMARK_KEY(userId), null);
    return Array.isArray(saved) ? saved : DEFAULT_BOOKMARKS;
  });
  const [bookmarkName, setBookmarkName] = useState("");
  const [bookmarkUrl, setBookmarkUrl] = useState("");
  const [bookmarkError, setBookmarkError] = useState("");

  useEffect(() => {
    const savedTodos = readJson(TODO_KEY(userId), []);
    setTodos(Array.isArray(savedTodos) ? savedTodos : []);
    const savedReminders = readJson(REMINDER_KEY(userId), []);
    setReminders(Array.isArray(savedReminders) ? savedReminders : []);
    const savedBookmarks = readJson(BOOKMARK_KEY(userId), null);
    setBookmarks(Array.isArray(savedBookmarks) ? savedBookmarks : DEFAULT_BOOKMARKS);
  }, [userId]);

  useEffect(() => writeJson(TODO_KEY(userId), todos), [todos, userId]);
  useEffect(() => writeJson(REMINDER_KEY(userId), reminders), [reminders, userId]);
  useEffect(() => writeJson(BOOKMARK_KEY(userId), bookmarks), [bookmarks, userId]);

  const openTodoCount = todos.filter((item) => !item.done).length;
  const sortedReminders = useMemo(
    () => [...reminders].sort((a, b) => {
      if (a.done !== b.done) return a.done ? 1 : -1;
      const dateA = new Date(a.when || 0).getTime();
      const dateB = new Date(b.when || 0).getTime();
      return (Number.isFinite(dateA) ? dateA : 0) - (Number.isFinite(dateB) ? dateB : 0);
    }),
    [reminders]
  );

  const addTodo = (event) => {
    event.preventDefault();
    const text = todoText.trim();
    if (!text) return;
    setTodos((current) => [
      ...current,
      { id: `${Date.now()}-${Math.random().toString(36).slice(2)}`, text, done: false },
    ].slice(-100));
    setTodoText("");
  };

  const toggleTodo = (id) => {
    setTodos((current) => current.map((item) => item.id === id ? { ...item, done: !item.done } : item));
  };

  const addReminder = (event) => {
    event.preventDefault();
    const text = reminderText.trim();
    if (!text || !reminderWhen) return;
    setReminders((current) => [
      ...current,
      { id: `${Date.now()}-${Math.random().toString(36).slice(2)}`, text, when: reminderWhen, done: false },
    ].slice(-100));
    setReminderText("");
    setReminderWhen("");
  };

  const addBookmark = (event) => {
    event.preventDefault();
    const name = bookmarkName.trim();
    const rawUrl = bookmarkUrl.trim();
    if (!name || !rawUrl) return;

    const candidate = /^https?:\/\//i.test(rawUrl) ? rawUrl : `https://${rawUrl}`;
    try {
      const parsed = new URL(candidate);
      if (!(["http:", "https:"].includes(parsed.protocol))) throw new Error("Unsupported URL protocol");
      setBookmarks((current) => [
        ...current,
        { id: `${Date.now()}-${Math.random().toString(36).slice(2)}`, name, href: parsed.href },
      ].slice(-50));
      setBookmarkName("");
      setBookmarkUrl("");
      setBookmarkError("");
    } catch {
      setBookmarkError("Enter a valid website URL, such as https://www.nseindia.com");
    }
  };

  return (
    <div className="w-full min-w-0 space-y-6 overflow-x-hidden">
      <PageHeader
        eyebrow="Personal workspace"
        title={<>{greetingForNow()}, <span className="text-slate-950">{displayName}</span></>}
        description="Your to-dos, reminders and bookmarked links in one place."
      />

      <div className="grid min-w-0 grid-cols-1 items-stretch gap-5 xl:grid-cols-3">
        <SectionCard
          title="To Do List"
          subtitle={`${openTodoCount} open ${openTodoCount === 1 ? "task" : "tasks"}`}
          icon={<ListTodo size={19} />}
          className="min-h-[420px]"
        >
          <form onSubmit={addTodo} className="flex gap-2">
            <input
              value={todoText}
              onChange={(event) => setTodoText(event.target.value)}
              placeholder="What needs to get done?"
              aria-label="New task"
              className="min-w-0 flex-1 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-sm outline-none transition focus:border-slate-400 focus:bg-white"
              maxLength={240}
            />
            <button
              type="submit"
              disabled={!todoText.trim()}
              aria-label="Add task"
              className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-slate-950 text-white transition hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-40"
            >
              <Plus size={17} />
            </button>
          </form>

          <div className="mt-4 flex-1 space-y-2 overflow-y-auto pr-1">
            {todos.length ? todos.map((item) => (
              <div key={item.id} className="flex items-center gap-3 rounded-xl border border-slate-100 bg-white px-3 py-3 hover:border-slate-200">
                <button
                  type="button"
                  onClick={() => toggleTodo(item.id)}
                  aria-label={item.done ? `Reopen ${item.text}` : `Complete ${item.text}`}
                  className={`grid h-6 w-6 shrink-0 place-items-center rounded-full border transition ${item.done ? "border-emerald-600 bg-emerald-600 text-white" : "border-slate-300 bg-white text-transparent hover:border-slate-500"}`}
                >
                  <Check size={13} />
                </button>
                <span className={`min-w-0 flex-1 break-words text-sm ${item.done ? "text-slate-400 line-through" : "text-slate-700"}`}>
                  {item.text}
                </span>
                <button
                  type="button"
                  onClick={() => setTodos((current) => current.filter((row) => row.id !== item.id))}
                  aria-label={`Delete task ${item.text}`}
                  className="rounded-lg p-1.5 text-slate-400 transition hover:bg-red-50 hover:text-red-600"
                >
                  <Trash2 size={15} />
                </button>
              </div>
            )) : (
              <div className="flex h-40 flex-col items-center justify-center text-center text-slate-400">
                <Circle size={25} strokeWidth={1.4} />
                <p className="mt-3 text-sm font-medium text-slate-600">Your list is clear</p>
                <p className="mt-1 text-xs">Add a task above to get started.</p>
              </div>
            )}
          </div>
        </SectionCard>

        <SectionCard
          title="Reminder"
          subtitle="Keep important follow-ups on time"
          icon={<BellRing size={19} />}
          className="min-h-[420px]"
        >
          <form onSubmit={addReminder} className="space-y-2.5">
            <input
              value={reminderText}
              onChange={(event) => setReminderText(event.target.value)}
              placeholder="What should you remember?"
              aria-label="Reminder text"
              maxLength={240}
              className="w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-sm outline-none transition focus:border-slate-400 focus:bg-white"
            />
            <div className="flex gap-2">
              <input
                type="datetime-local"
                value={reminderWhen}
                onChange={(event) => setReminderWhen(event.target.value)}
                aria-label="Reminder date and time"
                className="min-w-0 flex-1 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-xs outline-none transition focus:border-slate-400 focus:bg-white"
              />
              <button
                type="submit"
                disabled={!reminderText.trim() || !reminderWhen}
                className="inline-flex shrink-0 items-center gap-2 rounded-xl bg-slate-950 px-3 text-xs font-semibold text-white transition hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-40"
              >
                <Plus size={15} /> Add
              </button>
            </div>
          </form>

          <div className="mt-4 flex-1 space-y-2 overflow-y-auto pr-1">
            {sortedReminders.length ? sortedReminders.map((reminder) => {
              const status = reminderState(reminder);
              return (
                <div key={reminder.id} className={`rounded-xl border px-3 py-3 ${reminder.done ? "border-slate-100 bg-slate-50/70" : "border-slate-200 bg-white"}`}>
                  <div className="flex items-start gap-2.5">
                    <div className="mt-0.5 text-slate-400"><CalendarClock size={16} /></div>
                    <div className="min-w-0 flex-1">
                      <p className={`break-words text-sm font-semibold ${reminder.done ? "text-slate-400 line-through" : "text-slate-800"}`}>{reminder.text}</p>
                      <p className="mt-1 flex items-center gap-1.5 text-xs text-slate-500">
                        <Clock3 size={12} /> {formatDateTime(reminder.when)}
                      </p>
                      <span className={`mt-2 inline-flex rounded-full px-2 py-1 text-[10px] font-bold ${status.classes}`}>{status.label}</span>
                    </div>
                    <div className="flex shrink-0 items-center gap-1">
                      <button
                        type="button"
                        onClick={() => setReminders((current) => current.map((row) => row.id === reminder.id ? { ...row, done: !row.done } : row))}
                        aria-label={reminder.done ? "Mark reminder incomplete" : "Complete reminder"}
                        title={reminder.done ? "Mark incomplete" : "Mark complete"}
                        className={`rounded-lg p-1.5 ${reminder.done ? "text-emerald-700 hover:bg-emerald-50" : "text-slate-400 hover:bg-emerald-50 hover:text-emerald-700"}`}
                      >
                        <Check size={14} />
                      </button>
                      <button
                        type="button"
                        onClick={() => setReminders((current) => current.filter((row) => row.id !== reminder.id))}
                        aria-label={`Delete reminder ${reminder.text}`}
                        className="rounded-lg p-1.5 text-slate-400 hover:bg-red-50 hover:text-red-600"
                      >
                        <X size={14} />
                      </button>
                    </div>
                  </div>
                </div>
              );
            }) : (
              <div className="flex h-40 flex-col items-center justify-center text-center text-slate-400">
                <BellRing size={24} strokeWidth={1.4} />
                <p className="mt-3 text-sm font-medium text-slate-600">Nothing scheduled</p>
                <p className="mt-1 text-xs">Add a reminder with its date and time.</p>
              </div>
            )}
          </div>
          <p className="mt-3 text-[11px] text-slate-400">Reminders are saved in this browser and shown here when you return.</p>
        </SectionCard>

        <SectionCard
          title="Book Mark List"
          subtitle={`${bookmarks.length} saved ${bookmarks.length === 1 ? "link" : "links"}`}
          icon={<Bookmark size={19} />}
          className="min-h-[420px]"
        >
          <form onSubmit={addBookmark} className="space-y-2.5">
            <input
              value={bookmarkName}
              onChange={(event) => setBookmarkName(event.target.value)}
              placeholder="Bookmark name"
              aria-label="Bookmark name"
              maxLength={80}
              className="w-full rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-sm outline-none transition focus:border-slate-400 focus:bg-white"
            />
            <div className="flex gap-2">
              <input
                value={bookmarkUrl}
                onChange={(event) => setBookmarkUrl(event.target.value)}
                placeholder="Website URL"
                aria-label="Bookmark URL"
                className="min-w-0 flex-1 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5 text-xs outline-none transition focus:border-slate-400 focus:bg-white"
              />
              <button
                type="submit"
                disabled={!bookmarkName.trim() || !bookmarkUrl.trim()}
                aria-label="Add bookmark"
                className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-slate-950 text-white transition hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-40"
              >
                <Plus size={17} />
              </button>
            </div>
            {bookmarkError && <p role="alert" className="text-xs text-red-600">{bookmarkError}</p>}
          </form>

          <div className="mt-4 flex-1 space-y-2 overflow-y-auto pr-1">
            {bookmarks.length ? bookmarks.map((bookmark, index) => (
              <div key={bookmark.id || `${bookmark.name}-${index}`} className="flex items-center gap-2 rounded-xl border border-slate-100 bg-white p-2.5 hover:border-slate-200">
                <a
                  href={bookmark.href}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="flex min-w-0 flex-1 items-center gap-3"
                >
                  <span className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-slate-100 text-sm font-bold text-slate-700">
                    {(bookmark.name || "?").slice(0, 1).toUpperCase()}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm font-semibold text-slate-800">{bookmark.name}</span>
                    <span className="block truncate text-[11px] text-slate-400">{bookmark.href}</span>
                  </span>
                  <ExternalLink size={14} className="shrink-0 text-slate-400" />
                </a>
                <button
                  type="button"
                  onClick={() => setBookmarks((current) => current.filter((row, rowIndex) => rowIndex !== index))}
                  aria-label={`Delete bookmark ${bookmark.name}`}
                  className="rounded-lg p-1.5 text-slate-400 transition hover:bg-red-50 hover:text-red-600"
                >
                  <Trash2 size={14} />
                </button>
              </div>
            )) : (
              <div className="flex h-40 flex-col items-center justify-center text-center text-slate-400">
                <Bookmark size={24} strokeWidth={1.4} />
                <p className="mt-3 text-sm font-medium text-slate-600">No bookmarks yet</p>
                <p className="mt-1 text-xs">Add your frequently used websites above.</p>
              </div>
            )}
          </div>
          <div className="mt-3 flex items-center gap-1.5 text-[11px] text-slate-400">
            <ArrowUpRight size={12} /> Links open in a new tab.
          </div>
        </SectionCard>
      </div>
    </div>
  );
}
