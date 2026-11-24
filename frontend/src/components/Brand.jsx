import React from 'react';

// NIVESHAM mark: three rising bars inside a navy tile. Pure SVG, no image request.
export function BrandMark({ className = 'h-9 w-9' }) {
  return (
    <svg viewBox="0 0 48 48" className={className} aria-hidden="true" focusable="false">
      <rect width="48" height="48" rx="12" fill="#0B1F3A" stroke="#E0B25A" strokeOpacity="0.35" strokeWidth="1" />
      <rect x="11" y="27" width="6" height="11" rx="2" fill="#E0B25A" />
      <rect x="21" y="20" width="6" height="18" rx="2" fill="#E0B25A" opacity="0.75" />
      <rect x="31" y="12" width="6" height="26" rx="2" fill="#E0B25A" opacity="0.5" />
    </svg>
  );
}

export default function Brand({ tone = 'dark', compact = false, className = '' }) {
  const light = tone === 'light';
  return (
    <span className={`inline-flex items-center gap-2.5 ${className}`}>
      <BrandMark className={compact ? 'h-8 w-8' : 'h-9 w-9'} />
      <span className="leading-none">
        <span className={`font-display block text-[17px] font-bold tracking-[.14em] ${light ? 'text-white' : 'text-navy-900'}`}>
          NIVESHAM
        </span>
        {!compact && (
          <span className={`mt-1 block text-[10px] font-semibold uppercase tracking-[.2em] ${light ? 'text-amber-200/80' : 'text-slate-500'}`}>
            Investment Intelligence
          </span>
        )}
      </span>
    </span>
  );
}
