"""Typed value of one raw cell under a column spec (recomputed per semantic revision)."""
from ..normalization import normalize_value
from .temporal import parse_temporal


def typed_cell(raw, spec, cell=None, year_info=None):
    """Return {'value','status',...}. Status values stay raw text here: scoped status
    mappings are applied later by policy, never by a global dictionary at parse time."""
    typ = spec['sql_type'];role = spec['role']
    if typ in {'DATE', 'TIMESTAMP'}:
        parsed = parse_temporal(raw, cell=cell, timestamp=typ == 'TIMESTAMP', year_info=year_info,
                                order=spec.get('date_order', 'dmy'))
        return {'value': parsed['value'], 'status': parsed['status'], 'temporal': parsed}
    if role == 'status':
        value, status, _ = normalize_value(raw, {**spec, 'role': 'text', 'sql_type': 'TEXT'}, cell=cell)
        return {'value': value, 'status': status}
    value, status, unit = normalize_value(raw, spec, cell=cell)
    result = {'value': value, 'status': status}
    if unit:
        result['unit'] = unit
    return result
