"""Preliminary row classification (P1) and grouping of candidates for review.

Rules decide only clear structure (blank, exact repeated header, ragged CSV, total
labels with empty identity cells, explicit note/metadata labels). Rows that need
context stay candidates; an unresolved candidate becomes ``unknown`` and is kept in
raw staging, excluded from analytic views and reported to the quality registry.
A row is never excluded just because a cell contains "tổng" or lacks a quantity.
"""
from collections import OrderedDict
from functools import lru_cache
import hashlib
import re

from ..normalization import NUMERIC, key
from ..parsers import dumps
from .temporal import fold

IDENTITY_ROLES = {'order_id', 'product_id', 'product_name', 'customer_id', 'customer_name',
                  'purchase_id', 'receipt_id', 'lot_id', 'store_id'}
TOTAL_EXACT = {'tong', 'tongcong', 'cong', 'subtotal', 'total', 'grandtotal', 'tongso',
               'tongcongtoanbo', 'tongtoanbo', 'congdon', 'congphatsinh', 'tongphatsinh'}
GRAND_EXACT = {'tongcong', 'grandtotal', 'tongcongtoanbo', 'tongtoanbo', 'total'}
TOTAL_PERIOD = re.compile(r'^(tong|cong)( cong)?\s*(ngay|thang|nam|tuan|quy|ky|don|phieu|ca|'
                          r'chi nhanh|cua hang|nhom|hang|loai)\b')
NOTE_PREFIX = re.compile(r'^(\*|\(\*\)|ghi chu|luu y|chu y|note|ghichu)\b')
METADATA_PREFIX = re.compile(r'^(nguoi lap|ngay in|ngay xuat|xuat luc|ky bao cao|tu ngay|den ngay|'
                             r'don vi|chi nhanh|cua hang|ma so thue|dia chi|dien thoai|bao cao|'
                             r'so chi tiet|danh sach)\b')


def _empty(value):
    return value is None or isinstance(value, str) and not value.strip()


def _numeric_like(value):
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    return isinstance(value, str) and bool(re.fullmatch(r'\s*[(+-]?[0-9][0-9.,\s]*\)?\s*(đ|vnd|k)?\s*', value.lower()))


@lru_cache(maxsize=256)
def _header_keys(headers):
    return tuple(key(h) for h in headers)


def total_label(values):
    """Return (index, label_kind) for the first non-empty text cell when it is a total label."""
    for index, value in enumerate(values):
        if _empty(value):
            continue
        if not isinstance(value, str):
            return None
        # Cheap prefilter: every total label starts with t/c/s/g (tổng, cộng, subtotal, grand).
        if value.lstrip()[:1].lower() not in {'t', 'c', 's', 'g'}:
            return None
        folded = fold(value);compact = key(value)
        if compact in GRAND_EXACT:
            return index, 'grand_total'
        if compact in TOTAL_EXACT or TOTAL_PERIOD.match(folded):
            return index, 'subtotal'
        return None
    return None


def classify(values, *, headers, columns, flags=(), table_format='csv'):
    """Return a preliminary decision dict for one source row (no context lookahead)."""
    non_empty = [i for i, v in enumerate(values) if not _empty(v)]
    if not non_empty:
        return {'kind': 'blank', 'candidate_kind': None, 'decision_status': 'decided_rule', 'rule': 'all_cells_empty'}
    if any(isinstance(f, dict) and f.get('error') == 'ragged_row' for f in flags):
        return {'kind': 'quarantine', 'candidate_kind': None, 'decision_status': 'decided_rule', 'rule': 'ragged_csv_row'}
    header_keys = _header_keys(tuple(headers))
    # Header repeats are text equal to the header at the same position; compute keys
    # lazily and stop as soon as the 60% threshold cannot be reached.
    needed = max(2, int(0.6*len(non_empty)));matched = 0;remaining = len(non_empty)
    for i in non_empty:
        remaining -= 1
        value = values[i]
        if i < len(header_keys) and isinstance(value, str) and len(value) <= 3*len(header_keys[i])+12 and key(value) == header_keys[i]:
            matched += 1
        if matched+remaining < needed:
            break
    if matched == len(non_empty) and len(non_empty) == sum(1 for h in header_keys if h):
        return {'kind': 'repeated_header', 'candidate_kind': None, 'decision_status': 'decided_rule', 'rule': 'exact_header_repeat'}
    if len(non_empty) >= 2 and matched >= needed:
        return {'kind': 'unknown', 'candidate_kind': 'repeated_header', 'decision_status': 'candidate', 'rule': 'partial_header_repeat'}
    roles = [c['role'] for c in columns]
    identity = [i for i, r in enumerate(roles) if r in IDENTITY_ROLES]
    measures = [i for i, r in enumerate(roles) if r in NUMERIC]
    label = total_label(values)
    if label:
        index, label_kind = label
        other_identity = [i for i in identity if i != index and not _empty(values[i])]
        has_measure = any(_numeric_like(values[i]) for i in measures) or any(
            _numeric_like(values[i]) for i in non_empty if i != index)
        if not other_identity and has_measure:
            return {'kind': label_kind, 'candidate_kind': None, 'decision_status': 'decided_rule',
                    'rule': 'total_label_with_empty_identity', 'label_column': index}
        if roles[index] == 'product_name' and other_identity:
            return {'kind': 'data', 'candidate_kind': None, 'decision_status': 'decided_rule',
                    'rule': 'total_word_inside_product_name', 'note': 'label_like_product_kept_as_data'}
        return {'kind': 'unknown', 'candidate_kind': label_kind, 'decision_status': 'candidate',
                'rule': 'total_label_with_identity_or_no_measure', 'label_column': index}
    if len(non_empty) == 1:
        index = non_empty[0];value = values[index]
        if roles[index] in NUMERIC and _numeric_like(value) and identity:
            return {'kind': 'unknown', 'candidate_kind': 'subtotal', 'decision_status': 'candidate', 'rule': 'unlabeled_numeric_row'}
        if isinstance(value, str) and NOTE_PREFIX.match(fold(value)):
            return {'kind': 'note', 'candidate_kind': None, 'decision_status': 'decided_rule', 'rule': 'note_prefix_single_cell'}
        if isinstance(value, str) and METADATA_PREFIX.match(fold(value)):
            return {'kind': 'metadata', 'candidate_kind': None, 'decision_status': 'decided_rule', 'rule': 'metadata_label_single_cell'}
        if roles[index] in IDENTITY_ROLES:
            return {'kind': 'data', 'candidate_kind': None, 'decision_status': 'decided_rule', 'rule': 'single_identity_cell'}
        if isinstance(value, str) and len(value.split()) >= 4:
            return {'kind': 'unknown', 'candidate_kind': 'note', 'decision_status': 'candidate', 'rule': 'single_long_text_cell'}
        return {'kind': 'unknown', 'candidate_kind': 'metadata', 'decision_status': 'candidate', 'rule': 'single_non_identity_cell'}
    if len(non_empty) <= 2 and isinstance(values[non_empty[0]], str) and METADATA_PREFIX.match(fold(values[non_empty[0]])) and not any(
            i in identity for i in non_empty[1:]):
        return {'kind': 'metadata', 'candidate_kind': None, 'decision_status': 'decided_rule', 'rule': 'metadata_label_value'}
    if identity and all(_empty(values[i]) for i in identity):
        texts = [i for i in non_empty if isinstance(values[i], str) and not _numeric_like(values[i])]
        dates = [i for i in non_empty if roles[i] in {'date', 'timestamp'}]
        if not texts and not dates and all(_numeric_like(values[i]) for i in non_empty):
            return {'kind': 'unknown', 'candidate_kind': 'subtotal', 'decision_status': 'candidate', 'rule': 'unlabeled_numeric_row'}
    return {'kind': 'data', 'candidate_kind': None, 'decision_status': 'decided_rule', 'rule': 'default_structured_row'}


def finalize_part(decisions):
    """Context pass after one source part: totals after the last data row are grand totals."""
    last_data = max((d['seq'] for d in decisions if d['kind'] == 'data'), default=-1)
    for d in decisions:
        if d['seq'] > last_data and d['kind'] == 'subtotal' and d['decision_status'] == 'decided_rule':
            d['kind'] = 'grand_total';d['rule'] += '+after_last_data_row'
        if d['seq'] > last_data and d.get('candidate_kind') == 'subtotal':
            d['candidate_kind'] = 'grand_total'
    return decisions


def candidate_groups(candidates, columns, *, max_groups=12, samples_per_group=4, ref_prefix='S'):
    """Group candidate rows by rule/label/presence pattern with diverse samples.

    Returns groups with sample refs; rows beyond max_groups stay unreviewed (unknown).
    """
    groups = OrderedDict()
    for row in candidates:
        values = row['raw']
        label = total_label(values)
        first = next((v for v in values if not _empty(v)), '')
        words = ' '.join(fold(first).split()[:2]) if isinstance(first, str) else type(first).__name__
        presence = ''.join('1' if not _empty(v) else '0' for v in values)
        signature = (row['decision'].get('candidate_kind'), row['decision']['rule'],
                     label[1] if label else words, presence)
        groups.setdefault(signature, []).append(row)
    result = [];counter = 0;overflow = []
    for index, (signature, rows) in enumerate(groups.items(), 1):
        if index > max_groups:
            overflow.extend(rows);continue
        ordered = sorted(rows, key=lambda r: hashlib.sha256(r['row_id'].encode()).hexdigest())
        chosen = [rows[0]] + ([rows[-1]] if len(rows) > 1 else [])
        chosen += [r for r in ordered if r not in chosen][:max(0, samples_per_group-len(chosen))]
        samples = []
        for row in chosen:
            counter += 1
            samples.append({'ref': f'{ref_prefix}{counter}', 'row_id': row['row_id'],
                            'source': row['source'], 'values': {c['original_name']: (None if _empty(v) else str(v)[:80])
                                                                 for c, v in zip(columns, row['raw'])}})
        result.append({'group_id': f'G{index}', 'candidate_kind': signature[0], 'rule': signature[1],
                       'label': signature[2], 'rows': len(rows), 'row_ids': [r['row_id'] for r in rows],
                       'samples': samples})
    return result, overflow


def decision_record(row_id, seq, part, decision, **extra):
    record = {'row_id': row_id, 'seq': seq, 'part': part, 'kind': decision['kind'],
              'candidate_kind': decision.get('candidate_kind'), 'decision_status': decision['decision_status'],
              'method': decision.get('method', 'rule'), 'rule': decision.get('rule'),
              'evidence_refs': decision.get('evidence_refs', [])}
    record.update(extra)
    return record


def decision_digest(decisions):
    return hashlib.sha256(dumps(sorted((d['row_id'], d['kind'], d['decision_status']) for d in decisions)).encode()).hexdigest()
