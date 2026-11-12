import csv
from pathlib import Path
from datamate_v2.common import PipelineError

def parse_csv(path,encoding="utf-8-sig"):
    with Path(path).open(encoding=encoding,newline="") as stream:
        sample=stream.read(65536);stream.seek(0)
        try: dialect=csv.Sniffer().sniff(sample,delimiters=",;\t|")
        except csv.Error:
            try: dialect=csv.Sniffer().sniff(sample.splitlines()[0],delimiters=",;\t|")
            except (csv.Error,IndexError):
                # A true single-column CSV is valid; strict csv parsing still applies.
                dialect=csv.excel
        rows=[]
        for values in csv.reader(stream,dialect,strict=True):
            rows.append({"index":len(rows)+1,"values":values,"flags":[]})
    return [{"source":str(path),"name":Path(path).stem,"rows":rows,"format":"csv","encoding":encoding}]
