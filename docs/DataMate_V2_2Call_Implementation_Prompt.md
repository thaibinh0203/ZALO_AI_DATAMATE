# DATAMATE V2 — FULL IMPLEMENTATION PROMPT

## 0. Vai trò và nhiệm vụ

Bạn là Senior AI Engineer và Software Engineer, chịu trách nhiệm phân tích và triển khai DataMate V2 — hệ thống hỏi đáp tiếng Việt trên dữ liệu có cấu trúc (CSV, Excel, SQLite), sử dụng SLM chạy local và PostgreSQL.

Repository: https://github.com/thaibinh0203/ZALO_AI_DATAMATE

Hãy đọc repository và các tài liệu cuộc thi trước khi sửa code. Xây dựng lại Data Pipeline và QA Pipeline theo kiến trúc mới, đơn giản hơn V1, dễ debug, dễ kiểm thử và ưu tiên độ chính xác. Không xóa hoặc thay đổi V1 khi chưa được yêu cầu; triển khai V2 độc lập.

**Mục tiêu:** Hiểu đúng câu hỏi và dữ liệu, tính toán chính xác, phân biệt Answer / Unanswerable / Abstain theo quy định chính thức.

---

# PHẦN I — NGUYÊN TẮC KIẾN TRÚC

## 1. Nguyên tắc bắt buộc

1. Rule-based trước SLM cho các nhiệm vụ có thể giải quyết bằng quy tắc.
2. SLM dùng cho suy luận ngữ nghĩa và dữ liệu không chuẩn; mọi đề xuất sửa dữ liệu phải qua deterministic validation.
3. Không suy đoán nghiệp vụ khi thiếu bằng chứng; giữ raw data, provenance và normalization logs.
4. README và Rulebook là nguồn thông tin nghiệp vụ quan trọng.
5. Data Pipeline và QA Pipeline tách biệt; cache kết quả tiền xử lý, không chạy lại theo từng câu hỏi.
6. Không dùng Context Retrieval trong QA baseline; Call 2 nhận full business metadata, README và Rulebook.
7. Không dùng MCP cho PostgreSQL baseline; dùng Python QueryTool trực tiếp.
8. Model local, offline Docker, không external inference API nếu bị cấm.
9. Đọc tài liệu quy định để xác nhận output contract, runtime, tài nguyên và giới hạn model calls; không tự giả định.

## 2. Kiến trúc tổng thể

```text
INPUT DATA (CSV / Excel / SQLite)
    ↓
Parser / Adapter
    ↓
Header Detection (Rule-based)
    ↓
Column Normalization (Alias + Rule-based + SLM fallback)
    ↓
Value Normalization (Rule-based + SLM fallback)
    ↓
Summary Row Detection (Rule-based)
    ↓
Schema Description Generation (SLM + Validation)
    ↓
PostgreSQL Clean Tables + Full Business Metadata + Cache

USER QUESTION
    ↓
SLM CALL 1 — Query Rewriting + Entity Extraction + Time Resolution (as_of_date)
    ↓
Python Temporal Validation
    ↓
Load Full Business Context (NO RETRIEVAL)
    ↓
Python Data Exploration (bounded, khi cần)
    ↓
SLM CALL 2 — Question Planning + Answerability + SQL Generation
    ├── Unanswerable / Abstain (khi có căn cứ)
    ↓ Answerable: calculation_plan + SQL trong cùng output
    ↓
SQL Validation → PostgreSQL Execution → Result Validation
    ↓
Answer / Unanswerable / Abstain
```

---

# PHẦN II — DATA PIPELINE V2

## 3. Mục tiêu và outputs

**Output 1 — PostgreSQL Clean Database:** bảng, tên cột SQL, data types, clean/raw values, source mapping, summary-row flags, trạng thái chất lượng dữ liệu.

**Output 2 — Full Business Metadata:** tên bảng/cột, schema descriptions do SLM sinh, mapping đã/chưa xác minh, sample/last rows, quan hệ đã xác minh, thống kê, data quality notes, quy tắc sử dụng dữ liệu.

Lưu thêm Mapping Registry, normalization logs, provenance và fingerprint phục vụ cache/debug.

## 4. Parser

- Hỗ trợ CSV, XLS, XLSX, SQLite.
- Thu thập filename, sheet/table, raw rows, data types, row/column counts, source row index, sample rows, last rows.
- Không giả định Excel có header ở dòng đầu.
- Không chỉnh sửa dữ liệu; giữ bản raw để đối chiếu.

## 5. Adapter và Header Detection

**Baseline chỉ dùng rule-based, không cần prompt SLM riêng.**

- Tìm header trong 10 dòng đầu; nếu không rõ, mở rộng 30–50 dòng.
- Scoring theo nonempty ratio, tên cột, duplicate headers, kiểu dữ liệu các dòng bên dưới, alias matching, dấu hiệu tiêu đề/ghi chú.
- Nếu không đủ bằng chứng: đánh dấu unresolved, log; không tự chọn ngẫu nhiên.
- Hỗ trợ nhiều bảng trong cùng sheet, unnamed/duplicate columns, phần ghi chú phía trên/bên dưới.
- Giữ vị trí nguồn; không tự JOIN hoặc gộp bảng chưa có quan hệ xác minh.

## 6. Column Normalization

**Alias Dictionary → Rule-based Matching → SLM Fallback → Validation.**

### 6.1. Alias dictionary

Xây từ tập train, có khả năng mở rộng. Ví dụ:

```python
COLUMN_ALIASES = {
    "customer_name": ["tên khách hàng", "khách hàng", "tên khách", "người mua"],
    "quantity": ["số lượng", "sl", "qty"],
    "unit_price": ["đơn giá", "giá bán một sản phẩm"],
    "line_total": ["thành tiền", "tiền hàng"]
}
```

Không đồng nhất `unit_price`, `line_total`, `revenue`, `amount_paid` chỉ vì đều là số tiền.

### 6.2. Rule-based

Unicode/whitespace/special character normalization, exact alias, fuzzy matching có ngưỡng và bằng chứng. Chỉ xác nhận mapping khi đủ chắc chắn.

### 6.3. SLM fallback

Batch các cột không khớp hoặc mơ hồ của cùng bảng; không gọi từng cột nếu có thể gom nhóm.

Input: tên bảng/cột gốc, data types, 3–5 samples, các cột liên quan, alias dictionary, README, Rulebook.

Output: tên chuẩn đề xuất hoặc tên SQL mới, candidate meanings, bằng chứng, trạng thái. Không tự đoán khi thiếu bằng chứng.

### 6.4. Mapping Registry và Schema

Mọi mapping lưu tên gốc, tên SQL thực tế, trạng thái và evidence:

- `verified`: đã xác minh.
- `uncertain`: còn nhiều cách hiểu.
- `unresolved`: chưa xác định ý nghĩa.

```json
{
  "original_column": "Tiền TH",
  "sql_column": "tien_th",
  "semantic_status": "uncertain",
  "candidate_meanings": ["thành tiền", "tiền thực thu"],
  "evidence": ["Giá trị mẫu có dạng tiền", "README không định nghĩa Tiền TH"]
}
```

**Bắt buộc đưa thông tin mapping uncertain/unresolved vào Schema Description dùng cho QA**, bao gồm tên gốc, tên SQL, candidate meanings và evidence. Planning SLM có thể suy luận thêm nhưng không được tự sửa PostgreSQL/registry. Mapping verified có thể đưa tên gốc nếu giúp hiểu dữ liệu.

### 6.5. Validation

SQL identifiers hợp lệ, không trùng, phù hợp sample values và các cột liên quan, không mâu thuẫn README/Rulebook. Confidence tự khai báo của SLM không phải bằng chứng duy nhất.

## 7. Value Normalization

**Rule-based → SLM Fallback → Validation.**

- Xử lý tiền, số lượng, ngày, %, đơn vị, trạng thái, missing values và định dạng số tiếng Việt.
- Ví dụ (chỉ khi ngữ cảnh xác nhận là tiền): `1tr500k` → `1500000`, `1.250.000` → `1250000`, `1,5 triệu` → `1500000`.
- Không áp dụng quy tắc tiền cho mọi cột; phân biệt dấu phân cách nghìn/thập phân theo ngữ cảnh.
- Với giá trị không parse được, batch theo kiểu bất thường rồi gọi SLM, truyền raw value, column meaning, neighboring values, related columns, README/Rulebook.
- SLM trả đề xuất clean value, unit/type, evidence, status; Python kiểm tra range, type, unit, constraints và cross-column consistency (ví dụ quantity × unit_price = line_total khi đúng nghiệp vụ).
- Giữ raw/clean riêng (`amount_raw`, `amount_clean`); unresolved thì clean NULL/flag và log, không tự điền số.
- Lưu số lượng giá trị chưa chuẩn hóa trong metadata để QA không âm thầm tính thiếu.

## 8. Summary Row và Anomaly Detection

**Rule-based trong baseline; không mặc định dùng SLM.**

- Quét **toàn bảng**, không chỉ 50 dòng cuối.
- Nhận diện total/subtotal/grand total, footer, notes, bất thường cấu trúc, các giá trị bằng tổng nhóm dòng.
- Không coi mọi dòng chứa chữ “tổng” là summary.
- Đánh dấu `_is_summary_row`; giữ raw rows. Trường hợp không rõ phải có uncertain status và ghi vào metadata.
- Khi tính tổng thông thường, SQL loại summary rows nếu đúng nghiệp vụ; không loại khi người dùng hỏi chính dòng tổng.
- Chỉ thêm SLM anomaly prompt nếu evaluation cho thấy cần.

## 9. Schema Description Generation — SLM BẮT BUỘC

### 9.1. Input

Tên bảng, tên cột gốc và SQL, types, mapping statuses/candidates/evidence, 3–5 sample rows và last rows, column stats, null counts, date coverage, normalization notes, summary flags, verified relationships, README, Rulebook và data quality warnings. **Không đưa hàng triệu dòng vào prompt.**

### 9.2. Nhiệm vụ

Sinh mô tả ngữ nghĩa rõ ràng:

1. Ý nghĩa bảng và mỗi dòng đại diện cho cái gì.
2. Ý nghĩa từng cột, đơn vị, khi nào nên/không nên dùng tính toán.
3. Quan hệ bảng đã xác minh và rủi ro JOIN double counting.
4. Mapping uncertain/unresolved và evidence; không che giấu bất định.
5. Unnormalized values, summary rows, data coverage và giới hạn.
6. Business rules có nguồn từ README/Rulebook.

Không tự tạo foreign key, công thức hoặc định nghĩa không có bằng chứng. Phân biệt confirmed fact và hypothesis.

### 9.3. Ví dụ output

```text
BUSINESS ID: business_001
TABLE: orders
DESCRIPTION: Bảng đơn hàng; mỗi dòng thông thường tương ứng một đơn hàng.

COLUMNS:
- order_id (TEXT), Original: Mã đơn, Status: VERIFIED — mã đơn hàng.
- order_date (DATE), Original: Ngày đặt, Status: VERIFIED — ngày phát sinh.
- amount_clean (NUMERIC), Original: Thành tiền, Status: VERIFIED — tiền VND đã chuẩn hóa.
- tien_th (NUMERIC), Original: Tiền TH, Status: UNCERTAIN
  Possible meanings: Thành tiền / Tiền thực thu.
  Không mặc định dùng làm doanh thu khi chưa xác minh.
- _is_summary_row (BOOLEAN) — đánh dấu dòng tổng kết.

SAMPLE ROWS: ...
LAST ROWS: ...
RELATIONSHIPS: Chỉ liệt kê quan hệ đã xác minh.
DATA QUALITY: 3 giá trị tiền unresolved; 12 summary rows; 1 cột uncertain.
BUSINESS RULES: Chỉ ghi công thức có nguồn từ Rulebook/README.
```

### 9.4. Validation và cache

Yêu cầu SLM sinh JSON có cấu trúc, Python render thành text. Kiểm tra mọi bảng/cột đều được mô tả, tên SQL tồn tại, không thiếu uncertain mapping, không tự tạo quan hệ/công thức, không bỏ qua warnings.

Có thể gọi theo bảng hoặc batch. Cache theo Business ID, data fingerprint, README/Rulebook version, model/prompt version; regenerate khi nguồn liên quan thay đổi.

---

# PHẦN III — QA PIPELINE V2

## 10. Kiến trúc

Hai SLM calls mặc định: (1) Query Rewriting + Entity Extraction + Time Resolution; (2) Question Planning + Answerability + SQL Generation trong **cùng một inference**, theo thứ tự logic lập calculation_plan rồi sinh SQL. Chỉ gọi thêm inference khi repair/escalation có căn cứ. **Không BM25, embedding retrieval, reranker hoặc hybrid retrieval trong baseline.** Call 2 nhận full business metadata.

## 10A. Input Contract và Time Resolution tích hợp Call 1 (BẮT BUỘC)

Input chuẩn mỗi câu hỏi:

```json
{"id":"q_06de33fa21cc","business_id":"biz_118","question":"sp bán dc nhiều nhất tuần rồi là gì","as_of_date":"2026-03-04"}
```

- `id`: giữ nguyên để liên kết câu hỏi với output.
- `business_id`: giới hạn truy vấn đúng doanh nghiệp, không truy cập chéo dữ liệu.
- `question`: câu hỏi gốc, giữ nguyên để audit.
- `as_of_date`: ngày tham chiếu ISO `YYYY-MM-DD` cho mọi biểu thức thời gian tương đối; **không dùng system clock**.
- Validate đủ trường, kiểu dữ liệu, định dạng ngày trước khi gọi model. Không tự điền ngày hệ thống nếu thiếu.

**Luồng mới:** Input Validation → **Call 1: Query Rewriting + Entity Extraction + Time Resolution** → Python Temporal Validation/Correction → Load Full Business Context → Python Data Exploration có giới hạn (khi cần) → Call 2 Planning + SQL Generation (đối chiếu Rulebook, xác nhận lịch) → Python Revalidation nếu lịch thay đổi → SQL Guard → Execution → Result Validation → Serializer.

**Call 1 chịu trách nhiệm hiểu và đề xuất cả khoảng thời gian cụ thể**, không chỉ trích xuất biểu thức. Truyền `as_of_date` và các quy tắc lịch mặc định đã cấu hình vào Call 1. Model xuất `temporal_expressions`, `interpretation`, `start_date`, `end_date_exclusive`, `calendar_rule`, `status`, `ambiguities`. Nếu không thể xác định chắc chắn, model phải trả `ambiguous` với các cách hiểu thay vì bịa mốc ngày.

**Python không phải một model call khác:** chỉ validate/hiệu chỉnh deterministic bằng `datetime`/`dateutil` và cấu hình lịch. Kiểm tra ngày hợp lệ, thứ trong tuần, số ngày tháng, năm nhuận, ranh giới năm, start < end, và đúng quy ước lịch. Nếu model trả ngày sai, tính lại bằng code theo `interpretation` đã được xác nhận; nếu interpretation mơ hồ, không tự chọn. Giữ log cả proposed và validated time ranges.

**Quy tắc mặc định (chỉ khi không bị quy định cuộc thi/Rulebook thay thế):** tuần từ thứ Hai đến Chủ nhật; tháng/quý/năm theo lịch dương. `tuần trước` = tuần lịch hoàn chỉnh trước tuần chứa `as_of_date`; `tháng trước` = tháng lịch hoàn chỉnh trước tháng chứa `as_of_date`; `7 ngày qua` = 7 ngày lịch bao gồm `as_of_date`; `đến nay` không bao gồm dữ liệu tương lai. `tuần rồi` có thể mơ hồ giữa tuần lịch trước và 7 ngày qua; chỉ đánh dấu resolved khi có quy ước xác nhận.

Ví dụ `as_of_date = 2026-03-04`:

| Biểu thức | start_date | end_date_exclusive |
|---|---|---|
| hôm qua | 2026-03-03 | 2026-03-04 |
| tuần trước (thứ Hai–Chủ nhật) | 2026-02-23 | 2026-03-02 |
| 7 ngày qua (bao gồm 04/03) | 2026-02-26 | 2026-03-05 |
| tháng trước | 2026-02-01 | 2026-03-01 |
| quý trước | 2025-10-01 | 2026-01-01 |
| năm trước | 2025-01-01 | 2026-01-01 |

Ví dụ output Call 1 cho câu hỏi mẫu, **giả sử đã có quy ước `tuần rồi` là tuần lịch trước**:

```json
{
  "id": "q_06de33fa21cc",
  "business_id": "biz_118",
  "original_question": "sp bán dc nhiều nhất tuần rồi là gì",
  "rewritten_question": "Sản phẩm nào bán được nhiều nhất trong tuần trước?",
  "entities": {"metrics": ["mức độ bán chạy"], "dimensions": ["sản phẩm"], "ranking": "top_1"},
  "resolved_time": {
    "original_expression": "tuần rồi",
    "reference_date": "2026-03-04",
    "interpretation": "previous_calendar_week",
    "calendar_rule": "MONDAY_TO_SUNDAY",
    "start_date": "2026-02-23",
    "end_date_exclusive": "2026-03-02",
    "status": "proposed"
  },
  "ambiguities": ["Chỉ tiêu 'bán nhiều nhất' cần được định nghĩa bằng Rulebook"]
}
```

Sau Python validation, chuyển `status` thành `validated` khi quy tắc lịch được xác nhận. Call 2 phải xác định metric thật sự (số lượng, doanh thu hay số đơn) từ Rulebook; **không tự mặc định `SUM(quantity)`**. Nếu Call 2 phát hiện lịch kinh doanh, timezone, lịch âm–dương hoặc quy ước tuần khác, phải cập nhật `interpretation/calendar_rule` và yêu cầu Python revalidate trước khi thực thi SQL. Nếu Call 2 đề xuất quy ước lịch khác, Python phải xác minh và cập nhật mốc thời gian; SQL sinh trong cùng call chỉ được thực thi khi khớp mốc đã xác minh, nếu không phải sửa bằng code khi an toàn hoặc repair có kiểm soát. SQL chỉ dùng mốc đã validated với điều kiện nửa mở `date_col >= start_date AND date_col < end_date_exclusive`; không tự tính lại bằng `CURRENT_DATE`/`NOW()`.

Hỗ trợ ngày tuyệt đối, khoảng ngày, `hôm nay/hôm qua`, `tuần này/trước`, `tháng này/trước`, `quý này/trước`, `năm này/trước`, N ngày/tuần/tháng gần nhất, so sánh cùng kỳ, đầu/cuối tháng, ranh giới năm, năm nhuận, ngày tính đến `as_of_date`, timezone của TIMESTAMP, và lịch âm–dương khi có quy định/nguồn chuyển đổi đáng tin cậy. Với lịch đặc biệt không thể xác minh, giữ ambiguity/abstain theo quy định, không đoán.

Không tạo `time_resolver.py` làm bước suy luận ngôn ngữ riêng. Có thể dùng `qa_pipeline/temporal_validator.py` chứa các hàm Python thuần để kiểm tra và hiệu chỉnh khoảng ngày Call 1 đề xuất. Không tạo thêm prompt hoặc model call cho thời gian.

## 11. Model Call 1 — Query Rewriting + Entity Extraction + Time Resolution

Input bắt buộc: `id`, `business_id`, `question`, `as_of_date`; có thể thêm abbreviation dictionary và quy ước lịch mặc định. Model thực hiện **trong cùng một call**:

1. Chuẩn hóa viết tắt, lỗi chính tả, cách nói thông tục mà không làm mất ý nghĩa.
2. Trích xuất entities: metrics, products, categories, customers, customer groups, employees, branches, locations, suppliers, orders, invoices, statuses, payment methods, channels, units, operations, filters, grouping, comparison targets, ranking, sorting, limits và `other_entities`.
3. Phân rã các yêu cầu phức tạp thành sub-questions.
4. Nhận diện biểu thức thời gian, sử dụng **chính `as_of_date`** để đề xuất `start_date`/`end_date_exclusive` theo quy ước lịch phù hợp.
5. Ghi rõ ambiguities về metric, entity và temporal interpretation; trả structured JSON.

Ví dụ `dthu sp áo polo cn hn t10/25 so với t9 tăng bn %?` → `Doanh thu sản phẩm áo polo tại chi nhánh Hà Nội tháng 10/2025 tăng bao nhiêu phần trăm so với tháng 9/2025?`; output có **hai time windows** tháng 10 và tháng 9/2025, không chỉ một khoảng.

Call 1 **không** tự chọn SQL tables/columns, công thức nghiệp vụ theo Rulebook chưa đọc, không kết luận answerability hoặc khẳng định dữ liệu tồn tại. Không tự biến `bán nhiều nhất` thành `SUM(quantity)` nếu chưa có định nghĩa nghiệp vụ. Giữ original question và `as_of_date` xuyên suốt pipeline.

## 12. Full Business Context Loading — KHÔNG RETRIEVAL

Python load **toàn bộ metadata của doanh nghiệp**: original/rewritten question, entities, all tables/columns/types, schema descriptions, original mappings, uncertain/unresolved mappings, sample/last rows, relationships, data quality, README và Rulebook đầy đủ, thời gian/phạm vi dữ liệu.

Không loại bảng/cột chỉ vì không trùng keyword. Không đưa toàn bộ PostgreSQL records vào prompt. Nếu context vượt giới hạn model, báo rõ; không âm thầm cắt mất metadata/rules quan trọng.

## 13. Model Call 2 — Question Planning + Answerability + SQL Generation (GỘP TRONG MỘT INFERENCE)

**Chỉ một lần inference:** model nhận original/rewritten question, entities, các time windows đã được Python validated, **full business context**, README/Rulebook, mapping evidence, kết quả Python exploration (nếu có), và quy tắc SQL. Model phải **lập `calculation_plan` bằng ngôn ngữ tự nhiên trước, rồi sinh SQL theo chính plan đó trong cùng một structured JSON output**. Không tạo Call 3 riêng ở baseline.

### 13.1. Planning trước khi sinh SQL

1. Xác định ý định, metric, định nghĩa nghiệp vụ và công thức có bằng chứng trong README/Rulebook; không tự gán `bán nhiều nhất` = số lượng nếu chưa được xác nhận.
2. Chọn bảng/cột, time windows, filters, grouping, ranking, JOIN, aggregation, đơn vị và grain; tránh JOIN fanout/double counting.
3. Đọc mappings `verified`/`uncertain`/`unresolved`, evidence, sample/last rows và data quality. Không coi sample là toàn bộ dữ liệu; không tự sửa database.
4. Tạo `calculation_plan` **dạng danh sách các bước bằng chữ**, giải thích dữ liệu nào cần lấy, điều kiện lọc, công thức, phép tính trung gian, phép so sánh, xử lý NULL/unresolved và kiểm tra chất lượng.
5. Đánh giá schema answerability có căn cứ; chỉ sau khi xác định có thể lập truy vấn mới sinh SQL. Các trạng thái cuối cùng phải tuân thủ Evaluation Rules chính thức.

**Answerable:** đủ schema/rules để lập SQL; kết quả vẫn phải được xác minh sau execution. **Unanswerable:** có bằng chứng thiếu dữ liệu/định nghĩa bắt buộc. **Abstain:** không đủ bằng chứng để lập plan đáng tin hoặc có mơ hồ chưa giải quyết. Không kết luận Unanswerable chỉ vì sample không có bản ghi.

### 13.2. Sinh SQL trong cùng Call 2

- Chuyển từng bước `calculation_plan` thành **PostgreSQL SELECT**; chỉ dùng bảng/cột có thật, clean values đúng nghiệp vụ, JOIN được chứng minh, thời gian nửa mở `[start_date, end_date_exclusive)`.
- Không tự bổ sung định nghĩa, bộ lọc hay công thức trái với plan. Không dùng `NOW()`/`CURRENT_DATE` thay cho `as_of_date`.
- Nếu `status` không phải `answerable`, trường `sql` phải là `null` và nêu lý do; không tạo SQL giả.
- SQL và plan là **hai phần output của cùng một lần sinh**, không phải hai lượt model. Python kiểm tra JSON schema, SQLGlot AST, whitelist, đối chiếu SQL/plan/time/filter và quyền truy cập. SQLGlot không đủ để chứng minh đúng ngữ nghĩa: thêm kiểm tra deterministic cho các ràng buộc quan trọng.
- Nếu cần thay đổi quy ước lịch theo Rulebook, output phải nêu rõ `temporal_override`; Python xác minh lại. SQL chỉ chạy nếu khoảng thời gian trong SQL khớp với kết quả đã xác minh; nếu không khớp thì chặn và repair khi cần.

### 13.3. Ví dụ output Call 2

Ví dụ minh họa **chỉ khi Rulebook xác nhận** doanh thu là tổng `amount_clean` của đơn `Hoàn tất`, và các cột này có thật:

```json
{
  "status": "answerable",
  "required_tables": ["orders"],
  "required_columns": ["amount_clean", "order_date", "status", "_is_summary_row"],
  "calculation_plan": [
    "Lấy các dòng đơn hàng từ bảng orders.",
    "Lọc order_date từ 2025-10-01 đến trước 2025-11-01.",
    "Chỉ lấy status = 'Hoàn tất' theo định nghĩa doanh thu đã xác nhận trong Rulebook.",
    "Loại các dòng _is_summary_row = TRUE để tránh tính trùng.",
    "Cộng amount_clean của các dòng hợp lệ.",
    "Kiểm tra các giá trị tiền unresolved thuộc đúng phạm vi lọc trước khi xác nhận kết quả đầy đủ."
  ],
  "filters": {
    "start_date": "2025-10-01",
    "end_date_exclusive": "2025-11-01",
    "status": "Hoàn tất",
    "exclude_summary_rows": true
  },
  "join_requirements": [],
  "data_quality_checks": ["unresolved_amount_in_scope"],
  "sql": "SELECT SUM(amount_clean) AS revenue FROM orders WHERE order_date >= DATE '2025-10-01' AND order_date < DATE '2025-11-01' AND status = 'Hoàn tất' AND _is_summary_row = FALSE",
  "reason": "Các bảng, cột và quy tắc nghiệp vụ đã được xác minh."
}
```

`data_quality_checks` phải được Python thực thi hoặc đối chiếu bằng truy vấn kiểm tra trong budget; chỉ viết chúng trong plan không đủ chứng minh dữ liệu đầy đủ. SQL ví dụ chỉ là truy vấn tính giá trị, không thay thế quality checks.

### 13.4. Data Exploration trước Call 2 và repair có giới hạn

- Python QueryTool có thể thực hiện `lookup_entity`, `inspect_column_values`, `check_data_coverage`, `inspect_data_quality` dựa trên entities từ Call 1 và metadata, **trước Call 2**, khi cần xác minh giá trị thực tế. Không tự suy luận alias chỉ từ string similarity; kết quả tra cứu phải có provenance.
- Không cho SLM truy vấn tự do nhiều vòng ở baseline; nếu cần tool results mới phát hiện trong Call 2, chỉ thêm lượt inference có điều kiện và ghi nhận call count thực tế.
- Mặc định **2 model calls/câu hỏi**; tối đa một model repair khi lỗi có thể sửa và budget cho phép. Không repair chỉ vì kết quả `0`, `NULL` hoặc empty.
- Không gọi thêm model chỉ để tạo final answer nếu Python serializer làm được.

## 14. Ranh giới trách nhiệm giữa Plan, SQL và Python

**SLM Call 2** chịu trách nhiệm lập calculation plan và sinh SQL trong một output. **Python** chịu trách nhiệm xác minh thời gian, schema/SQL safety, thực thi QueryTool, data quality checks, result validation và serializer. Giữ `calculation_plan` trong logs để phân tích lỗi ngữ nghĩa, dù không có Call 3 riêng.

## 15. SQL Validation

Dùng SQLGlot AST để chỉ cho phép read-only queries, chặn DDL/DML, kiểm tra syntax, allowed tables/columns, business isolation, JOIN/double counting, clean columns, filters, summary rows. Kết hợp PostgreSQL read-only role, read-only transaction, safe search_path, allowlist, statement timeout, execution budget. Không dựa vào prompt để bảo đảm an toàn. Cho phép tối đa một SLM repair khi phù hợp cấu hình.

## 16. PostgreSQL QueryTool

Dùng Python `psycopg` hoặc SQLAlchemy, **không MCP baseline**. Interface:

```python
list_tables()
describe_table()
get_sample_rows()
lookup_entity()
check_data_coverage()
execute_readonly_sql()
```

Python orchestration gọi trực tiếp; không nhất thiết SLM phải tự tool-call. Có thể mở rộng thành MCP sau này.

## 17. SQL Execution và Result Validation

Phân biệt success, no rows, NULL, zero, timeout, SQL/type errors, incomplete data. Không tự động coi NULL/empty/0 là Unanswerable; ví dụ `COUNT` không có đơn có thể trả 0, nhưng giá trị đơn không tồn tại không phải 0.

Python kiểm tra type, units, NULL, coverage, unresolved clean values, duplicate counting, calculation/plan consistency. Có thể chạy một truy vấn kiểm tra bổ sung trong budget. Không bịa đáp án khi thiếu bằng chứng.

## 18. Final Answer

Answer / Unanswerable / Abstain theo contract chính thức. Python serializer xuất đúng JSON/đơn vị/kiểu; không gọi SLM thứ tư chỉ để format khi Python làm được. Không thêm SQL hoặc giải thích ngoài format nếu không được yêu cầu.

---

# PHẦN IV — MODEL VÀ INFERENCE

## 19. Model Selection

Muốn thử **Gemma 4 12B**: trước khi code phải xác minh model tồn tại, model ID, license, offline support, VRAM/RAM, Docker compatibility, structured JSON output. Nếu không tồn tại/không khả thi, báo rõ và đề xuất lựa chọn thay thế; **không tự đổi model**. Một local model có thể dùng cho cả Data và QA, load một lần và tái sử dụng.

### 19.1. Local Model Storage và Model Loading (bắt buộc)

- Bổ sung thư mục `datamate_v2/models/` để chứa **model weights, tokenizer và config** đã chuẩn bị sẵn, tách biệt với source code inference.
- `models/local_model/` là tên thư mục minh họa, **không khẳng định** Gemma 4 12B tồn tại hoặc là model ID hợp lệ. Xác minh model chính xác trước khi đặt tên thực tế.
- Tạo `models/README.md` hướng dẫn chuẩn bị model ở máy có Internet (nếu được phép), kiểm tra tính toàn vẹn, đặt đúng vị trí và chạy offline. Không tải weights trong quá trình inference hoặc khi chạy bài thi.
- `inference/model_loader.py` đọc `configs/model.yaml`, kiểm tra đường dẫn và các tệp bắt buộc, load model/tokenizer từ ổ đĩa với chế độ offline và báo lỗi rõ ràng khi thiếu tệp.
- `inference/model_client.py` cung cấp một interface dùng chung cho cả 3 loại nhiệm vụ SLM trong Data Pipeline và 2 QA calls mặc định. **Không reload model sau mỗi prompt**. Dùng cùng một model instance/engine trong cùng worker, quản lý concurrency an toàn.
- `inference/structured_output.py` kiểm tra JSON/schema output, retry có giới hạn và ghi log; không tự tạo nội dung thay cho model.
- Không hardcode model ID, path, dtype, device hay số GPU trong code. Lấy từ cấu hình và kiểm tra tương thích backend.
- Model weights lớn không commit lên Git; dùng `.gitignore`, mount read-only từ host hoặc đóng gói theo đúng yêu cầu submission. Không mặc định host volume được phép trong môi trường chấm.
- Docker phải hoạt động với `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1` khi dùng Transformers/Hugging Face, hoặc cài đặt offline tương đương của backend đã chọn. Không được tự động gọi mạng khi thiếu model.

Ví dụ cấu hình (điều chỉnh sau khi xác minh backend/model):

```yaml
model:
  path: /app/models/local_model
  backend: transformers
  device: cuda
  dtype: bfloat16
  local_files_only: true
  trust_remote_code: false
```

Ví dụ Docker Compose mount model (chỉ dùng nếu runtime/submission cho phép host mount):

```yaml
services:
  datamate_v2:
    volumes:
      - ./models:/app/models:ro
    environment:
      HF_HUB_OFFLINE: "1"
      TRANSFORMERS_OFFLINE: "1"
```

Ví dụ `.gitignore`:

```gitignore
models/*
!models/README.md
!models/.gitkeep
```

**Acceptance criteria:** chạy smoke test load model một lần, gọi liên tiếp prompt Column Normalization, Schema Description, Query Rewriting, Planning + SQL Generation mà không reload weights; kiểm tra chế độ không Internet; kiểm tra thông báo lỗi khi thiếu weights/tokenizer.


## 20. Số lần gọi model

**Data Pipeline — ba loại nhiệm vụ SLM:**

- Column Normalization fallback: chỉ khi alias/rules không giải quyết được.
- Value Normalization fallback: chỉ khi rules không giải quyết được.
- Schema Description Generation: bắt buộc, có thể theo bảng/batch.

Đây **không phải đúng ba inference calls cố định**; số calls tùy batch và số bảng. Header Detection và Summary Row Detection dùng rule-based baseline.

**QA Pipeline — hai calls mặc định mỗi câu hỏi:** (1) Rewriting + Entity Extraction + Time Resolution; (2) Planning + Answerability + SQL Generation. Nếu Call 2 trả Unanswerable/Abstain, không sinh SQL. Có thể thêm tối đa một repair/escalation inference theo budget; ghi nhận mọi lượt gọi thực tế.

Cache clean data, mappings, schema descriptions và model outputs. Invalidate khi dữ liệu, README, Rulebook, normalization rules, prompt/model versions thay đổi. Log call type, model/prompt version, token counts, latency, validation, retries.

---

# PHẦN V — PROJECT STRUCTURE

```text
datamate_v2/
├── models/
│   ├── README.md
│   ├── .gitkeep
│   └── local_model/                 # weights/tokenizer/config; tên thực tế theo model xác minh
│       ├── config.json
│       ├── tokenizer_config.json
│       ├── tokenizer.json          # nếu model dùng tệp này
│       └── *.safetensors           # có thể gồm nhiều shards/index
├── configs/
│   ├── model.yaml
│   ├── database.yaml
│   └── pipeline.yaml
├── data_pipeline/
│   ├── parsers/
│   │   ├── csv_parser.py
│   │   ├── excel_parser.py
│   │   └── sqlite_parser.py
│   ├── adapters/
│   │   ├── header_detector.py
│   │   └── table_extractor.py
│   ├── normalization/
│   │   ├── column_normalizer.py
│   │   ├── value_normalizer.py
│   │   └── validators.py
│   ├── anomaly_detection/
│   │   └── summary_detector.py
│   ├── schema_generation/
│   │   ├── schema_builder.py
│   │   └── schema_validator.py
│   └── pipeline.py
├── qa_pipeline/
│   ├── query_rewriting/
│   ├── temporal_validator.py
│   ├── planning_sql/                 # Call 2: plan + SQL trong một inference
│   ├── sql_validation/
│   ├── execution/
│   ├── result_validation/
│   └── pipeline.py
├── database/
│   ├── connection.py
│   ├── loader.py
│   └── tools.py
├── inference/
│   ├── model_loader.py
│   ├── model_client.py
│   └── structured_output.py
├── prompts/
│   ├── data_pipeline/
│   │   ├── column_normalization.txt
│   │   ├── value_normalization.txt
│   │   └── schema_description.txt
│   └── qa_pipeline/
│       ├── query_rewriting.txt
│       └── planning_sql_generation.txt
├── metadata/
│   ├── aliases/
│   ├── mappings/
│   └── schemas/
├── evaluation/
│   ├── evaluator.py
│   ├── error_analysis.py
│   ├── latency_profiler.py
│   ├── metrics.py
│   └── compare_variants.py
├── tests/
├── scripts/
├── Dockerfile
├── docker-compose.yml
├── .gitignore
└── README.md
```

Không tạo abstraction phức tạp không cần thiết; mọi module có trách nhiệm rõ ràng. `models/` là nơi lưu artifact model, không thay thế `inference/model_loader.py`. Không commit weights lên repository trừ khi quy định nộp bài yêu cầu và cho phép.

---

# PHẦN VI — EVALUATION

## 21. Official Evaluation

Đọc `DATAMATE_Description_Rules_Evaluation (1).pdf` để xác minh input/output contracts, Answerability definitions, scoring formula, offline/model restrictions, runtime và submission requirements. Không tự giả định luật hay công thức.

## 22. Evaluation trên 50 câu train và hiệu năng

Chạy 50 câu train thực tế (hoặc tập chính thức có sẵn), lưu kết quả theo đúng contract; **official score là metric chính**, các chỉ số phụ chỉ để phân tích và tối ưu. Không tạo ground truth hoặc điểm giả. Đảm bảo cùng dataset, cấu hình model, hardware, thời gian warm-up và budget khi so sánh các biến thể.

### 22.1. Accuracy và reliability

- **Official score / accuracy theo kết quả chấm:** gọi evaluator chính thức nếu có, lưu per-question score, tổng điểm và số mẫu; nếu không có evaluator thì ghi rõ chỉ số proxy và cách đối chiếu ground truth.
- **Answerability metrics:** confusion matrix Answer / Unanswerable / Abstain, precision/recall theo lớp khi có nhãn.
- **SQL execution success rate:** số câu có SQL chạy thành công / số câu thực sự thử execute SQL; báo riêng tỷ lệ JSON/SQL validation pass, timeout và execution error. Không tính UA/Abstain không cần SQL vào mẫu số.
- **Semantic error rate:** số câu có kết quả sai **do hiểu sai câu hỏi, metric, Rulebook, schema, entity, time window, grain, JOIN, filter hoặc calculation** / số câu có thể phân loại nguyên nhân lỗi. Ghi rõ tiêu chí gán nhãn và `unknown_root_cause`; không gộp lỗi cú pháp SQL vào lỗi ngữ nghĩa. Có thể báo thêm semantic errors / all evaluated questions để so sánh.
- **Repair rate:** tỷ lệ câu cần repair; số repair attempts/câu (mean, max), repair success rate, tỷ lệ lỗi không phục hồi; phân biệt JSON repair, SQL repair và semantic escalation.
- **Data-quality failures:** unresolved values trong phạm vi tính toán, missing coverage, summary/double counting, normalization mistakes; phân tích lỗi theo bước.

### 22.2. Latency profiling (bắt buộc)

- Đo **end-to-end QA latency** bằng monotonic clock, từ lúc nhận input hợp lệ đến khi tạo final output; báo **p50, p95, mean, max** theo milliseconds và seconds, cùng số mẫu.
- Đo latency theo stage: input validation, Call 1 prefill/decode (nếu backend hỗ trợ), temporal validation, context loading, Python exploration, Call 2 prefill/decode, SQL guard, PostgreSQL execution, result validation, repair, serialization.
- Đo **cold-start/model loading**, **Data Pipeline ingest time** và **warm QA latency** riêng; không gộp thời gian preprocessing vào từng câu hỏi. Nếu môi trường chấm tính startup/ingest vào runtime, báo thêm end-to-end cold-start scenario.
- Đo p50/p95 riêng cho câu không repair, câu có repair, câu answerable, UA/Abstain và các nhóm độ phức tạp khi cỡ mẫu đủ; với chỉ 50 câu, ghi chú độ bất định của p95.
- Log `timestamp`, `business_id`, `question_id`, `stage`, `duration_ms`, `model_call_index`, `is_repair`, `status`, `error_type`; không ghi secrets.

### 22.3. Token consumption và inference efficiency

- Tổng **input/prompt tokens**, **output/generated tokens** và **total tokens** theo từng call, theo câu hỏi và toàn tập; dùng tokenizer của model/backend thực tế.
- Báo **mean/p50/p95 tokens per question**, token consumption theo stage, **model calls per question**, tokens/s khi backend cung cấp; tách cache hits/prefix reuse nếu có.
- Đo throughput và peak VRAM/RAM nếu khả thi, không tự suy ra token counts từ độ dài ký tự.

### 22.4. Ablation và so sánh kiến trúc

- So sánh **2-call baseline** (Call 1 Rewriting+Time; Call 2 Planning+SQL) với **3-call reference** (Call 1; Planning; SQL) nếu có thời gian triển khai, trên cùng tập câu hỏi và cùng giới hạn tài nguyên. Không bắt buộc duy trì 3-call trong production.
- Báo bảng so sánh: official accuracy/score, SQL execution success, semantic error rate, repair count/rate, latency p50/p95, mean input/output/total tokens, số calls trung bình, chi phí GPU (nếu đo được).
- Đối chiếu trade-off tốc độ–độ chính xác; chọn 2-call mặc định nếu đạt chất lượng phù hợp. Không khẳng định 2-call tốt hơn trước khi đo.
- So sánh V1/V2 nếu khả thi, phân loại lỗi Parser, Header Detection, Column/Value Normalization, Summary Detection, Schema Description, Rewriting, Temporal Resolution, Planning, Answerability, SQL Generation, SQL Validation, Execution, Result Validation, Output Formatting.

### 22.5. Artifacts cần xuất

Lưu per-question JSONL/CSV logs (question ID, model/prompt/data fingerprint, rewritten question, entities, validated windows, calculation_plan, SQL, result, final answer, ground truth nếu có, score, root cause, repair details, stage latencies, tokens), aggregate report và bảng comparison. Tạo báo cáo Markdown có công thức, mẫu số và số mẫu cho từng metric; không tạo số liệu benchmark giả.

---

# PHẦN VII — TRIỂN KHAI

## 23. Implementation Phases

**Phase 1 — Repository Analysis:** đọc repo, README, Rulebook, Evaluation Rules; phân tích V1, module tái sử dụng, model và môi trường.

**Phase 2 — Data Pipeline V2:** Parser, Header Detection, Table Extraction, Column/Value Normalization, Summary Detection, SLM Schema Description, PostgreSQL Loader, Mapping Registry, Cache.

**Phase 3 — QA Pipeline V2:** Query Rewriting + Time Resolution (Call 1), Python exploration, Full Context Loader, Planning + Answerability + SQL Generation (Call 2), SQL Guard, QueryTool, Execution, Result Validation, Final Formatting.

**Phase 4 — Tests:** unit/integration tests, đặc biệt ngoài alias, uncertain mapping, tiền không chuẩn, summary rows, JOIN double counting, viết tắt, mơ hồ, UA/Abstain, NULL/zero, SQL safety.

**Phase 5 — Evaluation:** 50 câu train, official score, SQL success, semantic errors, repair, latency p50/p95, token consumption, so sánh 2-call/3-call và V1/V2 nếu khả thi.

**Phase 6 — Docker:** đóng gói dependencies/model theo quy định, offline startup, PostgreSQL connectivity, I/O contract, latency, resources, end-to-end.

## 24. Những điều không được tự ý thay đổi

1. Không xóa V1 hoặc quay lại catalog phức tạp.
2. Không MCP PostgreSQL baseline; không Context Retrieval QA baseline.
3. Không bỏ SLM Schema Description.
4. Không dùng SLM Header Detection mặc định; không dùng SLM Summary Detection mặc định.
5. Không bỏ uncertain/unresolved mappings khỏi schema.
6. Không để SLM sửa dữ liệu khi thiếu bằng chứng.
7. Không chạy Data Pipeline cho mỗi câu hỏi.
8. Không gọi SLM chỉ để format khi Python làm được.
9. Không tự đổi model, tự đặt luật cuộc thi hoặc tạo evaluation giả.
10. Không tự tách Planning và SQL Generation thành hai inference mặc định; phải giữ 2-call QA baseline.

## 25. Deliverables

1. Source code DataMate V2 và project structure.
2. Năm prompt files chính (3 Data Pipeline, 2 QA); optional repair prompt nếu triển khai fallback.
3. Hướng dẫn local và Docker offline.
4. Unit/integration test results.
5. Evaluation 50 câu: official score, SQL success, semantic errors, repair, latency p50/p95, tokens, error analysis và comparisons nếu khả thi.
6. Các vấn đề chưa giải quyết và đề xuất cải tiến.

## 26. Cách bắt đầu

Trước tiên phân tích repository và tài liệu quy định; tóm tắt V1, module có thể tái sử dụng, module cần thay thế, điểm chưa rõ và kế hoạch triển khai từng phase. Sau đó **triển khai code thực tế**, không chỉ mô tả kiến trúc. Nếu phát hiện yêu cầu mâu thuẫn quy định hoặc không khả thi, giải thích rõ trước khi thay đổi.

**Mục tiêu cuối cùng:** DataMate V2 là hệ thống Text-to-SQL tiếng Việt chạy offline, dùng SLM có kiểm soát, hiểu ngữ nghĩa dữ liệu, lập kế hoạch tính toán chính xác và trả lời đáng tin cậy.
