# DataMate — Project brief cho Codex trong VS Code

## Cách sử dụng tài liệu

Đặt file này tại `docs/DATAMATE_PROJECT_BRIEF.md` trong repository DataMate ở WSL. Đây là brief mới tổng hợp kế hoạch, phân công và lịch 11 ngày; thay thế lịch minh họa dài hơn trong bản plan trước. Không phải yêu cầu thực hiện toàn bộ dự án trong một lượt.

Codex cần đọc tài liệu người dùng đính kèm, các hướng dẫn repository áp dụng và brief này trước khi sửa code. Quy định chính thức do ban tổ chức cung cấp là nguồn ưu tiên về cuộc thi; ghi rõ mâu thuẫn/chỗ thiếu thay vì tự bịa. Kiểm tra file thực tế tồn tại; không giả vờ đã đọc training package hoặc rulebook chưa được cung cấp. Không sửa tài liệu gốc cuộc thi.

## Tình trạng và mục tiêu hiện tại

- Đội có 3 người, còn khoảng 11 ngày tới hạn nộp Docker 16/11/2026.
- Người dùng làm phần B, đã mở WSL và tạo dự án riêng trong Linux; đường dẫn có thể là `~/projects/datamate`, cần xác nhận từ workspace thực tế.
- Chưa biết GPU, VRAM, RAM, dung lượng đĩa, Python và CUDA của máy phát triển. Không giả định giống máy ban tổ chức.
- Nhiệm vụ đầu tiên được giao là **B1: kiểm tra môi trường, tải/load model local, hiểu câu hỏi và sinh SQL trên fixture synthetic**. Xem đặc tả B1 cuối file.
- Không cần đợi pipeline A hoàn tất. Dùng fixture schema được ghi rõ là giả lập, sau đó thay bằng BusinessContext thật qua interface.
- Chưa train, chưa cần embedding, chưa cần nhiều agent, chưa thực hiện Docker production đầy đủ trong nhiệm vụ B1.
- Dữ liệu test không dùng để train, tạo nhãn hoặc viết đáp án thủ công. Không hard-code theo ID.

## Owner và ranh giới phối hợp

| Owner | Phần chính | Bàn giao |
|---|---|---|
| A | Ingestion, normalization, catalog, relationships, query store, tools | BusinessContext và QueryTool |
| B | Local model wrapper, prompts, question planning, SQL/tool actions, bounded agent loop | QuestionPlan và kết quả agent |
| C | Evaluation, split, serializer/logging, integration, Docker, submission | Scorer, runner và gói final |

B không ghi đè module A/C hoặc tái cấu trúc repo không cần thiết. Nếu có code sẵn thì mở rộng và giữ tương thích. Windows/Linux phối hợp bằng pathlib, UTF-8, đường dẫn cấu hình; không hard-code ổ Windows hay username. Python 3.11 là mục tiêu ban đầu nếu stack phù hợp, không hạ/nâng global Python tùy tiện. Không commit weights, virtualenv, caches hoặc dữ liệu lớn.

## Interfaces phải thống nhất sớm

- `BusinessContext`: business reference, README, schema/catalog, rulebook references, calendar access, query-store handle, quality flags. Quyền đọc labels nằm ngoài context.
- `QueryTool`: query + parameters → success/error/ambiguous, typed data, row_count, truncated, warnings, provenance, timing.
- `QuestionPlan`: status, target answer type, metric, time bounds, filters, table/column references, SQL/tool action, missing requirements.
- `QuestionResult`: id, final answer, timing, model calls và actual execution trace.
- Không trộn dialect: baseline dùng DuckDB SQL cho dữ liệu đã chuẩn bị; SQLite input vẫn do adapter đọc. Nếu repo chọn SQLite làm query engine thì cập nhật config/prompt và mọi kiểm thử đồng bộ.

---

# DataMate — Kế hoạch triển khai theo thứ tự

Ngày lập: 05/11/2026. Đây là kế hoạch kỹ thuật đề xuất, không phải quy định bổ sung của ban tổ chức. Căn cứ: đề bài, ảnh Evaluation và Docker Submission Guide đã cung cấp. Chưa khảo sát training package và rulebook thực tế; mọi quyết định về cấu trúc dữ liệu, thuật ngữ nghiệp vụ và thời gian phải được xác nhận khi có các nguồn đó.

## 1. Mục tiêu và kiến trúc xuất phát

Xây dựng hệ thống nhận gói CSV/XLSX/SQLite, README, rulebook, calendar, câu hỏi tiếng Việt và as_of_date; trả đúng kiểu và giá trị, hoặc unanswerable/abstain. Tối ưu Penalized Accuracy: PA = (đúng − sai) / tổng câu, đồng thời giữ thời gian và khả năng tái lập.

Khởi đầu: một LLM local, Python điều phối vòng lặp, công cụ SQL và các hàm Python xác định. Chưa cần nhiều agent, vector database, reranker hoặc fine-tuning. Cùng một bộ trọng số có thể được gọi cho nhiều vai trò lập kế hoạch, sinh truy vấn, kiểm tra; đó không phải nhiều model riêng.

Luồng runtime: nạp cấu hình/model → đọc rulebook/calendar → chuẩn bị và cache gói doanh nghiệp → hiểu câu hỏi → chọn schema/entity → lập kế hoạch → truy vấn → kiểm tra → chọn answer/unanswerable/abstain → chuẩn hóa và lưu kết quả/log.

Adapter đọc file chạy trong pipeline; LLM chủ yếu gọi tool trên bảng đã chuẩn bị. Không đọc lại toàn bộ Excel ở mỗi câu. Mọi chuẩn bị lúc chấm, gồm lập chỉ mục và load model, đều nằm trong ngân sách 6 giờ.

## 2. Thứ tự và các mốc

| Giai đoạn | Công việc | Điều kiện chuyển bước |
|---|---|---|
| 0 | Chốt giao diện, quy định, repo, smoke test GPU/model | Docker tối thiểu chạy offline và ghi đúng đường dẫn |
| 1 | Khảo sát training data, tạo split, evaluator | Đánh giá được PA và phân biệt đúng/sai/bỏ qua |
| 2 | Inventory và adapters CSV/XLSX/SQLite | Đọc được nguồn, giữ nguồn gốc và mọi lỗi |
| 3 | Chuẩn hóa, profile, quan hệ, catalog, query store | Đối soát được dữ liệu và truy vấn thủ công |
| 4 | Tool layer, thời gian, rulebook/calendar | Tools có hợp đồng và kiểm thử tình huống thật |
| 5 | So sánh model và dựng baseline một lượt | Chạy toàn bộ validation, có score/log/thời gian |
| 6 | Agent loop có giới hạn, kiểm tra, sửa truy vấn | Cải thiện PA so với baseline trong ngân sách |
| 7 | Phát hiện thiếu dữ liệu và abstention | Ngưỡng được chọn trên calibration, không trên test |
| 8 | Embedding/hybrid retrieval, tùy nhu cầu | Tăng recall cần thiết và PA sau cùng |
| 9 | Fine-tuning, tùy nhu cầu | Tăng PA trên doanh nghiệp chưa dùng để train |
| 10 | Tối ưu tốc độ, bộ nhớ và chạy lại | Hoàn thành dự kiến trong 6 giờ, có dư địa |
| 11 | Đóng gói cuối, kiểm chứng, đóng băng | Đủ inference/training evidence, image ≤100 GB .tar |

Các bước kiểm tra Docker, logging và đánh giá bắt đầu từ đầu rồi hoàn thiện dần, không để đến giai đoạn cuối mới làm. Các giai đoạn embedding và fine-tuning là nhánh có điều kiện, có thể bỏ qua.

## 3. Giai đoạn 0 — Khởi tạo dự án và giao diện

### 0.1. Lập bảng ràng buộc

- Tổng mọi model dùng inference ≤15B tham số thực tế; gồm embedding/reranker/classifier và toàn bộ MoE, dù chạy lần lượt. Không cộng số đã làm tròn trong tên model. Quantization không làm giảm số tham số khai báo.
- Model gốc công khai trước 26/10/2026 và giấy phép phù hợp; khóa revision, lưu nguồn, ngày phát hành, LICENSE, checksum.
- Inference offline; không tải model, thư viện hoặc database extension lúc chấm.
- /data chỉ đọc; cache và dữ liệu chuẩn hóa ở /tmp hoặc vùng ghi được.
- Entry point: bash /code/predict.sh không đối số. Kết quả /result/submission.jsonl, log từng câu /result/logs/.
- 2 RTX 5090 32 GB/GPU, 512 GB RAM; không coi 64 GB VRAM là một vùng nhớ tự gộp. Cần cấu hình shard/tensor parallel nếu dùng cả hai GPU.
- Tổng thời gian ≤6 giờ gồm load, compile, đọc file, indexing, inference, logging.
- linux/amd64; .tar chưa nén của docker save ≤100 GB gồm layers.

### 0.2. Khởi tạo Git và cấu hình

Tách cấu hình runtime, model, normalization, prompts, agent budget, decoding và evaluation. Ghi run_id, git commit, phiên bản dữ liệu, revision model và phiên bản prompt trong mỗi lần chạy. Không commit weights, cache hoặc file dữ liệu lớn vào Git; lưu manifest để tái lập.

### 0.3. Xác định hợp đồng dữ liệu nội bộ

- Question: id, business_id, question, as_of_date.
- BusinessPackage: file manifest, README, fingerprint và rulebook/calendar version.
- TableMetadata: original name, internal name, source, grain, columns, types, units, keys, relationships, quality flags.
- ToolResult: status, data, schema, row_count, truncated, warnings, provenance, elapsed_ms.
- FinalAnswer: id, type, answer khi cần; không thêm diễn giải vào giá trị.
- AgentState: câu hỏi, rule references, schema chọn, time range, metric, query history, execution results, checks, budget, final decision.

### 0.4. Smoke test từ sớm

Dùng base image khuyến nghị trong guide vllm/vllm-openai:v0.10.1.1 hoặc stack thay thế đã xác nhận tương thích. Chạy một model nhỏ/ứng viên với prompt ngắn, xác nhận GPU, tokenizer/chat template, dtype, context và log. Tạo predict.sh có thể ghi một dòng abstain và log hợp lệ trước khi triển khai phần thông minh. Đây là kiểm thử giao diện, không phải giải pháp hoàn chỉnh.

Đầu ra: repo, config, schema nội bộ và Docker skeleton. Tiêu chí: chạy bằng lệnh chấm offline và không ghi vào /data.

## 4. Giai đoạn 1 — Khảo sát dữ liệu và dựng evaluator trước agent

### 1.1. Khảo sát training package

Đọc rulebook, calendar, README và mẫu từng định dạng trong training. Thống kê doanh nghiệp, file, sheet, bảng, số dòng/cột, kích thước, câu hỏi và kiểu đáp án. Phân nhóm: đếm/tổng, xếp hạng, công nợ, tồn kho, thời gian tương đối, âm lịch, tỷ lệ tăng trưởng, multi-table, thiếu thông tin.

Không giả định tất cả doanh nghiệp có sales/products/orders, không hard-code schema theo ID. Có thể dùng thuật ngữ chung được rulebook xác nhận. Ghi rõ chỗ nào là quy định, quan sát dữ liệu hay suy đoán.

### 1.2. Tạo split đánh giá

Chia training theo business_id; mọi câu hỏi của một doanh nghiệp thuộc cùng nhóm. Nếu có nhiều doanh nghiệp dùng chung template/schema hoặc câu tổng hợp gần trùng, cân nhắc chia theo nhóm template để đánh giá khả năng khái quát. Giữ phần train, development, calibration và một phần holdout cuối nếu kích thước cho phép. Nếu ít dữ liệu, dùng grouped cross-validation và giữ một holdout nhỏ thay vì chia quá vụn.

Chỉ dùng nhãn training hợp lệ. answers.jsonl thuộc evaluator, không đưa cho inference agent. Chỉ lấy few-shot và trajectory từ phần train. Public/private test không vào tập train, synthetic generation hoặc bộ ví dụ; không tạo đáp án test bằng tay.

### 1.3. Dựng evaluator

Ưu tiên evaluator chính thức nếu được cung cấp. Nếu chưa có, triển khai bản gần theo Evaluation và ghi các khác biệt chưa xác nhận. Kiểm thử đúng type; number; percent; Unicode NFC; lowercase; thay ký tự không phải letter/digit/whitespace; collapse whitespace; giữ dấu; date; boolean; ordered list; table matching; unanswerable; abstain/missing. Không tự thêm tolerance số hoặc tự quy mọi tên thành không dấu. Những chi tiết như duplicate row trong table phải theo scorer thật, không tự đoán.

Báo cáo PA, correct/wrong/abstain, coverage, accuracy trong câu đã trả lời, kết quả từng nhóm câu/business/type, tỷ lệ lỗi format, thời gian cold-start/từng câu/tổng, token và peak memory. Với dự đoán unanswerable, đo precision/recall riêng; unanswerable đúng vẫn là câu trả lời được chấm +1.

Đầu ra: split manifest, evaluator, báo cáo thống kê. Tiêu chí: các ví dụ chấm điểm đã biết được xử lý đúng, agent không truy cập nhãn.

## 5. Giai đoạn 2 — Data ingestion: inventory và adapters

### 2.1. Inventory

Liệt kê file, kích thước, checksum, sheet/table/view. Tạo fingerprint gồm dữ liệu nguồn, README, rulebook, calendar và phiên bản pipeline. Dùng fingerprint cho cache; ID chỉ định tuyến đúng gói dữ liệu, không làm khóa hard-code đáp án.

### 2.2. CSV adapter

- Xác định encoding, BOM, delimiter, quote, escape, newline, header và dòng lỗi.
- Đọc raw values ở dạng bảo toàn; không ép cột mã thành số chỉ do mẫu trông giống số.
- Không tự coi chuỗi NA/NULL là thiếu trong mọi cột: có thể là mã hợp lệ.
- Kiểm tra dòng thiếu/thừa trường, quote lỗi và số dòng thực; không silently skip bad lines.
- Sampling giúp chọn giả thuyết đọc, sau đó kiểm tra toàn bộ dữ liệu liên quan.

### 2.3. XLSX adapter

- Dùng openpyxl khảo sát workbook, sheet, vùng bảng, merged cells, hidden rows/columns, cell type và format.
- Phát hiện title trước header, header nhiều dòng, cột trống, bảng cạnh nhau và subtotal/total.
- Lưu công thức và cached value khi có. openpyxl đọc giá trị đã lưu, không tự tính công thức. Cache thiếu/lỗi không được đổi thành 0; nếu cần fallback tính lại chỉ hỗ trợ những công thức đã kiểm chứng hoặc dùng nguồn dữ liệu chi tiết đủ căn cứ.
- Không mặc định hidden row là vô hiệu hoặc là không thuộc dữ liệu; theo bối cảnh.
- Ô numeric hiển thị mã có số 0 đầu cần xét format và khóa liên quan; giữ raw numeric và displayed representation nếu có thể xác định. Không giả vờ khôi phục số 0 đã mất mà không có bằng chứng.
- Chỉ fill-down ô gộp khi rõ đó là nhãn nhóm; không fill-down số tiền/ngày tùy tiện.

### 2.4. SQLite adapter

- Mở read-only bằng sqlite3, đọc schema, tables/views, declared types, PK/FK và indexes; kiểm tra dữ liệu thực vì declared type chưa đảm bảo mọi ô đồng nhất.
- Giữ ý nghĩa view và các khóa. Chuyển sang query store nếu cần truy vấn chéo nguồn; đối soát kiểu và số dòng sau chuyển.
- Nếu dùng DuckDB extension đọc SQLite trực tiếp, đóng gói và kiểm tra extension offline theo phiên bản/platform. Baseline có thể dùng sqlite3 để đọc rồi materialize vào DuckDB, tránh phụ thuộc runtime download.

### 2.5. Provenance

Mỗi bảng có nguồn file/sheet/table. Mỗi hàng có source row hoặc stable row reference. Khi chuẩn hóa, lưu transformation version, raw value hoặc khả năng truy ngược tới raw source. Bảo toàn tên gốc song song tên SQL nội bộ, xử lý trùng tên khi bỏ dấu/slug.

Đầu ra: bảng raw + manifest + ingestion errors. Tiêu chí: không mất dòng/ô một cách im lặng; kiểm thử được file thực và trường hợp lỗi ảnh hưởng kết quả.

## 6. Giai đoạn 3 — Chuẩn hóa, catalog và query store

### 3.1. Chuẩn hóa có bằng chứng

- Text: giữ nguyên bản trả lời; tạo bản NFC/trim/lookup riêng. Bản không dấu nếu dùng chỉ là khóa tìm ứng viên, không thay tên gốc hoặc khóa nối chính.
- Identifier: text, giữ zero padding, không gộp mã khác chỉ vì gần giống.
- Numeric: locale theo cột/nguồn; phân biệt dấu nghìn/thập phân, phần trăm, ngoặc số âm, đồng/nghìn/triệu. Tiền dùng integer theo đơn vị gốc phù hợp hoặc DECIMAL, tránh float sai số. Tỷ lệ giữ cả representation và đơn vị để không nhân 100 hai lần.
- Date/time: phân biệt dd/mm và mm/dd, Excel serial, ngày thiếu năm, timestamp/timezone. Chuẩn hóa khi có bằng chứng; không ép ngày không rõ thành một cách hiểu duy nhất. Chỉ đổi timezone nếu nguồn/rulebook xác định, không tự giả định.
- Missing: phân biệt trống, marker thiếu đã xác nhận, parse failure, giá trị không áp dụng và số 0 thật.
- Unit: quy đổi chỉ khi có tỷ lệ hợp lệ; lưu hệ số và nguồn quy đổi.
- Duplicate/negative/outlier: flag để kiểm tra, không xóa/sửa tự động. Dòng âm có thể là trả hàng; giao dịch giống nhau có thể đều hợp lệ.

Mọi cột typed quan trọng kèm parse status và quality flag. Một giá trị SQL NULL do parse fail không được bị SUM bỏ qua mà vẫn coi kết quả là đầy đủ.

### 3.2. Grain và relationships

Xác định mỗi hàng là một đơn, dòng sản phẩm, lần thanh toán, lô thuốc hay snapshot tồn kho. Phân biệt event/flow và snapshot/stock. Không cộng tất cả snapshot tồn kho theo ngày thành số tồn hiện tại.

Đề xuất khóa từ PK/FK, uniqueness, overlap value và README. Kiểm tra orphan rows, multiplicity 1–1/1–many/many–many, composite keys, mã tái sử dụng theo chi nhánh/thời gian. Quan hệ suy ra là candidate có bằng chứng, không cùng tên là nối ngay. Không flatten tất cả bảng trước câu hỏi.

### 3.3. Profile

Số dòng; null/parse-error counts; số distinct; min/max; range ngày; numeric summaries; vài ví dụ đại diện; frequent status; đơn vị; candidate keys; dangling keys; coverage hạn chế. Sampling có nhãn và chỉ phục vụ khám phá; đáp án cuối phải tính trên toàn bộ phạm vi cần thiết.

### 3.4. Query store và catalog

Đề xuất Python + openpyxl + sqlite3 làm adapters, DuckDB làm SQL analytical store. pandas dùng cho biến đổi cần thiết, không biến thành nguồn tính toán thứ hai khác quy ước. Có thể thay bằng SQLite nếu phép toán và khối lượng đơn giản; chọn một SQL dialect và khóa trong prompt.

Tạo bảng raw/typed hoặc views tương ứng, bảng ánh xạ tên, metadata JSON và business summary. Catalog có schema, grain, units, relationship status, provenance, quality flags và rule references. Nêu rõ metadata suy luận chưa xác nhận.

Cache theo fingerprint; chỉ chuẩn bị doanh nghiệp có câu hỏi hoặc dùng lazy load để tránh làm thừa. Hạn mức cache/RAM, eviction nếu cần, stable sort cho outputs. Không dùng một bảng/vocabulary cố định chỉ phù hợp training.

### 3.5. Kiểm thử đối soát

Chọn truy vấn mẫu từ training: tổng tiền, đếm đơn, distinct khách hàng, join, xếp hạng, ngày, âm lịch, missingness, snapshot. Tính độc lập trên mẫu nhỏ và so với query store. Xác nhận tổng không bị nhân sau join, zero padding không mất, locale và decimal không sai, tổng/subtotal không bị tính hai lần.

Đầu ra: query store, catalog, relationship report, normalization/quality report. Tiêu chí: có thể tự truy vấn đúng các câu mẫu và giải thích nguồn, phạm vi và mọi hạn chế.

## 7. Giai đoạn 4 — Tool layer, rulebook và thời gian

### 4.1. Công cụ agent nhìn thấy

| Tool | Input | Output |
|---|---|---|
| list_tables | business context | Tên và mô tả ngắn các bảng |
| describe_table | table | Schema, grain, units, keys, warnings |
| preview_rows | table, bounded filters, limit | Mẫu và cờ truncated |
| lookup_values | table, column, query | Mã/tên ứng viên, cách khớp, mức mơ hồ |
| get_relationships | selected tables | Khóa nối và bằng chứng cardinality |
| read_rules | topic | Đoạn quy định chính xác, nguồn |
| resolve_time | phrase, as_of_date, rule references | Khoảng thời gian, boundary, căn cứ |
| query_sql | SQL, parameters | Kết quả typed + diagnostic metadata |
| run_calculation | helper name, typed inputs | Kết quả Python xác định |

CSV/XLSX/SQLite readers là công cụ nội bộ pipeline, không bắt LLM tự đoán encoding và đường dẫn mỗi câu. Python code generation tùy chọn, thêm sau SQL baseline nếu cần.

### 4.2. Hợp đồng và giới hạn thực thi

Tool trả status success/error/ambiguous, data, row_count, truncated, warnings và nguồn. Có timeout, giới hạn bộ nhớ, số hàng trả về, số lần gọi. Giới hạn rows hiển thị không được giới hạn dữ liệu đầu vào của phép SUM/COUNT. Nếu list/table bị cắt, paginate hoặc giảm truy vấn đúng nghĩa; không trả mẫu như đáp án hoàn chỉnh.

Runner giới hạn bảng/đường dẫn được phép, không cho SQL mutation/extension/network/file export tùy ý. Kiểm tra AST + policy và hạn chế engine; không chỉ kiểm tra chuỗi bắt đầu bằng SELECT vì SELECT vẫn có thể gọi hàm đọc file ngoài. Python sinh động chạy process riêng với giới hạn tài nguyên và quyền đọc/ghi tối thiểu; AST filtering đơn lẻ không phải sandbox đầy đủ. Bản đầu dùng helper đã định nghĩa thường dễ kiểm soát hơn arbitrary Python.

### 4.3. Rulebook và calendar

Lưu bản gốc và hash; trích quy tắc thành cấu trúc có reference. Bao phủ trạng thái hợp lệ, revenue vs collections, quantity vs orders, returns, debt, stock, rankings/ties, periods, rounding và defaults thực tế có trong rulebook. Không tự chế nội dung còn thiếu.

Time resolver hỗ trợ đúng những dạng thấy trong training: hôm nay/hôm qua, tuần/tháng/quý/năm, kỳ trước, cùng kỳ, month-to-date, lịch âm. Định nghĩa theo rulebook: đầu tuần, cuối kỳ, as-of cut-off, partial period và khoảng so sánh. Query timestamp có thể dùng [start, end_exclusive) để tránh bỏ giờ cuối ngày, nhưng chỉ sau khi quy tắc mốc được xác định.

Tra lịch âm bằng calendar.csv; kiểm tra miền ngày và tháng nhuận, không gọi dịch vụ bên ngoài hoặc tự suy lịch khi bảng không bao phủ mà chưa có phương án hợp lệ. Không thấy giao dịch không chứng minh dữ liệu thiếu, và cũng không chứng minh cửa hàng thật sự không có giao dịch nếu README nói file chưa đầy đủ.

Đầu ra: tool registry, JSON schemas, runtime budgets, time/rule helpers. Tiêu chí: thực hiện được truy vấn thủ công qua tool, lỗi được trả rõ và logging phản ánh lệnh thực tế.

## 8. Giai đoạn 5 — Chọn model bằng baseline thực tế

### 5.1. Danh sách ứng viên khởi đầu

| Model | Vai trò | Cách đưa vào thử nghiệm |
|---|---|---|
| Qwen3-4B | Smoke test/phát triển nhẹ | Nếu phần cứng phát triển hạn chế |
| Qwen3-8B | Ứng viên baseline chính | Hiểu tiếng Việt, lập kế hoạch, SQL/tool calls |
| Qwen2.5-Coder-7B-Instruct | Ứng viên thay thế | So sánh khả năng sinh SQL/Python |
| Qwen3-14B | Nhánh thử model lớn | Chỉ dùng nếu tổng tham số thực và VRAM/time phù hợp; không mặc định cộng thêm embedding |
| Qwen3-Embedding-0.6B | Nhánh retrieval tùy chọn | Chưa cần ở baseline |

Các bản Qwen3 ban đầu công khai 29/04/2025; Qwen2.5-Coder-7B-Instruct đã được giới thiệu 19/09/2024; Qwen3-Embedding công bố 05/06/2025. Nguồn chính thức công bố Apache 2.0 cho các ứng viên nêu trên. Cần chốt đúng repo/revision và kiểm tra lại license/files khi dùng. Đây không phải tuyên bố chúng là các model tốt nhất có thể dùng trong cuộc thi.

Mỗi thí nghiệm có thể so sánh model khác nhau, nhưng deployment cuối chỉ chứa và sử dụng tập model đã khai báo trong ngân sách. Không triển khai cả Qwen3-8B và Coder-7B theo mặc định; tổng thực tế có thể vượt 15B. Model 14B trong tên không chắc chừa đủ 0.6B.

### 5.2. Chạy benchmark nhỏ có đại diện

Dùng cùng subset development, catalog, tools, prompt structure, decoding và ngân sách. Kiểm tra câu không dấu/viết tắt, entity matching, nhiều bảng, thời gian, âm lịch, unanswerable và mọi kiểu đầu ra. Đo PA/format/SQL success/latency/tokens/VRAM; SQL chạy được chưa chắc đúng nghiệp vụ.

Chọn checkpoint/revision, dtype, context length, max output, thinking/non-thinking và local loading đã kiểm tra trên stack dự kiến. Ban đầu BF16 nếu đủ bộ nhớ để giảm biến số; quantize khi cần rồi đánh giá lại toàn bộ pipeline. Dùng Transformers để debug nếu tiện; runtime cuối ưu tiên kiểm tra với stack vLLM của guide.

### 5.3. Baseline một lượt

Input gồm question/as_of_date, README, rulebook liên quan, schema, mẫu giới hạn và dialect. Model tạo structured plan + SQL/tool action. Chạy một query, serializer chuyển kết quả sang type định trước; khi format/tool lỗi không sửa được thì abstain. Không cho model viết lại số bằng văn xuôi sau khi code đã tính đúng.

Đầu ra: baseline v1 và error report. Tiêu chí: toàn bộ validation có một dòng kết quả/log mỗi câu, có PA và thời gian đo được. Chưa đặt tỷ lệ đúng tùy tiện khi chưa thấy dữ liệu.

## 9. Giai đoạn 6 — Xây agent loop có giới hạn

### 6.1. State machine

Các trạng thái: inspect → interpret → plan → execute → verify → finalize. Khi thiếu schema/entity, quay lại inspect; khi query lỗi hoặc verification thất bại có căn cứ, quay lại plan. Hết budget thì abstain. Python state machine và kiểu dữ liệu rõ ràng đủ cho bản đầu; framework agent tùy chọn nếu giúp quan sát/điều phối, không thay thế thiết kế tool.

### 6.2. Question plan

Ghi target type, metric, grouping, filters, status rules, time window, units, required sources, entity candidates, join keys, sorting/tie rules và quality requirements. Không nhất thiết công khai plan như lời giải; đây là cấu trúc điều phối và kiểm tra.

Ví dụ hỏi số ly trà sữa tuần rồi: metric SUM(quantity), unit ly, product candidates đã đối chiếu, time resolver output, valid-order rule reference, output number. Nếu chỉ có doanh thu, không lấy số tiền chia giá hiện tại khi không có căn cứ về giá thực tế.

### 6.3. Execute–repair

Cho phép sửa lỗi cú pháp, cột không tồn tại, join chưa đủ khóa, lọc ngày không đúng kiểu. Trả đúng lỗi engine và schema để model sửa; không yêu cầu thử vô hạn. Giá trị khởi đầu đề xuất: tối đa 2 lần sửa, tổng 6–8 tool calls; điều chỉnh trên development/time profiling. Đây là ngân sách đề xuất, không phải luật thi.

### 6.4. Verification theo từng dạng

- Tổng/đếm: đúng grain; COUNT rows vs DISTINCT orders; giữ tiền nguyên/Decimal.
- Join: số dòng trước/sau, khóa nối đầy đủ, fanout, unmatched rows.
- Time: đúng as_of_date, kỳ, cut-off, timestamp boundaries; không lẫn ngày hiện tại máy.
- Ranking/list: tiêu chí, trạng thái, thứ tự, mã tie-break, đủ ứng viên.
- Growth/percent: mẫu số, kỳ so sánh, đơn vị và mẫu số 0 theo rulebook.
- Stock/debt: snapshot vs flow, hoàn trả/thanh toán một phần, thời điểm chốt; không suy stock từ sales nếu thiếu nhập hàng/đầu kỳ.
- Quality: parse failures/missing rows có thể ảnh hưởng kết quả không; không đánh fail vì một cột không liên quan.
- Format: type, precision, date, list order và table schema.

Verifier xác định là ưu tiên; cùng model đóng vai reviewer chỉ là tín hiệu bổ sung, không phải bằng chứng độc lập. Hai SQL tương đương hoặc hai lần model đồng ý vẫn có thể cùng sai cách hiểu.

### 6.5. Logging

Lưu id, final answer, elapsed_ms, generated model reasoning theo guide (list theo thứ tự nếu nhiều calls; null nếu không sinh), executed SQL/code/helper inputs và actual output. Thêm tool errors, checks, model/prompt revision và source refs nếu hữu ích. Không tạo reasoning giả cho call không sinh reasoning; không thay log tính toán bằng một bản tóm tắt khác với thực thi.

Đầu ra: agent v2. Tiêu chí: ablation cùng validation cho thấy giá trị của loop so với v1 và không vượt ngân sách.

## 10. Giai đoạn 7 — Answerability và chọn abstain

Tách ba câu hỏi: dữ liệu có đủ không; tính toán có đúng không; kết quả có đúng format không. SQL lỗi hoặc model không tìm thấy cột không chứng minh dữ liệu không hỗ trợ trả lời.

Trả unanswerable chỉ khi có căn cứ về thông tin bắt buộc thiếu/không thể xác định theo rulebook. Trả abstain khi còn mơ hồ, execution/verification thất bại, thiếu thời gian hoặc độ tin cậy không đủ. Kết quả rỗng/0 cần diễn giải theo câu hỏi, không mặc định unanswerable.

Tín hiệu confidence: unresolved entities/time, join quality, relevant parse errors, rule ambiguity, execution status, result completeness, verifier findings và agreement bổ sung. Không dùng cosine score hoặc tự đánh giá 90% của LLM như xác suất đúng đã hiệu chuẩn.

Dùng calibration thuộc training để chọn threshold tối đa PA; sau đó chốt và kiểm tra holdout. Về lý thuyết xác suất đúng p cho điểm kỳ vọng 2p−1, nhưng thực tế threshold dựa trên score đã đánh giá. Báo cáo risk/coverage, PA và unanswerable precision. Không dùng public/private labels để tune.

Đầu ra: decision policy và calibration report. Tiêu chí: PA tăng nhờ giảm câu sai, không chỉ tăng coverage hoặc accuracy có điều kiện.

## 11. Giai đoạn 8 — Embedding và retrieval, có điều kiện

Chỉ bắt đầu nếu context quá dài, schema linking/entity lookup thường sai hoặc lexical search bỏ sót ứng viên. Nếu schema nhỏ và baseline tốt, bỏ qua.

Thứ tự: exact code/name → normalized lexical/fuzzy lookup → schema/relationship expansion → thử dense embedding → tùy chọn reranker.

Embed table/column descriptions, README sections, rulebook sections khi cần và entity names cùng mã/thuộc tính phân biệt. Không chỉ embed raw name mất đơn vị/grain; không embed toàn bộ transaction rows để thay phép SUM/COUNT.

Dùng Qwen3-Embedding-0.6B như một ứng viên local. Khóa revision, instruction template, pooling, normalization và dimension đúng model card. Index tách theo business; rulebook dùng đúng split. Cache embeddings theo fingerprint, luôn có đường fallback để lấy full schema/quan hệ cần thiết nếu top-k thiếu. Không suy rằng entity gần vector nhất chắc là entity người dùng muốn.

Đánh giá recall@k của bảng/cột/entity cần thiết trên nhãn do nhóm tạo từ training hoặc truy vấn đã xác minh; sau đó đánh giá PA toàn pipeline. So sánh lexical, dense và hybrid ở cùng budget. Reranker thêm sau nếu còn lỗi thứ tự ứng viên; phải tính model này vào 15B.

Vector nhỏ có thể lưu NumPy và tính cosine, chưa nhất thiết cần vector database riêng. Chọn theo kích thước thực tế.

Đầu ra: retrieval v3 hoặc quyết định không dùng. Tiêu chí: tăng PA/hiệu quả thực tế đủ bù indexing, latency và tham số.

## 12. Giai đoạn 9 — Fine-tuning tùy chọn

Chỉ train khi error analysis chỉ ra lỗi hành vi model lặp lại sau khi pipeline/tools/prompts đã ổn: tiếng Việt viết tắt, schema linking, tool schema, SQL patterns, answerability. Không dùng fine-tuning để che lỗi parser hoặc thiếu dữ liệu.

### 9.1. Chuẩn bị supervision

- Lấy dữ liệu train hợp lệ; labels câu trả lời không tự cung cấp ground-truth SQL/trajectory.
- Tạo schema-grounded questions, SQL/tool trajectories và final answer từ dữ liệu train hoặc doanh nghiệp synthetic riêng.
- Thực thi SQL/Python để xác minh label; loại trajectory tính sai hoặc chỉ may mắn trùng đáp án.
- Tạo negative/near-miss: thiếu chi phí, không rõ đơn vị, partial coverage, missing calendar, code lookup mơ hồ. Giữ phân biệt unanswerable/abstain, không dạy bỏ qua mọi câu khó.
- Tăng đa dạng tên bảng, header, schema, dates, ngành và cách viết tiếng Việt. Không bịa quy tắc xung đột rulebook.
- Chia business/template trước augmentation; deduplicate trước split; không để bản biến thể của cùng ví dụ lọt sang validation.
- Nếu dùng API/generator không tái lập, lưu prompts, responses, model/settings, filtering code và manifest theo guide.

### 9.2. Huấn luyện và chọn checkpoint

Bắt đầu SFT bằng LoRA/QLoRA tùy phần cứng. Mục tiêu structured tool actions/SQL/format; RL/DPO không phải yêu cầu để agent có thể hoạt động. Cân bằng kiểu câu, không chỉ train câu tổng đơn giản. Theo dõi PA end-to-end thay vì chỉ loss. Chọn checkpoint trên development, calibration lại decision policy rồi kiểm tra holdout chưa dùng để chọn.

### 9.3. Export và bằng chứng

Đóng gói base+adapter hoặc merged weights, tokenizer/config và license. Nếu quantize sau merge, đánh giá đúng bản export inference. Kiểm kê tham số thực của deployment, mọi model phụ và cách tính adapter nếu giữ riêng; merge có thể tránh cấu hình adapter tách. Lưu seed, dependencies, configs, dữ liệu, logs, verification checkpoints và data_manifest SHA-256 trong image. Không đóng gói optimizer state thừa nếu không cần kiểm chứng.

Đầu ra: model v4 hoặc giữ pretrained nếu không tốt hơn. Tiêu chí: tăng PA trên doanh nghiệp chưa thấy, không làm giảm reproducibility/time/compliance.

## 13. Giai đoạn 10 — Tối ưu runtime

Đo cold run: load/compile + ingestion + indexing + tất cả câu + logs + flush. Guide dùng average time ×500 để ước lượng; 500 là mốc lập kế hoạch của guide, đối chiếu thông tin chính thức mới nhất khi có. 6 giờ/500 ≈43.2 giây mỗi câu trước khi trừ overhead; không coi đây là timeout bắt buộc từng câu.

Nhắm ngân sách nội bộ 4.5–5 giờ để có dư địa là đề xuất kỹ thuật. Phân tích mean, p50/p95 và câu outlier. Cache theo business, prefix metadata nếu runtime hỗ trợ, batch phù hợp, giới hạn tokens và retries, ưu tiên query xác định. Tránh chọn quantization, tensor parallel hoặc dùng hai GPU chỉ vì có sẵn; đo throughput và memory với context/concurrency thật.

Nếu cần chọn lịch xử lý, dựa trên độ phức tạp/chi phí tổng quát và metadata, không dựa đáp án hoặc hard-coded IDs. Mọi câu cuối cùng phải có kết quả; thiếu thời gian thì ghi abstain hợp lệ. Không tính điểm từ suy đoán số câu dễ.

Chạy lại cùng input với thứ tự và settings cố định, so sánh per-question diff, PA và thời gian. Fixed seed/temperature 0 hữu ích nhưng không đảm bảo tuyệt đối deterministic trên GPU. Giảm nguồn sai khác nếu batching/concurrency làm output dao động.

Đầu ra: runtime profile và frozen config. Tiêu chí: hai cold runs hoàn chỉnh trên phần cứng phù hợp, đủ margin và score agreement theo quy định.

## 14. Giai đoạn 11 — Docker cuối và nộp

### 11.1. Assets

/code/predict.sh; /code/README.md; inference src/configs/prompts; model weights/tokenizer/config; dependencies và extensions offline; training_code, train/validation data, logs/checkpoints cần thiết và manifest. Tất cả đường dẫn khác ghi trong README. Không cần đưa test data vào image; test được mount runtime.

### 11.2. Khởi động

Reset ENTRYPOINT của vLLM base theo guide. predict.sh tạo output dirs, đặt fixed defaults và gọi pipeline. Không cần đối số hoặc biến bí mật từ host. HF_HUB_OFFLINE=1, TRANSFORMERS_OFFLINE=1; dùng local model path. Các NCCL workaround trong guide là cấu hình mẫu cần xác nhận trên stack thực, không mặc định chữa mọi lỗi.

### 11.3. Chạy giống ban tổ chức

Hai GPU, network none, /data:ro, /result writable, shm-size=16g và entrypoint bash gọi /code/predict.sh. Không privileged, Docker socket, host services/credentials hoặc host IPC dependency. Làm cold run không dựa cache host/model đã download bên ngoài image. Write/flush từng đáp án; tránh duplicate id khi recovery.

### 11.4. Kiểm tra trước export

Đủ một JSONL record mỗi câu; strict JSON, không NaN/Infinity; type/answer schema; logs có elapsed_ms và thinking phù hợp, actual executed code/output; no-network; /data unchanged; initialization + runtime dưới 6 giờ; hai run tái lập; complete training evidence và declaration. Kiểm tra image layers để không giữ weights cũ bị xóa trong layer sau; dung lượng .tar đo thực tế ≤100 GB.

### 11.5. Nộp và đóng băng

Export đúng một linux/amd64 image/tag, set pipefail khi docker save qua gzip, đo kích thước chưa nén, tạo MD5 .tar.gz, kiểm tra tải lại từ link. Gửi link/checksum/size theo guide; không đổi tên archive sau tạo MD5. Lưu bản final revision; không sửa code/config/weights/training artifacts sau deadline.

## 15. Cấu trúc thư mục đề xuất

| Đường dẫn trong /code | Trách nhiệm |
|---|---|
| predict.sh | Entry point cố định |
| README.md | Cách chạy, model, dữ liệu, approach |
| configs/ | Runtime/model/agent budgets, pipeline policy |
| src/contracts/ | Question, metadata, tool result, final answer |
| src/ingestion/ | CSV/XLSX/SQLite adapters, inventory |
| src/normalization/ | Types, units, dates, missingness, provenance |
| src/catalog/ | Profiles, grain, relationships, schema descriptions |
| src/storage/ | Query store, caching, fingerprint |
| src/rules/ | Rulebook references, calendar/time helpers |
| src/tools/ | Registry, query/lookup tools, execution limits |
| src/models/ | Local inference interface, prompts/chat template |
| src/retrieval/ | Lexical first; embeddings/hybrid nếu dùng |
| src/agent/ | State machine, planning, execution, repair |
| src/verification/ | Checks, answerability, abstention policy |
| src/output/ | Serialization, append/flush, per-question logs |
| evaluation/ | Scorer, split manifests, experiment reports; không đưa labels vào agent |
| tests/ | Kiểm thử parser/metrics/time/join/scorer có ý nghĩa |
| models/ | Weights/config/tokenizer cho inference |
| training_code/ | Train/generation source, data, evidence, manifest |

Đây là module theo trách nhiệm, không bắt buộc tạo hàng chục file rỗng ngay ngày đầu. Bắt đầu nhỏ nhưng giữ interface ổn định để thay model/retrieval mà không viết lại ingestion.

## 16. Thí nghiệm và tiêu chí giữ thay đổi

| Phiên bản | Thay đổi chính | Câu hỏi cần trả lời |
|---|---|---|
| v0 | Tools và truy vấn thủ công | Dữ liệu và phép tính đã đúng chưa? |
| v1 | Một LLM, một lượt SQL | Model tự động hóa được bao nhiêu? |
| v2 | Inspect/repair/verify có giới hạn | Loop tăng PA hay chỉ tăng chi phí? |
| v2a | Confidence/abstain calibration | Giảm sai có tăng PA không? |
| v3 | Embedding/hybrid nếu cần | Retrieval có giảm schema/entity errors? |
| v4 | SFT nếu cần | Hành vi model khái quát tốt hơn không? |
| final | Tối ưu và Docker frozen | Bản nộp giữ được score, time, determinism? |

Mỗi lần thay một nhóm thành phần chính, giữ subset/settings so sánh và lưu paired per-question results. Nếu sửa evaluator hoặc phát hiện pipeline bug, tính lại baseline; không so score từ hai scorer khác nhau. Không dùng holdout cuối để lựa chọn liên tục. Loại bỏ cải tiến không giúp mục tiêu PA/time hoặc gây rủi ro tái lập.

## 17. Timeline thực tế — 3 người, 11 ngày

Ngày 1 là 06/11/2026, ngày 11 là 16/11/2026 theo giờ Việt Nam. Nếu bắt đầu muộn hơn, giữ thứ tự ưu tiên và rút phần tùy chọn; không dời deadline 23:59 ngày 16/11. Nhắm hoàn tất trước 18:00 ngày cuối để có thời gian xử lý upload.

| Ngày | A: Data & Tools | B: Model & Agent | C: Evaluation & Docker | Mốc tích hợp |
|---|---|---|---|---|
| 1 — 06/11 | Khảo sát, reader mẫu, metadata | Kiểm tra WSL/GPU, load model, sinh SQL fixture | Repo, split, scorer, Docker skeleton | Chốt interfaces; smoke test |
| 2 — 07/11 | Ba adapters tối thiểu, query store/catalog | Nối schema→SQL→tool→answer | Serializer/logs, tích hợp | Một lệnh input→JSONL |
| 3 — 08/11 | Sửa lỗi ingestion/type/join | Baseline trên validation | PA/error report, public run | Submission đầu tiên |
| 4 — 09/11 | Grain, relationships, quality flags | Inspect/execute/repair có giới hạn | So sánh baseline/agent | Agent v2 |
| 5 — 10/11 | Rule/time/calendar helpers | Multi-step, ranking, lookup | All answer types, runtime | Bao phủ nhóm chính |
| 6 — 11/11 | Sửa nhóm lỗi tác động lớn | Model comparison; retrieval nếu cần | Abstain calibration | Chọn kiến trúc ứng viên |
| 7 — 12/11 | Cache và ingestion ổn định | Verifier và budgets | Full offline Docker run | Release candidate |
| 8 — 13/11 | Sửa lỗi có bằng chứng | Sửa lỗi có bằng chứng | Cold runs/profiling | Feature freeze |
| 9 — 14/11 | Review provenance/join | Review prompts/revisions | Export/upload/checksum | Nộp Docker lần 1 nếu đạt |
| 10 — 15/11 | Review chéo | Review chéo | Regression/reproducibility | Cả nhóm duyệt final |
| 11 — 16/11 | Hỗ trợ lỗi khẩn cấp | Hỗ trợ lỗi khẩn cấp | Nộp lần 2 nếu cần, kiểm tra form/link | Hoàn tất trước hạn |

Fine-tuning không được mặc định chen vào lịch này. Chỉ cân nhắc thử sớm nếu baseline đã ổn, có lỗi model rõ, có dữ liệu và GPU; không mở nhánh train mới sau feature freeze. Phần cứng phát triển chưa xác nhận, không giả định đội sở hữu máy chấm.

Public JSONL: tối đa 5 submissions/ngày/đội, reset cuối ngày theo portal; không liên quan Git commit. Docker solution: tối đa 2 lượt, chỉ lượt cuối được đánh giá. Private round dùng giải pháp đã đóng băng; đoạn luật đã cung cấp chưa nêu quota JSONL private. Kiểm tra thông báo mới nhất nếu mốc giờ release private khác nhau giữa tài liệu.

Mỗi ngày: sync 15 phút đầu buổi, integration check giữa buổi, review score/error cuối buổi. Task nhỏ 0.5–1 ngày; mỗi task có owner và reviewer. Branch ngắn, merge thường xuyên, không đợi nhiều ngày. Mỗi submission/experiment lưu commit/config/model/prompt revision, local PA và runtime; không lựa chọn chỉ theo public score.

## 18. Việc cần làm ngay khi bắt đầu code

1. Có training package, rulebook và calendar thực tế; inventory và đọc một số doanh nghiệp đại diện.
2. Tạo repo/config/contracts và local evaluator.
3. Chia training theo business, tách labels khỏi agent.
4. Smoke test model trên Docker stack để phát hiện sớm lỗi GPU/môi trường.
5. Hoàn thành một lát cắt: một business → đọc file → typed query store → manual SQL → final JSONL/log.
6. Mở rộng ba định dạng và các trường hợp dữ liệu ảnh hưởng kết quả.
7. Sau đó mới nối LLM vào tool layer và đo baseline toàn validation.

## 19. Nguồn kỹ thuật đã kiểm tra

- Đề bài, ảnh Evaluation và Docker Submission Guide Zalo AI Challenge 2026 do người dùng cung cấp; là nguồn ưu tiên cho quy định cuộc thi.
- Qwen3 release: https://qwenlm.github.io/blog/qwen3/
- Qwen3-8B model card: https://huggingface.co/Qwen/Qwen3-8B
- Qwen2.5-Coder release: https://qwenlm.github.io/blog/qwen2.5-coder/
- Qwen3 Embedding release: https://qwenlm.github.io/blog/qwen3-embedding/
- Qwen3-Embedding-0.6B model card: https://huggingface.co/Qwen/Qwen3-Embedding-0.6B
- DuckDB CSV reader: https://duckdb.org/docs/stable/data/csv/overview
- openpyxl workbook reading: https://openpyxl.readthedocs.io/en/3.1/tutorial.html

Các lựa chọn kiến trúc, giai đoạn, module, budgets và lịch làm là đề xuất cho dự án này. Không tuyên bố đã benchmark, triển khai hay bảo đảm mức điểm cụ thể khi chưa có dữ liệu thực tế.


## 20. Đặc tả công việc B1 — local model smoke test và sinh SQL

### B1.1. Mục tiêu

Có các lệnh tái lập được: chẩn đoán môi trường; chuẩn bị model khi có mạng; load model hoàn toàn từ local path; nhập schema + rule mô phỏng + as_of_date + câu hỏi tiếng Việt; nhận JSON chứa SQL hoặc trạng thái thiếu thông tin. B1 chỉ sinh và kiểm tra cấu trúc output, không tự thực thi SQL sinh ra trên dữ liệu thật. Không phải agent hoàn chỉnh, không phải final submission runner.

### B1.2. Đọc nguồn và khảo sát môi trường

1. Đọc brief này và tài liệu đã cung cấp; tìm module/config/env hiện có trước khi tạo mới.
2. Kiểm tra OS/WSL, cwd, Python/venv, `nvidia-smi`, GPU/VRAM, RAM và free disk; sau khi có torch kiểm tra CUDA availability và versions. Không in token/secret.
3. Chọn bản model theo nguồn lực đo được; không tải 8B mặc định khi máy không phù hợp. Qwen3-4B là ứng viên smoke test; Qwen3-8B là ứng viên baseline nếu đủ bộ nhớ. Chưa chọn embedding.
4. Phân biệt weights memory với KV cache/runtime overhead. CPU fallback có thể rất chậm và tốn RAM; không tự tải/load float32 model lớn chỉ để thử khi CUDA lỗi.
5. Kiểm tra hướng dẫn chính thức để chọn PyTorch/CUDA/Transformers tương thích. Không cài vLLM hoặc flash-attn cho B1 nếu Transformers đủ. Không thay driver/global environment hoặc bỏ torch GPU đang hoạt động.

Nếu không có GPU/phần cứng phù hợp hoặc mạng bị chặn: vẫn hoàn thành code, fixture, validation không cần model và lệnh chạy; báo chính xác phần chưa kiểm chứng. Chỉ hỏi khi cần quyết định thực sự về GPU/cloud, quyền cài đặt hoặc môi trường; không giả vờ inference đã chạy.

### B1.3. Dependencies và quản lý model

Dùng venv riêng hoặc env repo đang hoạt động. Tách dependencies B tối thiểu: torch, transformers, accelerate, huggingface_hub; thêm kiểm tra JSON/schema bằng stdlib hoặc dependency nhẹ nếu đã có. Khóa phiên bản sau khi xác nhận tương thích; ghi lệnh cài GPU đúng platform. Không tự thay requirements của A/C bằng một tập dependency lớn.

Tạo download command riêng dùng repo_id + fixed revision. Có thể resolve revision sang commit hash ở bước chuẩn bị có mạng rồi ghi manifest; offline không resolve remote. Download vào models/<local-name>, giữ tokenizer/config/license và safetensors cần thiết; không chạy model bằng repo_id online trong lệnh inference.

Manifest: repo, resolved revision, source URL, license, original release date đã xác minh, dtype/quantization, local path, versions và cách xác định tổng tham số. Chưa biết thông tin thì ghi unknown cần xác minh, không bịa.

Đặt ignore cho .venv, weights, cache và smoke outputs. Tách clearly `download_model` online khỏi `smoke_sql` offline. Local load dùng `local_files_only=True`, HF_HUB_OFFLINE=1 và cấu hình offline phù hợp phiên bản.

### B1.4. Module và CLI tối thiểu

Tích hợp vào cấu trúc repo có sẵn; nếu chưa có, đề xuất nhóm file nhỏ:

| File đề xuất | Vai trò |
|---|---|
| scripts/check_environment.py | Báo môi trường và tài nguyên |
| scripts/download_model.py | Download có revision và manifest |
| src/datamate_b/model.py | Local load và generate |
| src/datamate_b/prompts.py | Prompt và chat template |
| src/datamate_b/contracts.py | Request/response validation |
| scripts/smoke_sql.py | CLI chạy fixture hoặc câu hỏi tùy chọn |
| examples/b1_schema.sql | DDL synthetic rõ ràng |
| examples/b1_rules.md | Quy tắc demo, không phải rulebook cuộc thi |
| examples/b1_questions.jsonl | Câu hỏi demo, không có test question |
| docs/B1_MODEL_SMOKE.md | Cách cài, tải, chạy, giải thích, troubleshooting |

CLI hỗ trợ model_path, schema/rules file, question hoặc questions file, as_of_date, output path, max_new_tokens và cấu hình device/dtype đã xác minh. Kiểm tra local path và lỗi cấu hình trước khi load.

Model wrapper tách `load()` và `generate()` để load một lần cho nhiều câu; dùng inference mode, chat template đúng model và chỉ decode generated tokens. Với Qwen3, bắt đầu non-thinking theo model card để dễ nhận JSON; giữ raw output để debug, không giả định mọi model hỗ trợ cùng tham số template. Fixed seed và greedy decoding cho baseline; không tuyên bố GPU tuyệt đối deterministic.

### B1.5. Fixture synthetic và hợp đồng output

DDL: products(product_id TEXT PRIMARY KEY, product_name TEXT, unit TEXT); sales(sale_id TEXT PRIMARY KEY, sale_date DATE, product_id TEXT, quantity INTEGER, amount_vnd BIGINT, status TEXT). Mỗi sales row là một sale line, sale_id unique, FK product_id→products; không có COGS/chi phí.

Demo rules: chỉ status='completed' hợp lệ; tuần bắt đầu thứ Hai; 'tuần rồi' là tuần lịch ngay trước tuần chứa as_of_date, dùng start inclusive/end exclusive; 'bán nhiều nhất' nghĩa SUM(quantity), không phải revenue; cùng số lượng sort product_id tăng dần. Tất cả đều là quy ước fixture, không được áp dụng như luật thật khi thay sang data cuộc thi.

Ba câu tối thiểu:
1. 'Tổng doanh thu của các giao dịch hợp lệ ngày 2026-03-03 là bao nhiêu?' → cần SUM(amount_vnd), filter date/status.
2. 'sp bán dc nhiều nhất tuần rồi là gì', as_of_date=2026-03-04 → cần join, SUM(quantity), date range 2026-02-23 đến trước 2026-03-02, sort và trả tên; SQL không dùng CURRENT_DATE.
3. 'Lợi nhuận tháng này là bao nhiêu?' → thiếu chi phí/COGS; không tự coi revenue là profit.

Output nội bộ đề xuất:
```json
{
  "status": "sql",
  "answer_type": "number",
  "sql": "SELECT ...",
  "missing_requirements": []
}
```

Cho phép status sql / unanswerable / abstain. Với sql: SQL không rỗng, answer_type thuộc value types. Với unanswerable: sql=null, answer_type='unanswerable', missing_requirements nêu thông tin cần thiết còn thiếu. Với abstain: sql=null, answer_type='abstain'. Đây là contract nội bộ B1, không phải submission.jsonl; final schema nằm ở runner của C.

Prompt: chỉ dùng bảng/cột được cung cấp; DuckDB SQL; không bịa thông tin, không dùng ngày máy; chỉ JSON; không Markdown fence; không ghi SQL mutation. JSON parser phải phát hiện output lỗi/truncated, không âm thầm coi là SQL đúng. Có thể loại fence bằng parser kiểm soát và lưu raw; không coi chuỗi bắt đầu SELECT là bảo đảm an toàn. Không thực thi SQL trong B1.

### B1.6. Verification và log

Chạy static/import checks nếu env cho phép, các kiểm tra JSON contract và date fixture có ý nghĩa, rồi model inference thật khi phần cứng cho phép. Không bắt đúng một chuỗi SQL duy nhất; câu tương đương có thể hợp lệ. Syntax/JSON hợp lệ không chứng minh semantic accuracy. Ghi từng câu: model/revision/config, question, as_of_date, raw output, parsed object/parse_error, generation timing, prompt/output token counts nếu có, reasoning_generated=null khi non-thinking không sinh reasoning, executed_code=null/output=null khi chưa thực thi. Thời gian load model ghi riêng. Phân biệt tooling smoke report với log final competition.

Không ghi đáp án mong đợi vào model input. Không chỉnh tay output model để làm test pass, không hard-code SQL cho ba câu demo. Test fixture chỉ kiểm tra khả năng hoạt động; không là bằng chứng model đủ tốt cho cuộc thi.

### B1.7. Acceptance và bàn giao

- Có báo cáo hardware/environment thực tế.
- Có lệnh download riêng, revision/manifest và local path.
- Có lệnh offline load/generate, model load một lần cho nhiều câu.
- Có ba fixture cases và raw/parsed outputs hoặc báo cáo chính xác chưa chạy vì lý do gì.
- Có xử lý missing info và output không hợp lệ; không chỉ nhận mọi SQL là hợp lệ.
- Không thêm embedding, fine-tuning, full ingestion, multi-agent hay production Docker trong B1.
- Có docs ngắn giải thích flow schema/question→chat template→tokens→generate→JSON.
- Báo file đã tạo/sửa, lệnh chạy lại, checks passed/failed/not run và bước tiếp theo là nối QueryTool từ A.

## 21. Prompt khởi động B1 để dán vào Codex

Đọc docs/DATAMATE_PROJECT_BRIEF.md, các hướng dẫn repository áp dụng và tài liệu cuộc thi tôi đã đặt trong workspace. Tôi là người B. Hãy thực hiện đúng đặc tả B1 ở mục 20: kiểm tra WSL/Python/GPU/RAM/disk, chọn model smoke test phù hợp, tạo môi trường/dependencies tối thiểu, công cụ download riêng có fixed revision và local model wrapper, CLI sinh JSON chứa SQL trên ba fixture synthetic, cùng tài liệu chạy lại.

Bắt đầu bằng khảo sát repo và môi trường rồi triển khai, không chỉ đưa kế hoạch. Ưu tiên Transformers cho smoke test; Qwen3-4B hoặc Qwen3-8B tùy phần cứng, không tự tải 8B khi chưa kiểm tra tài nguyên. Không thay driver/global Python, không ghi đè code A/C, không dùng API ngoài cho inference, không train, không thêm embedding hay framework agent. Lệnh inference chỉ load model local và hoạt động offline. B1 chưa thực thi SQL sinh ra.

Lưu raw output và parsed result; kiểm tra JSON/type, xử lý thiếu thông tin và output lỗi, không hard-code đáp án. Load model một lần cho nhiều câu. Nếu không có GPU/mạng phù hợp, hoàn thành code và các checks không cần model, báo rõ phần chưa chạy, không bịa kết quả. Tải model phục vụ smoke test khi tài nguyên và truy cập cho phép; không mở rộng sang dịch vụ cloud trả phí.

Kết thúc bằng danh sách file, lệnh chuẩn bị/tải/chạy offline, phiên bản/model revision, kết quả kiểm tra thật và giải thích ngắn từng bước bằng tiếng Việt. Chỉ làm B1 ở lượt này.
