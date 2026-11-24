// In Vite development, always use the same-origin proxy. This avoids an
// absolute VITE_API_BASE_URL such as http://localhost:8000/api/v1 bypassing
// Vite and resolving localhost to ::1 while Uvicorn listens on 127.0.0.1.
// Production builds may still use an explicitly configured API origin.
const configuredApiBase = String(import.meta.env.VITE_API_BASE_URL || "").trim();
const BASE = (
  import.meta.env.DEV
    ? "/api/v1"
    : (configuredApiBase || "/api/v1")
).replace(/\/$/, "");

export const getToken = () =>

  localStorage.getItem("ii_access_token");

export const setToken = (token) =>

  localStorage.setItem(

    "ii_access_token",

    token

  );

export const clearToken = () =>

  localStorage.removeItem(

    "ii_access_token"

  );

export const getUserFromToken = () => {

  const token = getToken();

  if (!token) {

    return null;

  }

  try {

    const payloadPart =

      token.split(".")[1];

    if (!payloadPart) {

      return null;

    }

    const normalized =

      payloadPart

        .replace(/-/g, "+")

        .replace(/_/g, "/");

    const padded =

      normalized +

      "=".repeat(

        (4 - (normalized.length % 4)) % 4

      );

    const payload =

      JSON.parse(

        atob(padded)

      );

    if (

      payload.exp &&

      Number(payload.exp) * 1000 <=

      Date.now()

    ) {

      return null;

    }

    if (

      !payload.sub ||

      !payload.username

    ) {

      return null;

    }

    return {

      id: Number(payload.sub),

      username: String(

        payload.username

      ),

      role: payload.role

        ? String(payload.role)

        : "ANALYST",

    };

  } catch {

    return null;

  }

};

// -----------------------------------------------------------------------------

// Request configuration

// -----------------------------------------------------------------------------

const RETRYABLE_STATUSES =

  new Set([

    408,

    425,

    429,

    500,

    502,

    503,

    504,

  ]);

const DEFAULT_MAX_RETRIES = 1;

const DEFAULT_BACKOFF_MS = 500;

const SHORT_CACHE_TTL_MS = 3000;

const SNAPSHOT_REFRESH_AFTER_MS =

  5 * 60 * 1000;

// The backend worker owns ingestion cadence. Keep a useful browser snapshot

// for up to six hours and refresh it in the background rather than on every

// navigation or tab switch.

const SNAPSHOT_MAX_AGE_MS =

  6 * 60 * 60 * 1000;

const SNAPSHOT_CACHE_PREFIXES = [

  "/dashboard/summary",

  "/dashboard/recent-activity",

  "/coverage-list",

  "/regulatory-signals",
  "/regulatory-activity",

  "/sector-screener",

  // Crisp Notes contains user-owned recordings/transcripts; never persist it in
  // the cross-session snapshot cache (especially when users share a workstation).

];

const SNAPSHOT_STORAGE_PREFIX =

  "chemnine:snapshot:v5:";

const SNAPSHOT_CACHE_MAX_BYTES =

  1_500_000;

const inFlightGetRequests =

  new Map();

const shortGetCache =

  new Map();

const sleep = (ms) =>

  new Promise((resolve) =>

    setTimeout(resolve, ms)

  );

const isIdempotentMethod = (

  method

) =>

  ["GET", "HEAD", "OPTIONS"].includes(

    String(

      method || "GET"

    ).toUpperCase()

  );

const matchesPrefix = (

  path,

  prefix

) =>

  path === prefix ||

  path.startsWith(`${prefix}?`) ||

  path.startsWith(`${prefix}/`);

const isSnapshotCacheable = (

  path

) =>

  SNAPSHOT_CACHE_PREFIXES.some(

    (prefix) =>

      matchesPrefix(

        path,

        prefix

      )

  );

const getCacheScope = () => {

  try {

    const user =

      getUserFromToken();

    return user?.id

      ? String(user.id)

      : "anonymous";

  } catch {

    return "anonymous";

  }

};

const snapshotStorageKey = (

  path

) =>

  `${SNAPSHOT_STORAGE_PREFIX}${getCacheScope()}:${path}`;

const parseStoredSnapshot = (

  path

) => {

  if (

    typeof window ===

    "undefined" ||

    !window.localStorage

  ) {

    return null;

  }

  try {

    const raw =

      window.localStorage.getItem(

        snapshotStorageKey(path)

      );

    if (!raw) {

      return null;

    }

    const parsed =

      JSON.parse(raw);

    if (

      !parsed ||

      typeof parsed.timestamp !==

      "number"

    ) {

      return null;

    }

    return parsed;

  } catch {

    return null;

  }

};

const canPersist = (

  data

) => {

  try {

    return (

      JSON.stringify(data)

        .length <=

      SNAPSHOT_CACHE_MAX_BYTES

    );

  } catch {

    return false;

  }

};

const isMeaningfulData = (

  data

) => {

  if (Array.isArray(data)) {

    return data.length > 0;

  }

  if (

    data &&

    typeof data ===

    "object"

  ) {

    return (

      Object.keys(data)

        .length > 0

    );

  }

  return (

    data !== null &&

    data !== undefined

  );

};

const persistSnapshot = (

  path,

  data

) => {

  if (

    typeof window ===

    "undefined" ||

    !window.localStorage

  ) {

    return;

  }

  if (

    !isMeaningfulData(data) ||

    !canPersist(data)

  ) {

    return;

  }

  try {

    window.localStorage.setItem(

      snapshotStorageKey(path),

      JSON.stringify({

        timestamp: Date.now(),

        data,

      })

    );

  } catch {

    // localStorage is an optimization only.

  }

};

const readSnapshot = (

  path

) => {

  if (

    !isSnapshotCacheable(path)

  ) {

    return null;

  }

  const memory =

    shortGetCache.get(path);

  if (memory) {

    return memory;

  }

  const stored =

    parseStoredSnapshot(path);

  if (stored) {

    shortGetCache.set(

      path,

      stored

    );

    return stored;

  }

  return null;

};

const getRetryDelay = (

  response,

  attempt

) => {

  const retryAfter =

    response.headers.get(

      "Retry-After"

    );

  if (retryAfter) {

    const seconds =

      Number(retryAfter);

    if (

      Number.isFinite(

        seconds

      )

    ) {

      return Math.min(

        seconds * 1000,

        30000

      );

    }

    const date =

      Date.parse(

        retryAfter

      );

    if (

      Number.isFinite(

        date

      )

    ) {

      return Math.min(

        Math.max(

          0,

          date - Date.now()

        ),

        30000

      );

    }

  }

  const base =

    DEFAULT_BACKOFF_MS *

    2 ** attempt;

  return Math.min(

    base +

    Math.random() *

    base *

    0.25,

    10000

  );

};

const parseResponseBody =

  async (response) => {

    const contentType =

      response.headers.get(

        "content-type"

      ) || "";

    if (

      contentType.includes(

        "application/json"

      )

    ) {

      try {

        return await response.json();

      } catch {

        return null;

      }

    }

    try {

      return (

        (await response.text()) ||

        null

      );

    } catch {

      return null;

    }

  };

const errorMessage = (

  status,

  data,

  location = ""

) => {

  if (

    typeof data ===

    "string" &&

    data.trim()

  ) {

    return data;

  }

  if (data?.detail) {

    return typeof data.detail ===

      "string"

      ? data.detail

      : JSON.stringify(

        data.detail

      );

  }

  if (data?.message) {

    return typeof data.message ===

      "string"

      ? data.message

      : JSON.stringify(

        data.message

      );

  }

  if (data?.error) {

    return typeof data.error ===

      "string"

      ? data.error

      : JSON.stringify(

        data.error

      );

  }

  if (status >= 300 && status < 400) {

    return location

      ? `HTTP ${status} redirect response; Location: ${location}`

      : `HTTP ${status} redirect response without a Location header`;

  }

  return `Request failed (${status})`;

};

// -----------------------------------------------------------------------------

// Generic request

// -----------------------------------------------------------------------------

export async function request(

  path,

  options = {}

) {

  const method =

    String(

      options.method ||

      "GET"

    ).toUpperCase();

  const {

    retry = undefined,

    maxRetries =

    DEFAULT_MAX_RETRIES,

    timeoutMs =

    isIdempotentMethod(

      method

    )

      ? 20000

      : 0,

    forceRefresh = false,

    background = false,

    persist = undefined,

    ...fetchOptions

  } = options;

  const shouldRetry =

    retry ??

    isIdempotentMethod(

      method

    );

  const cacheable =

    method === "GET" &&

    isSnapshotCacheable(

      path

    );

  const shouldPersist =

    persist ?? cacheable;

  const cacheKey =

    method === "GET"

      ? path

      : null;

  // ---------------------------------------------------------------------------

  // Snapshot-first UX

  // ---------------------------------------------------------------------------

  if (

    cacheable &&

    !forceRefresh &&

    !background

  ) {

    const cached =

      readSnapshot(path);

    if (cached) {

      const age =

        Date.now() -

        cached.timestamp;

      if (age <= SNAPSHOT_MAX_AGE_MS) {

        if (

          age >= SNAPSHOT_REFRESH_AFTER_MS &&

          !inFlightGetRequests.has(cacheKey)

        ) {

          void request(

            path,

            {

              ...options,

              forceRefresh: true,

              background: true,

              retry: true,

              maxRetries: 1,

            }

          ).catch(() => { });

        }

        return cached.data;

      }

    }

  }

  if (

    method === "GET" &&

    !forceRefresh

  ) {

    const inFlight =

      inFlightGetRequests.get(

        cacheKey

      );

    if (inFlight) {

      return inFlight;

    }

  }

  const execute = async () => {

    let attempt = 0;

    while (true) {

      const headers =

        new Headers(

          fetchOptions.headers ||

          {}

        );

      const token =

        getToken();

      if (token) {

        headers.set(

          "Authorization",

          `Bearer ${token}`

        );

      }

      const body =

        fetchOptions.body;

      const isFormData =

        typeof FormData !==

        "undefined" &&

        body instanceof

        FormData;

      const isUrlEncoded =

        typeof URLSearchParams !==

        "undefined" &&

        body instanceof

        URLSearchParams;

      if (

        body !== undefined &&

        !isFormData &&

        !isUrlEncoded &&

        !headers.has(

          "Content-Type"

        )

      ) {

        headers.set(

          "Content-Type",

          "application/json"

        );

      }

      let response;

      let timeoutHandle =

        null;

      let timeoutController =

        null;

      try {

        if (

          timeoutMs > 0 &&

          typeof AbortController !==

          "undefined"

        ) {

          timeoutController =

            new AbortController();

          timeoutHandle =

            setTimeout(

              () => {

                timeoutController.abort();

              },

              timeoutMs

            );

        }

        response =

          await fetch(

            `${BASE}${path}`,

            {

              ...fetchOptions,

              method,

              headers,

              ...(timeoutController

                ? {

                  signal:

                    timeoutController.signal,

                }

                : {}),

            }

          );

      } catch (error) {

        if (

          timeoutController &&

          error?.name ===

          "AbortError"

        ) {

          const timeoutError =

            new Error(

              `Request timed out after ${Math.round(

                timeoutMs / 1000

              )}s`

            );

          timeoutError.code =

            "REQUEST_TIMEOUT";

          throw timeoutError;

        }

        if (

          shouldRetry &&

          attempt <

          maxRetries &&

          error instanceof

          TypeError

        ) {

          await sleep(

            Math.min(

              DEFAULT_BACKOFF_MS *

              2 ** attempt +

              Math.random() *

              150,

              5000

            )

          );

          attempt += 1;

          continue;

        }

        if (error instanceof TypeError) {
          const networkError = new Error(
            `Unable to reach API at ${BASE}${path}. Check that FastAPI is running and that the Vite /api proxy targets 127.0.0.1:8000.`
          );
          networkError.code = "API_NETWORK_ERROR";
          networkError.cause = error;
          throw networkError;
        }

        throw error;

      } finally {

        if (timeoutHandle) {

          clearTimeout(

            timeoutHandle

          );

        }

      }

      const data =

        await parseResponseBody(

          response

        );

      if (

        response.status ===

        401

      ) {

        clearToken();

        if (

          typeof window !==

          "undefined"

        ) {

          window.dispatchEvent(

            new Event(

              "auth-expired"

            )

          );

        }

      }

      if (!response.ok) {

        if (

          shouldRetry &&

          RETRYABLE_STATUSES.has(

            response.status

          ) &&

          attempt <

          maxRetries

        ) {

          await sleep(

            getRetryDelay(

              response,

              attempt

            )

          );

          attempt += 1;

          continue;

        }

        const error =

          new Error(

            errorMessage(

              response.status,

              data,

              response.headers.get("Location") ||

              response.headers.get("location") ||

              ""

            )

          );

        error.status =

          response.status;

        error.data =

          data;

        throw error;

      }

      if (cacheable) {

        const entry = {

          timestamp:

            Date.now(),

          data,

        };

        shortGetCache.set(

          cacheKey,

          entry

        );

        if (

          shouldPersist

        ) {

          persistSnapshot(

            path,

            data

          );

        }

      }

      return data;

    }

  };

  if (

    method === "GET"

  ) {

    const promise =

      execute();

    inFlightGetRequests.set(

      cacheKey,

      promise

    );

    try {

      return await promise;

    } finally {

      inFlightGetRequests.delete(

        cacheKey

      );

    }

  }

  return execute();

}

// -----------------------------------------------------------------------------

// Binary download request

// -----------------------------------------------------------------------------

export async function requestBlob(

  path,

  options = {}

) {

  const method = String(options.method || 'GET').toUpperCase();

  const {

    retry = undefined,

    maxRetries = DEFAULT_MAX_RETRIES,

    timeoutMs = 60000,

    ...fetchOptions

  } = options;

  const shouldRetry = retry ?? isIdempotentMethod(method);

  let attempt = 0;

  while (true) {

    const headers = new Headers(fetchOptions.headers || {});

    const token = getToken();

    if (token) headers.set('Authorization', `Bearer ${token}`);

    let response;

    let timeoutHandle = null;

    let timeoutController = null;

    try {

      if (timeoutMs > 0 && typeof AbortController !== 'undefined') {

        timeoutController = new AbortController();

        timeoutHandle = setTimeout(() => timeoutController.abort(), timeoutMs);

      }

      response = await fetch(`${BASE}${path}`, {

        ...fetchOptions,

        method,

        headers,

        ...(timeoutController ? { signal: timeoutController.signal } : {}),

      });

    } catch (error) {

      if (timeoutController && error?.name === 'AbortError') {

        const timeoutError = new Error(`Request timed out after ${Math.round(timeoutMs / 1000)}s`);

        timeoutError.code = 'REQUEST_TIMEOUT';

        throw timeoutError;

      }

      if (shouldRetry && attempt < maxRetries && error instanceof TypeError) {

        await sleep(Math.min(DEFAULT_BACKOFF_MS * 2 ** attempt + Math.random() * 150, 5000));

        attempt += 1;

        continue;

      }

      throw error;

    } finally {

      if (timeoutHandle) clearTimeout(timeoutHandle);

    }

    if (response.status === 401) {

      clearToken();

      if (typeof window !== 'undefined') window.dispatchEvent(new Event('auth-expired'));

    }

    if (!response.ok) {

      const data = await parseResponseBody(response);

      if (shouldRetry && RETRYABLE_STATUSES.has(response.status) && attempt < maxRetries) {

        await sleep(getRetryDelay(response, attempt));

        attempt += 1;

        continue;

      }

      const error = new Error(errorMessage(

        response.status,

        data,

        response.headers.get('Location') || response.headers.get('location') || ''

      ));

      error.status = response.status;

      error.data = data;

      throw error;

    }

    return response.blob();

  }

}

// -----------------------------------------------------------------------------

// Snapshot warming

// -----------------------------------------------------------------------------

export async function warmCaches(

  paths = [],

  concurrency = 1

) {

  const unique = [

    ...new Set(

      paths.filter(

        (path) =>

          typeof path ===

          "string" &&

          path.startsWith(

            "/"

          )

      )

    ),

  ];

  const limit =

    Math.max(

      1,

      Math.min(

        Number(

          concurrency

        ) || 4,

        6

      )

    );

  let cursor = 0;

  const worker =

    async () => {

      while (

        cursor <

        unique.length

      ) {

        const index =

          cursor;

        cursor += 1;

        const path =

          unique[index];

        try {

          // Cache-aware prefetch: never force a backend refresh during startup.

          await request(

            path,

            {

              retry: false,

              maxRetries: 0,

              timeoutMs: 12000,

            }

          );

        } catch {

          /*

           * One broken desk must not prevent

           * other desk snapshots from warming.

           */

        }

      }

    };

  await Promise.all(

    Array.from(

      {

        length:

          Math.min(

            limit,

            unique.length

          ),

      },

      () => worker()

    )

  );

}

// -----------------------------------------------------------------------------

// Snapshot management

// -----------------------------------------------------------------------------

export function clearSnapshotCaches() {

  shortGetCache.clear();

  if (

    typeof window ===

    "undefined" ||

    !window.localStorage

  ) {

    return;

  }

  const scope =

    getCacheScope();

  const prefix =

    `${SNAPSHOT_STORAGE_PREFIX}${scope}:`;

  try {

    for (

      let index =

        window.localStorage

          .length -

        1;

      index >= 0;

      index -= 1

    ) {

      const key =

        window.localStorage.key(

          index

        );

      if (

        key &&

        key.startsWith(

          prefix

        )

      ) {

        window.localStorage.removeItem(

          key

        );

      }

    }

  } catch {

    // Best effort only.

  }

}

// -----------------------------------------------------------------------------

// Download helper

// -----------------------------------------------------------------------------

export async function downloadBlob(

  path,

  options = {}

) {

  const headers =

    new Headers(

      options.headers ||

      {}

    );

  const token =

    getToken();

  if (token) {

    headers.set(

      "Authorization",

      `Bearer ${token}`

    );

  }

  const response =

    await fetch(

      `${BASE}${path}`,

      {

        ...options,

        method:

          options.method ||

          "GET",

        headers,

      }

    );

  if (

    response.status ===

    401

  ) {

    clearToken();

    if (

      typeof window !==

      "undefined"

    ) {

      window.dispatchEvent(

        new Event(

          "auth-expired"

        )

      );

    }

  }

  if (!response.ok) {

    const data =

      await parseResponseBody(

        response

      );

    const error =

      new Error(

        errorMessage(

          response.status,

          data,

          response.headers.get("Location") ||

          response.headers.get("location") ||

          ""

        )

      );

    error.status =

      response.status;

    error.data =

      data;

    throw error;

  }

  return response.blob();

}

// -----------------------------------------------------------------------------

// Query helper

// -----------------------------------------------------------------------------

const buildQuery = (

  params = {}

) => {

  const search =

    new URLSearchParams();

  Object.entries(

    params

  ).forEach(

    ([key, value]) => {

      if (

        value !==

        undefined &&

        value !== null &&

        value !== ""

      ) {

        search.set(

          key,

          String(value)

        );

      }

    }

  );

  const query =

    search.toString();

  return query

    ? `?${query}`

    : "";

};

// -----------------------------------------------------------------------------

// API

// -----------------------------------------------------------------------------

export const api = {

  base: BASE,

  // ===========================================================================

  // AUTH

  // ===========================================================================

  login: (

    username,

    password

  ) =>

    request(

      "/auth/token",

      {

        method:

          "POST",

        retry:

          false,

        maxRetries:

          0,

        timeoutMs:

          12000,

        headers: {

          "Content-Type":

            "application/x-www-form-urlencoded",

        },

        body:

          new URLSearchParams({

            username,

            password,

          }),

      }

    ),

  me: () =>

    request(

      "/auth/me"

    ),

  logout: () =>

    request(

      "/auth/logout",

      {

        method: "POST",

        retry: false,

        maxRetries: 0,

        persist: false,

      }

    ),

  // Signed-in user changes their own password. Backend route: POST /auth/change-password.
  changeOwnPassword: (currentPassword, newPassword) =>
    request(
      "/auth/change-password",
      {
        method: "POST",
        retry: false,
        maxRetries: 0,
        persist: false,
        body: JSON.stringify({
          current_password: currentPassword,
          new_password: newPassword,
        }),
      }
    ),

  peWarrantQuotes: (symbols = []) =>

    request(

      `/regulatory-signals/pe-warrant/quotes${buildQuery({

        symbols: Array.isArray(symbols) ? symbols.join(",") : symbols,

      })}`,

      { retry: false, timeoutMs: 5000, maxRetries: 0 }

    ),

  // ===========================================================================

  // NEWS ALERT

  // ===========================================================================

  news: ({

    company = "",

    analyst = "",

    analysts = null,

    track_mode = "",

    tracks = null,

    source = "",

    includeRegulatory = false,

    content_type = "",

    types = null,

    dateFrom = "",

    dateTo = "",

    limit = 100,

    offset = 0,

    retry = true,

    forceRefresh = false,

  } = {}) => {

    const selectedTypes = Array.isArray(types) ? types.join(",") : (types || content_type);

    const selectedTracks = Array.isArray(tracks) ? tracks.join(",") : (tracks || track_mode);

    const selectedAnalysts = Array.isArray(analysts) ? analysts.join(",") : (analysts || analyst);

    return request(

      `/news${buildQuery({

        company,

        analyst: selectedAnalysts,

        track_mode: selectedTracks,

        source,

        include_regulatory: includeRegulatory,

        content_type: selectedTypes,

        date_from: dateFrom,

        date_to: dateTo,

        limit,

        offset,

      })}`,

      {

        retry,

        forceRefresh,

      }

    );

  },

  newsArchiveStatus: () =>

    request("/news/archive-status", { retry: true }),

  newsCoverage: () =>

    request("/news/coverage", { retry: true }),

  sources: () =>

    request(

      "/news/sources"

    ),

  sourceStatus: () =>

    request(

      "/news/source-status"

    ),

  newsAlertCustomSources: () =>

    request("/news/custom-sources", { retry: true }),

  addNewsAlertCustomSource: (data) =>

    request("/news/custom-sources", {

      method: "POST",

      body: JSON.stringify(data),

    }),

  deleteNewsAlertCustomSource: (sourceId) =>

    request(`/news/custom-sources/${encodeURIComponent(sourceId)}`, {

      method: "DELETE",

    }),

  ingestNews: ({

    feedUrls = null,

    limitPerFeed = 30,

    maxItemsPerSource = 30,

    sourceIds = null,

    includeYouTube = true,

    includeXPosts = true,

    includeRegulatory = false,

  } = {}) =>

    request(

      "/news/ingest",

      {

        method:

          "POST",

        body:

          JSON.stringify({

            feed_urls:

              feedUrls,

            limit_per_feed:

              limitPerFeed,

            max_items_per_source:

              maxItemsPerSource,

            source_ids:

              sourceIds,

            include_youtube:

              includeYouTube,

            include_x_posts:

              includeXPosts,

            include_regulatory:

              includeRegulatory,

          }),

      }

    ),

  newsIngestLatest: () =>

    request(

      "/news/ingest/latest",

      {

        retry: true,

      }

    ),

  newsIngestStatus: (

    runId

  ) =>

    request(

      `/news/ingest/status/${encodeURIComponent(

        runId

      )}`,

      {

        retry: true,

      }

    ),

  analysts: () =>

    request(

      "/news/analysts"

    ),

  // ===========================================================================

  // COVERAGE LIST

  // ===========================================================================

  coverageList: () =>

    request(

      "/coverage-list",

      {

        retry: true,

      }

    ),

  coverageHealth: () =>

    request(

      "/coverage-list/health",

      {

        retry: true,

        forceRefresh: true,

      }

    ),

  createCoverageListRow: (

    data

  ) =>

    request(

      "/coverage-list",

      {

        method:

          "POST",

        body:

          JSON.stringify(

            data

          ),

      }

    ),

  updateCoverageListRow: (

    id,

    data

  ) =>

    request(

      `/coverage-list/${id}`,

      {

        method:

          "PATCH",

        body:

          JSON.stringify(

            data

          ),

      }

    ),

  deleteCoverageListRow: (

    id

  ) =>

    request(

      `/coverage-list/${id}`,

      {

        method:

          "DELETE",

      }

    ),

  uploadCoverageList: (

    file

  ) => {

    const formData =

      new FormData();

    formData.append(

      "file",

      file

    );

    return request(

      "/coverage-list/upload",

      {

        method:

          "POST",

        body:

          formData,

      }

    );

  },

  downloadCoverageList:

    () =>

      downloadBlob(

        "/coverage-list/export"

      ),

  // ===========================================================================

  // REGULATORY ACTIVITY (unified normalized event table)

  // ===========================================================================

  regulatoryActivityEvents: ({
    activity_type = "",
    activity_types = "",
    activity_sub_type = "",
    company = "",
    person = "",
    action = "",
    actions = "",
    status = "",
    statuses = "",
    coverage_modes = "",
    analysts = "",
    from_date = "",
    to_date = "",
    quantity_min = "",
    quantity_max = "",
    cmp_min = "",
    cmp_max = "",
    value_min_crore = "",
    value_max_crore = "",
    price_min = "",
    price_max = "",
    change_min = "",
    change_max = "",
    holding_min = "",
    holding_max = "",
    sort_by = "date",
    sort_dir = "desc",
    limit = 100,
    offset = 0,
    retry = true,
    forceRefresh = false,
  } = {}) => request(
    `/regulatory-activity${buildQuery({
      activity_type, activity_types, activity_sub_type, company, person, action, actions,
      status, statuses, coverage_modes, analysts, from_date, to_date,
      quantity_min, quantity_max, cmp_min, cmp_max, value_min_crore, value_max_crore,
      price_min, price_max, change_min, change_max, holding_min, holding_max,
      sort_by, sort_dir, limit, offset,
    })}`,
    { retry, forceRefresh, persist: false }
  ),

  regulatoryActivityFilterOptions: () => request("/regulatory-activity/filter-options", { persist: false }),

  regulatoryActivitySummary: () => request("/regulatory-activity/summary", { persist: false }),

  syncRegulatoryActivity: ({ days = 7 } = {}) => request(
    `/regulatory-activity/sync${buildQuery({ days })}`,
    { method: "POST", retry: false, persist: false }
  ),

  // ===========================================================================

  // REGULATORY DESK

  // ===========================================================================

  regulatorySignals: ({

    analyst = "",

    track_mode = "",

    company = "",

    sort_by = "date",

    sort_dir = "desc",

    limit = 100,

    offset = 0,

    retry = true,

    forceRefresh = false,

  } = {}) =>

    request(

      `/regulatory-signals${buildQuery(

        {

          analyst,

          track_mode,

          company,

          sort_by,

          sort_dir,

          limit,

          offset,

        }

      )}`,

      {

        retry,

        forceRefresh,

      }

    ),

  regulatoryEvents: ({
    event_type = "",
    analyst = "",
    company = "",
    sort_by = "date",
    sort_dir = "desc",
    limit = 100,
    offset = 0,
    days = 2557,
    forceRefresh = false,
    retry = true,
  } = {}) =>
    request(
      `/regulatory-signals/events${buildQuery({
        event_type,
        analyst,
        company,
        sort_by,
        sort_dir,
        limit,
        offset,
        days,
        force_refresh: forceRefresh,
      })}`,
      { retry, forceRefresh, persist: false }
    ),

  regulatorySources: () =>

    request(

      "/regulatory-signals/sources"

    ),

  regulatorySourceStatus:

    () =>

      request(

        "/regulatory-signals/source-status"

      ),

  regulatoryCapitalActions: ({

    event_type = "",

    company = "",

    track_mode = "",

    sort_by = "date",

    sort_dir = "desc",

    limit = 100,

    offset = 0,

    include_quotes = false,

    retry = true,

    forceRefresh = false,

  } = {}) =>

    request(

      `/regulatory-signals/pe-warrant${buildQuery(

        {

          event_type,

          company,

          track_mode,

          sort_by,

          sort_dir,

          limit,

          offset,

          include_quotes,

        }

      )}`,

      {

        retry,

        forceRefresh,

      }

    ),

  ingestRegulatoryCapitalActions:

    ({

      history_days = 6,

    } = {}) =>

      request(

        `/regulatory-signals/pe-warrant/ingest${buildQuery(

          {

            history_days,

          }

        )}`,

        {

          method:

            "POST",

        }

      ),

  regulatoryCoverageList:

    () =>

      request(

        "/regulatory-signals/coverage-list"

      ),

  ingestRegulatory: ({

    history_days = 6,

  } = {}) =>

    request(

      `/regulatory-signals/ingest${buildQuery(

        {

          history_days,

        }

      )}`,

      {

        method:

          "POST",

      }

    ),

  regulatoryCompanies: () =>

    request(

      "/regulatory-signals/companies"

    ),

  createRegulatoryCompany: (

    data

  ) =>

    request(

      "/regulatory-signals/companies",

      {

        method:

          "POST",

        body:

          JSON.stringify(

            data

          ),

      }

    ),

  updateRegulatoryCompany: (

    id,

    data

  ) =>

    request(

      `/regulatory-signals/companies/${id}`,

      {

        method:

          "PATCH",

        body:

          JSON.stringify(

            data

          ),

      }

    ),

  approveRegulatoryCompany: (

    id,

    {

      analyst_id,

      track_mode = "LT",

      start_date = null,

      end_date = null,

    } = {}

  ) =>

    request(

      `/regulatory-signals/companies/${id}/approve`,

      {

        method:

          "POST",

        body:

          JSON.stringify({

            analyst_id,

            track_mode,

            start_date,

            end_date,

          }),

      }

    ),

  removeRegulatoryCompany: (

    id

  ) =>

    request(

      `/regulatory-signals/companies/${id}`,

      {

        method:

          "DELETE",

      }

    ),

  signals: () =>

    request(

      "/regulatory-signals"

    ),

  // ===========================================================================

  // SECTOR SCREENER

  // ===========================================================================

  sectorScreenerCatalog: () =>

    request("/sector-screener/catalog", {

      timeoutMs: 12000, retry: false, maxRetries: 0, forceRefresh: true, persist: false,

    }),

  solarCompanySeries: (product, agencyIds, period = "monthly") =>
    request(
      `/sector-screener/reports/solar/overview/company-series${buildQuery({
        product,
        agency_ids: Array.isArray(agencyIds) ? agencyIds.join(",") : agencyIds,
        period,
      })}`,
      { timeoutMs: 15000, retry: false, maxRetries: 0, forceRefresh: true, persist: false }
    ),

  sectorScreenerReport: (sectorKey, reportKey = "overview", { refresh = false, filters = {}, timeline = "monthly" } = {}) =>

    request(

      `/sector-screener/reports/${encodeURIComponent(sectorKey)}/${encodeURIComponent(reportKey)}${buildQuery({

        refresh: refresh ? "true" : "",

        category: filters.category || "",

        fuel: filters.fuel || "",

        maker: filters.maker || "",

        makers: Array.isArray(filters.makers) ? filters.makers.join(",") : (filters.makers || ""),

        timeline: filters.timeline || timeline || "monthly",
        product: filters.product || "",
        period: filters.period || "",
        source_filters: filters.sourceFilters && Object.keys(filters.sourceFilters).length ? JSON.stringify(filters.sourceFilters) : "",

      })}`,

      // `forceRefresh` here bypasses only this browser helper's local snapshot.
      // The backend's `refresh=true` query parameter is the actual scrape trigger.
      // The report endpoint should return its cached/queued status promptly; a
      // 15-second timeout makes a broken API/proxy visible without masking it.
      { timeoutMs: 15000, retry: false, maxRetries: 0, forceRefresh: true, persist: false }

    ),

  automotiveMakerOptions: (search, limit = 30) =>

    request(

      `/sector-screener/reports/automotive/overview/makers${buildQuery({ search, limit })}`,

      { timeoutMs: 30000, retry: false, maxRetries: 0, forceRefresh: true, persist: false }

    ),

  tradestatWatchlist: () =>
    request("/sector-screener/tradestat/watchlist", { timeoutMs: 12000, retry: false, maxRetries: 0, forceRefresh: true, persist: false }),

  tradestatAddWatchlist: (hsCode, commodity = "", tradeType = "import") =>
    request("/sector-screener/tradestat/watchlist", { method: "POST", body: JSON.stringify({ hs_code: hsCode, commodity, trade_type: tradeType }), timeoutMs: 15000, retry: false, maxRetries: 0, forceRefresh: true, persist: false }),

  tradestatRemoveWatchlist: (hsCode, tradeType = "import") =>
    request(`/sector-screener/tradestat/watchlist/${encodeURIComponent(hsCode)}${buildQuery({ trade_type: tradeType })}`, { method: "DELETE", timeoutMs: 15000, retry: false, maxRetries: 0, forceRefresh: true, persist: false }),

  tradestatMonitor: ({ refresh = false } = {}) =>
    request(`/sector-screener/tradestat/monitor${buildQuery({ refresh: refresh ? "true" : "" })}`, { timeoutMs: 30000, retry: false, maxRetries: 0, forceRefresh: true, persist: false }),

  tradestatCommodityHistory: (hsCode, { refresh = false, tradeType = "import" } = {}) =>
    request(`/sector-screener/tradestat/commodity/${encodeURIComponent(hsCode)}${buildQuery({ trade_type: tradeType, refresh: refresh ? "true" : "" })}`, { timeoutMs: 30000, retry: false, maxRetries: 0, forceRefresh: true, persist: false }),

  // ===========================================================================

  // CRISP INVESTOR NOTES

  // ===========================================================================

  notes: () =>

    request(

      "/crisp-notes"

    ),

  crispNote: (

    noteId

  ) =>

    request(

      `/crisp-notes/${encodeURIComponent(

        noteId

      )}`

    ),

  // Large recordings use 8 MiB raw-binary chunks so each request remains below
  // Microsoft Dev Tunnels' 16 MiB forwarding limit. The backend assembles the
  // original file and then runs the same Deepgram/Groq pipeline as direct upload.
  uploadAudio: async (
    file,
    { title = "", summarize = false, onProgress = null } = {}
  ) => {
    const filename = file?.name || "recording.webm";
    const CHUNK_SIZE = 8 * 1024 * 1024;

    if (!file || !Number.isFinite(file.size) || file.size <= 0) {
      throw new Error("Please select a non-empty audio recording.");
    }

    // Keep the original single-request path for small files.
    if (file.size <= CHUNK_SIZE) {
      const formData = new FormData();
      formData.append("file", file, filename);
      if (String(title || "").trim()) formData.append("title", String(title).trim());
      formData.append("summarize", summarize ? "true" : "false");
      return request("/crisp-notes/upload", { method: "POST", body: formData });
    }

    let uploadId = null;
    try {
      const session = await request("/crisp-notes/upload/chunked/init", {
        method: "POST",
        body: JSON.stringify({
          filename,
          file_size: file.size,
          title: String(title || "").trim() || null,
          summarize: Boolean(summarize),
        }),
        retry: false,
      });
      uploadId = session.upload_id;
      const chunkSize = Number(session.chunk_size) || CHUNK_SIZE;
      const totalChunks = Number(session.total_chunks) || Math.ceil(file.size / chunkSize);

      for (let index = 0; index < totalChunks; index += 1) {
        const start = index * chunkSize;
        const end = Math.min(start + chunkSize, file.size);
        const chunk = file.slice(start, end);
        await request(`/crisp-notes/upload/chunked/${encodeURIComponent(uploadId)}/chunks/${index}`, {
          method: "PUT",
          body: chunk,
          headers: { "Content-Type": "application/octet-stream" },
          retry: false,
          timeoutMs: 0,
        });
        if (typeof onProgress === "function") {
          onProgress({ uploadedBytes: end, totalBytes: file.size, chunkIndex: index + 1, totalChunks });
        }
      }

      if (typeof onProgress === "function") {
        onProgress({ uploadedBytes: file.size, totalBytes: file.size, chunkIndex: totalChunks, totalChunks, assembling: true });
      }
      return await request(`/crisp-notes/upload/chunked/${encodeURIComponent(uploadId)}/complete`, {
        method: "POST",
        retry: false,
        timeoutMs: 0,
      });
    } catch (error) {
      if (uploadId) {
        // Best-effort cleanup; preserve the original upload error for the user.
        request(`/crisp-notes/upload/chunked/${encodeURIComponent(uploadId)}`, {
          method: "DELETE", retry: false, timeoutMs: 10000,
        }).catch(() => {});
      }
      throw error;
    }
  },

  renameCrispNote: (

    noteId,

    title

  ) =>

    request(

      `/crisp-notes/${encodeURIComponent(noteId)}`,

      {

        method: "PATCH",

        body: JSON.stringify({

          title: String(title ?? "").trim() || null,

        }),

      }

    ),

  summarizeCrispNote: (

    noteId,

    { force = false } = {}

  ) =>

    request(

      `/crisp-notes/${encodeURIComponent(noteId)}/summarize${force ? "?force=true" : ""}`,

      {

        method: "POST",

        retry: false,

        timeoutMs: 0,

        persist: false,

      }

    ),

  crispChat: (

    noteId,

    question,

    history = []

  ) =>

    request(

      `/crisp-notes/${encodeURIComponent(noteId)}/chat`,

      {

        method: "POST",

        body: JSON.stringify({

          question: String(question ?? "").trim(),

          history: Array.isArray(history) ? history : [],

        }),

        retry: false,

        timeoutMs: 0,

        persist: false,

      }

    ),

  // ===========================================================================

  // DASHBOARD

  // ===========================================================================

  dashboardSummary: ({

    limit = 12,

    retry = true,

    maxRetries = 2,

    forceRefresh = false,

  } = {}) =>

    request(

      `/dashboard/summary${buildQuery({

        limit,

      })}`,

      {

        retry,

        maxRetries,

        forceRefresh,

      }

    ),

  requestDashboardSummary:

    ({

      limit = 12,

      retry = true,

      maxRetries = 2,

      forceRefresh = false,

    } = {}) =>

      request(

        `/dashboard/summary${buildQuery({

          limit,

        })}`,

        {

          retry,

          maxRetries,

          forceRefresh,

        }

      ),

  dashboardInsights: ({

    retry = false,

  } = {}) =>

    request(

      "/dashboard/insights",

      {

        retry,

      }

    ),

  recentActivity: ({

    limit = 12,

    retry = true,

  } = {}) =>

    request(

      `/dashboard/recent-activity${buildQuery({

        limit,

      })}`,

      {

        retry,

      }

    ),

  recordActivity: (

    data

  ) =>

    request(

      "/activity",

      {

        method:

          "POST",

        body:

          JSON.stringify(

            data

          ),

      }

    ),

  // ===========================================================================

  // NOTIFICATIONS

  // ===========================================================================

  notifications: ({

    unreadOnly = false,

    limit = 30,

  } = {}) =>

    request(

      `/notifications${buildQuery({

        unread_only:

          unreadOnly,

        limit,

      })}`

    ),

  markNotification: (

    id,

    read = true

  ) =>

    request(

      `/notifications/${id}`,

      {

        method:

          "PATCH",

        body:

          JSON.stringify({

            read,

          }),

      }

    ),

  // ---------------------------------------------------------------------------

  // DEBT TRACKER

  // ---------------------------------------------------------------------------

  debtTrackerJobs: ({ retry = true } = {}) =>

    request('/debt-tracker/jobs', {

      retry,

      forceRefresh: false,

      persist: false,

    }),

  debtTrackerJob: (

    jobId,

    { retry = true } = {}

  ) =>

    request(

      `/debt-tracker/jobs/${encodeURIComponent(jobId)}`,

      { retry, persist: false }

    ),

  debtTrackerJobData: (

    jobId,

    { retry = true } = {}

  ) =>

    request(

      `/debt-tracker/jobs/${encodeURIComponent(jobId)}/data`,

      { retry, persist: false }

    ),

  saveDebtTrackerMapping: ({ mappingType, sourceKey, targetKey } = {}) =>

    request('/debt-tracker/mappings', {
      method: 'POST',
      body: JSON.stringify({
        mapping_type: mappingType,
        source_key: sourceKey,
        target_key: targetKey,
      }),
      retry: false,
      maxRetries: 0,
    }),

  generateDebtTracker: ({

    mappingFile,

    holdingsFile,

    brokerFiles = [],

    reportDate,

    baselineDate = '',

    baseSheet = '',

    estimateBharatFof = false,

    limitPolicy = 'standard',

  } = {}) => {

    const form = new FormData();

    form.append('mapping_file', mappingFile);

    form.append('holdings_file', holdingsFile);

    brokerFiles.forEach((file) => form.append('raw_file', file));

    form.append('report_date', reportDate || '');

    form.append('baseline_date', baselineDate || '');

    form.append('base_sheet', baseSheet || '');

    form.append('estimate_bharat_fof', estimateBharatFof ? 'true' : 'false');

    form.append('limit_policy', limitPolicy || 'standard');

    return request('/debt-tracker/generate', {

      method: 'POST',

      body: form,

      retry: false,

      maxRetries: 0,

      timeoutMs: 600000,

    });

  },

  downloadDebtTracker: (

    jobId,

    { retry = true } = {}

  ) =>

    requestBlob(

      `/debt-tracker/jobs/${encodeURIComponent(jobId)}/download`,

      { retry, maxRetries: 1, timeoutMs: 120000 }

    ),

  downloadDebtTrackerSheet: (

    jobId,

    sheet,

    { retry = true } = {}

  ) =>

    requestBlob(

      `/debt-tracker/jobs/${encodeURIComponent(jobId)}/download-sheet${buildQuery({ sheet })}`,

      { retry, maxRetries: 1, timeoutMs: 120000 }

    ),

  // ===========================================================================

  // ADAPTIVE PMS PORTFOLIO MANAGER

  // ===========================================================================

  portfolioRegistry: () =>

    request("/portfolio-comparison/registry", {

      retry: true,

      forceRefresh: true,

    }),

  portfolioMonths: () =>

    request("/portfolio-comparison/months", {

      retry: true,

      forceRefresh: true,

    }),

  portfolioJobs: ({ limit = 20 } = {}) =>
    request(
      `/portfolio-comparison/jobs${buildQuery({ limit })}`,
      {
        retry: false,
        maxRetries: 0,
        forceRefresh: true,
        timeoutMs: 60000,
      }
    ),

  portfolioJob: (batchId) =>
    request(
      `/portfolio-comparison/jobs/${encodeURIComponent(batchId)}`,
      {
        // The UI will poll again on its next cycle instead of doubling a wait.
        retry: false,
        maxRetries: 0,
        forceRefresh: true,
        timeoutMs: 60000,
      }
    ),

  portfolioHistory: ({ limit = 24 } = {}) =>
    request(
      `/portfolio-comparison/history${buildQuery({ limit })}`,
      {
        retry: false,
        maxRetries: 0,
        forceRefresh: true,
        timeoutMs: 90000,
      }
    ),

  portfolioUpload: ({ comparisonMonth, file }) => {
    const formData = new FormData();
    formData.append("comparison_month", comparisonMonth);
    formData.append("archive", file);
    return request("/portfolio-comparison/upload", {
      method: "POST",
      body: formData,
      retry: false,
      timeoutMs: 120000,
      maxRetries: 0,
    });
  },

  portfolioComparison: ({ month } = {}) =>
    request(
      `/portfolio-comparison/comparison${buildQuery({ month })}`,
      {
        retry: false,
        maxRetries: 0,
        forceRefresh: true,
        timeoutMs: 60000,
      }
    ),

  portfolioReportJson: (batchId) =>
    request(
      `/portfolio-comparison/reports/${encodeURIComponent(batchId)}/json`,
      {
        retry: false,
        maxRetries: 0,
        forceRefresh: true,
        timeoutMs: 90000,
      }
    ),

  downloadPortfolioReport: (batchId) =>
    downloadBlob(
      `/portfolio-comparison/reports/${encodeURIComponent(batchId)}/download`,
      { retry: true }
    ),

  earningsCalendar: ({ startDate, endDate, eventType, analyst, search, forceRefresh = false } = {}) =>

    request(

      `/earnings-calendar${buildQuery({

        start_date: startDate,

        end_date: endDate,

        event_type: eventType,

        analyst,

        search,

      })}`,

      { retry: true, maxRetries: 1, forceRefresh, persist: false, timeoutMs: 30000 }

    ),

  // ===========================================================================

  // PULSE — all live-data requests bypass browser persistent snapshot storage.

  // The backend owns the 10-minute in-memory market-data cache.

  // ===========================================================================

  pulseSnapshot: ({ refresh = false } = {}) =>

    request(

      `/pulse${buildQuery({ refresh: refresh ? "true" : undefined })}`,

      {

        retry: false,

        maxRetries: 0,

        timeoutMs: 45000,

        persist: false,

      }

    ),

  pulseHeatmap: (index, { refresh = false } = {}) =>

    request(

      `/pulse/heatmap${buildQuery({

        index,

        refresh: refresh ? "true" : undefined,

      })}`,

      {

        retry: false,

        maxRetries: 0,

        timeoutMs: 60000,

        persist: false,

      }

    ),

  pulseMovers: ({ refresh = false } = {}) =>

    request(

      `/pulse/movers${buildQuery({ refresh: refresh ? "true" : undefined })}`,

      {

        retry: false,

        maxRetries: 0,

        timeoutMs: 60000,

        persist: false,

      }

    ),

  pulseStockContext: (symbol, { refresh = false } = {}) =>

    request(

      `/pulse/stock/${encodeURIComponent(symbol)}${buildQuery({

        refresh: refresh ? "true" : undefined,

      })}`,

      {

        retry: false,

        maxRetries: 0,

        timeoutMs: 5000,

        persist: false,

      }

    ),

  // ===========================================================================

  // ADMIN

  // ===========================================================================

  users: () =>

    request(

      "/admin/users"

    ),

  adminUsage: ({

    days = 30,

    forceRefresh = false,

  } = {}) =>

    request(

      `/admin/usage${buildQuery({

        days,

      })}`,

      {

        retry: true,

        maxRetries: 1,

        forceRefresh,

        persist: false,

      }

    ),

  createUser: (

    data

  ) =>

    request(

      "/admin/users",

      {

        method:

          "POST",

        body:

          JSON.stringify(

            data

          ),

      }

    ),

  updateUser: (

    id,

    data

  ) =>

    request(

      `/admin/users/${id}`,

      {

        method:

          "PATCH",

        body:

          JSON.stringify(

            data

          ),

      }

    ),

  removeUser: (

    id

  ) =>

    request(

      `/admin/users/${id}`,

      {

        method:

          "DELETE",

      }

    ),

};

export default api;