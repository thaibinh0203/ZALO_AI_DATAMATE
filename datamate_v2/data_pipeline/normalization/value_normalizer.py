"""Conservative contextual parsing; IDs and arbitrary text remain text."""
from datetime import date,datetime
from decimal import Decimal,InvalidOperation
import re
import unicodedata
from datamate_v2.common import fold

MONEY={"unit_price","line_total","amount_paid","revenue","discount","unit_cost","amount"}
VI_DATE=re.compile(r'^\s*(\d{1,2})\s+(?:thg|tháng|thang)\s+(\d{1,2})\s+(\d{4})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?\s*$',re.I)
DATE_FORMATS=("%d/%m/%Y","%d-%m-%Y","%d.%m.%Y","%Y/%m/%d","%d/%m/%Y %H:%M:%S","%d/%m/%Y %H:%M","%d/%m/%y")
QUANTITY_UNIT=re.compile(r'^\s*([+-]?\d+(?:[.,]\d+)*)\s*([^\d\s.,+\-].*?)\s*$')

def normalize_unit(value):
    return ' '.join(unicodedata.normalize('NFKC',str(value)).split())

def quantity_parts(value):
    if not isinstance(value,str): return None
    match=QUANTITY_UNIT.fullmatch(value)
    return match.groups() if match else None

def numeric(value,*,money=False,percent=False,locale=None):
    if isinstance(value,bool): raise ValueError("Boolean is not numeric")
    if isinstance(value,(int,float,Decimal)):
        result=Decimal(str(value))
        if not result.is_finite(): raise ValueError("Non-finite number")
        return result
    text=str(value).strip().lower().replace("\u00a0","").replace(" ","")
    negative=text.startswith('(') and text.endswith(')')
    if negative: text=text[1:-1]
    if money:
        text=re.sub(r"(?:vnd|vnđ|đ|₫)$","",text)
        match=re.fullmatch(r"([+-]?\d+(?:[.,]\d+)?)(trieu|triệu|tr|k|nghin|nghìn)(?:(\d+(?:[.,]\d+)?)(k|nghin|nghìn))?",text)
        if match:
            first=Decimal(match[1].replace(',','.'))*(1000000 if match[2] in ('trieu','triệu','tr') else 1000)
            second=Decimal(match[3].replace(',','.'))*1000 if match[3] else 0
            return -(first+second) if negative else first+second
    if percent and text.endswith('%'): text=text[:-1]
    if re.fullmatch(r"[+-]?\d{1,3}(?:\.\d{3}){2,}",text): text=text.replace('.','')
    elif re.fullmatch(r"[+-]?\d{1,3}(?:,\d{3}){2,}",text): text=text.replace(',','')
    elif ',' in text and '.' in text:
        if locale=='vi' and re.fullmatch(r"[+-]?\d{1,3}(?:\.\d{3})+,\d+",text): text=text.replace('.','').replace(',','.')
        elif locale=='en' and re.fullmatch(r"[+-]?\d{1,3}(?:,\d{3})+\.\d+",text): text=text.replace(',','')
        else: raise ValueError("Ambiguous numeric separators")
    elif ',' in text:
        if locale=='en' and re.fullmatch(r"[+-]?\d{1,3}(?:,\d{3})+",text): text=text.replace(',','')
        elif locale=='vi' or not re.fullmatch(r"[+-]?\d+,\d{3}",text): text=text.replace(',','.')
        else: raise ValueError("Ambiguous comma separator")
    elif re.fullmatch(r"[+-]?\d{1,3}\.\d{3}",text):
        if locale=='vi': text=text.replace('.','')
        elif locale!='en': raise ValueError("Ambiguous dot separator")
    if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?",text): raise ValueError("Unsupported number syntax")
    result=Decimal(text)
    return -result if negative else result

def parse_value(value,column,locale=None,year=None,timezone=None,allowed_units=None):
    if value is None or (isinstance(value,str) and not value.strip()): return None,"missing"
    kind=column["type"]
    try:
        if kind=="NUMERIC":
            parts=quantity_parts(value) if column.get('meaning')=='quantity' else None
            if parts:
                number,unit=parts
                if normalize_unit(unit) not in {normalize_unit(u) for u in (allowed_units or [])}:
                    raise ValueError('Quantity unit is not verified against its source unit column')
                value=number
            return numeric(value,money=column.get("meaning") in MONEY,percent=column.get("meaning")=="percent",locale=locale),"verified"
        if kind in ("DATE","TIMESTAMPTZ"):
            if isinstance(value,datetime): dt=value
            elif isinstance(value,date):
                if kind=='DATE': return value.isoformat(),"verified"
                dt=datetime.combine(value,datetime.min.time())
            else:
                text=str(value).strip()
                try: dt=datetime.fromisoformat(text)
                except ValueError:
                    dt=None;match=VI_DATE.fullmatch(text)
                    if match:
                        day,month,yr,hour,minute,second=match.groups()
                        dt=datetime(int(yr),int(month),int(day),int(hour or 0),int(minute or 0),int(second or 0))
                    else:
                        for fmt in DATE_FORMATS:
                            try: dt=datetime.strptime(text,fmt);break
                            except ValueError: pass
                    if dt is None and year and re.fullmatch(r"\d{1,2}/\d{1,2}",text): dt=datetime.strptime(text+f"/{year}","%d/%m/%Y")
                    if dt is None: raise ValueError("Unresolved date")
            if kind=="DATE": return dt.date().isoformat(),"verified"
            if dt.tzinfo is None:
                if not timezone: raise ValueError("Timestamp requires configured timezone")
                from zoneinfo import ZoneInfo
                dt=dt.replace(tzinfo=ZoneInfo(timezone))
            return dt.isoformat(),"verified"
        if isinstance(value,bytes): return value.hex(),"verified"
        return str(value),"verified"
    except (ValueError,InvalidOperation,OverflowError): return None,"unresolved"
