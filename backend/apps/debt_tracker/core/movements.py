"""Auditable snapshot comparisons; valuation differences are never trade amounts."""
from collections import defaultdict
from dataclasses import asdict
from .models import frozen_mapping
from .normalization import normalize_fund_name, normalize_text


MOVEMENT_SCOPE = ('Combined named groups across the selected funds, excluding Cash/Others classifications. '
                  'Client-classified units and unrated holdings are included. Opening membership uses its source classification.')
_NON_DEBT = frozenset({'equity', 'equities', 'others', 'cash', 'cash equivalent', 'cash and equivalent',
    'derivatives', 'commodity', 'commodities', 'mutual funds', 'mutual fund units', 'reit', 'invit',
    'reit units', 'invit units', 'alternative investment fund', 'not disclosed for papers'})


def named_debt_group(group):
    return normalize_text(group) not in _NON_DEBT | {'', 'blank', 'unmapped'}


def group_movement_eligible(group, category):
    """Follow the client's classification filter, independent of instrument type."""
    value = normalize_text(category)
    return (named_debt_group(group) and value not in _NON_DEBT
            and not value.startswith(('cash', 'others ')))


def status(previous, current):
    """Membership status: whether a named group exists in each period."""
    if abs(previous) <= 1e-12 and abs(current) > 1e-12:
        return 'Added'
    if abs(previous) > 1e-12 and abs(current) <= 1e-12:
        return 'Exited'
    return 'Continuing'


def trend_status(previous, current, tolerance=1e-9):
    """Exposure trend for groups that exist in both periods.

    A reduction is not an exit: an exit requires closing exposure to be zero.
    """
    membership = status(previous, current)
    if membership != 'Continuing':
        return membership
    delta = current - previous
    if delta < -tolerance:
        return 'Reduced'
    if delta > tolerance:
        return 'Increased'
    return 'Unchanged'


def _group_presence(rows):
    grouped = defaultdict(list)
    for row in rows:
        if not row['movement_eligible']:
            continue
        grouped[(row['previous_date'], row['current_date'], row['group'])].append(row)
    return tuple(frozen_mapping({
        'previous_date': old, 'current_date': new, 'group': group,
        'status': status(sum(r['previous_allocation'] for r in members),
                         sum(r['current_allocation'] for r in members)),
    }) for (old, new, group), members in sorted(grouped.items()))


def broker_comparison(positions, classifications, classification_key, categories):
    schemes = defaultdict(list)
    for position in positions:
        schemes[normalize_fund_name(position.scheme)].append(position)
    output, unavailable, periods = [], [], set()
    for members in schemes.values():
        dates = {(p.comparison.previous_date, p.comparison.current_date) for p in members if p.comparison}
        if (any(p.comparison is None for p in members) or len(dates) != 1
                or abs(sum(p.comparison.previous_allocation for p in members) - 1) > .001):
            unavailable.append(members[0].scheme)
            continue
        previous_date, current_date = next(iter(dates))
        periods.add((previous_date.isoformat(), current_date.isoformat()))
        instruments = defaultdict(list)
        for p in members:
            instruments[("isin", normalize_text(p.isin)) if p.isin else ("paper", normalize_text(p.paper))].append(p)
        for rows in instruments.values():
            previous = sum(p.comparison.previous_allocation for p in rows)
            current = sum(p.allocation for p in rows)
            if abs(previous) <= 1e-12 and abs(current) <= 1e-12:
                continue
            representative = next((p for p in rows if p.allocation > 0), rows[0])
            group = classifications[classification_key(representative)][0]
            output.append(frozen_mapping({
                'fund': representative.scheme, 'paper': representative.paper, 'isin': representative.isin,
                'group': group, 'previous_allocation': previous, 'current_allocation': current,
                'change': current - previous, 'status': status(previous, current),
                'movement_eligible': group_movement_eligible(group, categories[representative]),
                'asset_class': representative.asset_class, 'asset_type': representative.asset_type,
                'previous_date': previous_date.isoformat(), 'current_date': current_date.isoformat(),
                'previous_sources': tuple(asdict(p.comparison.source) for p in rows),
                'current_sources': tuple(asdict(p.source) for p in rows if p.source),
            }))
    return frozen_mapping({'available': bool(output), 'rows': tuple(output),
        'groupChanges': _group_presence(output),
        'unavailableFunds': tuple(unavailable),
        'periods': tuple({'previous': old, 'current': new} for old, new in sorted(periods)),
        'scope': MOVEMENT_SCOPE,
        'basis': 'Broker allocation snapshots for the matched funds. Same fund and ISIN rows are combined. '
                 'Weight changes include market movements and fund flows; purchases and sales require transaction data.'})
