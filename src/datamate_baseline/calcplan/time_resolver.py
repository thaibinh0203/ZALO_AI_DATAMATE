"""Deterministic question-time resolution from as_of_date and the rulebook (§2).

The model only identifies time phrases; bounds are computed here. Unsupported or
ambiguous expressions are reported as such and never guessed. Records dated after
as_of_date are treated as not yet happened, so windows are clamped to as_of_date.
"""
import calendar as _calendar
import csv
from datetime import date, timedelta
from pathlib import Path
import re
import unicodedata

KINDS = ('day', 'until_day', 'range', 'last_n_days', 'week', 'month', 'quarter', 'year', 'year_to_date',
         'tet', 'before_tet', 'shift')
RELATIVE = ('this', 'last')


def fold(text):
    text = unicodedata.normalize('NFD', str(text).lower().replace('đ', 'd'))
    return re.sub(r'\s+', ' ', ''.join(c for c in text if not unicodedata.combining(c))).strip()


class LunarCalendar:
    """calendar.csv lookup: date,weekday,lunar_day,lunar_month,lunar_year,lunar_leap,holiday."""

    def __init__(self, path):
        self.by_lunar = {};self.dates = []
        if path is None or not Path(path).exists():
            return
        with Path(path).open(encoding='utf-8-sig') as stream:
            for row in csv.DictReader(stream):
                try:
                    d = date.fromisoformat(row['date'])
                    lunar = (int(row['lunar_year']), int(row['lunar_month']), int(row['lunar_day']))
                except (KeyError, ValueError):
                    continue
                leap = str(row.get('lunar_leap', '')).strip().lower() in {'true', '1', 'yes'}
                self.by_lunar.setdefault(lunar, []).append((d, leap));self.dates.append(d)

    def solar(self, year, month, day):
        """Return (date, note). Prefer the non-leap month; None if not covered."""
        entries = self.by_lunar.get((year, month, day), [])
        regular = [d for d, leap in entries if not leap]
        if regular:
            return min(regular), 'leap_month_ignored' if len(entries) > len(regular) else None
        return None, 'not_in_calendar'


def _month_start(d):
    return d.replace(day=1)


def _next_month(d):
    return (d.replace(day=28)+timedelta(days=4)).replace(day=1)


def _prev_month(d):
    return (d.replace(day=1)-timedelta(days=1)).replace(day=1)


def _year_without(as_of, month):
    """Rulebook: month M without a year is this year unless after today, else last year."""
    return as_of.year if date(as_of.year, month, 1) <= as_of else as_of.year-1


class TimeResolver:
    def __init__(self, calendar_path=None, *, rule_refs=None):
        self.calendar = LunarCalendar(calendar_path);self.rule_refs = rule_refs or {}

    def resolve(self, spec, as_of):
        """spec: {'kind': ..., ...params, 'phrase': str}. Returns a resolution dict."""
        as_of = date.fromisoformat(as_of) if isinstance(as_of, str) else as_of
        kind = spec.get('kind');phrase = spec.get('phrase')
        base = {'phrase': phrase, 'kind': kind, 'start': None, 'end_exclusive': None, 'time_of_day': None,
                'clamped': False, 'notes': [], 'rule_refs': [r for r in [self.rule_refs.get('time')] if r]}
        try:
            if kind not in KINDS:
                return {**base, 'status': 'unsupported', 'notes': ['unsupported time kind']}
            start, end, notes = getattr(self, '_'+kind)(spec, as_of)
        except _Unresolved as exc:
            return {**base, 'status': exc.status, 'notes': [exc.reason]}
        result = {**base, 'status': 'resolved', 'notes': notes}
        if kind == 'shift':
            result['time_of_day'] = start;return result
        limit = as_of+timedelta(days=1)
        if end is not None and end > limit:
            end = limit;result['clamped'] = True;result['notes'].append('records_after_as_of_date_not_yet_happened')
        if start is not None and end is not None and start >= end:
            return {**base, 'status': 'ambiguous', 'notes': ['window starts after as_of_date']}
        result['start'] = start.isoformat() if start else None
        result['end_exclusive'] = end.isoformat() if end else None
        return result

    def previous(self, resolution, spec, as_of):
        """Comparison period per rulebook: previous period, same number of days for to-date windows."""
        as_of = date.fromisoformat(as_of) if isinstance(as_of, str) else as_of
        if resolution['status'] != 'resolved' or resolution['start'] is None:
            return {'status': 'unsupported', 'notes': ['no base window'], 'phrase': 'previous period'}
        start = date.fromisoformat(resolution['start']);end = date.fromisoformat(resolution['end_exclusive'])
        kind = spec.get('kind');notes = []
        if kind == 'month':
            prev = _prev_month(start)
            if spec.get('relative') == 'this':
                last = _calendar.monthrange(prev.year, prev.month)[1]
                pend = prev.replace(day=min(as_of.day, last))+timedelta(days=1);notes.append('same_number_of_days_previous_month')
            else:
                pend = start
            return self._pack(prev, pend, notes, 'previous month')
        if kind == 'tet':
            days = (end-start).days
            return self._pack(start-timedelta(days=days), start, ['same_number_of_days_before_23_chap'], 'before Tet')
        if kind == 'quarter':
            prev_start = date(start.year-1, 10, 1) if start.month == 1 else date(start.year, start.month-3, 1)
            span = (end-start).days if spec.get('relative') == 'this' else None
            pend = prev_start+timedelta(days=span) if span is not None else start
            return self._pack(prev_start, pend, ['same_number_of_days'] if span is not None else [], 'previous quarter')
        if kind in {'year', 'year_to_date'}:
            prev_start = date(start.year-1, 1, 1)
            if kind == 'year_to_date' or spec.get('relative') == 'this':
                try:
                    pend = date(start.year-1, as_of.month, as_of.day)+timedelta(days=1)
                except ValueError:
                    pend = date(start.year-1, 3, 1)
                notes.append('same_calendar_span_previous_year')
            else:
                pend = start
            return self._pack(prev_start, pend, notes, 'previous year')
        days = (end-start).days
        return self._pack(start-timedelta(days=days), start, ['same_length_immediately_before'], 'previous period')

    def _pack(self, start, end, notes, phrase):
        return {'status': 'resolved', 'phrase': phrase, 'kind': 'comparison', 'start': start.isoformat(),
                'end_exclusive': end.isoformat(), 'time_of_day': None, 'clamped': False, 'notes': notes,
                'rule_refs': [r for r in [self.rule_refs.get('time')] if r]}

    # -- kinds --------------------------------------------------------------------------
    def _date(self, spec, as_of):
        if spec.get('date'):
            return date.fromisoformat(spec['date']), []
        rel = spec.get('relative')
        if rel in {'today', 'yesterday', 'day_before_yesterday'}:
            return as_of-timedelta(days={'today': 0, 'yesterday': 1, 'day_before_yesterday': 2}[rel]), []
        day, month, year = spec.get('day'), spec.get('month'), spec.get('year')
        if not (isinstance(day, int) and isinstance(month, int)):
            raise _Unresolved('ambiguous', 'day requires date, relative or day+month')
        notes = []
        if year is None:
            year = as_of.year if date(as_of.year, month, 1) <= as_of else as_of.year-1
            notes.append('year_from_rulebook_no_year_rule')
        try:
            return date(year, month, day), notes
        except ValueError:
            raise _Unresolved('ambiguous', 'invalid calendar day')

    def _day(self, spec, as_of):
        d, notes = self._date(spec, as_of)
        return d, d+timedelta(days=1), notes

    def _until_day(self, spec, as_of):
        d, notes = self._date(spec, as_of)
        return None, d+timedelta(days=1), notes+['from_start_of_data']

    def _range(self, spec, as_of):
        s, n1 = self._date(spec['start'], as_of);e, n2 = self._date(spec['end'], as_of)
        if e < s:
            raise _Unresolved('ambiguous', 'range end before start')
        return s, e+timedelta(days=1), n1+n2+['inclusive_both_ends']

    def _last_n_days(self, spec, as_of):
        n = spec.get('n')
        if not isinstance(n, int) or not 1 <= n <= 3660:
            raise _Unresolved('ambiguous', 'n must be a positive integer')
        return as_of-timedelta(days=n-1), as_of+timedelta(days=1), ['includes_today']

    def _week(self, spec, as_of):
        monday = as_of-timedelta(days=as_of.weekday())
        if spec.get('relative') == 'this':
            return monday, as_of+timedelta(days=1), ['monday_to_today']
        if spec.get('relative') == 'last':
            return monday-timedelta(days=7), monday, ['full_previous_week_monday_to_sunday']
        raise _Unresolved('unsupported', 'only this/last week are supported')

    def _month(self, spec, as_of):
        rel = spec.get('relative')
        if rel == 'this':
            return _month_start(as_of), as_of+timedelta(days=1), ['month_to_today']
        if rel == 'last':
            prev = _prev_month(as_of);return prev, _month_start(as_of), ['full_previous_month']
        month = spec.get('month')
        if not isinstance(month, int) or not 1 <= month <= 12:
            raise _Unresolved('ambiguous', 'month must be 1..12 or relative')
        notes = []
        year = spec.get('year')
        if year is None:
            year = _year_without(as_of, month);notes.append('year_from_rulebook_no_year_rule')
        start = date(year, month, 1)
        return start, _next_month(start), notes+['full_month']

    def _quarter(self, spec, as_of):
        rel = spec.get('relative');q = spec.get('quarter')
        current = (as_of.month-1)//3+1
        if rel == 'this':
            return date(as_of.year, 3*current-2, 1), as_of+timedelta(days=1), ['quarter_to_today']
        if rel == 'last':
            year, q = (as_of.year-1, 4) if current == 1 else (as_of.year, current-1)
        else:
            if not isinstance(q, int) or not 1 <= q <= 4:
                raise _Unresolved('ambiguous', 'quarter must be 1..4')
            year = spec.get('year')
            if year is None:
                year = as_of.year if date(as_of.year, 3*q-2, 1) <= as_of else as_of.year-1
        start = date(year, 3*q-2, 1)
        end = date(year+1, 1, 1) if q == 4 else date(year, 3*q+1, 1)
        return start, end, ['calendar_quarter']

    def _year(self, spec, as_of):
        rel = spec.get('relative')
        if rel == 'this':
            return date(as_of.year, 1, 1), as_of+timedelta(days=1), ['year_to_today']
        if rel == 'last':
            return date(as_of.year-1, 1, 1), date(as_of.year, 1, 1), ['full_previous_year']
        year = spec.get('year')
        if not isinstance(year, int):
            raise _Unresolved('ambiguous', 'year required')
        return date(year, 1, 1), date(year+1, 1, 1), ['full_year']

    def _year_to_date(self, spec, as_of):
        return date(as_of.year, 1, 1), as_of+timedelta(days=1), ['from_january_1_to_today']

    def _tet(self, spec, as_of):
        year = spec.get('lunar_year') or (as_of.year if spec.get('relative') in {None, 'this'} else as_of.year-1)
        start, n1 = self.calendar.solar(year-1, 12, 23)
        end, n2 = self.calendar.solar(year, 1, 10)
        if start is None or end is None:
            raise _Unresolved('unsupported', 'calendar.csv does not cover this Tet window')
        return start, end+timedelta(days=1), [n for n in (n1, n2) if n]+['23_chap_to_10_gieng']

    def _before_tet(self, spec, as_of):
        start, end, notes = self._tet(spec, as_of)
        days = (end-start).days
        return start-timedelta(days=days), start, notes+['same_number_of_days_before_23_chap']

    def _shift(self, spec, as_of):
        shifts = {'morning': {'start': '06:00:00', 'end_exclusive': '14:00:00'},
                  'afternoon': {'start': '14:00:00', 'end_exclusive': '22:00:00'}}
        if spec.get('shift') not in shifts:
            raise _Unresolved('unsupported', 'shift must be morning/afternoon')
        return shifts[spec['shift']], None, ['by_order_creation_time']


class _Unresolved(Exception):
    def __init__(self, status, reason):
        super().__init__(reason);self.status = status;self.reason = reason


_DATE = r'(\d{1,2})[/-](\d{1,2})(?:[/-](\d{4}))?'
PATTERNS = [
    ('before_tet', re.compile(r'\btruoc tet\b')),
    ('tet', re.compile(r'\b(?:dip |mua )?tet(?: nam (\d{4}))?\b(?! (?:duong|tay))')),
    ('range', re.compile(r'\btu (?:ngay )?'+_DATE+r' (?:den|toi) (?:het )?(?:ngay )?'+_DATE)),
    ('until_day', re.compile(r'\b(?:den het|tinh den|den cuoi) (?:ngay )?(?:'+_DATE+r'|hom nay)')),
    ('last_n_days', re.compile(r'\b(\d{1,3}) ngay (?:gan nhat|gan day|qua|vua qua|gan nay)\b')),
    ('year_to_date', re.compile(r'\btu dau nam(?: (?:toi|den) (?:nay|hom nay))?\b')),
    ('month_year', re.compile(r'\bthang (\d{1,2}) ?(?:/|-|nam) ?(\d{4})\b')),
    ('month_year', re.compile(r'\bt(\d{1,2})/(\d{4})\b')),
    ('quarter_year', re.compile(r'\bquy (\d) ?(?:/|-|nam) ?(\d{4})\b')),
    ('day', re.compile(r'\b(?:ngay )?(\d{1,2})/(\d{1,2})/(\d{4})\b')),
    ('day_iso', re.compile(r'\b(\d{4})-(\d{2})-(\d{2})\b')),
    ('day_no_year', re.compile(r'\bngay (\d{1,2})/(\d{1,2})\b(?!/)')),
    ('relative_day', re.compile(r'\b(hom nay|hom qua|hom kia)\b')),
    ('week', re.compile(r'\btuan (nay|truoc|roi|vua roi|qua)\b')),
    ('month_rel', re.compile(r'\bthang (nay|truoc|roi|vua roi|qua)\b')),
    ('month', re.compile(r'\bthang (\d{1,2})\b(?! ?(?:/|-|nam) ?\d)')),
    ('quarter_rel', re.compile(r'\bquy (nay|truoc)\b')),
    ('quarter', re.compile(r'\bquy (\d)\b(?! ?(?:/|-|nam) ?\d)')),
    ('year_rel', re.compile(r'\bnam (nay|ngoai|truoc|roi)\b')),
    ('year', re.compile(r'\bnam (\d{4})\b')),
    ('shift', re.compile(r'\bca (sang|chieu)\b')),
]
LAST = {'truoc': 'last', 'roi': 'last', 'vua roi': 'last', 'qua': 'last', 'ngoai': 'last', 'nay': 'this'}


def extract_time(question):
    """Deterministic time phrases with resolver specs; longest/priority spans first."""
    text = fold(question);taken = [];found = []
    for name, pattern in PATTERNS:
        for m in pattern.finditer(text):
            if any(m.start() < e and s < m.end() for s, e in taken):
                continue
            spec = _spec(name, m)
            if spec is None:
                continue
            spec['phrase'] = m.group(0);spec['source'] = 'rule_extractor'
            taken.append((m.start(), m.end()));found.append((m.start(), spec))
    return [s for _, s in sorted(found, key=lambda x: x[0])]


def _int(value):
    return int(value) if value is not None else None


def _spec(name, m):
    g = m.groups()
    if name == 'tet':
        return {'kind': 'tet', 'lunar_year': _int(g[0])}
    if name == 'before_tet':
        return {'kind': 'before_tet'}
    if name == 'range':
        return {'kind': 'range', 'start': {'day': int(g[0]), 'month': int(g[1]), 'year': _int(g[2])},
                'end': {'day': int(g[3]), 'month': int(g[4]), 'year': _int(g[5])}}
    if name == 'until_day':
        if g[0] is None:
            return {'kind': 'until_day', 'relative': 'today'}
        return {'kind': 'until_day', 'day': int(g[0]), 'month': int(g[1]), 'year': _int(g[2])}
    if name == 'last_n_days':
        return {'kind': 'last_n_days', 'n': int(g[0])}
    if name == 'year_to_date':
        return {'kind': 'year_to_date'}
    if name == 'month_year':
        return {'kind': 'month', 'month': int(g[0]), 'year': int(g[1])} if 1 <= int(g[0]) <= 12 else None
    if name == 'quarter_year':
        return {'kind': 'quarter', 'quarter': int(g[0]), 'year': int(g[1])}
    if name == 'day':
        return {'kind': 'day', 'day': int(g[0]), 'month': int(g[1]), 'year': int(g[2])}
    if name == 'day_iso':
        return {'kind': 'day', 'date': f'{g[0]}-{g[1]}-{g[2]}'}
    if name == 'day_no_year':
        return {'kind': 'day', 'day': int(g[0]), 'month': int(g[1]), 'year': None}
    if name == 'relative_day':
        return {'kind': 'day', 'relative': {'hom nay': 'today', 'hom qua': 'yesterday', 'hom kia': 'day_before_yesterday'}[g[0]]}
    if name in {'week', 'month_rel', 'quarter_rel', 'year_rel'}:
        kind = {'week': 'week', 'month_rel': 'month', 'quarter_rel': 'quarter', 'year_rel': 'year'}[name]
        return {'kind': kind, 'relative': LAST[g[0]]}
    if name == 'month':
        return {'kind': 'month', 'month': int(g[0]), 'year': None} if 1 <= int(g[0]) <= 12 else None
    if name == 'quarter':
        return {'kind': 'quarter', 'quarter': int(g[0]), 'year': None} if 1 <= int(g[0]) <= 4 else None
    if name == 'year':
        return {'kind': 'year', 'year': int(g[0])}
    if name == 'shift':
        return {'kind': 'shift', 'shift': {'sang': 'morning', 'chieu': 'afternoon'}[g[0]]}
    return None


def comparison_requested(question):
    text = fold(question)
    return bool(re.search(r'\bso (?:voi|vs) (?:thang|tuan|quy|nam|ky|ngay|cung ky) ?(?:truoc|ngoai|roi)?\b|\btang truong\b|'
                          r'\bthay doi\b|\btang (?:hay giam|bao nhieu)\b|\bgiam bao nhieu\b|\bchenh lech\b|\bkem\b', text))
