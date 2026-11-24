import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Check,
  ChevronDown,
  Clock3,
  Columns3,
  Download,
  Edit3,
  Plus,
  RefreshCw,
  Search,
  Trash2,
  Upload,
  X,
  XCircle,
} from "lucide-react";
import { api, request } from "../lib/api";
import { ErrorBox, Loading, PageHeader } from "../components/UI";

const TRACK_ORDER = ["PO", "HT", "LT", "NT"];
const TRACK_LABELS = {
  PO: "Portfolio",
  HT: "High Track",
  LT: "Low Track",
  NT: "No Track",
};
const TRACK_PRIORITY = { PO: 1, HT: 2, LT: 3, NT: 4 };
const ALLOWED_ANALYSTS = ["aashil", "ahmed", "jainam", "raj", "romil"];
const ANALYST_LABELS = { aashil: "Aashil" };

const normalizeAnalystName = (value) => {
  const normalized = String(value || "").trim().normalize("NFKC").toLocaleLowerCase();
  // Accept the alternate spelling "Ashil" as the same analyst as "Aashil".
  return normalized === "ashil" ? "aashil" : normalized;
};

const isAllowedAnalyst = (value) => ALLOWED_ANALYSTS.includes(normalizeAnalystName(value));
const analystLabel = (value) => ANALYST_LABELS[normalizeAnalystName(value)] || value;

const isDash = (value) => {
  if (value == null) return true;
  const text = String(value).trim();
  return text === "" || text === "-" || text === "—" || text === "–";
};

const displayValue = (value) => (isDash(value) ? "-" : String(value));
const isValidDateInput = (value) => /^\d{4}-\d{2}-\d{2}$/.test(String(value || ""));

const approvalLabel = (status) => {
  switch (status) {
    case "APPROVED":
      return "Approved";
    case "PENDING_AASHIL_APPROVAL":
    case "PENDING_APPROVAL":
      return "Pending";
    case "REJECTED":
      return "Rejected";
    default:
      return "Pending";
  }
};

const approvalClass = (status) => {
  switch (status) {
    case "APPROVED":
      return "border-emerald-200 bg-emerald-50 text-emerald-700";
    case "REJECTED":
      return "border-red-200 bg-red-50 text-red-700";
    default:
      return "border-amber-200 bg-amber-50 text-amber-700";
  }
};

function FilterButton({ label, count, active, onClick }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={[
        "inline-flex items-center gap-2 rounded-md border px-3 py-1.5 text-xs font-semibold transition",
        active
          ? "border-slate-900 bg-slate-900 text-white"
          : "border-slate-300 bg-white text-slate-700 hover:bg-slate-50",
      ].join(" ")}
    >
      <span>{label}</span>
      <span className={active ? "rounded-full bg-white/15 px-1.5" : "rounded-full bg-slate-100 px-1.5"}>
        {count}
      </span>
    </button>
  );
}

function MultiSelectDropdown({ label, allLabel, options, selectedValues, onChange, totalCount, widthClass = "w-52" }) {
  const [open, setOpen] = useState(false);
  const wrapperRef = useRef(null);
  const selected = new Set(selectedValues);
  const selectedOptions = options.filter((option) => selected.has(option.value));
  const buttonLabel = selectedOptions.length === 0
    ? `${allLabel} (${totalCount})`
    : selectedOptions.length === 1
      ? selectedOptions[0].label
      : `${label} (${selectedOptions.length})`;

  useEffect(() => {
    if (!open) return undefined;
    const handlePointerDown = (event) => {
      if (!wrapperRef.current?.contains(event.target)) setOpen(false);
    };
    const handleKeyDown = (event) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", handlePointerDown);
    document.addEventListener("touchstart", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      document.removeEventListener("touchstart", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [open]);

  const toggleOption = (value) => {
    onChange(selected.has(value)
      ? selectedValues.filter((item) => item !== value)
      : [...selectedValues, value]);
  };

  return (
    <div ref={wrapperRef} className={`relative ${widthClass} shrink-0`}>
      <button
        type="button"
        onClick={() => setOpen((current) => !current)}
        aria-expanded={open}
        aria-haspopup="listbox"
        className="flex w-full items-center justify-between gap-2 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-left text-xs font-semibold text-slate-700 outline-none hover:bg-slate-50 focus:border-slate-500"
      >
        <span className="min-w-0 truncate">{buttonLabel}</span>
        <ChevronDown size={14} className={`shrink-0 transition-transform ${open ? "rotate-180" : ""}`} />
      </button>
      {open && (
        <div className="absolute left-0 z-50 mt-1 max-h-72 w-full min-w-[190px] overflow-y-auto rounded-lg border border-slate-300 bg-white p-2 shadow-lg">
          <div className="mb-1 flex items-center justify-between border-b border-slate-100 px-1 pb-2">
            <span className="text-[11px] font-bold uppercase tracking-wide text-slate-500">Select {label.toLowerCase()}</span>
            <button
              type="button"
              onClick={() => onChange([])}
              className="rounded px-1.5 py-0.5 text-[11px] font-semibold text-slate-600 hover:bg-slate-100"
            >
              Clear
            </button>
          </div>
          {options.map((option) => (
            <label key={option.value} className="flex cursor-pointer items-center gap-2 rounded px-2 py-2 text-xs text-slate-700 hover:bg-slate-50">
              <input
                type="checkbox"
                checked={selected.has(option.value)}
                onChange={() => toggleOption(option.value)}
                className="h-3.5 w-3.5 rounded border-slate-300 accent-slate-900"
              />
              <span className="min-w-0 flex-1 truncate">{option.label}</span>
              <span className="tabular-nums text-[10px] text-slate-400">{option.count}</span>
            </label>
          ))}
          {options.length === 0 && <div className="px-2 py-3 text-xs text-slate-500">No options available</div>}
          <div className="mt-1 border-t border-slate-100 px-2 pt-2 text-[10px] text-slate-400">Select multiple options to combine this filter.</div>
        </div>
      )}
    </div>
  );
}

export default function CoverageList({ user }) {
  const isAdmin = user?.role === "ADMIN";
  const isAnalyst = user?.role === "ANALYST";
  const canAdd = isAdmin || isAnalyst;

  const [grid, setGrid] = useState(null);
  const [search, setSearch] = useState("");
  const [trackFilter, setTrackFilter] = useState([]);
  const [analystFilter, setAnalystFilter] = useState([]);
  const [approvalFilter, setApprovalFilter] = useState([]);
  const [sortMode, setSortMode] = useState("priority");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [editingRow, setEditingRow] = useState(null);
  const [editorValues, setEditorValues] = useState([]);
  const [inlineEditId, setInlineEditId] = useState(null);
  const [inlineDraft, setInlineDraft] = useState({});
  const [columnMenuOpen, setColumnMenuOpen] = useState(false);
  const [columnPickerSearch, setColumnPickerSearch] = useState("");
  const [columnEditorOpen, setColumnEditorOpen] = useState(false);
  const [newColumnName, setNewColumnName] = useState("");
  const [insertAfterColumn, setInsertAfterColumn] = useState("");
  const [columnPreference, setColumnPreference] = useState(null);
  const [syncHealth, setSyncHealth] = useState(null);
  const fileRef = useRef(null);

  const columnPreferenceKey = useMemo(
    () => `coverage-list-visible-columns:${String(user?.id ?? user?.username ?? "guest")}`,
    [user?.id, user?.username],
  );

  const loadGrid = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    setError("");
    try {
      const data = await request("/coverage-list/grid", { retry: true });
      setGrid(data && Array.isArray(data.columns) && Array.isArray(data.rows)
        ? data
        : { columns: [], rows: [], counts: { total: 0, track: {}, analyst: {}, approval: {} }, version: null });
    } catch (e) {
      setError(e?.message || "Failed to load Coverage List");
    } finally {
      if (!silent) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadGrid();
    void (async () => {
      try {
        const health = await request("/coverage-list/health", { retry: false, maxRetries: 0, timeoutMs: 5000 });
        setSyncHealth(health || null);
      } catch {
        // Health is diagnostic only; keep the grid usable when unavailable.
      }
    })();
  }, [loadGrid]);

  useEffect(() => {
    if (!grid?.version) return undefined;

    let cancelled = false;
    const checkVersion = async () => {
      try {
        const latest = await request("/coverage-list/version", { retry: false });
        if (!cancelled && latest?.version && latest.version !== grid.version) {
          await loadGrid(true);
        }
      } catch {
        // Best effort: Excel may be temporarily locked while it is being saved.
      }
    };

    const timer = window.setInterval(checkVersion, 60000);
    const onFocus = () => void checkVersion();
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onFocus);

    return () => {
      cancelled = true;
      window.clearInterval(timer);
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onFocus);
    };
  }, [grid?.version, loadGrid]);

  const columns = grid?.columns || [];
  const rows = grid?.rows || [];
  const dataColumns = useMemo(
    () => columns.filter((column) => column.canonical !== "row number"),
    [columns],
  );
  const trackCounts = grid?.counts?.track || {};
  const analystCounts = grid?.counts?.analyst || {};
  const approvalCounts = grid?.counts?.approval || {};

  useEffect(() => {
    try {
      const saved = window.localStorage.getItem(columnPreferenceKey);
      if (saved) {
        const parsed = JSON.parse(saved);
        if (Array.isArray(parsed)) {
          // Migrate the previous pre-user-specific preference format.
          setColumnPreference({ selectedKeys: parsed, knownKeys: dataColumns.map((column) => column.storage_key || column.key) });
        } else if (parsed && typeof parsed === "object") {
          setColumnPreference({
            selectedKeys: Array.isArray(parsed.selectedKeys) ? parsed.selectedKeys : [],
            knownKeys: Array.isArray(parsed.knownKeys) ? parsed.knownKeys : [],
          });
        } else {
          setColumnPreference(null);
        }
      } else {
        // First use for this signed-in user: select every current Excel column.
        setColumnPreference(null);
      }
    } catch {
      setColumnPreference(null);
    }
  }, [columnPreferenceKey]);

  const dataColumnStorageKeys = useMemo(
    () => dataColumns.map((column) => column.storage_key || column.key),
    [dataColumns],
  );

  useEffect(() => {
    if (!dataColumns.length) return;

    const currentKeys = dataColumnStorageKeys;
    setColumnPreference((current) => {
      if (!current) {
        const next = { selectedKeys: [...currentKeys], knownKeys: [...currentKeys] };
        try {
          window.localStorage.setItem(columnPreferenceKey, JSON.stringify(next));
        } catch {
          // Best-effort persistence.
        }
        return next;
      }

      const previousKnown = Array.isArray(current.knownKeys) ? current.knownKeys : [];
      const selected = new Set(Array.isArray(current.selectedKeys) ? current.selectedKeys : []);
      const currentSet = new Set(currentKeys);
      const hadAllPreviousColumns = previousKnown.length > 0
        ? previousKnown.every((key) => selected.has(key))
        : false;

      // When the user had "All" selected, newly-added Excel columns are selected
      // automatically. A user with a custom subset keeps that subset.
      if (hadAllPreviousColumns) {
        currentKeys.forEach((key) => selected.add(key));
      }

      const nextSelected = currentKeys.filter((key) => selected.has(key));
      // If the old preference was an explicit empty set, keep it empty.
      const next = { selectedKeys: nextSelected, knownKeys: [...currentKeys] };
      if (
        nextSelected.length !== current.selectedKeys?.length
        || current.knownKeys?.length !== currentKeys.length
        || current.knownKeys?.some((key, index) => key !== currentKeys[index])
      ) {
        try {
          window.localStorage.setItem(columnPreferenceKey, JSON.stringify(next));
        } catch {
          // Best-effort persistence.
        }
      }
      return next;
    });
  }, [columnPreferenceKey, dataColumns.length, dataColumnStorageKeys]);

  const visibleColumnKeys = columnPreference?.selectedKeys || [];

  const visibleColumns = useMemo(() => {
    const selected = new Set(visibleColumnKeys);
    return dataColumns.filter((column) => selected.has(column.storage_key || column.key));
  }, [dataColumns, visibleColumnKeys]);

  // Keep the two fields required for admin inline editing visible while a row is
  // being edited, even when the user has hidden them from the normal column view.
  // This does not change the user's saved column selection.
  const renderColumns = useMemo(() => {
    if (!isAdmin || !inlineEditId) return visibleColumns;
    const selected = new Set(visibleColumns.map((column) => column.storage_key || column.key));
    dataColumns.forEach((column) => {
      if (column.canonical === "track mode" || column.canonical === "analyst") {
        selected.add(column.storage_key || column.key);
      }
    });
    return dataColumns.filter((column) => selected.has(column.storage_key || column.key));
  }, [dataColumns, inlineEditId, isAdmin, visibleColumns]);

  const filteredColumnOptions = useMemo(() => {
    const query = columnPickerSearch.trim().toLowerCase();
    if (!query) return dataColumns;
    return dataColumns.filter((column) => String(column.label || "").toLowerCase().includes(query));
  }, [columnPickerSearch, dataColumns]);

  const persistColumnPreference = useCallback((selectedKeys) => {
    const next = { selectedKeys, knownKeys: dataColumnStorageKeys };
    setColumnPreference(next);
    try {
      window.localStorage.setItem(columnPreferenceKey, JSON.stringify(next));
    } catch {
      // Best-effort persistence.
    }
  }, [columnPreferenceKey, dataColumnStorageKeys]);

  const setColumnVisible = (key, visible) => {
    const current = new Set(visibleColumnKeys);
    if (visible) current.add(key);
    else current.delete(key);
    persistColumnPreference(dataColumnStorageKeys.filter((candidate) => current.has(candidate)));
  };

  const showAllColumns = () => persistColumnPreference([...dataColumnStorageKeys]);
  const hideAllColumns = () => persistColumnPreference([]);

  const filteredRows = useMemo(() => {
    const filtered = rows.filter((row) => {
      const mode = String(row.track_mode || "NT").toUpperCase();
      // Compare analyst names case-insensitively so RAJ, Raj, and raj match.
      const analyst = normalizeAnalystName(row.analyst || "Unassigned");
      const status = row.approval_status || "PENDING_AASHIL_APPROVAL";
      const isPending = status === "PENDING_AASHIL_APPROVAL" || status === "PENDING_APPROVAL";
      if (trackFilter.length && !trackFilter.includes(mode)) return false;
      if (analystFilter.length && !analystFilter.some((name) => normalizeAnalystName(name) === analyst)) return false;
      if (approvalFilter.length && !approvalFilter.some((selectedStatus) => {
        if (selectedStatus === "PENDING") return isPending;
        return status === selectedStatus;
      })) return false;
      if (search.trim()) {
        const needle = search.trim().toLocaleLowerCase();
        const haystack = (row.cells || []).map((value) => String(value ?? "")).join(" ").toLocaleLowerCase();
        if (!haystack.includes(needle)) return false;
      }
      return true;
    });

    return [...filtered].sort((a, b) => {
      if (sortMode === "excel") return Number(a.excel_row || 0) - Number(b.excel_row || 0);
      const pa = TRACK_PRIORITY[String(a.track_mode || "NT").toUpperCase()] || 99;
      const pb = TRACK_PRIORITY[String(b.track_mode || "NT").toUpperCase()] || 99;
      return pa - pb || Number(a.excel_row || 0) - Number(b.excel_row || 0);
    });
  }, [rows, search, trackFilter, analystFilter, approvalFilter, sortMode]);

  const clearFilters = () => {
    setSearch("");
    setTrackFilter([]);
    setAnalystFilter([]);
    setApprovalFilter([]);
  };

  const openColumnEditor = () => {
    if (!isAdmin) return;
    setNewColumnName("");
    setInsertAfterColumn("");
    setColumnEditorOpen(true);
    setError("");
    setMessage("");
  };

  const addColumn = async (event) => {
    event.preventDefault();
    if (!isAdmin || !newColumnName.trim()) {
      setError("Column name is required.");
      return;
    }
    setBusy(true);
    setError("");
    setMessage("");
    try {
      await request("/coverage-list/columns", {
        method: "POST",
        body: JSON.stringify({
          name: newColumnName.trim(),
          after: insertAfterColumn || null,
        }),
      });
      setColumnEditorOpen(false);
      setNewColumnName("");
      setInsertAfterColumn("");
      await loadGrid(true);
      setMessage(`Column \"${newColumnName.trim()}\" added to the Coverage List Excel.`);
    } catch (e) {
      setError(e?.message || "Failed to add Coverage List column");
    } finally {
      setBusy(false);
    }
  };

  const deleteColumn = async (column) => {
    if (!isAdmin) return;
    const canonical = column.canonical;
    if (["company name", "track mode", "analyst"].includes(canonical)) {
      setError(`${column.label} is a required master column and cannot be deleted.`);
      return;
    }
    if (!window.confirm(`Delete the \"${column.label}\" column from the Coverage List Excel?`)) return;

    setBusy(true);
    setError("");
    setMessage("");
    try {
      await request(`/coverage-list/columns?name=${encodeURIComponent(column.label)}`, { method: "DELETE" });
      await loadGrid(true);
      setMessage(`Column \"${column.label}\" deleted from the Coverage List Excel.`);
    } catch (e) {
      setError(e?.message || `Failed to delete column ${column.label}`);
    } finally {
      setBusy(false);
    }
  };

  const beginAdd = () => {
    const initial = columns.map((column) => {
      if (column.input_type === "readonly") return "-";
      if (column.canonical === "track mode") return "LT";
      if (column.canonical === "analyst") {
        return isAnalyst && isAllowedAnalyst(user?.username)
          ? normalizeAnalystName(user.username)
          : "-";
      }
      return "-";
    });
    setEditingRow({ mode: "new", id: null });
    setEditorValues(initial);
    setError("");
    setMessage("");
  };

  const beginEdit = (row) => {
    if (!isAdmin || !row.id) return;
    setInlineEditId(row.id);
    setInlineDraft({
      track_mode: String(row.track_mode || "NT").toUpperCase(),
      analyst: isDash(row.analyst) ? "" : normalizeAnalystName(row.analyst),
    });
    setError("");
    setMessage("");
  };

  const cancelInlineEdit = () => {
    setInlineEditId(null);
    setInlineDraft({});
  };

  const closeEditor = () => {
    setEditingRow(null);
    setEditorValues([]);
  };

  const setEditorValue = (index, value) => {
    setEditorValues((current) => {
      const next = [...current];
      while (next.length < columns.length) next.push("-");
      next[index] = value === "" ? "-" : value;
      return next;
    });
  };

  const saveRow = async (event) => {
    event.preventDefault();
    if (!canAdd) return;
    if (editingRow?.mode === "edit" && !isAdmin) return;

    const cells = columns.map((column, index) => (
      column.input_type === "readonly" ? (editorValues[index] || "-") : (isDash(editorValues[index]) ? "-" : editorValues[index])
    ));
    const companyIndex = columns.findIndex((column) => column.canonical === "company name");
    if (companyIndex < 0 || isDash(cells[companyIndex])) {
      setError("Company is required.");
      return;
    }

    // Analyst is a constrained workflow field, not an arbitrary platform user.
    const analystIndex = columns.findIndex((column) => column.canonical === "analyst");
    if (analystIndex >= 0 && !isDash(cells[analystIndex])) {
      const value = normalizeAnalystName(cells[analystIndex]);
      if (!isAllowedAnalyst(value)) {
        setError("Analyst must be ahmed, jainam, raj, romil, or Aashil (also accepted as Ashil).");
        return;
      }
      cells[analystIndex] = value;
    }

    setSaving(true);
    setError("");
    setMessage("");
    try {
      if (editingRow?.mode === "new") {
        await api.createCoverageListRow({ cells });
        setMessage(isAdmin ? "Company added to the master Coverage List." : "Company submitted for admin approval.");
      } else {
        await api.updateCoverageListRow(editingRow.id, { cells });
        setMessage("Coverage List updated.");
      }
      closeEditor();
      await loadGrid(true);
    } catch (e) {
      setError(e?.message || "Failed to save Coverage List row");
    } finally {
      setSaving(false);
    }
  };

  const approveRow = async (row) => {
    if (!isAdmin || !row.id) return;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      await request(`/coverage-list/${row.id}/approve`, { method: "POST" });
      setMessage(`${row.company_name} approved.`);
      await loadGrid(true);
    } catch (e) {
      setError(e?.message || "Failed to approve company");
    } finally {
      setBusy(false);
    }
  };

  const rejectRow = async (row) => {
    if (!isAdmin || !row.id) return;
    if (!window.confirm(`Reject ${row.company_name || "this company"}?`)) return;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      await request(`/coverage-list/${row.id}/reject`, { method: "POST" });
      setMessage(`${row.company_name} rejected.`);
      await loadGrid(true);
    } catch (e) {
      setError(e?.message || "Failed to reject company");
    } finally {
      setBusy(false);
    }
  };

  const saveInlineAssignment = async (row) => {
    if (!isAdmin || !row.id) return;
    const trackMode = String(inlineDraft.track_mode || row.track_mode || "NT").toUpperCase();
    const analyst = inlineDraft.analyst ? normalizeAnalystName(inlineDraft.analyst) : null;

    if (!TRACK_ORDER.includes(trackMode)) {
      setError("Priority must be PO, HT, LT, or NT.");
      return;
    }
    if (analyst && !isAllowedAnalyst(analyst)) {
      setError("Analyst must be ahmed, jainam, raj, romil, or Aashil (also accepted as Ashil).");
      return;
    }

    setBusy(true);
    setError("");
    setMessage("");
    try {
      await request(`/coverage-list/${row.id}/assignment`, {
        method: "PATCH",
        body: JSON.stringify({
          track_mode: trackMode,
          analyst,
        }),
      });
      setMessage(`${row.company_name} priority / analyst updated.`);
      cancelInlineEdit();
      await loadGrid(true);
    } catch (e) {
      setError(e?.message || "Failed to update priority / analyst");
    } finally {
      setBusy(false);
    }
  };

  const removeRow = async (row) => {
    if (!isAdmin || !row.id) return;
    const name = row.company_name || "this company";
    if (!window.confirm(`Remove ${name} from the master Coverage List and Excel?`)) return;

    setBusy(true);
    setError("");
    setMessage("");
    try {
      await api.deleteCoverageListRow(row.id);
      if (editingRow?.id === row.id) closeEditor();
      setMessage(`${name} removed from the master Coverage List.`);
      await loadGrid(true);
    } catch (e) {
      setError(e?.message || "Failed to remove Coverage List row");
    } finally {
      setBusy(false);
    }
  };

  const refresh = async () => {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      await loadGrid(false);
      setMessage("Coverage List refreshed from Excel.");
    } finally {
      setBusy(false);
    }
  };

  const downloadExcel = async () => {
    setDownloading(true);
    setError("");
    try {
      const blob = await api.downloadCoverageList();
      const url = window.URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = "Coverage_List.xlsx";
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.URL.revokeObjectURL(url);
      setMessage("Current master Excel downloaded.");
    } catch (e) {
      setError(e?.message || "Failed to download Coverage List Excel");
    } finally {
      setDownloading(false);
    }
  };

  const uploadExcel = async (event) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file || !isAdmin) return;

    setBusy(true);
    setError("");
    setMessage("");
    try {
      const result = await api.uploadCoverageList(file);
      await loadGrid(true);
      setMessage(`Excel imported · ${result?.rows ?? 0} rows · ${result?.created ?? 0} created · ${result?.updated ?? 0} updated.`);
    } catch (e) {
      setError(e?.message || "Coverage List Excel import failed");
    } finally {
      setBusy(false);
    }
  };

  if (loading && !grid) return <Loading />;

  const pendingCount = approvalCounts.PENDING_AASHIL_APPROVAL || approvalCounts.PENDING_APPROVAL || 0;
  const allCount = grid?.counts?.total || 0;

  return (
    <div className="space-y-4">
      <PageHeader
        eyebrow="MASTER LIST"
        title="Coverage List"
        description="Excel is the canonical Coverage List. Columns and values follow the workbook automatically. Priority is PO → HT → LT → NT."
        action={(
          <div className="flex flex-wrap gap-2">
            <button type="button" onClick={refresh} disabled={busy} className="inline-flex items-center gap-2 rounded-md border border-slate-300 bg-white px-3 py-2 text-sm font-semibold text-slate-700 hover:bg-slate-50 disabled:opacity-50">
              <RefreshCw size={15} className={busy ? "animate-spin" : ""} /> Refresh
            </button>
            <button type="button" onClick={downloadExcel} disabled={downloading || busy} className="inline-flex items-center gap-2 rounded-md border border-slate-300 bg-white px-3 py-2 text-sm font-semibold text-slate-700 hover:bg-slate-50 disabled:opacity-50">
              <Download size={15} /> {downloading ? "Downloading…" : "Excel"}
            </button>
            {isAdmin && (
              <>
                <input ref={fileRef} type="file" accept=".xlsx" onChange={uploadExcel} className="hidden" />
                <button type="button" onClick={() => fileRef.current?.click()} disabled={busy} className="inline-flex items-center gap-2 rounded-md border border-slate-300 bg-white px-3 py-2 text-sm font-semibold text-slate-700 hover:bg-slate-50 disabled:opacity-50">
                  <Upload size={15} /> Upload
                </button>
              </>
            )}
            {canAdd && (
              <button type="button" onClick={beginAdd} disabled={busy} className="inline-flex items-center gap-2 rounded-md bg-slate-900 px-3 py-2 text-sm font-semibold text-white hover:bg-slate-800 disabled:opacity-50">
                <Plus size={15} /> Add Company
              </button>
            )}
          </div>
        )}
      />

      {(error || message) && (
        <div className="space-y-2">
          {syncHealth && !syncHealth.nse_ready && (
        <div className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
          <div className="font-semibold">Coverage List sync / NSE readiness issue</div>
          <div className="mt-1">
            {syncHealth.last_error ||
              (syncHealth.missing_nse_symbol_count
                ? `${syncHealth.missing_nse_symbol_count} active approved companies are missing NSE Symbol.`
                : "The workbook has not been accepted by the backend yet.")}
          </div>
          {Array.isArray(syncHealth.missing_nse_symbols) && syncHealth.missing_nse_symbols.length > 0 && (
            <div className="mt-1 break-words text-xs">Missing NSE symbols: {syncHealth.missing_nse_symbols.slice(0, 15).join(", ")}{syncHealth.missing_nse_symbols.length > 15 ? " …" : ""}</div>
          )}
        </div>
      )}
      {error && <ErrorBox error={error} />}
          {message && !error && <div className="rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-700">{message}</div>}
        </div>
      )}

      <div className="overflow-hidden rounded-lg border border-slate-300 bg-white">
        <div className="border-b border-slate-300 bg-slate-50 px-4 py-3">
          <div className="flex flex-col gap-3 xl:flex-row xl:items-center xl:justify-between">
            <div className="flex flex-wrap items-center gap-2">
              <FilterButton label="All" count={allCount} active={!trackFilter.length && !analystFilter.length && !approvalFilter.length} onClick={clearFilters} />
              {isAdmin && (
                <FilterButton
                  label="Pending"
                  count={pendingCount}
                  active={approvalFilter.length === 1 && approvalFilter[0] === "PENDING"}
                  onClick={() => setApprovalFilter((current) => current.length === 1 && current[0] === "PENDING" ? [] : ["PENDING"])}
                />
              )}
            </div>

            <div className="flex flex-wrap items-center gap-2">
              <MultiSelectDropdown
                label="Tracks"
                allLabel="All Tracks"
                totalCount={allCount}
                selectedValues={trackFilter}
                onChange={setTrackFilter}
                options={TRACK_ORDER.map((mode) => ({ value: mode, label: `${mode} · ${TRACK_LABELS[mode]}`, count: trackCounts[mode] || 0 }))}
                widthClass="w-48"
              />
              <MultiSelectDropdown
                label="Analysts"
                allLabel="All Analysts"
                totalCount={Object.values(analystCounts).reduce((sum, count) => sum + count, 0)}
                selectedValues={analystFilter}
                onChange={setAnalystFilter}
                options={[
                  ...ALLOWED_ANALYSTS.map((name) => ({ value: name, label: analystLabel(name), count: analystCounts[name] || 0 })),
                  ...(analystCounts.unassigned ? [{ value: "Unassigned", label: "Unassigned", count: analystCounts.unassigned }] : []),
                ]}
                widthClass="w-44"
              />
              <MultiSelectDropdown
                label="Statuses"
                allLabel="All Status"
                totalCount={allCount}
                selectedValues={approvalFilter}
                onChange={setApprovalFilter}
                options={[
                  { value: "APPROVED", label: "Approved", count: approvalCounts.APPROVED || 0 },
                  { value: "PENDING", label: "Pending", count: pendingCount },
                  { value: "REJECTED", label: "Rejected", count: approvalCounts.REJECTED || 0 },
                ]}
                widthClass="w-40"
              />
              <select value={sortMode} onChange={(e) => setSortMode(e.target.value)} className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-semibold text-slate-700 outline-none">
                <option value="priority">Priority: PO → HT → LT → NT</option>
                <option value="excel">Excel row order</option>
              </select>

              <div className="relative">
                <button
                  type="button"
                  onClick={() => setColumnMenuOpen((current) => !current)}
                  className="inline-flex items-center gap-2 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-semibold text-slate-700 hover:bg-slate-50"
                  aria-expanded={columnMenuOpen}
                  aria-haspopup="menu"
                >
                  <Columns3 size={14} />
                  Columns ({visibleColumns.length}/{dataColumns.length})
                </button>
                {columnMenuOpen && (
                  <div className="absolute right-0 z-40 mt-2 w-80 rounded-lg border border-slate-300 bg-white p-3 shadow-lg">
                    <div className="mb-2 flex items-start justify-between gap-2">
                      <div>
                        <div className="text-xs font-bold text-slate-900">Visible columns</div>
                        <div className="text-[10px] text-slate-500">Per-user selection · synced from Excel headers</div>
                      </div>
                      <div className="flex gap-1">
                        <button type="button" onClick={showAllColumns} className="rounded border border-slate-300 px-2 py-1 text-[10px] font-semibold text-slate-600 hover:bg-slate-50">All</button>
                        <button type="button" onClick={hideAllColumns} className="rounded border border-slate-300 px-2 py-1 text-[10px] font-semibold text-slate-600 hover:bg-slate-50">None</button>
                      </div>
                    </div>
                    {isAdmin && (
                      <button type="button" onClick={openColumnEditor} className="mb-2 inline-flex w-full items-center justify-center gap-2 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-semibold text-slate-700 hover:bg-slate-50">
                        <Plus size={13} /> Add Column
                      </button>
                    )}
                    <input
                      value={columnPickerSearch}
                      onChange={(e) => setColumnPickerSearch(e.target.value)}
                      placeholder="Search columns…"
                      className="mb-2 w-full rounded-md border border-slate-300 bg-white px-2.5 py-1.5 text-xs text-slate-700 outline-none focus:border-slate-500"
                      aria-label="Search coverage columns"
                    />
                    <div className="mb-2 flex items-center justify-between text-[10px] text-slate-500">
                      <span>{visibleColumns.length} selected</span>
                      <span>{dataColumns.length} available from Excel</span>
                    </div>
                    <div className="max-h-64 space-y-1 overflow-y-auto pr-1">
                      {filteredColumnOptions.map((column) => {
                        const checked = visibleColumnKeys.includes(column.storage_key || column.key);
                        return (
                          <div key={column.key} className="flex items-center gap-1 rounded px-2 py-1.5 hover:bg-slate-50">
                            <label className="flex min-w-0 flex-1 cursor-pointer items-center gap-2">
                              <input
                                type="checkbox"
                                checked={checked}
                                onChange={(e) => setColumnVisible(column.storage_key || column.key, e.target.checked)}
                                className="h-3.5 w-3.5 rounded border-slate-300"
                              />
                              <span className="min-w-0 flex-1 truncate text-xs text-slate-700" title={column.label}>{column.label}</span>
                            </label>
                            {isAdmin && (
                              ["company name", "track mode", "analyst"].includes(column.canonical) ? (
                                <span className="px-1 text-[10px] text-slate-300" title="Required master column">•</span>
                              ) : (
                                <button
                                  type="button"
                                  onClick={() => deleteColumn(column)}
                                  disabled={busy}
                                  className="rounded p-1 text-slate-400 hover:bg-red-50 hover:text-red-600 disabled:opacity-40"
                                  title={`Delete ${column.label} from Excel`}
                                >
                                  <Trash2 size={12} />
                                </button>
                              )
                            )}
                          </div>
                        );
                      })}
                      {!filteredColumnOptions.length ? (
                        <div className="px-2 py-4 text-center text-xs text-slate-500">No columns match.</div>
                      ) : null}
                    </div>
                  </div>
                )}
              </div>

              <div className="relative min-w-[220px]">
                <Search size={15} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-400" />
                <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search any column…" className="w-full rounded-md border border-slate-300 bg-white py-1.5 pl-8 pr-3 text-xs outline-none focus:border-slate-500" />
              </div>
              <button type="button" onClick={clearFilters} className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-semibold text-slate-700 hover:bg-slate-50">Clear Filters</button>
              <span className="text-xs text-slate-500">{filteredRows.length} / {rows.length}</span>
            </div>
          </div>
        </div>

        {editingRow && canAdd && (editingRow.mode === "new" || isAdmin) && (
          <div className="border-b border-slate-300 bg-white p-4">
            <div className="mb-3 flex items-center justify-between">
              <div>
                <div className="text-sm font-bold text-slate-900">{editingRow.mode === "new" ? "Add Company" : "Edit Company"}</div>
                <div className="text-xs text-slate-500">
                  Fields follow the current Excel columns. {isAnalyst && editingRow.mode === "new" ? "Your submission will remain pending until an admin approves it." : "Admin changes are published to the master immediately."}
                </div>
              </div>
              <button type="button" onClick={closeEditor} className="rounded-md p-1.5 text-slate-400 hover:bg-slate-100 hover:text-slate-800"><X size={16} /></button>
            </div>
            <form onSubmit={saveRow} className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
              {columns.filter((column) => column.input_type !== "readonly").map((column) => {
                const index = column.index;
                const value = editorValues[index] ?? "-";
                const labelClass = "mb-1 block text-[11px] font-bold uppercase tracking-wide text-slate-500";
                const inputClass = "w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm outline-none focus:border-slate-500";

                if (column.input_type === "track") {
                  return (
                    <label key={column.key} className="block">
                      <span className={labelClass}>{column.label}</span>
                      <select value={isDash(value) ? "NT" : value} onChange={(e) => setEditorValue(index, e.target.value)} className={inputClass}>
                        {TRACK_ORDER.map((mode) => <option key={mode} value={mode}>{mode} — {TRACK_LABELS[mode]}</option>)}
                      </select>
                    </label>
                  );
                }

                if (column.input_type === "analyst") {
                  const currentAnalyst = isDash(value) ? "" : normalizeAnalystName(value);
                  const analystOptions = currentAnalyst && !isAllowedAnalyst(currentAnalyst)
                    ? [currentAnalyst, ...ALLOWED_ANALYSTS]
                    : ALLOWED_ANALYSTS;
                  return (
                    <label key={column.key} className="block">
                      <span className={labelClass}>{labelClass.includes("uppercase") ? column.label : column.label}</span>
                      <select value={currentAnalyst} onChange={(e) => setEditorValue(index, e.target.value || "-")} className={inputClass}>
                        <option value="">-</option>
                        {analystOptions.map((name) => (
                          <option key={name} value={name}>
                            {analystLabel(name)}{name === currentAnalyst && !isAllowedAnalyst(name) ? " (current)" : ""}
                          </option>
                        ))}
                      </select>
                    </label>
                  );
                }

                if (column.input_type === "date") {
                  const asText = isDash(value) || !isValidDateInput(value);
                  return (
                    <label key={column.key} className="block">
                      <span className={labelClass}>{column.label}</span>
                      {asText ? (
                        <input type="text" value={isDash(value) ? "" : value} onChange={(e) => setEditorValue(index, e.target.value)} placeholder="-" className={inputClass} />
                      ) : (
                        <input type="date" value={value} onChange={(e) => setEditorValue(index, e.target.value)} className={inputClass} />
                      )}
                    </label>
                  );
                }

                return (
                  <label key={column.key} className="block">
                    <span className={labelClass}>{column.label}</span>
                    <input required={column.canonical === "company name"} type={column.input_type === "number" ? "number" : "text"} value={isDash(value) ? "" : value} onChange={(e) => setEditorValue(index, e.target.value)} placeholder="-" className={inputClass} />
                  </label>
                );
              })}
              <div className="flex justify-end gap-2 md:col-span-2 xl:col-span-3">
                <button type="button" onClick={closeEditor} className="rounded-md border border-slate-300 bg-white px-4 py-2 text-sm font-semibold text-slate-700 hover:bg-slate-50">Cancel</button>
                <button type="submit" disabled={saving} className="rounded-md bg-slate-900 px-4 py-2 text-sm font-semibold text-white hover:bg-slate-800 disabled:opacity-50">{saving ? "Saving…" : editingRow.mode === "new" ? (isAdmin ? "Add Company" : "Submit for Approval") : "Save"}</button>
              </div>
            </form>
          </div>
        )}

        {columnEditorOpen && isAdmin && (
          <div className="border-b border-slate-300 bg-slate-50 p-4">
            <div className="mb-3 flex items-center justify-between">
              <div>
                <div className="text-sm font-bold text-slate-900">Add Excel Column</div>
                <div className="text-xs text-slate-500">Adds a real column to the canonical Coverage List workbook. All current rows receive “-”.</div>
              </div>
              <button type="button" onClick={() => setColumnEditorOpen(false)} className="rounded-md p-1.5 text-slate-400 hover:bg-slate-100 hover:text-slate-800"><X size={16} /></button>
            </div>
            <form onSubmit={addColumn} className="grid gap-3 md:grid-cols-[1fr_1fr_auto] md:items-end">
              <label className="block">
                <span className="mb-1 block text-[11px] font-bold uppercase tracking-wide text-slate-500">Column name</span>
                <input
                  autoFocus
                  value={newColumnName}
                  onChange={(e) => setNewColumnName(e.target.value)}
                  placeholder="e.g. Sector, Theme, Investment, Profit"
                  className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm outline-none focus:border-slate-500"
                />
              </label>
              <label className="block">
                <span className="mb-1 block text-[11px] font-bold uppercase tracking-wide text-slate-500">Insert after</span>
                <select
                  value={insertAfterColumn}
                  onChange={(e) => setInsertAfterColumn(e.target.value)}
                  className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm outline-none focus:border-slate-500"
                >
                  <option value="">End of Excel</option>
                  {dataColumns.map((column) => <option key={column.key} value={column.label}>{column.label}</option>)}
                </select>
              </label>
              <div className="flex gap-2">
                <button type="button" onClick={() => setColumnEditorOpen(false)} className="rounded-md border border-slate-300 bg-white px-4 py-2 text-sm font-semibold text-slate-700 hover:bg-slate-50">Cancel</button>
                <button type="submit" disabled={busy || !newColumnName.trim()} className="inline-flex items-center gap-2 rounded-md bg-slate-900 px-4 py-2 text-sm font-semibold text-white hover:bg-slate-800 disabled:opacity-50"><Plus size={15} /> Add</button>
              </div>
            </form>
          </div>
        )}

        <div className="max-h-[calc(100vh-290px)] overflow-auto">
          {filteredRows.length === 0 ? (
            <div className="p-10 text-center text-sm text-slate-500">No Coverage List rows match the current filters.</div>
          ) : (
            <table className="min-w-full border-collapse bg-white" style={{ fontFamily: "Arial, Calibri, sans-serif" }}>
              <thead className="sticky top-0 z-20 bg-slate-900 text-white">
                <tr>
                  {renderColumns.map((column) => (
                    <th key={column.key} className="border border-slate-900 px-3 py-2 text-center text-sm font-bold whitespace-nowrap">{column.label}</th>
                  ))}
                  <th className="sticky right-0 border border-slate-900 bg-slate-900 px-3 py-2 text-center text-sm font-bold">Status / Actions</th>
                </tr>
              </thead>
              <tbody>
                {filteredRows.map((row, rowIndex) => {
                  const pending = row.approval_status !== "APPROVED" && row.approval_status !== "REJECTED";
                  const isEditing = isAdmin && inlineEditId === row.id;
                  const currentTrack = String(inlineDraft.track_mode ?? row.track_mode ?? "NT").toUpperCase();
                  const currentAnalyst = normalizeAnalystName(inlineDraft.analyst ?? (row.analyst || ""));
                  return (
                    <tr key={row.id || `${row.excel_row}-${rowIndex}`} className="bg-white hover:bg-slate-50">
                      {renderColumns.map((column) => {
                        const value = row.cells?.[column.index];
                        const isCompany = column.canonical === "company name";
                        const isTrack = column.canonical === "track mode";
                        const isAnalystColumn = column.canonical === "analyst";
                        const editInputClass = "w-full rounded border border-slate-300 bg-white px-2 py-1.5 text-xs font-semibold text-slate-700 outline-none focus:border-slate-500";

                        return (
                          <td key={column.key} className={[
                            "border border-slate-300 px-3 py-2 text-sm text-slate-900",
                            isTrack || isAnalystColumn ? "text-center" : "",
                            isCompany ? "whitespace-pre-line font-medium" : "whitespace-nowrap",
                          ].join(" ")}>
                            {isEditing && isTrack ? (
                              <select
                                value={currentTrack}
                                onChange={(e) => setInlineDraft((current) => ({ ...current, track_mode: e.target.value }))}
                                className={editInputClass}
                                aria-label={`Change priority for ${row.company_name || "company"}`}
                              >
                                {TRACK_ORDER.map((mode) => (
                                  <option key={mode} value={mode}>{mode}</option>
                                ))}
                              </select>
                            ) : isEditing && isAnalystColumn ? (
                              <select
                                value={currentAnalyst}
                                onChange={(e) => setInlineDraft((current) => ({ ...current, analyst: e.target.value }))}
                                className={editInputClass}
                                aria-label={`Change analyst for ${row.company_name || "company"}`}
                              >
                                <option value="">-</option>
                                {ALLOWED_ANALYSTS.map((name) => (
                                  <option key={name} value={name}>{analystLabel(name)}</option>
                                ))}
                                {currentAnalyst && !isAllowedAnalyst(currentAnalyst) ? (
                                  <option value={currentAnalyst}>{analystLabel(currentAnalyst)} — current</option>
                                ) : null}
                              </select>
                            ) : (
                              displayValue(value)
                            )}
                          </td>
                        );
                      })}
                      <td className="sticky right-0 border border-slate-300 bg-white px-2 py-1.5">
                        <div className="flex min-w-[150px] flex-col items-center gap-1.5">
                          <span className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide ${approvalClass(row.approval_status)}`}>
                            {pending ? <Clock3 size={11} /> : row.approval_status === "REJECTED" ? <XCircle size={11} /> : <Check size={11} />}
                            {approvalLabel(row.approval_status)}
                          </span>

                          {isEditing ? (
                            <div className="flex flex-wrap justify-center gap-1">
                              <button
                                type="button"
                                onClick={() => saveInlineAssignment(row)}
                                disabled={busy}
                                className="rounded-md border border-emerald-200 bg-emerald-50 px-2.5 py-1 text-[10px] font-bold text-emerald-700 hover:bg-emerald-100 disabled:opacity-50"
                                title="Save analyst and priority changes"
                              >
                                Save
                              </button>
                              <button
                                type="button"
                                onClick={cancelInlineEdit}
                                disabled={busy}
                                className="rounded-md border border-slate-300 bg-white px-2.5 py-1 text-[10px] font-bold text-slate-600 hover:bg-slate-50 disabled:opacity-50"
                                title="Cancel edit"
                              >
                                Cancel
                              </button>
                            </div>
                          ) : (
                            <div className="flex flex-wrap justify-center gap-1">
                              {isAdmin && row.id && pending && (
                                <>
                                  <button type="button" onClick={() => approveRow(row)} disabled={busy} className="rounded-md border border-emerald-200 p-1.5 text-emerald-700 hover:bg-emerald-50 disabled:opacity-50" title="Approve"><Check size={14} /></button>
                                  <button type="button" onClick={() => rejectRow(row)} disabled={busy} className="rounded-md border border-red-200 p-1.5 text-red-700 hover:bg-red-50 disabled:opacity-50" title="Reject"><XCircle size={14} /></button>
                                </>
                              )}
                              {isAdmin && row.id && (
                                <button type="button" onClick={() => beginEdit(row)} disabled={busy || inlineEditId !== null} className="rounded-md border border-slate-300 p-1.5 text-slate-600 hover:bg-slate-50 disabled:opacity-50" title="Edit analyst and priority"><Edit3 size={14} /></button>
                              )}
                              {isAdmin && row.id && <button type="button" onClick={() => removeRow(row)} disabled={busy} className="rounded-md border border-red-200 p-1.5 text-red-600 hover:bg-red-50 disabled:opacity-50" title="Delete"><Trash2 size={14} /></button>}
                            </div>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-4 text-xs text-slate-500">
        <span>Master rows: <strong className="text-slate-900">{allCount}</strong></span>
        {TRACK_ORDER.map((mode) => <span key={mode}>{mode}: <strong className="text-slate-900">{trackCounts[mode] || 0}</strong></span>)}
        <span>Pending approval: <strong className="text-slate-900">{pendingCount}</strong></span>
        <span>Columns: <strong className="text-slate-900">{visibleColumns.length} / {dataColumns.length}</strong></span>
        <span>Analysts: <strong className="text-slate-900">{ALLOWED_ANALYSTS.map((name) => analystLabel(name)).join(", ")}</strong></span>
        <span>Source: <strong className="text-slate-900">Coverage List Excel</strong></span>
      </div>
    </div>
  );
}