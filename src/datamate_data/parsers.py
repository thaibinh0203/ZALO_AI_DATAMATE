"""Decode supported source formats without reading questions or labels."""
import base64
import csv
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
import json
import math
from pathlib import Path
import sqlite3
import xml.etree.ElementTree as ET
import zipfile


def json_value(value):
    if isinstance(value,(datetime,date,time)): return value.isoformat()
    if isinstance(value,Decimal): return str(value)
    if isinstance(value,bytes): return {"base64":base64.b64encode(value).decode()}
    if isinstance(value,float) and not math.isfinite(value): return {"non_finite":str(value)}
    return value


def dumps(value):
    return json.dumps(value,ensure_ascii=False,default=json_value,allow_nan=False)


@dataclass
class Row:
    number: int
    values: list
    cells: dict
    flags: list


@dataclass
class Table:
    path: Path
    name: str
    format: str
    rows: object
    metadata: dict
    declared: list | None = None


def csv_tables(path, encoding="utf-8-sig"):
    # Strict decoding; alternate encoding must be explicitly configured.
    with path.open(encoding=encoding,newline="") as stream:
        sample=stream.read(16384);stream.seek(0)
        evidence='sample'
        try: dialect=csv.Sniffer().sniff(sample,delimiters=",;\t|")
        except csv.Error:
            # Inconsistent record widths must be quarantinable, not silently lost.
            try:dialect=csv.Sniffer().sniff(sample.splitlines()[0],delimiters=",;\t|");evidence='header_only'
            except (csv.Error,IndexError):raise ValueError(f"Cannot determine CSV delimiter: {path.name}")
        reader=csv.reader(stream,dialect,strict=True)
        def rows():
            for values in reader:
                yield Row(reader.line_num,values,{},[])
        yield Table(path,path.stem,"csv",rows(),{"encoding":encoding,"delimiter":dialect.delimiter,'delimiter_evidence':evidence})


def xlsx_metadata(path):
    ns={"m":"http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(path) as archive:
        rels=ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets={r.get("Id"):r.get("Target") for r in rels}
        workbook=ET.fromstring(archive.read("xl/workbook.xml"))
        out={}
        import posixpath
        for sheet in workbook.find("m:sheets",ns):
            rid=sheet.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
            target=targets[rid]
            member=target.lstrip("/") if target.startswith("/") else posixpath.normpath("xl/"+target)
            formulas={};merged=[];hidden=[]
            with archive.open(member) as stream:
                for _,node in ET.iterparse(stream,events=("end",)):
                    if node.tag.endswith("}c"):
                        formula=node.find("m:f",ns)
                        if formula is not None:
                            cache=node.find("m:v",ns)
                            value=cache.text if cache is not None else None
                            if value is not None and node.get("t") not in {"str","e"}:
                                try: value=float(value) if "." in value else int(value)
                                except ValueError: pass
                            formulas[node.get("r")]={"formula":formula.text or "shared_formula",
                                "has_cached_value":value is not None,"cached_value":value,
                                "cached_error":node.get("t")=="e"}
                    elif node.tag.endswith("}mergeCell"): merged.append(node.get("ref"))
                    elif node.tag.endswith("}row"):
                        if node.get("hidden")=="1": hidden.append(int(node.get("r")))
                        node.clear()
            out[sheet.get("name")]={"merged_ranges":merged,"hidden_rows":hidden,
                                    "sheet_state":sheet.get("state","visible"),"formulas":formulas}
        return out


def excel_tables(path):
    from openpyxl import load_workbook
    metadata=xlsx_metadata(path)
    workbook=load_workbook(path,read_only=True,data_only=False,keep_links=False)
    try:
        for sheet in workbook:
            sheet.reset_dimensions()  # Do not trust an exporter-supplied A1:A1 dimension.
            meta=metadata[sheet.title]
            def rows(sheet=sheet,meta=meta):
                for number,cells in enumerate(sheet.iter_rows(),1):
                    values=[];details={};flags=[]
                    for index,cell in enumerate(cells):
                        value=cell.value;values.append(value)
                        if value is None: continue
                        info={"cell_type":cell.data_type,"number_format":cell.number_format,
                              "excel_epoch":workbook.epoch.isoformat()}
                        if cell.coordinate in meta["formulas"]:
                            info.update(meta["formulas"][cell.coordinate])
                            if info.get("cached_error"): info["has_cached_value"]=False
                        if cell.data_type=="e": flags.append({"column":index,"error":"excel_error_cell"})
                        details[str(index)]=info
                    yield Row(number,values,details,flags)
            yield Table(path,sheet.title,"xlsx",rows(),{**meta,"formulas":len(meta["formulas"])})
    finally: workbook.close()


def sqlite_tables(path):
    with sqlite3.connect(path.resolve().as_uri()+"?mode=ro",uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        tables=db.execute("SELECT name,sql,type FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
        for name,ddl,kind in tables:
            quoted='"'+name.replace('"','""')+'"'
            info=db.execute(f"PRAGMA table_info({quoted})").fetchall()
            columns=[r[1] for r in info];declared=[r[2] for r in info]
            cursor=db.execute(f"SELECT * FROM {quoted}")
            def rows(cursor=cursor,columns=columns):
                yield Row(0,columns,{},[])
                for i,row in enumerate(cursor,1): yield Row(i,list(row),{},[])
            yield Table(path,name,"sqlite",rows(),{"ddl":ddl,"object_type":kind,
                "primary_key_columns":[r[1] for r in info if r[5]],
                "foreign_keys":db.execute(f"PRAGMA foreign_key_list({quoted})").fetchall(),
                "source_order":"scan_ordinal_not_stable_primary_key"},declared)


def parse_file(path, *, encoding="utf-8-sig"):
    if path.suffix.lower()==".csv": yield from csv_tables(path,encoding)
    elif path.suffix.lower()==".xlsx": yield from excel_tables(path)
    elif path.suffix.lower()==".sqlite": yield from sqlite_tables(path)
    else: raise ValueError(f"Unsupported data file: {path.name}")
