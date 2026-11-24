const THEME_STORAGE_KEY = 'chemnine:theme:v1';
const DENSITY_STORAGE_KEY = 'chemnine:density:v1';

export const THEMES = [
  { id: 'system', label: 'System', description: 'Follow your operating system' },
  { id: 'light', label: 'Light', description: 'Clean white workspace' },
  { id: 'dark', label: 'Dark', description: 'Low-glare dark workspace' },
  { id: 'night', label: 'Night Light', description: 'Warm, softer evening palette' },
  { id: 'slate', label: 'Slate', description: 'Cool graphite workspace' },
  { id: 'contrast', label: 'High Contrast', description: 'Maximum readability' },
];

export const DENSITIES = [
  { id: 'compact', label: 'Compact', description: 'Smaller controls, tighter spacing, and reduced page scale' },
  { id: 'comfortable', label: 'Comfortable', description: 'Balanced sizing' },
  { id: 'spacious', label: 'Spacious', description: 'Larger controls and spacing' },
];

const validTheme = (value) =>
  THEMES.some((item) => item.id === value) ? value : 'light';

const validDensity = (value) =>
  DENSITIES.some((item) => item.id === value) ? value : 'comfortable';

export function getCurrentTheme() {
  if (typeof window === 'undefined') return 'light';
  try {
    return validTheme(window.localStorage.getItem(THEME_STORAGE_KEY) || 'light');
  } catch {
    return 'light';
  }
}

export function getCurrentDensity() {
  if (typeof window === 'undefined') return 'comfortable';
  try {
    return validDensity(window.localStorage.getItem(DENSITY_STORAGE_KEY) || 'comfortable');
  } catch {
    return 'comfortable';
  }
}

export function getEffectiveTheme(theme = getCurrentTheme()) {
  return resolveTheme(theme);
}

function resolveTheme(theme) {
  if (theme !== 'system' || typeof window === 'undefined') return theme;
  try {
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  } catch {
    return 'light';
  }
}

function syncThemeClass(theme) {
  if (typeof document === 'undefined') return;
  const root = document.documentElement;
  const effective = resolveTheme(theme);
  root.classList.remove(
    'theme-system',
    'theme-light',
    'theme-dark',
    'theme-night',
    'theme-slate',
    'theme-contrast'
  );
  root.classList.add(`theme-${effective}`);
  if (theme === 'system') root.classList.add('theme-system');
  root.dataset.theme = theme;
  root.dataset.effectiveTheme = effective;
  root.style.colorScheme = effective === 'dark' || effective === 'slate' ? 'dark' : 'light';
}

function syncDensityClass(density) {
  if (typeof document === 'undefined') return;
  const root = document.documentElement;
  root.classList.remove('density-compact', 'density-comfortable', 'density-spacious');
  root.classList.add(`density-${density}`);
  root.dataset.density = density;
}

export function applyTheme(theme) {
  const next = validTheme(theme);
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, next);
  } catch {
    // Local storage is optional; the active theme still applies for this tab.
  }
  syncThemeClass(next);
  window.dispatchEvent(new CustomEvent('chemnine-theme-updated', { detail: { theme: next } }));
  return next;
}

export function applyDensity(density) {
  const next = validDensity(density);
  try {
    window.localStorage.setItem(DENSITY_STORAGE_KEY, next);
  } catch {
    // Local storage is optional.
  }
  syncDensityClass(next);
  window.dispatchEvent(new CustomEvent('chemnine-density-updated', { detail: { density: next } }));
  return next;
}

export function initializeTheme() {
  syncThemeClass(getCurrentTheme());
  syncDensityClass(getCurrentDensity());
}

if (typeof window !== 'undefined') {
  const media = window.matchMedia?.('(prefers-color-scheme: dark)');
  if (media?.addEventListener) {
    media.addEventListener('change', () => {
      if (getCurrentTheme() === 'system') syncThemeClass('system');
    });
  }
}

// Apply before React mounts so there is no light-theme flash on reload.
initializeTheme();