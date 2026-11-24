/** Pure, non-mutating automotive analytics helpers. Counts and percentages are distinct. */
export const numberOrNull = (value) => {
  if (value === null || value === undefined || value === '') return null;
  if (typeof value === 'number') return Number.isFinite(value) ? value : null;
  const s = String(value).trim().replace(/,/g, '').replace(/%$/, '');
  if (!/^-?(?:\d+\.?\d*|\.\d+)$/.test(s)) return null;
  const n = Number(s); return Number.isFinite(n) ? n : null;
};
export const ratioPct = (numerator, denominator) => {
  const n = numberOrNull(numerator), d = numberOrNull(denominator);
  return n === null || d === null || d <= 0 ? null : n / d * 100;
};
export const yoyGrowth = (current, previous) => {
  const c = numberOrNull(current), p = numberOrNull(previous);
  return c === null || p === null || p <= 0 ? null : (c - p) / p * 100;
};
export const marketShare = (manufacturerRegistrations, segmentRegistrations) => ratioPct(manufacturerRegistrations, segmentRegistrations);
export const topThreeShare = (rows, valueKey = 'registrations', total = null) => {
  const values = (Array.isArray(rows) ? rows : []).map(r => numberOrNull(r?.[valueKey])).filter(v => v !== null && v >= 0).sort((a,b)=>b-a).slice(0,3);
  if (total !== null) return ratioPct(values.reduce((a,b)=>a+b,0), total);
  const denominator = (Array.isArray(rows) ? rows : []).map(r => numberOrNull(r?.[valueKey])).filter(v=>v!==null && v>=0).reduce((a,b)=>a+b,0);
  return denominator > 0 ? values.reduce((a,b)=>a+b,0)/denominator*100 : null;
};
export const periodKey = (period) => {
  const s = String(period ?? '');
  if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return s.slice(0,7);
  if (/^\d{4}-\d{2}$/.test(s)) return s;
  if (/^\d{4}-Q[1-4]$/i.test(s)) return s.toUpperCase();
  if (/^\d{4}$/.test(s)) return s;
  return null;
};
export const filterRows = (rows, filters = {}) => (Array.isArray(rows) ? rows : []).filter(row => {
  const p = periodKey(row.period);
  if (filters.year && (!p || !p.startsWith(String(filters.year)))) return false;
  if (filters.period && filters.period !== 'all' && (!p || (filters.period === 'monthly' ? !/^\d{4}-\d{2}$/.test(p) : filters.period === 'quarterly' ? !/^\d{4}-Q[1-4]$/.test(p) : !/^\d{4}$/.test(p)))) return false;
  for (const [filterKey, rowKey] of [['segment','segment'],['fuelType','fuel_type'],['manufacturer','manufacturer'],['state','state']]) {
    if (filters[filterKey] && filters[filterKey] !== 'all' && String(row[rowKey] ?? '') !== filters[filterKey]) return false;
  }
  if (filters.ev === 'ev' && row.is_ev !== true) return false;
  if (filters.ev === 'non-ev' && row.is_ev !== false) return false;
  return true;
});
export const rankManufacturers = (rows) => [...(Array.isArray(rows) ? rows : [])].filter(r => numberOrNull(r.registrations) !== null).sort((a,b)=>numberOrNull(b.registrations)-numberOrNull(a.registrations) || String(a.manufacturer).localeCompare(String(b.manufacturer))).map((r,i)=>({...r,rank:i+1}));
export const sumKnown = (values) => { const ns=values.map(numberOrNull); return ns.some(v=>v===null) ? null : ns.reduce((a,b)=>a+b,0); };