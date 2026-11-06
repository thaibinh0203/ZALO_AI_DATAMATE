"""Typed answer matching against the supplied DataMate rules (local profile v1).

No SQL execution, model imports or network access. See docs/EVALUATOR.md for
documented uncertainties versus the unavailable organizers' scorer.
"""
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import math
import re
import unicodedata

VALUE_TYPES = ("number", "percent", "string", "date", "boolean", "list", "table")
REFERENCE_TYPES = VALUE_TYPES + ("unanswerable",)
PROFILE = "datamate-local-v1"


class AnswerFormatError(ValueError):
    pass


def normalize_text(value):
    text = unicodedata.normalize("NFC", value).lower()
    return " ".join("".join(c if c.isalpha() or c.isdigit() or c.isspace()
                            else " " for c in text).split())


def is_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return False
    if isinstance(value, float):
        return math.isfinite(value)
    return not isinstance(value, Decimal) or value.is_finite()


def decimal_number(value):
    return value if isinstance(value, Decimal) else Decimal(str(value))


def rounded_percent(value):
    value = decimal_number(value)
    # Generous input guard; legitimate DataMate percentages are far below this.
    if abs(value.adjusted()) > 1000:
        raise AnswerFormatError("Percent exponent exceeds local evaluator limit")
    with localcontext() as ctx:
        ctx.prec = max(28, len(value.as_tuple().digits) + abs(value.adjusted()) + 4)
        return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def scalar_valid(value):
    return isinstance(value, str) or is_number(value)


def validate_date(value):
    if not isinstance(value, str):
        raise AnswerFormatError("date must be an ISO string")
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}", value):
        date.fromisoformat(value + "-01")
    elif re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        date.fromisoformat(value)
    else:
        raise AnswerFormatError("date must be YYYY-MM-DD or YYYY-MM")


def validate_payload(kind, value):
    if kind in ("number", "percent"):
        if not is_number(value):
            raise AnswerFormatError(f"{kind} requires a finite JSON number, not bool/string")
        if kind == "percent":
            rounded_percent(value)
    elif kind == "string":
        if not isinstance(value, str):
            raise AnswerFormatError("string requires a JSON string")
    elif kind == "boolean":
        if type(value) is not bool:
            raise AnswerFormatError("boolean requires true/false")
    elif kind == "date":
        try:
            validate_date(value)
        except ValueError as error:
            raise AnswerFormatError(str(error)) from error
    elif kind == "list":
        if not isinstance(value, list) or not all(scalar_valid(x) for x in value):
            raise AnswerFormatError("list requires ordered string/number elements")
    elif kind == "table":
        if not isinstance(value, dict) or set(value) != {"columns", "rows"}:
            raise AnswerFormatError("table requires exactly columns and rows")
        columns, rows = value["columns"], value["rows"]
        if not isinstance(columns, list) or not all(isinstance(c, str) and c.strip() for c in columns):
            raise AnswerFormatError("table columns must be nonempty strings")
        if len({normalize_text(c) for c in columns}) != len(columns):
            raise AnswerFormatError("Duplicate normalized table columns are ambiguous")
        if not isinstance(rows, list) or any(
            not isinstance(row, list) or len(row) != len(columns)
            or not all(scalar_valid(x) for x in row) for row in rows
        ):
            raise AnswerFormatError("table rows must be rectangular, with string/number cells")
    else:
        raise AnswerFormatError(f"Unsupported value type: {kind}")


def validate_record(record, *, reference=False):
    if not isinstance(record, dict):
        raise AnswerFormatError("Answer record must be an object")
    kind = record.get("type")
    allowed = REFERENCE_TYPES if reference else REFERENCE_TYPES + ("abstain",)
    if not isinstance(kind, str) or kind not in allowed:
        raise AnswerFormatError("Unknown/missing answer type")
    if kind in VALUE_TYPES:
        if "answer" not in record:
            raise AnswerFormatError("Missing answer value")
        validate_payload(kind, record["answer"])
    elif kind == "unanswerable":
        if record.get("answer") is not None:
            raise AnswerFormatError("unanswerable cannot contain a value answer")
        if "reason" in record and not isinstance(record["reason"], str):
            raise AnswerFormatError("unanswerable reason must be a string")
    if reference:
        aliases = record.get("alias", [])
        if not isinstance(aliases, list):
            raise AnswerFormatError("Reference alias must be a list")
        if kind == "string":
            if not all(isinstance(x, str) for x in aliases):
                raise AnswerFormatError("String aliases must be strings")
        elif kind in ("list", "table"):
            values = record["answer"] if kind == "list" else record["answer"]["rows"]
            if aliases and len(aliases) != len(values):
                raise AnswerFormatError("Aliases must align with list positions/table reference rows")
            if any(not isinstance(xs, list) or not all(isinstance(x, str) for x in xs) for xs in aliases):
                raise AnswerFormatError("Element/row aliases must be lists of strings")
        elif aliases:
            raise AnswerFormatError("Aliases are supported only for string/list/table")


def scalar_matches(predicted, expected, aliases=()):
    if isinstance(expected, str):
        return isinstance(predicted, str) and normalize_text(predicted) in {
            normalize_text(x) for x in (expected, *aliases)}
    return is_number(predicted) and is_number(expected) and decimal_number(predicted) == decimal_number(expected)


def table_matches(predicted, expected, aliases):
    pcols, ecols = predicted["columns"], expected["columns"]
    if len(pcols) != len(ecols) or len(predicted["rows"]) != len(expected["rows"]):
        return False
    normalized = [normalize_text(c) for c in pcols]
    wanted = [normalize_text(c) for c in ecols]
    # Match by all normalized names when possible; otherwise use column position.
    mapping = [normalized.index(c) for c in wanted] if set(normalized) == set(wanted) else list(range(len(ecols)))
    prows = [[row[i] for i in mapping] for row in predicted["rows"]]
    erows = expected["rows"]
    edges = []
    for prow in prows:
        edges.append([i for i, erow in enumerate(erows) if all(
            scalar_matches(pcell, ecell, aliases[i] if aliases and column == 0 else ())
            for column, (pcell, ecell) in enumerate(zip(prow, erow)))])
    # One-to-one matching avoids greedy failures when reference aliases overlap.
    assigned = {}
    def augment(pred_index, seen):
        for ref_index in edges[pred_index]:
            if ref_index in seen:
                continue
            seen.add(ref_index)
            if ref_index not in assigned or augment(assigned[ref_index], seen):
                assigned[ref_index] = pred_index
                return True
        return False
    return all(augment(i, set()) for i in range(len(prows)))


def matches(predicted, reference):
    kind = reference["type"]
    p, e = predicted.get("answer"), reference.get("answer")
    aliases = reference.get("alias", [])
    if kind == "unanswerable":
        return True
    if kind in ("number", "string"):
        return scalar_matches(p, e, aliases)
    if kind == "percent":
        return rounded_percent(p) == rounded_percent(e)
    if kind in ("boolean", "date"):
        return p == e
    if kind == "list":
        return len(p) == len(e) and all(scalar_matches(pv, ev, aliases[i] if aliases else ())
                                         for i, (pv, ev) in enumerate(zip(p, e)))
    return table_matches(p, e, aliases)


def score_answer(reference, prediction):
    """Return a diagnostic; invalid reference data is a hard error, never a wrong answer."""
    validate_record(reference, reference=True)
    result = {"expected_type": reference["type"], "prediction_type": None,
              "format_error": None, "outcome": "abstain", "score": 0, "reason": "missing_prediction"}
    if prediction is None:
        return result
    kind = prediction.get("type") if isinstance(prediction, dict) else None
    result["prediction_type"] = kind if isinstance(kind, str) else None
    try:
        validate_record(prediction)
    except (AnswerFormatError, InvalidOperation) as error:
        result["format_error"] = str(error)
        # Explicitly specified by rulebook: non-string reason is unreadable/abstain.
        if isinstance(prediction, dict) and prediction.get("type") == "unanswerable" and (
            "reason" in prediction and not isinstance(prediction["reason"], str)
        ):
            result["reason"] = "unreadable_unanswerable_reason"
            return result
        result.update(outcome="wrong", score=-1, reason="invalid_prediction_format")
        return result
    if prediction["type"] == "abstain":
        result["reason"] = "explicit_abstain"
    elif prediction["type"] != reference["type"]:
        result.update(outcome="wrong", score=-1, reason="type_mismatch")
    elif matches(prediction, reference):
        result.update(outcome="correct", score=1, reason="matched")
    else:
        result.update(outcome="wrong", score=-1, reason="value_mismatch")
    return result
