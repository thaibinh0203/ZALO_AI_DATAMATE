"""Conservative, evidence-driven field normalization; no question/answer access."""
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
import hashlib
import math
import re
import unicodedata


def key(value):
    value = unicodedata.normalize("NFD", str(value)).replace("đ", "d").replace("Đ", "D")
    return re.sub(r"[^a-z0-9]", "", "".join(c for c in value if not unicodedata.combining(c)).lower())


def identifier(value):
    text = unicodedata.normalize("NFD", str(value)).replace("đ", "d").replace("Đ", "D")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_") or "column"
    if text[0].isdigit(): text = "c_" + text
    if text.startswith("__"): text = "c" + text
    if len(text) > 52: text = text[:41] + "_" + hashlib.sha256(str(value).encode()).hexdigest()[:10]
    return text


ALIASES = {
    "order_id": "Mã hóa đơn|Mã đơn hàng|Mã phiếu|Mã bill|MaHoaDon|So hoa don|Mã bill bán|MaDonHang",
    "product_id": "Mã hàng|Mã hàng hóa|Mã sản phẩm|MaHangHoa|ma_sp|Mã HH Liên quan|Mã SKU",
    "product_name": "Tên hàng|Tên hàng hóa|Tên sản phẩm|Tên SP|Hàng|Nội dung|Product|Hàng hóa|Tên món|Tên mặt hàng|Mô tả sản phẩm|TenHang|TenSanPham",
    "customer_id": "Mã KH|Mã khách hàng|MaKhachHang",
    "customer_name": "Tên khách hàng|Khách hàng|Khách|TenKH|Bên mua|Đối tác",
    "store_id": "IDCuaHang|Mã cửa hàng",
    "purchase_id": "Mã nhập hàng|Mã phiếu nhập|Số phiếu|MaPhieuNhap|Mã đơn nhập",
    "receipt_id": "Mã phiếu thu|MaPhieuThu",
    "lot_id": "Lô|Số lô|Mã lô|MaLo",
    "line_number": "STT|SoThuTu|Số thứ tự",
    "quantity": "Số lượng|Số lượng sản phẩm|so_luong|Số lượng hàng|SL|SL nhập|Số lượng nhập|Số lượng trả",
    "base_quantity": "Số lượng quy đổi|SLQuyDoi|Quy ra ĐV lẻ|SL thực giao|Số lượng (ĐVT cơ bản)|SLNhapQuyDoi",
    "unit": "Đơn vị tính|ĐVT|DonVi|Đơn vị (tính)|Unit|ĐV nhập|Đơn vị quy đổi|ĐV lớn|ĐVT nhập|DonViTinh",
    "base_unit": "Đơn vị cơ bản|DonViCoSo|Đơn vị gốc|ĐV nhỏ|ĐV gốc",
    "conversion_factor": "Hệ số|Hệ số QĐ|Tỉ lệ quy đổi|HeSoQuyDoi|Tỷ lệ quy đổi",
    "unit_price": "Giá bán|Giá sản phẩm|Giá|Đơn giá|Giá lẻ|GiaDonVi|GiaBanLe|Giá mới|Giá cũ|Giá nhập|DonGiaNhap",
    "line_amount": "Thành tiền|Thanh tien|Trị giá|TriGia|Tiền|Tiền hàng|Tien hang|Tổng tiền hàng|TongTienHang",
    "unit_cogs": "Giá vốn|Giá vốn lúc bán|GiaVonLucBan|Giá vốn hiện tại|GiaVonHang",
    "money": "Tổng tiền|TongTien|Tổng sau giảm giá|TongThanhToan|Giảm giá|ChietKhau|Khách đã trả|Da tra|KhachDaTra|Còn nợ|Chưa trả|ConPhaiThu|Giá trị|Số tiền thu|SoTienThu|So tien|Số tiền|Cần trả NCC|TongGiaTriNhap|PhiVanChuyen|ThueGTGT|TienHoanTra",
    "date": "Ngày|Ngày bán|Ngày tháng|Ngày giao dịch|Ngày lập|NgayLap|Ngày đặt hàng|Ngày nhập|NgayNhapKho|Ngày chở|Ngày áp dụng|NgayApDung|Hạn sử dụng|HanSuDung|Han tra|NgayKhaiTruong|ngay_sinh",
    "timestamp": "Giờ|Thời gian|Thời gian tạo|Thời điểm|ThoiGianLap|NgayGioThu|NgayGioTra|NgayGioPhatSinh|TaoLuc",
    "status": "Trạng thái|Trạng thái đơn hàng|TinhTrang|Kết quả|Status|Ghi chú TT",
    "boolean": "Thuốc kê đơn|Kê đơn|BanTheoDon",
    "balance": "Tồn thực|Tồn kho|Ton kho|Tồn cuối|Tồn cuối kỳ|TonCuoi|SLTraQuyDoi|Số lượng trả (ĐVT cơ bản)",
}
BY_KEY = {key(alias): role for role, aliases in ALIASES.items() for alias in aliases.split("|")}
NUMERIC = {"quantity", "base_quantity", "conversion_factor", "unit_price", "line_amount", "unit_cogs", "money", "balance", "line_number"}
MONEY = {"unit_price", "line_amount", "unit_cogs", "money"}
SQL_TYPES = {**{r: "NUMERIC" for r in NUMERIC}, "date": "DATE", "timestamp": "TIMESTAMP", "boolean": "BOOLEAN"}


def column_spec(name, description="", declared=""):
    role = BY_KEY.get(key(name), "text")
    desc = key(description)
    evidence = "header_alias" if role != "text" else "preserved_text"
    # README descriptions disambiguate generic labels like TongTien/ThoiGian.
    if "thanhtiensaugiam" in desc:
        role, evidence = "line_amount", "readme"
    elif "ngayban" in desc or "ngaynhap" in desc:
        role, evidence = "date", "readme"
    elif "giavonmotdonviban" in desc:
        role, evidence = "unit_cogs", "readme"
    elif "soluongdadoi" in desc or "soluongquydoi" in desc:
        role, evidence = "base_quantity", "readme"
    elif "soluong" in desc and "donvicoban" in desc and role == "quantity":
        role, evidence = "base_quantity", "readme"
    elif "dongiacua" in desc or desc == "dongia":
        role, evidence = "unit_price", "readme"
    elif "donvicoban" in desc and role in {"unit", "base_unit", "text"}:
        role, evidence = "base_unit", "readme"
    sql_type = SQL_TYPES.get(role, "TEXT")
    if role == "text" and declared.upper() in {"INTEGER", "REAL", "NUMERIC", "BIGINT", "FLOAT", "DOUBLE"}:
        sql_type, evidence = "NUMERIC", "sqlite_declared_type"
    return {"original_name": name, "name": identifier(name), "role": role,
            "sql_type": sql_type, "description": description, "evidence": evidence}


def parse_number(value, *, currency=False):
    if isinstance(value, bool): raise ValueError("boolean_is_not_number")
    if isinstance(value, (int, float, Decimal)):
        result = Decimal(str(value))
        if not result.is_finite(): raise ValueError("non_finite_number")
        return result
    text = unicodedata.normalize("NFC", str(value)).strip().lower()
    negative = text.startswith("(") and text.endswith(")")
    if negative: text = text[1:-1].strip()
    if currency:
        text = re.sub(r"\s*(vnd|đồng|dong|đ)\s*$", "", text).strip()
        compact = re.fullmatch(r"([0-9]+)(?:tr|triệu)([0-9]{1,3})?", text.replace(" ", ""))
        if compact:
            a, b = compact.groups()
            result = Decimal(a) * 1000000 + (Decimal(b) * 10 ** (6-len(b)) if b else 0)
            return -result if negative else result
        suffix = re.fullmatch(r"([+-]?[0-9.,]+)\s*(k|nghìn|ngàn|tr|triệu)", text)
        if suffix:
            return parse_number(suffix[1], currency=True) * (1000 if suffix[2] in {"k","nghìn","ngàn"} else 1000000) * (-1 if negative else 1)
    text = text.replace("\u00a0", "").replace(" ", "")
    if "," in text and "." in text:
        decimal_sep = "," if text.rfind(",") > text.rfind(".") else "."
        group_sep = "." if decimal_sep == "," else ","
        integer, fraction = text.rsplit(decimal_sep, 1)
        if not fraction.isdigit() or not re.fullmatch(r"[+-]?[0-9]{1,3}(?:" + re.escape(group_sep) + r"[0-9]{3})+", integer):
            raise ValueError("invalid_grouped_number")
        text = integer.replace(group_sep, "") + "." + fraction
    elif "," in text or "." in text:
        sep = "," if "," in text else "."
        parts = text.split(sep)
        grouped = len(parts) >= 2 and re.fullmatch(r"[+-]?[0-9]{1,3}", parts[0]) and all(len(p)==3 and p.isdigit() for p in parts[1:])
        if grouped and (currency or len(parts)>2): text = "".join(parts)
        elif len(parts)==2 and len(parts[1])==3 and parts[0].lstrip("+-") != "0":
            raise ValueError("ambiguous_numeric_separator")
        elif len(parts)==2: text = text.replace(",", ".")
        else: raise ValueError("invalid_numeric_separator")
    if not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)", text):
        raise ValueError("invalid_number")
    result = Decimal(text)
    return -result if negative else result


def parse_date(value, *, year=None, timestamp=False):
    if isinstance(value, datetime): return value if timestamp else value.date()
    if isinstance(value, date): return datetime.combine(value,time()) if timestamp else value
    text = str(value).strip()
    vietnamese=re.fullmatch(r'([0-9]{1,2})\s*(?:thg|tháng)\s*([0-9]{1,2})\s*(?:năm\s*)?([0-9]{4})',text,re.I)
    if vietnamese:text='/'.join(vietnamese.groups())
    try:
        result = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if result.tzinfo: raise ValueError("timezone_requires_explicit_policy")
    except ValueError:
        if re.search(r"[+-][0-9]{2}:[0-9]{2}$|Z$",text): raise ValueError("timezone_requires_explicit_policy")
        formats = ["%d/%m/%Y %H:%M:%S","%d/%m/%Y %H:%M","%d/%m/%Y","%d-%m-%Y","%d.%m.%Y","%Y/%m/%d"]
        result = None
        for fmt in formats:
            try: result = datetime.strptime(text,fmt); break
            except ValueError: pass
        if result is None and year and re.fullmatch(r"[0-9]{1,2}/[0-9]{1,2}",text):
            result = datetime.strptime(text+"/"+str(year),"%d/%m/%Y")
        if result is None: raise ValueError("invalid_or_missing_year_date")
    return result if timestamp else result.date()


STATUSES = {"hoanthanh":"completed","dahoanthanh":"completed","thanhcong":"completed",
            "xong":"completed","daxong":"completed","doitra":"returned",
            "dalay":"completed","hangtrave":"returned","tralai":"returned","dahoanhang":"returned",
            "danggiaodich":"delivering",
            "ok":"completed","khachhuy":"cancelled","chogiao":"delivering",
            "dangvanchuyen":"delivering","dangxuly":"delivering",
            "danhaphang":"received","danhap":"received","danghoatdong":"active","ngungkinhdoanh":"inactive",
            "dagiao":"completed","completed":"completed","huy":"cancelled","dahuy":"cancelled",
            "dahuydon":"cancelled","huydon":"cancelled","cancelled":"cancelled",
            "trahang":"returned","datrahang":"returned","hoantra":"returned","returned":"returned",
            "danggiao":"delivering","danggiaohang":"delivering","delivering":"delivering"}


def normalize_value(value, spec, *, year=None, cell=None):
    if value is None or isinstance(value,str) and not value.strip(): return None, "missing", None
    cell = cell or {}
    try:
        if cell.get("formula"):
            if not cell.get("has_cached_value"): raise ValueError("formula_cache_missing")
            value = cell["cached_value"]
        typ, role = spec["sql_type"], spec["role"]
        if typ == "NUMERIC":
            if role == "quantity" and isinstance(value,str):
                match = re.fullmatch(r"\s*([+-]?[0-9.,]+)\s+([^0-9\s].*?)\s*",value)
                if match: return parse_number(match[1]), "ok_unit_extracted", match[2].strip()
            return parse_number(value,currency=role in MONEY), "ok", None
        if typ in {"DATE","TIMESTAMP"}:
            if isinstance(value,(int,float)) and cell.get("excel_epoch"):
                from openpyxl.utils.datetime import from_excel
                value = from_excel(value, datetime.fromisoformat(cell["excel_epoch"]))
            return parse_date(value,year=year,timestamp=typ=="TIMESTAMP"),"ok",None
        if typ == "BOOLEAN":
            choices={"true":True,"1":True,"co":True,"yes":True,"false":False,"0":False,"khong":False,"no":False}
            if key(value) not in choices: raise ValueError("invalid_boolean")
            return choices[key(value)],"ok",None
        if role=="status":
            if key(value) not in STATUSES: raise ValueError("unknown_status")
            return STATUSES[key(value)],"ok",None
        if isinstance(value,float) and not math.isfinite(value): raise ValueError("non_finite_value")
        fmt=cell.get("number_format","")
        if role.endswith("_id") and isinstance(value,(int,float)) and re.fullmatch(r"0+",fmt):
            if Decimal(str(value))!=int(value): raise ValueError("fractional_identifier")
            return str(int(value)).zfill(len(fmt)),"ok_display_padding",None
        if role.endswith("_id") and isinstance(value,(int,float)):
            return str(value),"identifier_numeric_source_padding_unknown",None
        return unicodedata.normalize("NFC",str(value)).strip(),"ok",None
    except (ValueError,InvalidOperation,TypeError,OverflowError) as error:
        return None,"error:"+str(error),None
