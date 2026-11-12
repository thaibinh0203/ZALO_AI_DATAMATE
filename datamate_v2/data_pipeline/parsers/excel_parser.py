from pathlib import Path
from datetime import datetime

def parse_excel(path):
    if Path(path).suffix.lower()==".xls": return parse_xls(path)
    from openpyxl import load_workbook
    formulas=load_workbook(path,read_only=False,data_only=False,keep_links=False)
    cached=load_workbook(path,read_only=False,data_only=True,keep_links=False)
    out=[]
    try:
        for sheet in formulas:
            rows=[]; cache=cached[sheet.title]
            for cells in sheet.iter_rows():
                values=[]; flags=[]; raw_cells={}
                for cell in cells:
                    value=cell.value
                    if cell.data_type=="f":
                        raw_cells[cell.column-1]={"formula":value,"cached":cache[cell.coordinate].value}
                        value=cache[cell.coordinate].value
                        if value is None: flags.append({"column":cell.column-1,"reason":"formula_without_cache"})
                    if cell.data_type=="e": flags.append({"column":cell.column-1,"reason":"excel_error"});value=None
                    if isinstance(value,datetime): value=value.isoformat()
                    values.append(value)
                rows.append({"index":cells[0].row,"values":values,"flags":flags,"cells":raw_cells})
            out.append({"source":str(path),"name":sheet.title,"rows":rows,"format":"xlsx",
                        "merged_ranges":[str(x) for x in sheet.merged_cells.ranges],
                        "hidden_rows":[i for i,d in sheet.row_dimensions.items() if d.hidden]})
    finally: formulas.close();cached.close()
    return out

def parse_xls(path):
    import xlrd
    book=xlrd.open_workbook(path,formatting_info=True)
    out=[]
    for sheet in book.sheets():
        rows=[]
        for r in range(sheet.nrows):
            values=[];flags=[]
            for c in range(sheet.ncols):
                cell=sheet.cell(r,c);value=cell.value
                if cell.ctype==xlrd.XL_CELL_DATE: value=xlrd.xldate_as_datetime(value,book.datemode).isoformat()
                elif cell.ctype==xlrd.XL_CELL_ERROR: flags.append({"column":c,"reason":"excel_error"});value=None
                elif cell.ctype==xlrd.XL_CELL_EMPTY: value=None
                values.append(value)
            rows.append({"index":r+1,"values":values,"flags":flags})
        out.append({"source":str(path),"name":sheet.name,"rows":rows,"format":"xls",
                    "warning":"XLS reader uses stored formula results; formulas not recoverable"})
    return out
