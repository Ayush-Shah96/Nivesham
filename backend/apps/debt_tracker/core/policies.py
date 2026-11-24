"""Per-report concentration limits, separate from instrument credit categories."""
from collections import defaultdict

from .errors import WorkbookSchemaError
from .models import frozen_mapping


POLICY_NAMES = frozen_mapping({'standard': 'Standard rating limits',
                               'client-aggregate': 'Client aggregate limits'})
CLIENT_LIMITS = frozen_mapping({'Govt': 1.0, 'Govt Owned': .25, 'SDL': .15,
                               'Bank': .35, 'AAA Non-Bank': .25,
                               'AA Non-Bank': .05, 'Cash & Equivalent': 1.0})


def validated_policy(value='standard'):
    if not isinstance(value, str) or value not in POLICY_NAMES:
        raise WorkbookSchemaError('Select a valid category limit policy.')
    return value


def limit_bucket(category, policy):
    if policy == 'client-aggregate':
        if category.endswith(' Bank'):
            return 'Bank'
        if category.startswith('AAA') and category.endswith(' Non-Bank'):
            return 'AAA Non-Bank'
        if category.startswith(('AA ', 'AA+', 'AA-', 'AA(')) and category.endswith(' Non-Bank'):
            return 'AA Non-Bank'
    return category


def effective_limits(policy, standard_limits):
    validated_policy(policy)
    if policy == 'standard':
        return frozen_mapping(standard_limits)
    defaults = {limit_bucket(category, policy): limit
                for category, limit in standard_limits.items()}
    return frozen_mapping({**defaults, **CLIENT_LIMITS})


def evaluate_limits(categories, policy, standard_limits):
    limits = effective_limits(policy, standard_limits)
    totals = defaultdict(float)
    for row in categories:
        totals[limit_bucket(row['category'], policy)] += row['exposure']
    total = sum(totals.values())
    return tuple(frozen_mapping({
        'category': category, 'exposure': exposure,
        'percentage': exposure / total if total else 0,
        'limit': limits.get(category),
        'breached': bool(total and limits.get(category) is not None
                         and exposure / total > limits[category] + 1e-12),
        'limitSource': ('Standard fallback' if policy == 'client-aggregate' and category not in CLIENT_LIMITS
                        else POLICY_NAMES[policy]),
    }) for category, exposure in sorted(totals.items()))
