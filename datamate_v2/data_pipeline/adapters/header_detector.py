"""Header scoring with an explicit unresolved outcome, never a random fallback."""
from datamate_v2.common import fold
import re

def shape(value):
    if not isinstance(value,str): return type(value).__name__
    text=value.strip()
    if re.fullmatch(r'[+-]?[\d\s.,%]+',text): return 'numeric_text'
    if any(c.isdigit() for c in text): return 'mixed_text'
    return 'words' if len(text.split())>1 else 'word'

def nonempty(v): return v is not None and str(v).strip()!=""

def score_header(rows,index,aliases,max_width=None):
    values=rows[index]["values"];filled=[v for v in values if nonempty(v)]
    if not values or len(filled)!=len(values) or not all(isinstance(v,str) for v in values): return 0.0
    names=[fold(v) for v in filled]
    unique=len(set(names))/len(names)
    below=[r["values"] for r in rows[index+1:index+5] if any(nonempty(v) for v in r["values"])]
    if not below: return 0.0
    width=max_width if max_width is not None else max((len(r['values']) for r in rows),default=1)
    differences=[]
    for col,value in enumerate(values):
        observed=[r[col] for r in below if col<len(r) and nonempty(r[col])]
        # Blank cells below have no vote: optional/sparse export columns must
        # never lower a genuine header's score.
        if observed: differences.append(sum(shape(v)!=shape(value) for v in observed)/len(observed))
    contrast=sum(differences)/len(differences) if differences else 0
    return round(0.25+0.30*len(filled)/width+0.15*unique+0.30*contrast,4)

def detect_header(rows,aliases,declared=False):
    if declared: return {"status":"verified","position":0,"score":1,"evidence":"source-declared columns"}
    width=max((len(r['values']) for r in rows),default=1)
    for limit in (10,50):
        scored=sorted(((score_header(rows,i,aliases,width),i) for i in range(min(limit,len(rows)))),key=lambda pair:(-pair[0],pair[1]))
        if not scored: break
        top,pos=scored[0]
        # Later tables must not displace an earlier strong header merely because
        # their next four rows happen to contain fewer subsequent headers.
        strong=[(s,i) for s,i in scored if s>=0.90 and top-s<=0.08]
        if strong: top,pos=min(strong,key=lambda pair:pair[1])
        alternatives=[s for s,i in scored[1:] if i<pos or i>pos+1]
        margin=top-max(alternatives,default=0)
        if top>=0.74 and (margin>=0.04 or top>=0.90):
            return {"status":"verified","position":pos,"score":top,"evidence":"nonempty all-string header, maximum column count, unique labels and value-shape contrast; blank data cells ignored"}
    return {"status":"unresolved","position":None,"candidates":[{"position":i,"score":s} for s,i in scored[:5]] if rows else []}
