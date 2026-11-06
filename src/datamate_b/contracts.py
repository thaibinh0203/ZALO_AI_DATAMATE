"""Strict internal B1 contracts. Validation never executes generated SQL."""
import json
import math
import re
from datetime import date

VALUE_TYPES = {"number", "percent", "string", "date", "boolean", "list", "table"}


def reject_constant(value):
    raise ValueError(f"Non-finite JSON constant: {value}")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def strict_json(text):
    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("Non-finite JSON number")
        return number
    return json.loads(text, parse_constant=reject_constant, parse_float=finite_float,
                      object_pairs_hook=unique_object)


def validate_request(value):
    if not isinstance(value, dict):
        raise ValueError("Question must be an object")
    for key in ("question", "as_of_date"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise ValueError(f"Missing/non-string {key}")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value["as_of_date"]):
        raise ValueError("as_of_date must be YYYY-MM-DD")
    date.fromisoformat(value["as_of_date"])
    if "id" in value and (not isinstance(value["id"], str) or not value["id"].strip()):
        raise ValueError("id must be a nonempty string")
    return value


def validate_sql(sql, schema=None, dialect="postgres"):
    import sqlglot
    from sqlglot import exp
    from sqlglot.errors import ErrorLevel
    from sqlglot.optimizer.qualify import qualify

    if dialect not in {"duckdb","postgres"}:raise ValueError("Unsupported SQL dialect")
    trees = sqlglot.parse(sql, read=dialect, error_level=ErrorLevel.RAISE)
    if len(trees) != 1 or not isinstance(trees[0], (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        raise ValueError("Expected exactly one read-only SELECT query")
    tree = trees[0]
    forbidden = {"insert", "update", "delete", "create", "drop", "alter", "copy", "command",
                 "merge", "transaction", "into", "use", "pragma", "attach", "detach", "load",
                 "currentdate", "currenttimestamp", "currenttime"}
    for node in tree.walk():
        if node.key.lower() in forbidden:
            raise ValueError(f"Disallowed SQL construct: {node.key}")
        # External/file table functions are not plain table identifiers.
        if isinstance(node, exp.Table) and (
            not isinstance(node.this, exp.Identifier) or node.db or node.catalog
        ):
            raise ValueError("Only local schema tables/CTEs are supported")
        if isinstance(node, exp.Anonymous):
            raise ValueError(f"Unsupported/unknown function: {node.name}")
    if schema:
        catalog = {}
        for ddl in sqlglot.parse(schema, read=dialect):
            if not isinstance(ddl, exp.Create) or not isinstance(ddl.this, exp.Schema):
                raise ValueError("Schema must contain CREATE TABLE DDL")
            catalog[ddl.this.this.name] = {
                col.name: col.args["kind"].sql(dialect=dialect)
                for col in ddl.this.expressions if isinstance(col, exp.ColumnDef)
            }
        if not catalog:
            raise ValueError("Empty schema")
        ctes = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
        for table in tree.find_all(exp.Table):
            if table.name.lower() not in {n.lower() for n in catalog} | ctes:
                raise ValueError(f"Unknown table: {table.name}")
        qualify(tree.copy(), dialect=dialect, schema=catalog, validate_qualify_columns=True)
    # AST/column checks are not a sandbox or proof of business correctness.


def validate_result(value, schema=None, dialect="postgres"):
    keys = {"status", "answer_type", "sql", "missing_requirements"}
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"Expected exactly these keys: {sorted(keys)}")
    missing = value["missing_requirements"]
    if not isinstance(missing, list) or any(not isinstance(x, str) or not x.strip() for x in missing):
        raise ValueError("missing_requirements must be a list of nonempty strings")
    status = value["status"]
    if status == "sql":
        if not isinstance(value["answer_type"], str) or value["answer_type"] not in VALUE_TYPES:
            raise ValueError("Invalid SQL answer_type")
        if missing or not isinstance(value["sql"], str) or not value["sql"].strip():
            raise ValueError("sql requires nonempty SQL and no missing_requirements")
        validate_sql(value["sql"], schema, dialect)
    elif status in ("unanswerable", "abstain"):
        if value["sql"] is not None or value["answer_type"] != status:
            raise ValueError("Non-SQL status requires sql=null and matching answer_type")
        if status == "unanswerable" and not missing:
            raise ValueError("unanswerable requires missing information")
    else:
        raise ValueError("Unknown status")
    return value


def parse_result(raw, schema=None, dialect="postgres"):
    # No fence stripping, JSON extraction or silent repair: preserve exact output.
    return validate_result(strict_json(raw), schema, dialect)
