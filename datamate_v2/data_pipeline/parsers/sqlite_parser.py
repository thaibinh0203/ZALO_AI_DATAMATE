import sqlite3
from pathlib import Path

def quote(name): return '"'+name.replace('"','""')+'"'

def parse_sqlite(path):
    out=[]
    with sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro&immutable=1",uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        for name,ddl in db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
            info=db.execute("PRAGMA table_info("+quote(name)+")").fetchall()
            columns=[x[1] for x in info]
            rows=[{"index":0,"values":columns,"flags":[]}]
            for index,row in enumerate(db.execute("SELECT * FROM "+quote(name)),1):
                rows.append({"index":index,"values":list(row),"flags":[]})
            out.append({"source":str(path),"name":name,"rows":rows,"format":"sqlite","declared_header":True,
                        "declared_types":[x[2] for x in info],"ddl":ddl,
                        "foreign_keys":db.execute("PRAGMA foreign_key_list("+quote(name)+")").fetchall()})
    return out
