"""Split blank-separated vertical/horizontal regions and repeated export headers."""
from datamate_v2.common import fold
from .header_detector import nonempty,detect_header,score_header

def extract_tables(source,aliases):
    rows=source["rows"]
    if source.get("declared_header"):
        return [{**source,"header":rows[0],"data_rows":rows[1:],"header_detection":detect_header(rows,aliases,True)}]
    # Empty columns separate independent tables. Do not guess joins between blocks.
    width=max((len(r["values"]) for r in rows),default=0)
    used=[any(i<len(r["values"]) and nonempty(r["values"][i]) for r in rows) for i in range(width)]
    spans=[];start=None
    for i,active in enumerate(used+[False]):
        if active and start is None: start=i
        elif not active and start is not None: spans.append((start,i));start=None
    out=[]
    for left,right in spans:
        sliced=[{**r,"values":r["values"][left:right],
                 "flags":[{**f,"column":f["column"]-left} for f in r.get("flags",[]) if left<=f.get("column",-1)<right]} for r in rows]
        chunks=[];chunk=[]
        for row in sliced:
            if any(nonempty(v) for v in row["values"]): chunk.append(row)
            elif chunk: chunks.append(chunk);chunk=[]
        if chunk: chunks.append(chunk)
        for chunk in chunks:
            detection=detect_header(chunk,aliases)
            if detection["position"] is None:
                out.append({**source,"name":f"{source['name']}_{len(out)+1}","header":None,"data_rows":chunk,
                            "header_detection":detection,"column_offset":left});continue
            pos=detection["position"]
            starts=[pos];last=[fold(v) for v in chunk[pos]['values']]
            for i in range(pos+1,len(chunk)):
                names=[fold(v) for v in chunk[i]['values']]
                if names!=last and score_header(chunk,i,aliases,right-left)>=0.90:
                    starts.append(i);last=names
            for start,end in zip(starts,starts[1:]+[len(chunk)]):
                header=chunk[start];names=[fold(v) for v in header['values']]
                data=[r for r in chunk[start+1:end] if [fold(v) for v in r['values']]!=names]
                out.append({**source,"name":f"{source['name']}_{len(out)+1}","header":header,"data_rows":data,
                            "preamble":chunk[:start] if start==pos else [],
                            "header_detection":{**detection,'position':start},"column_offset":left})
    return out
