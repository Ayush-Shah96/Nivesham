import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AlertCircle, CalendarDays, Check, CheckCheck, ChevronDown,
  Flag, Loader2, Plus, Search, Trash2, X,
} from 'lucide-react';
import { opsTrackerApi } from '../lib/opsTrackerApi';

const FILTERS = [
  { id: 'open', label: 'Open' },
  { id: 'mine', label: 'My tasks' },
  { id: 'pending', label: 'For approval' },
  { id: 'done', label: 'Completed' },
  { id: 'all', label: 'All tasks' },
];
const FREQUENCIES = [
  ['once', 'Does not repeat'], ['daily', 'Daily'], ['weekly', 'Weekly'],
  ['biweekly', 'Every 2 weeks'], ['monthly', 'Monthly'], ['quarterly', 'Quarterly'], ['annual', 'Annually'],
];
const AVATAR_STYLES = [
  'bg-teal-50 text-teal-700 ring-teal-100', 'bg-indigo-50 text-indigo-700 ring-indigo-100',
  'bg-amber-50 text-amber-700 ring-amber-100', 'bg-sky-50 text-sky-700 ring-sky-100',
  'bg-rose-50 text-rose-700 ring-rose-100', 'bg-violet-50 text-violet-700 ring-violet-100',
];
const today = () => new Date().toISOString().slice(0, 10);
const initials = (name = '') => name.trim().split(/\s+/).slice(0, 2).map((part) => part[0]?.toUpperCase()).join('') || '?';
const avatarStyle = (id = '') => AVATAR_STYLES[[...String(id)].reduce((sum, char) => sum + char.charCodeAt(0), 0) % AVATAR_STYLES.length];
const isLate = (task) => !task.done && !task.pending_approval && task.due_date && task.due_date < today();
const prettyDate = (value) => value ? new Date(`${value}T00:00:00`).toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' }) : 'No due date';
const messageOf = (error) => error?.message || 'Something went wrong. Please try again.';

function sortTasks(tasks) {
  return [...tasks].sort((a, b) => {
    if (a.pending_approval !== b.pending_approval) return a.pending_approval ? -1 : 1;
    if (a.done !== b.done) return a.done ? 1 : -1;
    if (Boolean(a.priority) !== Boolean(b.priority)) return a.priority ? -1 : 1;
    if (a.due_date && b.due_date) return a.due_date.localeCompare(b.due_date);
    if (a.due_date) return -1;
    if (b.due_date) return 1;
    return (a.name || '').localeCompare(b.name || '');
  });
}

function TaskCard({ task, members, isLead, isMine, onPatch, onDelete, onEdit }) {
  const assignee = members.find((member) => member.id === task.member_id);
  const late = isLate(task);
  const status = task.done
    ? { label: 'Completed', cls: 'bg-teal-50 text-teal-700' }
    : task.pending_approval
      ? { label: 'For approval', cls: 'bg-amber-50 text-amber-700' }
      : late
        ? { label: 'Overdue', cls: 'bg-rose-50 text-rose-700' }
        : { label: 'Open', cls: 'bg-slate-100 text-slate-600' };
  const canComplete = !task.done && !task.pending_approval && (isLead || isMine);
  return (
    <article onClick={() => onEdit(task)} title="Click to edit task" className={`group flex min-h-[88px] w-full cursor-pointer items-center gap-4 rounded-lg border border-slate-200 bg-white px-5 py-5 shadow-sm transition hover:border-teal-300 hover:shadow ${task.pending_approval ? 'border-l-4 border-l-amber-400' : ''}`}>
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <h3 className={`min-w-0 text-sm font-semibold ${task.done ? 'text-slate-400 line-through' : 'text-slate-800'}`}>{task.name}</h3>
          {task.priority && <Flag size={13} className="shrink-0 fill-amber-500 text-amber-500" />}
          <span className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] font-semibold ${status.cls}`}>{status.label}</span>
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-slate-500">
          {task.due_date && <span className="inline-flex items-center gap-1"><CalendarDays size={12}/>{prettyDate(task.due_date)}</span>}
          {assignee && <span>{assignee.name}</span>}
          {task.freq && task.freq !== 'once' && <span>{task.freq}</span>}
          {task.notes && <span className="max-w-[280px] truncate">{task.notes}</span>}
        </div>
      </div>
      <div className="flex shrink-0 items-center gap-1.5" onClick={(event) => event.stopPropagation()}>
        {isLead && <button type="button" onClick={() => onPatch({ priority: !task.priority })} title={task.priority ? 'Remove flag' : 'Flag task'} className={`grid h-8 w-8 place-items-center rounded-md transition ${task.priority ? 'bg-amber-50 text-amber-600' : 'text-slate-400 hover:bg-amber-50 hover:text-amber-600'}`}><Flag size={15} className={task.priority ? 'fill-current' : ''}/></button>}
        {canComplete && <button type="button" onClick={() => onPatch({ done: true })} title={isLead ? 'Mark completed' : 'Submit for approval'} className="grid h-8 w-8 place-items-center rounded-md text-teal-700 hover:bg-teal-50"><Check size={17}/></button>}
        {task.pending_approval && isLead && <><button type="button" onClick={() => onPatch({ done: true })} title="Approve" className="grid h-8 w-8 place-items-center rounded-md text-teal-700 hover:bg-teal-50"><CheckCheck size={17}/></button><button type="button" onClick={() => onPatch({ pending_approval: false })} title="Send back" className="grid h-8 w-8 place-items-center rounded-md text-amber-700 hover:bg-amber-50"><X size={15}/></button></>}
        {isLead && <button type="button" onClick={onDelete} title="Delete task" className="grid h-8 w-8 place-items-center rounded-md text-slate-300 hover:bg-rose-50 hover:text-rose-600"><Trash2 size={15}/></button>}
      </div>
    </article>
  );
}

export default function OpsTracker({ user } = {}) {
  const [board, setBoard] = useState({ teams: [], members: [], tasks: [], role: null, current_member_id: null });
  const [activeTeam, setActiveTeam] = useState(null);
  const [filter, setFilter] = useState('open');
  const [search, setSearch] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [newTeam, setNewTeam] = useState('');
  const [newMember, setNewMember] = useState('');
  const [newTask, setNewTask] = useState({ name: '', member_id: '', backup_member_id: '', due_date: '', freq: 'once', priority: false, notes: '' });
  const [showTaskDetails, setShowTaskDetails] = useState(false);
  const [loginUsers, setLoginUsers] = useState([]);
  const [addingTask, setAddingTask] = useState(false);
  const [autoLinking, setAutoLinking] = useState(false);
  const [editingTask, setEditingTask] = useState(null);
  const [editDraft, setEditDraft] = useState({ name: '', notes: '', due_date: '', member_id: '', backup_member_id: '', freq: 'once', priority: false });
  const [showAddTeam, setShowAddTeam] = useState(false);
  const [showAllTeams, setShowAllTeams] = useState(false);
  const isLead = board.role ? board.role === 'Team Lead' : user?.role === 'ADMIN';
  const currentMemberId = board.current_member_id;

  const load = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true);
    try {
      const data = await opsTrackerApi.board();
      setBoard({ teams: data.teams || [], members: data.members || [], tasks: data.tasks || [], role: data.role || (user?.role === 'ADMIN' ? 'Team Lead' : 'Team Member'), current_member_id: data.current_member_id || null });
      setError('');
    } catch (err) {
      setError(messageOf(err));
    } finally {
      setLoading(false);
    }
  }, [user?.role]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    if (!isLead) return undefined;
    opsTrackerApi.loginUsers().then(setLoginUsers).catch(() => setLoginUsers([]));
    return undefined;
  }, [isLead]);

  useEffect(() => {
    if (!board.teams.some((team) => team.id === activeTeam)) setActiveTeam(board.teams[0]?.id || null);
  }, [board.teams, activeTeam]);

  const activeTeamRow = board.teams.find((team) => team.id === activeTeam);
  const teamMembers = useMemo(() => board.members.filter((member) => member.team_id === activeTeam || (member.extra_teams || []).includes(activeTeam)), [board.members, activeTeam]);
  const teamTasks = useMemo(() => board.tasks.filter((task) => task.team_id === activeTeam), [board.tasks, activeTeam]);
  const openTotal = board.tasks.filter((task) => !task.done && !task.pending_approval).length;
  const pendingTotal = board.tasks.filter((task) => task.pending_approval).length;
  const overdueTotal = board.tasks.filter(isLate).length;
  const completedTotal = board.tasks.filter((task) => task.done).length;
  const teamOpen = teamTasks.filter((task) => !task.done && !task.pending_approval).length;
  const teamPending = teamTasks.filter((task) => task.pending_approval).length;
  const teamDone = teamTasks.filter((task) => task.done).length;

  const visibleTasks = useMemo(() => {
    const term = search.trim().toLowerCase();
    return sortTasks(teamTasks.filter((task) => {
      if (filter === 'mine' && task.member_id !== currentMemberId && task.backup_member_id !== currentMemberId) return false;
      if (filter === 'open' && (task.done || task.pending_approval)) return false;
      if (filter === 'pending' && !task.pending_approval) return false;
      if (filter === 'done' && !task.done) return false;
      if (term && !`${task.name} ${task.notes || ''}`.toLowerCase().includes(term)) return false;
      return true;
    }));
  }, [teamTasks, filter, search, currentMemberId]);

  const updateTask = async (task, patch) => {
    try {
      const saved = await opsTrackerApi.updateTask(task.id, patch);
      setBoard((previous) => ({ ...previous, tasks: previous.tasks.map((row) => row.id === task.id ? saved : row) }));
      setError('');
      setNotice(patch.done === true && !isLead ? 'Task submitted to your Team Lead for approval.' : patch.done === true ? 'Task approved and completed.' : patch.pending_approval === false ? 'Task sent back to the team member.' : 'Task updated.');
      window.setTimeout(() => setNotice(''), 3500);
    } catch (err) { setError(messageOf(err)); }
  };

  const addTeam = async (event) => {
    event.preventDefault();
    if (!newTeam.trim()) return;
    try {
      const team = await opsTrackerApi.createTeam({ name: newTeam.trim() });
      setBoard((previous) => ({ ...previous, teams: [...previous.teams, team].sort((a, b) => a.name.localeCompare(b.name)) }));
      setActiveTeam(team.id); setNewTeam('');
    } catch (err) { setError(messageOf(err)); }
  };

  const deleteTeam = async () => {
    if (!activeTeam || !window.confirm(`Delete ${activeTeamRow?.name || 'this team'}? Its tasks will also be deleted. Members linked to other teams will be preserved.`)) return;
    try { await opsTrackerApi.deleteTeam(activeTeam); setActiveTeam(null); await load(true); }
    catch (err) { setError(messageOf(err)); }
  };

  const addMember = async (event) => {
    event.preventDefault();
    if (!newMember.trim() || !activeTeam) return;
    try {
      const member = await opsTrackerApi.createMember({ team_id: activeTeam, name: newMember.trim(), level: 1 });
      setBoard((previous) => ({ ...previous, members: [...previous.members, member] })); setNewMember('');
    } catch (err) { setError(messageOf(err)); }
  };

  const deleteMember = async (member) => {
    if (!window.confirm(`Remove ${member.name} from Ops Tracker? Their assigned tasks will become unassigned.`)) return;
    try { await opsTrackerApi.deleteMember(member.id); await load(true); }
    catch (err) { setError(messageOf(err)); }
  };

  const linkMember = async (member, value) => {
    try {
      const userId = value ? Number(value) : null;
      await opsTrackerApi.linkMember(member.id, userId);
      setBoard((previous) => ({ ...previous, members: previous.members.map((row) => row.id === member.id ? { ...row, user_id: userId } : row) }));
      setNotice(`${member.name}'s Intelligence System login mapping was updated.`);
      window.setTimeout(() => setNotice(''), 3500);
    } catch (err) { setError(messageOf(err)); }
  };

  const autoLink = async () => {
    setAutoLinking(true);
    try {
      const result = await opsTrackerApi.autoLinkMembers();
      setNotice(`Automatically linked ${result.linked?.length || 0} existing member account(s). ${result.skipped?.length || 0} need review.`);
      await load(true);
    } catch (err) { setError(messageOf(err)); }
    finally { setAutoLinking(false); }
  };

  const addTask = async (event) => {
    event.preventDefault();
    if (!newTask.name.trim() || !activeTeam) return;
    setAddingTask(true);
    try {
      const task = await opsTrackerApi.createTask({
        team_id: activeTeam, name: newTask.name.trim(), member_id: newTask.member_id || null,
        backup_member_id: newTask.backup_member_id || null, due_date: newTask.due_date || null,
        freq: newTask.freq, priority: newTask.priority, notes: newTask.notes.trim() || null,
      });
      setBoard((previous) => ({ ...previous, tasks: [...previous.tasks, task] }));
      setNewTask({ name: '', member_id: '', backup_member_id: '', due_date: '', freq: 'once', priority: false, notes: '' });
      setShowTaskDetails(false);
    } catch (err) { setError(messageOf(err)); }
    finally { setAddingTask(false); }
  };

  const deleteTask = async (task) => {
    if (!window.confirm(`Delete “${task.name}”? This cannot be undone.`)) return;
    try { await opsTrackerApi.deleteTask(task.id); setBoard((previous) => ({ ...previous, tasks: previous.tasks.filter((row) => row.id !== task.id) })); }
    catch (err) { setError(messageOf(err)); }
  };

  const openTaskEditor = (task) => {
    setEditingTask(task);
    setEditDraft({ name: task.name || '', notes: task.notes || '', due_date: task.due_date || '', member_id: task.member_id || '', backup_member_id: task.backup_member_id || '', freq: task.freq || 'once', priority: Boolean(task.priority) });
  };
  const saveTaskEditor = async (event) => {
    event.preventDefault();
    if (!editingTask || !editDraft.name.trim()) return;
    await updateTask(editingTask, { ...editDraft, name: editDraft.name.trim(), notes: editDraft.notes.trim() || null, due_date: editDraft.due_date || null, member_id: editDraft.member_id || null, backup_member_id: editDraft.backup_member_id || null });
    setEditingTask(null);
  };

  const renderMemberLink = (member) => {
    const linkedUser = loginUsers.find((login) => login.id === member.user_id);
    const taken = new Set(board.members.filter((row) => row.id !== member.id && row.user_id != null).map((row) => row.user_id));
    return (
      <div className="mt-1">
        {isLead ? (
          <select value={member.user_id ?? ''} onChange={(event) => linkMember(member, event.target.value)} aria-label={`Intelligence System login for ${member.name}`} className="w-full rounded-lg border border-slate-200 bg-white px-2 py-1.5 text-[11px] text-slate-600 outline-none focus:border-teal-500">
            <option value="">Not linked to a login</option>
            {loginUsers.filter((login) => !taken.has(login.id) || login.id === member.user_id).map((login) => <option key={login.id} value={login.id}>@{login.username}</option>)}
          </select>
        ) : <span className="text-[11px] text-slate-400">{linkedUser ? `@${linkedUser.username}` : 'Intelligence login not linked'}</span>}
      </div>
    );
  };

  if (loading) return <div className="mx-auto flex min-h-[280px] max-w-6xl items-center justify-center gap-3 px-6 text-sm text-slate-500"><Loader2 className="animate-spin text-teal-600" size={18} />Loading tasks…</div>;

  const unlinked = error.toLowerCase().includes('not linked to an ops tracker member');
  return (
    <div className="mx-auto flex min-h-full w-full max-w-none flex-col gap-3 px-3 py-4 sm:px-5 lg:px-7">
      {error && <div role="alert" className="flex shrink-0 items-center gap-2 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-800"><AlertCircle size={15}/><span className="min-w-0 flex-1">{unlinked ? 'Ops Tracker account not linked: ' : ''}{error}</span><button onClick={() => setError('')}><X size={14}/></button></div>}
      {notice && <div role="status" className="shrink-0 rounded-lg border border-teal-200 bg-teal-50 px-3 py-2 text-xs text-teal-800">{notice}</div>}

      <div className="flex items-start gap-2">
        <div className={`min-w-0 flex-1 ${showAllTeams ? 'flex flex-wrap items-center gap-1.5' : 'flex items-center gap-1.5 overflow-x-auto whitespace-nowrap pb-1 [scrollbar-width:thin]'}`}>
          {board.teams.map((team) => {
            const selected = team.id === activeTeam;
            const count = board.tasks.filter((task) => task.team_id === team.id && !task.done).length;
            return <button key={team.id} type="button" onClick={() => { setActiveTeam(team.id); setFilter('open'); }} aria-pressed={selected} className={`shrink-0 rounded-lg border px-3 py-2 text-xs font-medium transition ${selected ? 'border-slate-900 bg-slate-900 text-white' : 'border-slate-200 bg-white text-slate-600 hover:border-slate-300 hover:bg-slate-50'}`}>{team.name}<span className={`ml-1.5 text-[10px] ${selected ? 'text-slate-300' : 'text-slate-400'}`}>{count}</span></button>;
          })}
        </div>
        {board.teams.length > 0 && <button type="button" onClick={() => setShowAllTeams((value) => !value)} aria-label={showAllTeams ? 'Collapse teams' : 'Show all teams'} aria-expanded={showAllTeams} title={showAllTeams ? 'Collapse teams' : 'Show all teams'} className="mt-1 grid h-8 w-8 shrink-0 place-items-center rounded-md text-slate-500 hover:bg-slate-200"><ChevronDown size={18} className={`transition-transform ${showAllTeams ? 'rotate-180' : ''}`}/></button>}
        {isLead && <button type="button" onClick={() => setShowAddTeam((value) => !value)} title="Add team" className="mt-0.5 grid h-8 w-8 shrink-0 place-items-center rounded-md bg-slate-900 text-white hover:bg-slate-700"><Plus size={17}/></button>}
      </div>
      {isLead && showAddTeam && <form onSubmit={async (event) => { await addTeam(event); setShowAddTeam(false); }} className="flex shrink-0 gap-2"><input autoFocus value={newTeam} onChange={(event) => setNewTeam(event.target.value)} placeholder="New team name" className="w-56 rounded-lg border border-slate-200 px-3 py-2 text-xs outline-none focus:border-teal-500"/><button type="submit" disabled={!newTeam.trim()} className="rounded-lg bg-teal-700 px-3 py-2 text-xs font-semibold text-white disabled:opacity-40">Add team</button><button type="button" onClick={() => setShowAddTeam(false)} className="rounded-lg border px-3 py-2 text-xs">Cancel</button></form>}

      {!board.teams.length ? <div className="rounded-xl border border-dashed border-slate-300 bg-white px-5 py-12 text-center text-sm text-slate-500">{isLead ? 'Add a team to start creating tasks.' : 'You are not assigned to a team yet.'}</div> : activeTeam ? <section className="flex w-full flex-col rounded-xl border border-slate-200 bg-white">
        {isLead && <form onSubmit={addTask} className="flex shrink-0 flex-col gap-2 border-b border-slate-100 p-3">
          <div className="flex gap-2"><input value={newTask.name} onChange={(event) => setNewTask((value) => ({ ...value, name: event.target.value }))} placeholder="Add a task…" aria-label="New task name" className="min-w-0 flex-1 rounded-lg border border-slate-200 px-3 py-2 text-sm outline-none focus:border-teal-500"/><button type="submit" disabled={!newTask.name.trim() || addingTask} className="inline-flex shrink-0 items-center gap-1.5 rounded-lg bg-teal-700 px-3 py-2 text-xs font-semibold text-white disabled:opacity-40">{addingTask ? <Loader2 size={14} className="animate-spin"/> : <Plus size={14}/>}Add</button></div>
          <button type="button" onClick={() => setShowTaskDetails((value) => !value)} className="self-start text-[11px] text-slate-500 hover:text-teal-700">{showTaskDetails ? 'Hide task options' : 'Add assignee, due date or recurrence'} <ChevronDown size={11} className="inline"/></button>
          {showTaskDetails && <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4"><select value={newTask.member_id} onChange={(event) => setNewTask((value) => ({ ...value, member_id: event.target.value }))} className="rounded-lg border border-slate-200 px-2 py-2 text-xs"><option value="">Assign to…</option>{teamMembers.map((member) => <option key={member.id} value={member.id}>{member.name}</option>)}</select><input type="date" value={newTask.due_date} onChange={(event) => setNewTask((value) => ({ ...value, due_date: event.target.value }))} className="rounded-lg border border-slate-200 px-2 py-2 text-xs"/><select value={newTask.freq} onChange={(event) => setNewTask((value) => ({ ...value, freq: event.target.value }))} className="rounded-lg border border-slate-200 px-2 py-2 text-xs">{FREQUENCIES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select><label className="flex items-center gap-2 rounded-lg border border-slate-200 px-3 py-2 text-xs"><input type="checkbox" checked={newTask.priority} onChange={(event) => setNewTask((value) => ({ ...value, priority: event.target.checked }))}/>Flag as priority</label></div>}
        </form>}
        <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-slate-100 px-3 py-2">
          <div className="flex min-w-0 gap-1 overflow-x-auto">{FILTERS.filter((item) => (isLead || item.id !== 'pending') && (!isLead || item.id !== 'mine')).map((item) => <button key={item.id} type="button" onClick={() => setFilter(item.id)} className={`shrink-0 rounded-md px-2.5 py-1.5 text-[11px] font-medium ${filter === item.id ? 'bg-slate-100 text-slate-900' : 'text-slate-500 hover:bg-slate-50'}`}>{item.label}</button>)}</div>
          <label className="relative block w-full sm:w-52"><Search size={13} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400"/><input type="search" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search tasks" className="w-full rounded-lg border border-slate-200 py-2 pl-8 pr-3 text-xs outline-none focus:border-teal-500"/></label>
        </div>
        <div className="space-y-3 bg-slate-50/60 p-3 sm:p-4">
          {visibleTasks.length ? visibleTasks.map((task) => <TaskCard key={task.id} task={task} members={teamMembers} isLead={isLead} isMine={task.member_id === currentMemberId || task.backup_member_id === currentMemberId} onPatch={(patch) => updateTask(task, patch)} onDelete={() => deleteTask(task)} onEdit={openTaskEditor}/>) : <div className="py-12 text-center text-sm text-slate-500"><CheckCheck className="mx-auto mb-2 text-slate-300" size={22}/>No tasks in this list.</div>}
        </div>
      </section> : null}

      {isLead && activeTeam && <details className="shrink-0 rounded-lg border border-slate-200 bg-white"><summary className="cursor-pointer px-3 py-2 text-xs font-medium text-slate-600">Team members and login links ({teamMembers.length})</summary><div className="max-h-52 overflow-y-auto border-t border-slate-100 p-3"><div className="divide-y divide-slate-100">{teamMembers.map((member) => <div key={member.id} className="flex items-center gap-3 py-2"><span className={`grid h-7 w-7 shrink-0 place-items-center rounded-full text-[10px] font-bold ring-1 ring-inset ${avatarStyle(member.id)}`}>{initials(member.name)}</span><div className="min-w-0 flex-1"><p className="text-xs font-medium">{member.name}</p>{renderMemberLink(member)}</div><button type="button" onClick={() => deleteMember(member)} className="text-slate-300 hover:text-rose-600"><X size={14}/></button></div>)}</div><form onSubmit={addMember} className="mt-2 flex gap-2"><input value={newMember} onChange={(event) => setNewMember(event.target.value)} placeholder="Add member" className="min-w-0 flex-1 rounded-lg border border-slate-200 px-3 py-2 text-xs"/><button type="submit" disabled={!newMember.trim()} className="rounded-lg bg-slate-800 px-3 py-2 text-xs text-white disabled:opacity-40">Add</button></form><button type="button" onClick={autoLink} disabled={autoLinking} className="mt-3 text-xs font-medium text-teal-700">{autoLinking ? 'Linking…' : 'Auto-link matching usernames'}</button></div></details>}

      {editingTask && <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/40 p-4" onMouseDown={(event) => { if (event.target === event.currentTarget) setEditingTask(null); }}><form onSubmit={saveTaskEditor} className="max-h-[90vh] w-full max-w-xl overflow-y-auto rounded-xl bg-white p-4 shadow-2xl sm:p-5"><div className="mb-4 flex items-center justify-between"><div><h2 className="text-base font-semibold text-slate-900">Edit task</h2><p className="text-[11px] text-slate-500">Update details, notes or priority</p></div><button type="button" onClick={() => setEditingTask(null)} className="rounded-md p-2 text-slate-400 hover:bg-slate-100"><X size={17}/></button></div><div className="space-y-3"><label className="block text-xs font-medium text-slate-600">Task name<input autoFocus value={editDraft.name} onChange={(event) => setEditDraft((value) => ({ ...value, name: event.target.value }))} className="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2.5 text-sm outline-none focus:border-teal-500"/></label><label className="block text-xs font-medium text-slate-600">Notes<textarea rows={4} value={editDraft.notes} onChange={(event) => setEditDraft((value) => ({ ...value, notes: event.target.value }))} placeholder="Add task notes or instructions…" className="mt-1 w-full resize-y rounded-lg border border-slate-200 px-3 py-2.5 text-sm outline-none focus:border-teal-500"/></label><div className="grid gap-3 sm:grid-cols-2"><label className="text-xs font-medium text-slate-600">Assignee<select value={editDraft.member_id} onChange={(event) => setEditDraft((value) => ({ ...value, member_id: event.target.value }))} className="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 text-xs"><option value="">Unassigned</option>{teamMembers.map((member) => <option key={member.id} value={member.id}>{member.name}</option>)}</select></label><label className="text-xs font-medium text-slate-600">Due date<input type="date" value={editDraft.due_date} onChange={(event) => setEditDraft((value) => ({ ...value, due_date: event.target.value }))} className="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 text-xs"/></label><label className="text-xs font-medium text-slate-600">Backup member<select value={editDraft.backup_member_id} onChange={(event) => setEditDraft((value) => ({ ...value, backup_member_id: event.target.value }))} className="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 text-xs"><option value="">No backup</option>{teamMembers.map((member) => <option key={member.id} value={member.id}>{member.name}</option>)}</select></label><label className="text-xs font-medium text-slate-600">Repeat<select value={editDraft.freq} onChange={(event) => setEditDraft((value) => ({ ...value, freq: event.target.value }))} className="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 text-xs">{FREQUENCIES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label></div><label className="flex items-center gap-2 text-xs text-slate-700"><input type="checkbox" checked={editDraft.priority} onChange={(event) => setEditDraft((value) => ({ ...value, priority: event.target.checked }))}/><Flag size={14} className="text-amber-600"/>Flag this task as priority</label></div><div className="mt-5 flex justify-end gap-2"><button type="button" onClick={() => setEditingTask(null)} className="rounded-lg border border-slate-200 px-4 py-2 text-xs font-medium text-slate-600">Cancel</button><button type="submit" disabled={!editDraft.name.trim()} className="rounded-lg bg-teal-700 px-4 py-2 text-xs font-semibold text-white">Save changes</button></div></form></div>}
    </div>
  );
}