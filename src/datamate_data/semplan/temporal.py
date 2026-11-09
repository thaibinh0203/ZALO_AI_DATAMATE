"""Source dates with explicit precision/resolution; a year is never invented.

A source date missing its year stays partial unless source scope (sheet, title rows,
file name) supplies exactly one year. Question-time resolution is a separate concern
handled by the baseline time resolver using as_of_date and the rulebook.
"""
import calendar as _calendar
from datetime import date, datetime
import re
import unicodedata

PRECISIONS = ('datetime', 'day', 'month_day', 'month', 'year')
YEAR = re.compile(r'(?<![0-9])(20[0-9]{2}|19[0-9]{2})(?![0-9])')
DMY = re.compile(r'([0-9]{1,2})[/.-]([0-9]{1,2})(?:[/.-]([0-9]{4}|[0-9]{2}))?'
                 r'(?:[ T]+([0-9]{1,2}):([0-9]{2})(?::([0-9]{2}))?)?')
YMD = re.compile(r'([0-9]{4})[/.-]([0-9]{1,2})[/.-]([0-9]{1,2})(?:[ T]+([0-9]{1,2}):([0-9]{2})(?::([0-9]{2}))?(?:\.[0-9]+)?)?')
VIET = re.compile(r'(?:ngay\s*)?([0-9]{1,2})\s*(?:thg|thang)\s*([0-9]{1,2})(?:\s*(?:nam)?\s*([0-9]{4}))?')
MONTH_YEAR = re.compile(r'(?:thang\s*|t)?([0-9]{1,2})\s*[/.-]\s*([0-9]{4})')


def fold(text):
    """Lowercase, accent-free, single-spaced text for pattern matching only."""
    text = unicodedata.normalize('NFD', str(text).lower().replace('đ', 'd'))
    return re.sub(r'\s+', ' ', ''.join(c for c in text if not unicodedata.combining(c))).strip()


def _years(text):
    return {int(y) for y in YEAR.findall(str(text or ''))}


def year_evidence(part, rules=None):
    """Return the single source-scoped year for dates without a year, or why not.

    Precedence follows the rulebook clause (year of the sheet holding the row) when
    present; title rows and file names are used only when they agree.
    """
    sheet = part.get('source_table') if part.get('format') == 'xlsx' else None
    file_stem = str(part.get('source_file', '')).rsplit('.', 1)[0]
    header_texts = [v for row in part.get('header_context', [])[:-1] for v in (row or []) if isinstance(v, str)]
    found = {'sheet': _years(sheet), 'header': set().union(*[_years(t) for t in header_texts]) if header_texts else set(),
             'file': _years(file_stem)}
    sheet_rule = (rules or {}).get('missing_year_from_sheet')
    if len(found['sheet']) == 1:
        year = next(iter(found['sheet']))
        conflicts = [s for s in ('header', 'file') if found[s] and found[s] != {year}]
        return {'year': year, 'source': 'sheet', 'status': 'single', 'evidence_refs': [sheet_rule] if sheet_rule else [],
                'conflicting_sources': conflicts, 'candidates': {k: sorted(v) for k, v in found.items()}}
    if len(found['sheet']) > 1:
        return {'year': None, 'source': None, 'status': 'conflicting', 'evidence_refs': [],
                'conflicting_sources': ['sheet'], 'candidates': {k: sorted(v) for k, v in found.items()}}
    singles = {s: next(iter(v)) for s, v in found.items() if len(v) == 1}
    multi = [s for s, v in found.items() if len(v) > 1]
    if multi or len(set(singles.values())) > 1:
        return {'year': None, 'source': None, 'status': 'conflicting', 'evidence_refs': [],
                'conflicting_sources': multi or sorted(singles), 'candidates': {k: sorted(v) for k, v in found.items()}}
    if singles:
        source = 'header' if 'header' in singles else 'file'
        return {'year': singles[source], 'source': source, 'status': 'single', 'evidence_refs': [],
                'conflicting_sources': [], 'candidates': {k: sorted(v) for k, v in found.items()}}
    return {'year': None, 'source': None, 'status': 'none', 'evidence_refs': [], 'conflicting_sources': [],
            'candidates': {k: sorted(v) for k, v in found.items()}}


def readme_day_month_order(readme):
    folded = str(readme).lower()
    if re.search(r'mm/dd(/yyyy)?', folded):
        return 'mdy'
    if re.search(r'dd/mm(/yyyy)?', folded):
        return 'dmy'
    return None


def day_month_pattern(value):
    """Classify a raw text date as dmy-only / mdy-only / both-possible evidence."""
    if not isinstance(value, str):
        return None
    m = DMY.fullmatch(value.strip())
    if not m:
        return None
    a, b = int(m[1]), int(m[2])
    if a > 12 and b <= 12:
        return 'dmy_only'
    if b > 12 and a <= 12:
        return 'mdy_only'
    if a <= 12 and b <= 12:
        return 'both_possible'
    return 'invalid'


def infer_day_month_order(stats, readme_order=None):
    """Column-level dd/mm vs mm/dd decision with the evidence that justifies it."""
    dmy, mdy = stats.get('dmy_only', 0), stats.get('mdy_only', 0)
    if readme_order:
        conflict = (readme_order == 'dmy' and mdy) or (readme_order == 'mdy' and dmy)
        return {'order': 'ambiguous' if conflict else readme_order,
                'basis': 'readme_format_conflicts_with_values' if conflict else 'readme_format_statement'}
    if dmy and mdy:
        return {'order': 'ambiguous', 'basis': 'values_show_both_orders'}
    if mdy:
        return {'order': 'mdy', 'basis': 'values_show_month_first_only'}
    if dmy:
        return {'order': 'dmy', 'basis': 'values_show_day_first_only'}
    return {'order': 'dmy', 'basis': 'default_vietnamese_day_first_no_contrary_values'}


def _result(value=None, *, year=None, month=None, day=None, precision=None, resolution, status='ok'):
    return {'value': value, 'year': year, 'month': month, 'day': day, 'precision': precision,
            'resolution': resolution, 'status': status}


def _valid_day(year, month, day):
    if not 1 <= month <= 12 or day < 1:
        return False
    if year is None:
        return day <= (29 if month == 2 else _calendar.monthrange(2001, month)[1])
    return day <= _calendar.monthrange(year, month)[1]


def _full(year, month, day, hh, mm, ss, timestamp, resolution):
    try:
        if timestamp:
            value = datetime(year, month, day, int(hh or 0), int(mm or 0), int(ss or 0))
            precision = 'datetime' if hh is not None else 'day'
        else:
            value = date(year, month, day);precision = 'day'
    except ValueError:
        return _result(resolution='invalid', status='error:invalid_date', year=year, month=month, day=day)
    return _result(value, year=year, month=month, day=day, precision=precision, resolution=resolution)


def parse_temporal(raw, *, cell=None, timestamp=False, year_info=None, order='dmy'):
    """Parse one source cell. Returns dict with value/parts/precision/resolution/status.

    ``value`` is a full date/datetime only when the day is fully known. Partial dates
    keep known parts and ``status='partial'``; invalid dates are errors, never coerced.
    """
    cell = cell or {}
    if raw is None or isinstance(raw, str) and not raw.strip():
        return _result(resolution='missing', status='missing')
    if cell.get('formula'):
        if not cell.get('has_cached_value'):
            return _result(resolution='error', status='error:formula_cache_missing')
        raw = cell['cached_value']
    if isinstance(raw, datetime):
        value = raw if timestamp else raw.date()
        precision = 'datetime' if timestamp and raw.time() != datetime.min.time() else 'day'
        return _result(value, year=raw.year, month=raw.month, day=raw.day, precision=precision, resolution='native')
    if isinstance(raw, date):
        value = datetime(raw.year, raw.month, raw.day) if timestamp else raw
        return _result(value, year=raw.year, month=raw.month, day=raw.day, precision='day', resolution='native')
    if isinstance(raw, bool):
        return _result(resolution='invalid', status='error:boolean_is_not_date')
    if isinstance(raw, (int, float)):
        if cell.get('excel_epoch'):
            from openpyxl.utils.datetime import from_excel
            converted = from_excel(raw, datetime.fromisoformat(cell['excel_epoch']))
            parsed = parse_temporal(converted, timestamp=timestamp)
            parsed['resolution'] = 'excel_serial'
            return parsed
        return _result(resolution='invalid', status='error:number_without_date_format')
    text = str(raw).strip()
    if re.search(r'(?:[+-][0-9]{2}:[0-9]{2}|Z)$', text) and re.search(r'[0-9]T[0-9]', text):
        return _result(resolution='error', status='error:timezone_requires_explicit_policy')
    m = YMD.fullmatch(text)
    if m:
        return _full(int(m[1]), int(m[2]), int(m[3]), m[4], m[5], m[6], timestamp, 'explicit')
    m = DMY.fullmatch(text)
    if m:
        a, b = int(m[1]), int(m[2]);year = m[3]
        if year is not None and len(year) == 2:
            return _result(resolution='invalid', status='error:two_digit_year_requires_policy')
        if order == 'ambiguous' and a <= 12 and b <= 12 and a != b:
            return _result(year=int(year) if year else None, resolution='ambiguous_day_month', status='partial',
                           precision='year' if year else None)
        if order == 'mdy':
            a, b = b, a
        day, month = a, b
        if year is not None:
            return _full(int(year), month, day, m[4], m[5], m[6], timestamp, 'explicit')
        return _partial(day, month, year_info, timestamp, m[4], m[5], m[6])
    folded = fold(text)
    m = VIET.fullmatch(folded)
    if m and m[3]:
        return _full(int(m[3]), int(m[2]), int(m[1]), None, None, None, timestamp, 'explicit')
    if m and not m[3]:
        return _partial(int(m[1]), int(m[2]), year_info, timestamp)
    m = MONTH_YEAR.fullmatch(folded)
    if m and 1 <= int(m[1]) <= 12:
        return _result(year=int(m[2]), month=int(m[1]), precision='month', resolution='explicit', status='partial')
    if re.fullmatch(r'(19|20)[0-9]{2}', text):
        return _result(year=int(text), precision='year', resolution='explicit', status='partial')
    return _result(resolution='invalid', status='error:invalid_or_unsupported_date')


def _partial(day, month, year_info, timestamp, hh=None, mm=None, ss=None):
    if not _valid_day(None, month, day):
        return _result(resolution='invalid', status='error:invalid_month_day', month=month, day=day)
    info = year_info or {}
    if info.get('status') == 'single' and info.get('year'):
        year = info['year']
        if not _valid_day(year, month, day):
            return _result(resolution='invalid', status='error:invalid_for_source_year', year=year, month=month, day=day)
        resolved = _full(year, month, day, hh, mm, ss, timestamp, 'year_from_'+info['source'])
        return resolved
    resolution = 'conflicting_year' if info.get('status') == 'conflicting' else 'missing_year'
    return _result(month=month, day=day, precision='month_day', resolution=resolution, status='partial')


def companion_values(parsed):
    """Values stored in <col>__y/__m/__d/__p/__r companions."""
    return [parsed.get('year'), parsed.get('month'), parsed.get('day'), parsed.get('precision'), parsed.get('resolution')]
