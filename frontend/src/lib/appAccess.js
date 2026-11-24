// Frontend-only application visibility preferences.
//
// These settings are deliberately kept outside the backend because the requirement
// is UI-only: they control which applications appear in the React workspace.
// Keys are namespaced by backend user ID so different authenticated users can have
// different visibility settings in the same browser.

export const APP_ACCESS_STORAGE_KEY = 'chemnine:app-access:v2';

export const ACCESS_APPS = [
  { id: 'pulse', label: 'Dashboard', path: '/dashboard', group: 'Core' },
  { id: 'sector-screener', label: 'Sector Screener', path: '/sector-screener', group: 'Core' },
  { id: 'earnings-calendar', label: 'Earnings Calendar', path: '/earnings-calendar', group: 'Core' },
  { id: 'news-alert', label: 'NewsAlert Desk', path: '/news-alert', group: 'Core' },
  { id: 'insider-buying', label: 'Insider Buying', path: '/regulatory-signals', group: 'Core' },
  { id: 'crisp-notes', label: 'Crisp Notes', path: '/crisp-notes', group: 'Core' },
  { id: 'portfolio-comparison', label: 'Portfolio Comparison', path: '/portfolio-comparison', group: 'Core' },
  { id: 'coverage-list', label: 'Coverage List', path: '/coverage-list', group: 'Core' },
  { id: 'youtube-intelligence', label: 'YouTube Intelligence', path: '/youtube-intelligence', group: 'Core' },
];

export function readAppAccessStore() {
  if (typeof window === 'undefined') return {};

  try {
    const parsed = JSON.parse(
      window.localStorage.getItem(APP_ACCESS_STORAGE_KEY) || '{}',
    );

    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? parsed
      : {};
  } catch {
    return {};
  }
}

export function writeAppAccessStore(store) {
  if (typeof window === 'undefined') return false;

  try {
    window.localStorage.setItem(
      APP_ACCESS_STORAGE_KEY,
      JSON.stringify(store),
    );

    window.dispatchEvent(new Event('app-access-updated'));
    return true;
  } catch {
    // Browser storage is a UI preference only. Never break navigation if it fails.
    return false;
  }
}

export function defaultAccessForUser() {
  return ACCESS_APPS.reduce((acc, app) => {
    acc[app.id] = true;
    return acc;
  }, {});
}

export function permissionsForUser(userId, store = readAppAccessStore()) {
  const base = defaultAccessForUser();

  if (!userId) return base;

  const saved = store?.[String(userId)];

  if (!saved || typeof saved !== 'object' || Array.isArray(saved)) {
    return base;
  }

  ACCESS_APPS.forEach((app) => {
    if (typeof saved[app.id] === 'boolean') {
      base[app.id] = saved[app.id];
    }
  });

  return base;
}

export function accessIdForPath(pathname = '') {
  const match = ACCESS_APPS.find(
    (app) =>
      pathname === app.path ||
      pathname.startsWith(`${app.path}/`) ||
      pathname.startsWith(`${app.path}?`),
  );

  return match?.id || null;
}

export function isPathAllowed(pathname, permissions) {
  const id = accessIdForPath(pathname);
  return !id || permissions?.[id] !== false;
}

export function firstAllowedAppPath(permissions) {
  return (
    ACCESS_APPS.find((app) => permissions?.[app.id] !== false)?.path ||
    '/dashboard'
  );
}

export function saveUserAppPermissions(userId, permissions) {
  if (!userId) return false;

  const store = readAppAccessStore();

  store[String(userId)] = ACCESS_APPS.reduce((acc, app) => {
    acc[app.id] = permissions?.[app.id] !== false;
    return acc;
  }, {});

  return writeAppAccessStore(store);
}