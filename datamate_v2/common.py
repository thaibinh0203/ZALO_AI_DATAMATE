"""Small shared helpers; all persistent JSON uses exact Decimal numbers."""
from __future__ import annotations
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import hashlib
import re
import simplejson as json
import unicodedata

ROOT = Path(__file__).resolve().parent
VERSION = "v2.0.0"

class PipelineError(ValueError):
    pass

class ContextBudgetError(PipelineError):
    """Deterministic prompt overflow; repeating inference cannot repair it."""
    pass

class BudgetError(PipelineError):
    pass

def dumps(value, **kwargs):
    def default(x):
        if isinstance(x, (date, datetime, Path)): return str(x)
        if isinstance(x, bytes): return {"hex": x.hex()}
        raise TypeError(type(x).__name__)
    return json.dumps(value, ensure_ascii=False, use_decimal=True, allow_nan=False, default=default, **kwargs)

def loads(value):
    def pairs(xs):
        out={}
        for k,v in xs:
            if k in out: raise PipelineError(f"Duplicate JSON key: {k}")
            out[k]=v
        return out
    return json.loads(value, use_decimal=True, object_pairs_hook=pairs, allow_nan=False)

def read_json(path): return loads(Path(path).read_text(encoding="utf-8"))

def write_json(path, data):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+".tmp")
    temp.write_text(dumps(data, indent=2)+"\n",encoding="utf-8"); temp.replace(path)

def read_jsonl(path):
    records=[]; ids=set()
    with Path(path).open(encoding="utf-8") as stream:
        for number,line in enumerate(stream,1):
            if not line.strip(): continue
            row=loads(line)
            if not isinstance(row,dict) or not isinstance(row.get("id"),str) or not row["id"]:
                raise PipelineError(f"Invalid id at {path}:{number}")
            if row["id"] in ids: raise PipelineError(f"Duplicate id: {row['id']}")
            ids.add(row["id"]); records.append(row)
    return records

def fold(text):
    text=unicodedata.normalize("NFKD", str(text).lower().replace("đ","d"))
    return " ".join("".join(c if c.isalnum() else " " for c in text if not unicodedata.combining(c)).split())

def identifier(text, prefix="c"):
    value=re.sub(r"[^a-z0-9_]", "_", fold(text).replace(" ","_"))[:48].strip("_")
    if not value or value[0].isdigit(): value=prefix+"_"+value
    return value

def digest(value): return hashlib.sha256(dumps(value,sort_keys=True).encode()).hexdigest()

def file_hash(path):
    with Path(path).open("rb") as stream: return hashlib.file_digest(stream,"sha256").hexdigest()

def schema_name(business_id, fingerprint):
    return "b_"+hashlib.sha256(business_id.encode()).hexdigest()[:16]+"_"+fingerprint[:12]

def source_fingerprint(split,business_ids=None):
    split=Path(split)
    roots=[split/'data'/business for business in sorted(set(business_ids))] if business_ids is not None else [split/'data']
    paths=[p for root in roots for p in root.rglob('*') if p.is_file() and ':' not in p.name]
    paths += [split/'rulebook.md',split/'calendar.csv']
    return digest({str(p.relative_to(split)):file_hash(p) for p in sorted(paths)})

def utc_now(): return datetime.now(timezone.utc).isoformat()

def load_yaml(path):
    import yaml
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))
