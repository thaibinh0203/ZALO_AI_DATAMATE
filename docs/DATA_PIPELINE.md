# Data pipeline — bản refinement đã chốt

Luồng: CSV/XLSX/SQLite → parser → adapter → profile toàn cột → canonical mapping → SLM cho cột chưa rõ → refinement tối đa một lượt/cột đủ điều kiện → validator → chuẩn hóa → PostgreSQL.

## Output hiện tại

- Local: `outputs/data_pipeline/train`; logs kiểm tra: `outputs/data_pipeline/checks`.
- Bộ nhỏ để commit Git: [artifacts/data_pipeline_final](../artifacts/data_pipeline_final/README.md), gồm schema không có giá trị dòng, review từng business, thống kê và kết quả kiểm chứng.
- [Luồng canonical-first](CANONICAL_FIRST_PIPELINE.md), [refinement](BOUNDED_REFINEMENT_EXPERIMENT.md), [validation](DATA_PIPELINE_VALIDATION.json).

Đã chạy đủ 18 doanh nghiệp thuộc train theo frozen split: 80 bảng, 2.040.280 dòng nguồn, 2.033.999 dòng clean; nạp và đối chiếu PostgreSQL thành công. 115 tests passed. Semantic: existing_mapping=464, applied_candidate=18, described_other=168, unresolved=15, role_conflict=6. Các role do model đề xuất vẫn cần review nghiệp vụ; chưa đo độ chính xác QA.

## Cách dữ liệu được xử lý

- CSV đọc UTF-8 BOM/sniff delimiter; dòng lệch số cột được giữ và cách ly.
- Excel đọc read-only, giữ vị trí dòng/ô, merged cells/hidden rows và thông tin formula cache. Nếu header chưa đủ bằng chứng, SLM chỉ được chọn một dòng có sẵn.
- SQLite mở read-only, giữ metadata kiểu/PK/FK trước khi nạp PostgreSQL. Dữ liệu train đã chạy ở đây là CSV/XLSX; không suy rằng đã chạy business SQLite thuộc split khác.
- Alias và mô tả README được ưu tiên cho canonical role. SLM đối chiếu ý nghĩa với canonical definitions trước khi đề xuất semantic role mới. Canonical đặt nhầm ở trường semantic được chuyển về đúng vị trí rồi kiểm tra tiếp.
- Cột không bị xóa khi chưa rõ: giữ tên nguồn, raw values và provenance. `canonical_role`/`semantic_role` có thể null; `has_data` phản ánh cột có giá trị nguồn không rỗng.
- Kiểu số dùng Decimal/NUMERIC; mã định danh giữ TEXT. Kiểu mới phải qua kiểm tra toàn cột, không chỉ vài sample.
- Trạng thái đã có mapping được chuẩn hóa; giá trị chưa biết giữ nguyên với note `unmapped_status_preserved`, không tự coi là hoàn thành/hủy.
- Quy đổi đơn vị có scope sản phẩm/cửa hàng; số lượng cơ bản có sẵn được giữ và gắn cờ khi khác phép tính. Còn 4.998 cờ bất đồng và 969 trường hợp thiếu số lượng cơ bản cần được xử lý theo câu hỏi/rulebook.
- Raw duplicates được giữ trong bảng cơ sở; view `_clean` loại dòng duplicate/header/subtotal/quarantine theo kind/flags hiện tại.
- Mỗi business có schema PostgreSQL theo fingerprint nguồn/code/semantic plan. Catalog chứa tên SQL thực tế; không hard-code schema từ snapshot vào agent.

Pipeline không đọc questions/answers, không cập nhật alias từ model và không thực thi SQL do model sinh.

## Môi trường và cài đặt

Máy hiện tại dùng `.venv-b1-gpu` (Python 3.12, torch 2.7.1+cu128, Transformers 4.51.3) cho model/pipeline; `.venv-data` cho inspect/validation. Dependencies được pin ở `requirements-data-semantic.txt`, `requirements-b1.txt`, `requirements-data.txt`. Không cần tải lại trên máy hiện tại.

Chuẩn bị máy mới có CUDA phù hợp, khi có mạng:

```bash
uv venv --python python3.12 .venv-b1-gpu
uv pip install --python .venv-b1-gpu/bin/python \
  torch==2.7.1 --index-url https://download.pytorch.org/whl/cu128
uv pip install --python .venv-b1-gpu/bin/python -r requirements-data-semantic.txt
uv pip check --python .venv-b1-gpu/bin/python

.venv-b1-gpu/bin/python scripts/download_model.py \
  --repo-id Qwen/Qwen3-4B \
  --revision 1cfa9a7208912126459214e8b04321603b3df60c \
  --model-path models/qwen3-4b
```

Model chạy từ local path, offline/local_files_only, BF16, non-thinking, context 16.384/output 2.048 token; không gọi API. Chỉ chạy một pipeline model CUDA tại một thời điểm trên GPU hiện tại. Không dùng CPU fallback âm thầm.

PostgreSQL local hiện có binaries/cluster riêng trong `.cache/postgresql`, Unix socket port 55432, không phải system service. Trên Ubuntu 26.04 amd64, có thể chuẩn bị binaries bằng `python3 scripts/install_local_postgres.py` rồi `python3 scripts/local_postgres.py init`. Script cài local dùng các package đã pin; trên OS khác dùng PostgreSQL hiện có và đặt DSN phù hợp. Không chạy init trên cluster đang có.

## Chạy offline và xem kết quả

```bash
python3 scripts/local_postgres.py start
export DATAMATE_PG_DSN="$(python3 scripts/local_postgres.py dsn)"

.venv-b1-gpu/bin/python scripts/prepare_pipeline.py \
  --partition train --output-dir outputs/data_pipeline/train_new

.venv-b1-gpu/bin/python scripts/summarize_semantic.py \
  --run-dir outputs/data_pipeline/train_new

.venv-b1-gpu/bin/python scripts/validate_pipeline.py \
  --run-dir outputs/data_pipeline/train_new \
  --output outputs/data_pipeline/train_new/database_validation.json
```

Chọn output chưa tồn tại; không ghi đè `train` đã chốt. Nguồn CSV/XLSX/SQLite, calendar, weights và virtualenv không được commit; trên máy khác phải chuẩn bị lại đúng source manifest/frozen split và model trước khi chạy.

Xem semantic không cần PostgreSQL:

```bash
.venv-b1-gpu/bin/python scripts/inspect_pipeline.py \
  --prepared-dir outputs/data_pipeline/train/biz_107 --semantic
```

Liệt kê bảng hoặc xem dữ liệu qua PostgreSQL:

```bash
.venv-b1-gpu/bin/python scripts/inspect_pipeline.py \
  --prepared-dir outputs/data_pipeline/train/biz_107

.venv-b1-gpu/bin/python scripts/inspect_pipeline.py \
  --prepared-dir outputs/data_pipeline/train/biz_107 \
  --table sobanhang_48d2074d_clean --preview --limit 5
```

Mỗi business có catalog.json, postgres_catalog.json, semantic_plan.json, semantic_review.md/json, normalization_errors.jsonl và tables/*.jsonl. `_semantic/<biz>/model_calls.jsonl` giữ raw prompt/output/timing/error. `run.json`, `semantic_overview.md/json`, `database_validation.json` tổng hợp cả run. Snapshot nhỏ trong artifacts chỉ để review, không thay database hoặc dữ liệu giao dịch.

## Kiểm tra và xử lý lỗi

```bash
export DATAMATE_TEST_PG_DSN="$DATAMATE_PG_DSN"
.venv-b1-gpu/bin/python -m unittest discover -s tests -q
```

- Không thấy CUDA: kiểm tra driver/WSL/GPU access; không đổi global Python hoặc tự chuyển sang CPU model lớn.
- Không kết nối PostgreSQL: start cluster, export DSN đúng; trên sandbox có thể cần quyền truy cập socket local.
- Output đã tồn tại: chọn thư mục mới, không xóa bản train đã chốt để chạy thử.
- Context/token budget hoặc JSON lỗi: xem model_calls.jsonl; refinement lỗi giữ kết quả ban đầu, không retry vô hạn.
- Unresolved/role_conflict: đọc checks/evidence; không mặc định cột vô dụng hay dữ liệu bằng 0.
- Dữ liệu không khớp frozen manifest: không tự tái tạo split để vượt lỗi. Các file HTML/history phụ có thể làm full-source verify phát hiện extras; pipeline vẫn kiểm tra chính xác các nguồn được hỗ trợ và README/calendar/rulebook đã chọn.

`DATAMATE_PG_DSN` dùng cho ingestion và tool inspect có truy vấn cố định. Trước bước agent thực thi SQL tự sinh cần role SELECT riêng, SQL validation và giới hạn truy vấn theo kế hoạch; chưa có trong pipeline này.
