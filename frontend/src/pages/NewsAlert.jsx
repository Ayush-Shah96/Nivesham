import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
  ChevronDown,
  ExternalLink,
  RefreshCw,
  Search,
  X,
  Landmark,
  Globe,
  Youtube,
  AtSign,
  Plus,
  Trash2,
  CheckCircle2,
} from "lucide-react";
import { api } from "../lib/api";
import { Card, ErrorBox, PageHeader } from "../components/UI";

const PAGE_SIZE = 100;
const BACKGROUND_REFRESH_MS = 15 * 1000;
const REGULATORY_TYPE = "REGULATORY";

const TYPE_OPTIONS = [
  { value: "NEWS", label: "News" },
  { value: "POST", label: "Post" },
  { value: "INTERVIEW", label: "YouTube Interviews" },
];

const TRACK_OPTIONS = [
  { value: "HT", label: "HT" },
  { value: "LT", label: "LT" },
  { value: "PO", label: "PO" },
  { value: "NT", label: "Others" },
];

const ANALYST_OPTIONS = ["Aashil", "Jainam", "Raj", "Romil", "Ahmed"];

const DATE_SORT_OPTIONS = [
  { value: "desc", label: "Newest first" },
  { value: "asc", label: "Oldest first" },
];

const RELEVANCE_THRESHOLD_OPTIONS = [
  { value: "", label: "All scores" },
  { value: "50", label: "50 and above" },
  { value: "60", label: "60 and above" },
  { value: "70", label: "70 and above" },
  { value: "80", label: "80 and above" },
  { value: "90", label: "90 and above" },
];

const SOURCE_STYLES = {
  youtube: {
    row: "bg-red-50/40 hover:bg-red-50",
    text: "text-red-700",
    border: "border-red-200",
    badge: "border-red-200 bg-red-50 text-red-700",
  },
  x: {
    row: "bg-purple-50/40 hover:bg-purple-50",
    text: "text-purple-700",
    border: "border-purple-200",
    badge: "border-purple-200 bg-purple-50 text-purple-700",
  },
  regulatory: {
    row: "bg-green-50/40 hover:bg-green-50",
    text: "text-green-700",
    border: "border-green-200",
    badge: "border-green-200 bg-green-50 text-green-700",
  },
  other: {
    row: "bg-white hover:bg-slate-50/70",
    text: "text-black",
    border: "border-slate-200",
    badge: "border-slate-300 bg-white text-black",
  },
};

const TRACK_STYLES = {
  PO: "border-red-200 bg-red-50 text-red-700",
  HT: "border-orange-200 bg-orange-50 text-orange-700",
  LT: "border-blue-200 bg-blue-50 text-blue-700",
  NT: "border-slate-300 bg-slate-100 text-slate-700",
};

function displayDate(value) {
  if (!value) return "—";

  const parsed = new Date(value);

  if (Number.isNaN(parsed.getTime())) return "—";

  return parsed.toLocaleDateString("en-IN", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    timeZone: "Asia/Kolkata",
  });
}

function timestampOf(row) {
  if (!row?.timestamp) return null;

  const value = new Date(row.timestamp).getTime();

  return Number.isFinite(value) ? value : null;
}

function relevanceScoreOf(row) {
  if (
    row?.relevance_score === null ||
    row?.relevance_score === undefined ||
    row?.relevance_score === ""
  ) {
    return null;
  }

  const score = Number(row.relevance_score);

  return Number.isFinite(score) ? score : null;
}

// A row is regulatory only if its source is NSE or BSE (official filings).
// YouTube interviews, X posts and news articles are never regulatory.
function isRegulatoryRow(row) {
  const type = String(row?.content_type || "NEWS").toUpperCase();
  const source = String(row?.source || "").toLowerCase();
  const url = String(row?.url || "").toLowerCase();

  if (type === "INTERVIEW" || type === "POST") return false;

  if (
    source.includes("youtube") ||
    source.includes("twitter") ||
    url.includes("youtube.com") ||
    url.includes("youtu.be") ||
    url.includes("x.com/") ||
    url.includes("twitter.com/")
  ) {
    return false;
  }

  return (
    /\b(nse|bse)\b/.test(source) ||
    source.includes("corporate announcement") ||
    source.includes("regulatory announcement")
  );
}

function sourcePresentation(row) {
  const type = String(row?.content_type || "NEWS").toUpperCase();
  const source = String(row?.source || "").toLowerCase();
  const url = String(row?.url || "").toLowerCase();

  if (
    type === "INTERVIEW" ||
    source.includes("youtube") ||
    url.includes("youtube.com") ||
    url.includes("youtu.be")
  ) {
    return {
      kind: "youtube",
      label: "YouTube Interview",
      ...SOURCE_STYLES.youtube,
    };
  }

  if (
    type === "POST" ||
    source.includes("twitter") ||
    source.includes("x /") ||
    source === "x" ||
    url.includes("x.com/") ||
    url.includes("twitter.com/")
  ) {
    return {
      kind: "x",
      label: "X / Twitter",
      ...SOURCE_STYLES.x,
    };
  }

  if (isRegulatoryRow(row)) {
    return {
      kind: "regulatory",
      label: "Regulatory",
      ...SOURCE_STYLES.regulatory,
    };
  }

  return {
    kind: "other",
    label: "News",
    ...SOURCE_STYLES.other,
  };
}

function FilterHeaderButton({
  label,
  active,
  open,
  onClick,
  sortControl = null,
}) {
  return (
    <div className="inline-flex items-center gap-1.5">
      <button
        type="button"
        data-newsalert-filter-trigger="true"
        onClick={onClick}
        aria-expanded={open}
        className={`inline-flex items-center gap-1.5 rounded-md px-1.5 py-1 font-medium uppercase tracking-wide transition hover:bg-slate-100 hover:text-slate-900 ${
          open
            ? "bg-slate-100 text-slate-900"
            : "text-slate-600"
        }`}
      >
        <span>{label}</span>

        {active && (
          <span
            aria-label="Filter active"
            className="h-1.5 w-1.5 rounded-full bg-blue-600"
          />
        )}

        <ChevronDown
          size={12}
          className={`transition-transform ${open ? "rotate-180" : ""}`}
        />
      </button>

      {sortControl}
    </div>
  );
}

function MultiSelectOptions({
  options,
  selected,
  onChange,
  onClear,
  onDone,
}) {
  const toggleOption = (value, checked) => {
    onChange(
      checked
        ? [...selected, value]
        : selected.filter((item) => item !== value),
    );
  };

  return (
    <>
      <div className="max-h-60 overflow-y-auto">
        {options.map((option) => (
          <label
            key={option.value}
            className="flex cursor-pointer items-center gap-2 rounded-lg px-2 py-2 text-sm text-slate-700 hover:bg-slate-50"
          >
            <input
              type="checkbox"
              checked={selected.includes(option.value)}
              onChange={(event) =>
                toggleOption(option.value, event.target.checked)
              }
              className="h-4 w-4 rounded border-slate-300 accent-slate-800"
            />

            <span>{option.label}</span>
          </label>
        ))}
      </div>

      <div className="mt-2 flex justify-between border-t border-slate-100 pt-2">
        <button
          type="button"
          onClick={onClear}
          className="px-2 py-1 text-xs font-medium text-slate-600 hover:text-slate-900"
        >
          Clear
        </button>

        <button
          type="button"
          onClick={onDone}
          className="rounded-md bg-slate-900 px-2.5 py-1 text-xs font-medium text-white"
        >
          Done
        </button>
      </div>
    </>
  );
}

function SelectOptions({ label, value, options, onChange, onDone }) {
  return (
    <div className="space-y-2 p-1">
      <label className="block text-xs font-semibold text-slate-600">
        {label}

        <select
          value={value}
          onChange={(event) => onChange(event.target.value)}
          className="mt-1.5 block w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-sm font-normal text-slate-800 outline-none focus:border-slate-500"
        >
          {options.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </label>

      <div className="flex justify-end border-t border-slate-100 pt-2">
        <button
          type="button"
          onClick={onDone}
          className="rounded-md bg-slate-900 px-2.5 py-1.5 text-xs font-medium text-white"
        >
          Done
        </button>
      </div>
    </div>
  );
}

export default function NewsAlert({ user }) {
  const [rows, setRows] = useState([]);
  const [analystOptions, setAnalystOptions] = useState(ANALYST_OPTIONS);

  const [filters, setFilters] = useState({
    search: "",
    types: [],
    tracks: [],
    analysts: [],
    dateFrom: "",
    dateTo: "",
  });

  const [openDropdown, setOpenDropdown] = useState("");
  const [dropdownPosition, setDropdownPosition] = useState(null);

  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  // Date sorting defaults to newest first.
  const [dateSort, setDateSort] = useState("desc");

  // Relevance sorting and minimum score filter.
  const [relevanceSort, setRelevanceSort] = useState(null);
  const [minRelevance, setMinRelevance] = useState("");

  const hasLoadedRef = useRef(false);

  const canRefresh =
    user?.role === "ADMIN" || user?.role === "ANALYST";
  const canManageSources = canRefresh;
  const canManageXAccounts = Boolean(user?.id);

  const [activeTab, setActiveTab] = useState("feed");
  const [customSources, setCustomSources] = useState([]);
  const [customSourcesLoading, setCustomSourcesLoading] = useState(false);
  const [sourceType, setSourceType] = useState(user?.role === "INVESTMENT" ? "X" : "WEBSITE");
  const [sourceValue, setSourceValue] = useState("");
  const [sourceLabel, setSourceLabel] = useState("");
  const [sourceSaving, setSourceSaving] = useState(false);
  const [sourceDeletingId, setSourceDeletingId] = useState(null);
  const [sourceError, setSourceError] = useState("");
  const [sourceMessage, setSourceMessage] = useState("");

  // Investment users can manage only personal X handles; select that option
  // once their authenticated role has loaded (the first render may have no user).
  useEffect(() => {
    setSourceType(user?.role === "INVESTMENT" ? "X" : "WEBSITE");
  }, [user?.role]);

  // Regulatory is exclusive: when selected, nothing else is shown.
  const regulatoryOnly =
    filters.types.length === 1 &&
    filters.types[0] === REGULATORY_TYPE;

  const activeFilterCount = useMemo(
    () =>
      filters.types.length +
      filters.tracks.length +
      filters.analysts.length +
      Number(Boolean(filters.dateFrom)) +
      Number(Boolean(filters.dateTo)) +
      Number(minRelevance !== ""),
    [filters, minRelevance],
  );

  const load = useCallback(
    async ({ forceRefresh = false, quiet = false } = {}) => {
      if (!quiet) setLoading(true);

      setError("");

      try {
        const data = await api.news({
          company: filters.search,
          types: regulatoryOnly ? [] : filters.types,
          tracks: filters.tracks,
          analysts: filters.analysts,
          includeRegulatory: true,
          regulatoryOnly,
          dateFrom: filters.dateFrom,
          dateTo: filters.dateTo,
          limit: PAGE_SIZE,
          offset,
          retry: true,
          forceRefresh,
        });

        const raw = Array.isArray(data) ? data : [];

        let next;

        if (regulatoryOnly) {
          next = raw.filter(isRegulatoryRow);
        } else if (filters.types.length > 0) {
          next = raw.filter(
            (row) =>
              !isRegulatoryRow(row) &&
              filters.types.includes(
                String(row?.content_type || "NEWS").toUpperCase(),
              ),
          );
        } else {
          next = raw;
        }

        setRows(next);
        setHasMore(raw.length === PAGE_SIZE);
        hasLoadedRef.current = true;
      } catch (e) {
        if (!quiet) setRows([]);

        setError(e?.message || "Unable to load NewsAlert");
      } finally {
        setLoading(false);
      }
    },
    [filters, offset, regulatoryOnly],
  );

  // Apply the minimum relevance score and sort the current page.
  const displayedRows = useMemo(() => {
    let result = [...rows];

    if (minRelevance !== "") {
      const threshold = Number(minRelevance);

      result = result.filter((row) => {
        const score = relevanceScoreOf(row);

        return score !== null && score >= threshold;
      });
    }

    if (relevanceSort) {
      result.sort((a, b) => {
        const aScore = relevanceScoreOf(a);
        const bScore = relevanceScoreOf(b);

        // Keep unscored records at the bottom in either direction.
        if (aScore === null && bScore === null) return 0;
        if (aScore === null) return 1;
        if (bScore === null) return -1;

        return relevanceSort === "asc"
          ? aScore - bScore
          : bScore - aScore;
      });
    } else {
      result.sort((a, b) => {
        const aTime = timestampOf(a);
        const bTime = timestampOf(b);

        // Keep records with invalid dates at the bottom.
        if (aTime === null && bTime === null) return 0;
        if (aTime === null) return 1;
        if (bTime === null) return -1;

        return dateSort === "asc"
          ? aTime - bTime
          : bTime - aTime;
      });
    }

    return result;
  }, [rows, dateSort, relevanceSort, minRelevance]);

  useEffect(() => {
    let active = true;

    api
      .analysts()
      .then((result) => {
        if (!active || !Array.isArray(result)) return;

        const names = result
          .map((item) => String(item?.username || "").trim())
          .filter(Boolean);

        if (names.length) {
          setAnalystOptions(
            [...new Set(names)].sort((a, b) =>
              a.localeCompare(b),
            ),
          );
        }
      })
      .catch(() => {
        // Keep the compatibility list if the endpoint is unavailable.
      });

    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      void load({ quiet: hasLoadedRef.current });
    }, 100);

    return () => window.clearTimeout(timer);
  }, [load]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      void load({ quiet: true });
    }, BACKGROUND_REFRESH_MS);

    return () => window.clearInterval(timer);
  }, [load]);

  const refresh = async () => {
    if (!canRefresh || refreshing) return;

    setRefreshing(true);
    setMessage("");
    setError("");

    try {
      const queued = await api.ingestNews({
        includeYouTube: true,
        includeXPosts: true,
        includeRegulatory: true,
        limitPerFeed: 50,
        maxItemsPerSource: 50,
      });

      if (queued?.run_id) {
        setMessage(
          "News collection queued. Previously stored database records remain available while sources are checked.",
        );

        const deadline = Date.now() + 10 * 60 * 1000;
        let completed = false;

        while (Date.now() < deadline) {
          const status = await api.newsIngestStatus(queued.run_id);

          if (status?.status === "completed") {
            completed = true;
            break;
          }

          if (status?.status === "failed") {
            throw new Error(
              status.error || "NewsAlert refresh failed",
            );
          }

          await new Promise((resolve) =>
            window.setTimeout(resolve, 1500),
          );
        }

        if (!completed) {
          throw new Error(
            "News collection is still running. The archive has not been cleared; refresh the page shortly.",
          );
        }
      }

      await load({ forceRefresh: true, quiet: true });

      setMessage("NewsAlert database refreshed.");
    } catch (e) {
      setError(e?.message || "NewsAlert refresh failed");
    } finally {
      setRefreshing(false);
    }
  };

  const loadCustomSources = useCallback(async () => {
    setCustomSourcesLoading(true);
    setSourceError("");
    try {
      const data = await api.newsAlertCustomSources();
      setCustomSources(Array.isArray(data) ? data : []);
    } catch (e) {
      setSourceError(e?.message || "Unable to load custom NewsAlert sources");
    } finally {
      setCustomSourcesLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadCustomSources();
  }, [loadCustomSources]);

  const addCustomSource = async (event) => {
    event.preventDefault();
    const canSaveSelectedSource = sourceType === "X" ? canManageXAccounts : canManageSources;
    if (!canSaveSelectedSource || sourceSaving || !sourceValue.trim()) return;
    setSourceSaving(true);
    setSourceError("");
    setSourceMessage("");
    try {
      await api.addNewsAlertCustomSource({
        source_type: sourceType,
        value: sourceValue.trim(),
        ...(sourceLabel.trim() ? { label: sourceLabel.trim() } : {}),
      });
      setSourceValue("");
      setSourceLabel("");
      setSourceMessage(sourceType === "X"
        ? "X account saved for your feed. The shared scraper collects it, but only your selected accounts' posts appear to you."
        : "Source saved. It will be used by the next manual or scheduled NewsAlert collection.");
      await loadCustomSources();
    } catch (e) {
      setSourceError(e?.message || "Unable to save this source");
    } finally {
      setSourceSaving(false);
    }
  };

  const deleteCustomSource = async (source) => {
    const isXAccount = String(source?.source_type || "").toLowerCase() === "x";
    const canDeleteSelectedSource = isXAccount ? canManageXAccounts : canManageSources;
    if (!canDeleteSelectedSource || !source?.id || sourceDeletingId !== null) return;
    setSourceDeletingId(source.id);
    setSourceError("");
    setSourceMessage("");
    try {
      await api.deleteNewsAlertCustomSource(source.id);
      setCustomSources((current) => current.filter((item) => item.id !== source.id));
      setSourceMessage(isXAccount
        ? "X handle removed from your feed. It remains collected only if another user still follows it; previously collected posts are unchanged."
        : "Source removed from future collections. Previously collected news is unchanged.");
    } catch (e) {
      setSourceError(e?.message || "Unable to remove this source");
    } finally {
      setSourceDeletingId(null);
    }
  };

  const changeFilter = (key, value) => {
    setOffset(0);

    setFilters((current) => ({
      ...current,
      [key]: value,
    }));
  };

  const toggleColumnDropdown = (
    key,
    event,
    requestedWidth = 240,
    estimatedHeight = 300,
  ) => {
    if (openDropdown === key) {
      setOpenDropdown("");
      return;
    }

    const rect = event.currentTarget.getBoundingClientRect();

    const width = Math.min(
      requestedWidth,
      Math.max(220, window.innerWidth - 16),
    );

    const left = Math.max(
      8,
      Math.min(rect.left, window.innerWidth - width - 8),
    );

    const below = rect.bottom + 5;

    const top =
      below + estimatedHeight <= window.innerHeight
        ? below
        : Math.max(8, rect.top - estimatedHeight - 5);

    setDropdownPosition({
      top,
      left,
      width,
    });

    setOpenDropdown(key);
  };

  const changeTypes = (value) => {
    setOffset(0);

    setFilters((current) => {
      const previous = current.types;
      const added = value.find(
        (item) => !previous.includes(item),
      );

      let next = value;

      if (added === REGULATORY_TYPE) {
        next = [REGULATORY_TYPE];
      } else if (
        added &&
        previous.includes(REGULATORY_TYPE)
      ) {
        next = value.filter(
          (item) => item !== REGULATORY_TYPE,
        );
      }

      return {
        ...current,
        types: next,
      };
    });
  };

  const toggleRegulatoryOnly = () => {
    setOffset(0);

    setFilters((current) => {
      const isOn =
        current.types.length === 1 &&
        current.types[0] === REGULATORY_TYPE;

      return {
        ...current,
        types: isOn ? [] : [REGULATORY_TYPE],
      };
    });
  };

  const clearFilters = () => {
    setOffset(0);
    setOpenDropdown("");

    setFilters({
      search: "",
      types: [],
      tracks: [],
      analysts: [],
      dateFrom: "",
      dateTo: "",
    });

    setMinRelevance("");
  };

  useEffect(() => {
    if (!openDropdown) return undefined;

    const onPointerDown = (event) => {
      const target = event.target;

      if (
        target?.closest?.("[data-newsalert-filter-popover]") ||
        target?.closest?.("[data-newsalert-filter-trigger]")
      ) {
        return;
      }

      setOpenDropdown("");
    };

    const onKeyDown = (event) => {
      if (event.key === "Escape") setOpenDropdown("");
    };

    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);

    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [openDropdown]);

  const closeDropdown = () => setOpenDropdown("");

  const dropdownActive = (key) => openDropdown === key;

  const filterPopover =
    openDropdown &&
    dropdownPosition &&
    typeof document !== "undefined"
      ? createPortal(
          <div
            data-newsalert-filter-popover="true"
            className="rounded-xl border border-slate-200 bg-white p-2.5 shadow-xl"
            style={{
              position: "fixed",
              top: dropdownPosition.top,
              left: dropdownPosition.left,
              width: dropdownPosition.width,
              maxHeight: "min(360px, calc(100vh - 16px))",
              overflowY: "auto",
              zIndex: 1000,
            }}
          >
            {openDropdown === "company" && (
              <div>
                <label className="mb-1.5 block text-xs font-semibold text-slate-600">
                  Company / content search
                </label>

                <div className="relative">
                  <Search
                    size={15}
                    className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-500"
                  />

                  <input
                    autoFocus
                    value={filters.search}
                    onChange={(event) =>
                      changeFilter("search", event.target.value)
                    }
                    placeholder="Search company, headline, ticker or source…"
                    className="w-full rounded-lg border border-slate-300 bg-white py-2.5 pl-9 pr-3 text-sm text-slate-900 outline-none placeholder:text-slate-500 focus:border-slate-500"
                  />
                </div>

                <div className="mt-2 flex justify-between border-t border-slate-100 pt-2">
                  <button
                    type="button"
                    onClick={() => changeFilter("search", "")}
                    className="px-2 py-1 text-xs font-medium text-slate-600 hover:text-slate-900"
                  >
                    Clear
                  </button>

                  <button
                    type="button"
                    onClick={closeDropdown}
                    className="rounded-md bg-slate-900 px-2.5 py-1 text-xs font-medium text-white"
                  >
                    Done
                  </button>
                </div>
              </div>
            )}

            {openDropdown === "type" && (
              <MultiSelectOptions
                options={TYPE_OPTIONS}
                selected={filters.types.filter(
                  (value) => value !== REGULATORY_TYPE,
                )}
                onChange={changeTypes}
                onClear={() => changeTypes([])}
                onDone={closeDropdown}
              />
            )}

            {openDropdown === "date" && (
              <div className="space-y-3 p-1">
                <div className="text-xs font-semibold text-slate-600">
                  Filter and sort by date
                </div>

                <SelectOptions
                  label="Date order"
                  value={dateSort}
                  options={DATE_SORT_OPTIONS}
                  onChange={setDateSort}
                  onDone={closeDropdown}
                />

                <div className="border-t border-slate-100 pt-3">
                  <label className="block text-xs font-medium text-slate-600">
                    From date

                    <input
                      type="date"
                      value={filters.dateFrom}
                      max={filters.dateTo || undefined}
                      onChange={(event) =>
                        changeFilter("dateFrom", event.target.value)
                      }
                      className="mt-1 block w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm font-normal text-slate-800"
                    />
                  </label>

                  <label className="mt-3 block text-xs font-medium text-slate-600">
                    To date

                    <input
                      type="date"
                      value={filters.dateTo}
                      min={filters.dateFrom || undefined}
                      onChange={(event) =>
                        changeFilter("dateTo", event.target.value)
                      }
                      className="mt-1 block w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm font-normal text-slate-800"
                    />
                  </label>
                </div>

                <div className="flex justify-between border-t border-slate-100 pt-2">
                  <button
                    type="button"
                    onClick={() => {
                      changeFilter("dateFrom", "");
                      changeFilter("dateTo", "");
                    }}
                    className="px-2 py-1 text-xs font-medium text-slate-600 hover:text-slate-900"
                  >
                    Clear dates
                  </button>

                  <button
                    type="button"
                    onClick={closeDropdown}
                    className="rounded-md bg-slate-900 px-2.5 py-1 text-xs font-medium text-white"
                  >
                    Done
                  </button>
                </div>
              </div>
            )}

            {openDropdown === "analyst" && (
              <MultiSelectOptions
                options={analystOptions.map((value) => ({
                  value,
                  label: value,
                }))}
                selected={filters.analysts}
                onChange={(value) =>
                  changeFilter("analysts", value)
                }
                onClear={() => changeFilter("analysts", [])}
                onDone={closeDropdown}
              />
            )}

            {openDropdown === "track" && (
              <MultiSelectOptions
                options={TRACK_OPTIONS}
                selected={filters.tracks}
                onChange={(value) =>
                  changeFilter("tracks", value)
                }
                onClear={() => changeFilter("tracks", [])}
                onDone={closeDropdown}
              />
            )}

            {openDropdown === "relevance" && (
              <div className="space-y-3 p-1">
                <div className="text-xs font-semibold text-slate-600">
                  Filter and sort by relevance
                </div>

                <SelectOptions
                  label="Minimum relevance score"
                  value={minRelevance}
                  options={RELEVANCE_THRESHOLD_OPTIONS}
                  onChange={setMinRelevance}
                  onDone={closeDropdown}
                />

                <div className="border-t border-slate-100 pt-3">
                  <label className="block text-xs font-semibold text-slate-600">
                    Sort scored records

                    <select
                      value={relevanceSort || ""}
                      onChange={(event) =>
                        setRelevanceSort(
                          event.target.value || null,
                        )
                      }
                      className="mt-1.5 block w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-sm font-normal text-slate-800 outline-none focus:border-slate-500"
                    >
                      <option value="">
                        Use date order
                      </option>
                      <option value="desc">
                        Highest relevance first
                      </option>
                      <option value="asc">
                        Lowest relevance first
                      </option>
                    </select>
                  </label>
                </div>

                <div className="flex justify-between border-t border-slate-100 pt-2">
                  <button
                    type="button"
                    onClick={() => {
                      setMinRelevance("");
                      setRelevanceSort(null);
                    }}
                    className="px-2 py-1 text-xs font-medium text-slate-600 hover:text-slate-900"
                  >
                    Clear relevance
                  </button>

                  <button
                    type="button"
                    onClick={closeDropdown}
                    className="rounded-md bg-slate-900 px-2.5 py-1 text-xs font-medium text-white"
                  >
                    Done
                  </button>
                </div>
              </div>
            )}
          </div>,
          document.body,
        )
      : null;

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="NIVESHAM · News desk"
        title="Corporate & Market News"
        description="Monitor covered-company news, exchange filings, interviews and social posts. Add shared publisher/RSS and YouTube sources, plus your own X handles. The scraper collects all selected handles, while each user sees only posts from the handles they added."
        action={
          canRefresh ? (
            <button
              type="button"
              onClick={refresh}
              disabled={refreshing}
              className="inline-flex items-center gap-2 rounded-xl border border-slate-300 bg-white px-4 py-2.5 text-sm font-medium text-slate-800 hover:bg-slate-50 disabled:opacity-50"
            >
              <RefreshCw
                size={16}
                className={refreshing ? "animate-spin" : ""}
              />

              {refreshing ? "Collecting…" : "Refresh News"}
            </button>
          ) : null
        }
      />

      <div role="tablist" aria-label="NewsAlert sections" className="inline-flex w-fit max-w-full flex-wrap gap-1 rounded-xl bg-slate-100 p-1">
        <button
          type="button"
          role="tab"
          aria-selected={activeTab === "feed"}
          onClick={() => setActiveTab("feed")}
          className={`rounded-lg px-4 py-2 text-sm font-semibold transition ${activeTab === "feed" ? "bg-navy-900 text-white shadow-sm" : "text-slate-600 hover:text-navy-900"}`}
        >
          News Feed
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={activeTab === "sources"}
          onClick={() => setActiveTab("sources")}
          className={`inline-flex items-center gap-2 rounded-lg px-4 py-2 text-sm font-semibold transition ${activeTab === "sources" ? "bg-navy-900 text-white shadow-sm" : "text-slate-600 hover:text-navy-900"}`}
        >
          <Plus size={15} /> Manage Sources
          <span className={`rounded-full px-2 py-0.5 text-xs ${activeTab === "sources" ? "bg-white/20" : "bg-slate-100"}`}>{customSources.length}</span>
        </button>
      </div>

      {error && <ErrorBox error={error} />}

      {message && (
        <div className="rounded-xl border border-slate-200 bg-slate-50 px-4 py-3 text-sm text-slate-700">
          {message}
        </div>
      )}

      {activeTab === "feed" && <Card className="overflow-visible">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 bg-slate-50/80 px-5 py-3">
          <div className="flex flex-wrap items-center gap-3">
            <div className="flex items-center gap-2">
              <Landmark
                size={16}
                className={
                  regulatoryOnly
                    ? "text-green-700"
                    : "text-slate-500"
                }
              />

              <span className="text-xs font-medium text-slate-700">
                Regulatory Announcements
              </span>

              <button
                type="button"
                role="switch"
                aria-label="Show regulatory announcements only"
                aria-checked={regulatoryOnly}
                onClick={toggleRegulatoryOnly}
                className={`inline-flex items-center gap-2 rounded-full border px-3 py-1.5 text-xs font-semibold transition ${
                  regulatoryOnly
                    ? "border-green-300 bg-green-50 text-green-800"
                    : "border-slate-300 bg-white text-slate-700"
                }`}
              >
                <span
                  className={`h-2 w-2 rounded-full ${
                    regulatoryOnly
                      ? "bg-green-600"
                      : "bg-slate-400"
                  }`}
                />

                {regulatoryOnly ? "On" : "Off"}
              </button>
            </div>

            {(activeFilterCount > 0 || filters.search) && (
              <button
                type="button"
                onClick={clearFilters}
                className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-2 text-xs font-medium text-slate-600 hover:bg-white hover:text-slate-900"
              >
                <X size={14} />
                Clear filters
              </button>
            )}
          </div>

          <div className="text-xs text-slate-500">
            {loading
              ? "Loading archive…"
              : `${rows.length ? `${offset + 1}–${offset + rows.length}` : "0"} records on this page`}
            {minRelevance !== "" &&
              ` · Score ${minRelevance}+ · ${displayedRows.length} matching`}
          </div>
        </div>

        <div className="overflow-x-auto">
          <table className="min-w-[1050px] w-full border-collapse text-sm">
            <thead>
              <tr className="border-b border-slate-200 bg-white text-left text-[11px] uppercase tracking-wide text-slate-600">
                <th className="px-4 py-3 font-medium">
                  <FilterHeaderButton
                    label="Company Name"
                    active={Boolean(filters.search)}
                    open={dropdownActive("company")}
                    onClick={(event) =>
                      toggleColumnDropdown(
                        "company",
                        event,
                        330,
                        180,
                      )
                    }
                  />
                </th>

                <th className="px-4 py-3 font-medium">
                  Headline
                </th>

                <th className="px-4 py-3 font-medium">
                  <FilterHeaderButton
                    label="Type"
                    active={filters.types.some(
                      (value) => value !== REGULATORY_TYPE,
                    )}
                    open={dropdownActive("type")}
                    onClick={(event) =>
                      toggleColumnDropdown(
                        "type",
                        event,
                        230,
                        280,
                      )
                    }
                  />
                </th>

                <th className="px-4 py-3 font-medium">
                  <FilterHeaderButton
                    label="Relevance"
                    active={
                      minRelevance !== "" ||
                      relevanceSort !== null
                    }
                    open={dropdownActive("relevance")}
                    onClick={(event) =>
                      toggleColumnDropdown(
                        "relevance",
                        event,
                        260,
                        320,
                      )
                    }
                    sortControl={
                      <button
                        type="button"
                        onClick={() =>
                          setRelevanceSort((current) =>
                            current === "desc" ? "asc" : "desc",
                          )
                        }
                        aria-label={
                          relevanceSort === "asc"
                            ? "Sort relevance score descending"
                            : "Sort relevance score ascending"
                        }
                        title={
                          relevanceSort === "desc"
                            ? "Highest to lowest. Click for lowest to highest."
                            : "Lowest to highest. Click for highest to lowest."
                        }
                        className="inline-flex h-6 w-6 items-center justify-center rounded border border-slate-300 bg-white text-slate-600 hover:bg-slate-100 hover:text-slate-900"
                      >
                        {relevanceSort === "desc" ? (
                          <ArrowDown size={13} />
                        ) : relevanceSort === "asc" ? (
                          <ArrowUp size={13} />
                        ) : (
                          <ArrowUpDown size={13} />
                        )}
                      </button>
                    }
                  />
                </th>

                <th className="px-4 py-3 font-medium">
                  <FilterHeaderButton
                    label="Date"
                    active={
                      Boolean(filters.dateFrom || filters.dateTo) ||
                      dateSort !== "desc"
                    }
                    open={dropdownActive("date")}
                    onClick={(event) =>
                      toggleColumnDropdown(
                        "date",
                        event,
                        250,
                        360,
                      )
                    }
                    sortControl={
                      <button
                        type="button"
                        onClick={() =>
                          setDateSort((current) =>
                            current === "desc" ? "asc" : "desc",
                          )
                        }
                        aria-label={
                          dateSort === "asc"
                            ? "Sort date newest first"
                            : "Sort date oldest first"
                        }
                        title={
                          dateSort === "desc"
                            ? "Newest first. Click for oldest first."
                            : "Oldest first. Click for newest first."
                        }
                        className="inline-flex h-6 w-6 items-center justify-center rounded border border-slate-300 bg-white text-slate-600 hover:bg-slate-100 hover:text-slate-900"
                      >
                        {dateSort === "asc" ? (
                          <ArrowUp size={13} />
                        ) : dateSort === "desc" ? (
                          <ArrowDown size={13} />
                        ) : (
                          <ArrowUpDown size={13} />
                        )}
                      </button>
                    }
                  />
                </th>

                <th className="px-4 py-3 font-medium">
                  <FilterHeaderButton
                    label="Analyst"
                    active={filters.analysts.length > 0}
                    open={dropdownActive("analyst")}
                    onClick={(event) =>
                      toggleColumnDropdown(
                        "analyst",
                        event,
                        230,
                        320,
                      )
                    }
                  />
                </th>

                <th className="px-4 py-3 font-medium">
                  <FilterHeaderButton
                    label="Track"
                    active={filters.tracks.length > 0}
                    open={dropdownActive("track")}
                    onClick={(event) =>
                      toggleColumnDropdown(
                        "track",
                        event,
                        220,
                        260,
                      )
                    }
                  />
                </th>
              </tr>
            </thead>

            <tbody>
              {loading && rows.length === 0 && (
                <tr>
                  <td
                    colSpan={7}
                    className="px-4 py-10 text-center text-sm text-slate-600"
                  >
                    Loading NewsAlert archive…
                  </td>
                </tr>
              )}

              {!loading && displayedRows.length === 0 && (
                <tr>
                  <td
                    colSpan={7}
                    className="px-4 py-12 text-center text-sm text-slate-600"
                  >
                    No records match these filters. Try lowering the
                    minimum relevance score, clearing filters, or
                    running a collection cycle.
                  </td>
                </tr>
              )}

              {displayedRows.map((row) => {
                const type = String(
                  row.content_type || "NEWS",
                ).toUpperCase();

                const track = String(
                  row.track_mode || "NT",
                ).toUpperCase();

                const presentation = sourcePresentation(row);

                return (
                  <tr
                    key={`${row.id}-${row.timestamp}`}
                    className={`border-b border-slate-100 align-top ${presentation.row}`}
                  >
                    <td
                      className={`px-4 py-3 font-semibold ${presentation.text}`}
                    >
                      {row.company ||
                        (presentation.kind === "x"
                          ? String(row.source || "")
                              .replace(/^X\s*\/\s*@?/i, "@")
                              .trim()
                          : "—")}
                    </td>

                    <td className="max-w-[620px] px-4 py-3">
                      {row.url ? (
                        <a
                          href={row.url}
                          target="_blank"
                          rel="noreferrer"
                          className={`group inline-flex items-start gap-1.5 ${presentation.text}`}
                          title="Open original article or post"
                        >
                          <span className="leading-6 group-hover:underline">
                            {row.title || "Untitled"}
                          </span>

                          <ExternalLink
                            size={13}
                            className={`mt-1.5 shrink-0 ${presentation.text}`}
                          />
                        </a>
                      ) : (
                        <span
                          className={`leading-6 ${presentation.text}`}
                        >
                          {row.title || "Untitled"}
                        </span>
                      )}

                      {row.source && (
                        <div
                          className={`mt-1 text-[11px] ${presentation.text}`}
                        >
                          {row.source}
                        </div>
                      )}
                    </td>

                    <td className="px-4 py-3">
                      <span
                        className={`inline-flex whitespace-nowrap rounded-full border px-2.5 py-1 text-xs font-semibold ${presentation.badge}`}
                      >
                        {presentation.kind === "regulatory"
                          ? "Regulatory"
                          : type === "POST"
                            ? "X / Twitter"
                            : type === "INTERVIEW"
                              ? "YouTube Interview"
                              : "News"}
                      </span>
                    </td>

                    <td
                      className={`whitespace-nowrap px-4 py-3 ${presentation.text}`}
                    >
                      {row.relevance_score != null ? (
                        <span
                          title={
                            row.relevance_rationale ||
                            "Relevance score"
                          }
                        >
                          {row.relevance_score}/100
                        </span>
                      ) : (
                        <span>Not scored</span>
                      )}

                      {String(row.materiality || "").toLowerCase() ===
                        "high" && (
                        <span
                          className={`ml-1 rounded-full border px-1.5 py-0.5 text-[10px] ${presentation.border} ${presentation.text}`}
                        >
                          Material
                        </span>
                      )}
                    </td>

                    <td
                      className={`whitespace-nowrap px-4 py-3 ${presentation.text}`}
                    >
                      {displayDate(row.timestamp)}
                    </td>

                    <td
                      className={`px-4 py-3 ${presentation.text}`}
                    >
                      {row.analyst || "—"}
                    </td>

                    <td className="px-4 py-3">
                      <span
                        className={`inline-flex rounded-full border px-2.5 py-1 text-xs font-semibold ${
                          TRACK_STYLES[track] ||
                          presentation.badge
                        }`}
                      >
                        {track}
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>

        <div className="flex items-center justify-between border-t border-slate-200 bg-slate-50/70 px-5 py-3">
          <div className="text-xs text-slate-600">
            Page {Math.floor(offset / PAGE_SIZE) + 1}
          </div>

          <div className="flex gap-2">
            <button
              type="button"
              disabled={offset === 0}
              onClick={() =>
                setOffset((value) =>
                  Math.max(0, value - PAGE_SIZE),
                )
              }
              className="rounded-lg border border-slate-300 bg-white px-3 py-2 text-xs font-medium text-slate-800 disabled:cursor-not-allowed disabled:opacity-40"
            >
              Previous
            </button>

            <button
              type="button"
              disabled={!hasMore}
              onClick={() =>
                setOffset((value) => value + PAGE_SIZE)
              }
              className="rounded-lg border border-slate-300 bg-white px-3 py-2 text-xs font-medium text-slate-800 disabled:cursor-not-allowed disabled:opacity-40"
            >
              Next
            </button>
          </div>
        </div>
      </Card>}

      {activeTab === "sources" && (
        <div className="space-y-5">
          <Card className="overflow-hidden">
            <div className="border-b border-slate-200 px-5 py-4">
              <h2 className="text-base font-semibold text-slate-900">Add a collection source</h2>
              <p className="mt-1 text-sm text-slate-600">Saved entries become scraper configuration and are included in later manual and scheduled runs.</p>
            </div>
            {sourceError && <div className="px-5 pt-4"><ErrorBox error={sourceError} /></div>}
            {sourceMessage && <div className="px-5 pt-4"><div className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800">{sourceMessage}</div></div>}
            {(canManageSources || canManageXAccounts) ? (
              <form onSubmit={addCustomSource} className="grid gap-4 p-5 md:grid-cols-2">
                <label className="block text-sm font-medium text-slate-700">
                  Source type
                  <select
                    value={sourceType}
                    onChange={(event) => setSourceType(event.target.value)}
                    className="mt-1.5 block w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-sm outline-none focus:border-slate-500"
                  >
                    {canManageSources && <option value="WEBSITE">News website / RSS feed</option>}
                    {canManageSources && <option value="YOUTUBE">YouTube channel</option>}
                    {canManageXAccounts && <option value="X">X / Twitter handle</option>}
                  </select>
                </label>
                <label className="block text-sm font-medium text-slate-700">
                  Display name (optional)
                  <input
                    value={sourceLabel}
                    onChange={(event) => setSourceLabel(event.target.value)}
                    maxLength={255}
                    placeholder="e.g. Investor Relations channel"
                    className="mt-1.5 block w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-sm outline-none focus:border-slate-500"
                  />
                </label>
                <label className="block text-sm font-medium text-slate-700 md:col-span-2">
                  {sourceType === "WEBSITE" ? "Website or RSS URL" : sourceType === "YOUTUBE" ? "Channel URL, @handle, channel ID or name" : "X handle or profile URL"}
                  <input
                    value={sourceValue}
                    onChange={(event) => setSourceValue(event.target.value)}
                    required
                    maxLength={1500}
                    placeholder={sourceType === "WEBSITE" ? "https://publisher.example/rss.xml" : sourceType === "YOUTUBE" ? "https://www.youtube.com/@channel" : "@handle or https://x.com/handle"}
                    className="mt-1.5 block w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-sm outline-none focus:border-slate-500"
                  />
                  <span className="mt-1.5 block text-xs font-normal text-slate-500">
                    {sourceType === "WEBSITE" ? "Use an RSS/Atom feed URL when available. Publisher homepages are also accepted and use the site's public-page fallback." : sourceType === "YOUTUBE" ? "The collector resolves channel URLs, @handles, IDs and names before fetching recent videos." : "Enter an account handle, not a post URL. Public post content is collected subject to the configured X transport/provider."}
                  </span>
                </label>
                <div className="flex flex-wrap items-center gap-3 md:col-span-2">
                  <button type="submit" disabled={sourceSaving || !sourceValue.trim()} className="inline-flex items-center gap-2 rounded-lg bg-slate-900 px-4 py-2.5 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:opacity-50">
                    <Plus size={16} /> {sourceSaving ? "Saving…" : "Save source"}
                  </button>
                  <span className="text-xs text-slate-500">{canManageSources
                    ? "Your X handles are private to your feed. RSS and YouTube sources remain shared."
                    : "Add or remove your own X handles. Posts from other users' selected accounts stay hidden from your feed."}</span>
                </div>
              </form>
            ) : (
              <div className="px-5 py-4 text-sm text-slate-600">Source editing requires an ADMIN or ANALYST role.</div>
            )}
          </Card>

          <Card className="overflow-hidden">
            <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 px-5 py-4">
              <div>
                <h2 className="text-base font-semibold text-slate-900">Configured sources</h2>
                <p className="mt-1 text-sm text-slate-600">Removing your X handle hides its posts from your feed; the shared scraper keeps collecting it only while another user still follows it. Previously collected posts are not deleted.</p>
              </div>
              <button type="button" onClick={() => void loadCustomSources()} disabled={customSourcesLoading} className="inline-flex items-center gap-2 rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm font-medium text-slate-700 disabled:opacity-50">
                <RefreshCw size={14} className={customSourcesLoading ? "animate-spin" : ""} /> Reload list
              </button>
            </div>
            {customSourcesLoading && customSources.length === 0 ? (
              <div className="px-5 py-8 text-sm text-slate-500">Loading configured sources…</div>
            ) : customSources.length === 0 ? (
              <div className="px-5 py-8 text-sm text-slate-500">No custom sources have been added yet.</div>
            ) : (
              <div className="divide-y divide-slate-100">
                {customSources.map((source) => {
                  const sourceKind = String(source.source_type || "").toLowerCase();
                  const typeInfo = sourceKind === "youtube"
                    ? { Icon: Youtube, text: "YouTube channel" }
                    : sourceKind === "x"
                      ? { Icon: AtSign, text: "X / Twitter handle" }
                      : { Icon: Globe, text: "News website / RSS" };
                  const canDeleteThisSource = sourceKind === "x" ? canManageXAccounts : canManageSources;
                  const SourceIcon = typeInfo.Icon;
                  return (
                    <div key={source.id} className="flex flex-col gap-3 px-5 py-4 sm:flex-row sm:items-center sm:justify-between">
                      <div className="flex min-w-0 items-start gap-3">
                        <div className="mt-0.5 rounded-lg bg-slate-100 p-2 text-slate-600"><SourceIcon size={17} /></div>
                        <div className="min-w-0">
                          <div className="flex flex-wrap items-center gap-2">
                            <span className="break-words text-sm font-semibold text-slate-900">{source.label || source.value}</span>
                            <span className="rounded-full bg-slate-100 px-2 py-0.5 text-xs text-slate-600">{typeInfo.text}</span>
                            {source.enabled && <span className="inline-flex items-center gap-1 rounded-full bg-emerald-50 px-2 py-0.5 text-xs text-emerald-700"><CheckCircle2 size={12} /> Enabled</span>}
                          </div>
                          <p className="mt-1 break-all text-xs text-slate-500">{source.value}</p>
                        </div>
                      </div>
                      {canDeleteThisSource && (
                        <button type="button" onClick={() => void deleteCustomSource(source)} disabled={sourceDeletingId !== null} className="inline-flex shrink-0 items-center justify-center gap-2 self-start rounded-lg border border-rose-200 px-3 py-2 text-xs font-semibold text-rose-700 hover:bg-rose-50 disabled:opacity-50 sm:self-center">
                          <Trash2 size={14} /> {sourceDeletingId === source.id ? "Removing…" : "Remove"}
                        </button>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </Card>
        </div>
      )}

      {activeTab === "feed" && filterPopover}
    </div>
  );
}
