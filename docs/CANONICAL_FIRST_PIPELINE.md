# Pipeline hiện tại — canonical first

## Luồng nhận diện cột

1. Parser/adapter giữ tên và giá trị nguồn. Rule-based so tên cột đã chuẩn hóa với toàn bộ `ALIASES`. Nếu chưa nhận diện được, mô tả README trùng một alias cũng được dùng làm bằng chứng. Các quy tắc README cụ thể có sẵn vẫn áp dụng.
2. Cột đã có canonical role được giữ nguyên và không gửi làm mục tiêu phân tích SLM. `semantic_role` của các cột đó là `null`.
3. Với cột còn thiếu mapping, SLM nhận mô tả README, profile toàn cột, cột/bảng cùng business, rulebook liên quan, định nghĩa mọi canonical role và ví dụ alias tiếng Việt. Payload dùng ba ví dụ tên đầu tiên mỗi role để giữ context vừa ngân sách; rule-based vẫn dùng toàn bộ alias.
4. SLM phải ưu tiên đối chiếu **ý nghĩa** với canonical role, không yêu cầu tên cột giống alias. Chỉ đề xuất semantic role mới nếu không có canonical phù hợp. Đây là suy luận model, không phải bằng chứng chắc chắn về nghiệp vụ.
5. `resolve_canonical_role()` nhận tên role chuẩn hoặc tên alias (chuẩn hóa dấu/cách viết). Nếu model đặt role chuẩn/alias vào `semantic_role`, validator chuyển nó sang canonical và xóa semantic role. Audit giữ `proposed` gốc và `normalized_proposal` đã sửa vị trí.
6. Không bỏ qua kiểm tra bằng chứng, kiểu toàn cột hay xung đột với cột khác. Nếu hai trường chứa hai canonical role khác nhau, ghi `conflicting_canonical_role_fields` thay vì chọn tùy ý.
7. Canonical đã được áp dụng có `semantic_role = null`; ý nghĩa và nguồn bằng chứng vẫn nằm trong catalog/review. Role mới ngoài canonical chỉ là semantic metadata, không tự tạo JOIN, công thức hay quy đổi.

Những tên không thuộc canonical, không có ý nghĩa rõ vẫn có thể `unresolved`. Sửa vị trí trường không đồng nghĩa mọi dự đoán của SLM đều đúng.

## Nhận diện trạng thái khác với chuẩn hóa giá trị

Đã bỏ điều kiện chặn cố định `status_vocabulary_requires_explicit_mapping`.

- Xác định cột có role `status` dựa trên tên/mô tả/bằng chứng, độc lập với việc đã biết toàn bộ giá trị.
- Giá trị đã có trong `STATUSES` tiếp tục chuyển về nhãn chuẩn.
- Giá trị chưa có mapping được giữ nguyên, ghi note `unmapped_status_preserved` ở bản ghi và thống kê `normalization_notes`.
- Không tự gán trạng thái lạ thành completed/cancelled/returned. Agent sau này cần kiểm tra rulebook và các note này trước khi lọc số liệu theo trạng thái.

`biz_107`: tên “Trạng thái xử lý” và mô tả “trạng thái đơn” được nhận diện chung vào canonical `status`, không hard-code theo business ID.

## Code và kiểm tra

- `src/datamate_data/normalization.py`: alias, mô tả README và giữ nguyên giá trị trạng thái chưa biết.
- `src/datamate_data/semantic.py`: canonical-first prompt, alias examples, sửa role đặt nhầm trường và validator.
- `tests/test_semantic_pipeline.py`, `tests/test_data_pipeline.py`: regression tests; 106 tests đã qua với integration PostgreSQL.
- Phiên bản semantic `semantic-v4`, prompt `canonical-first-alias-meaning-v8`; cache model v3 không được giả làm kết quả mới.

## Chạy lại offline

Dùng model local Qwen3-4B và `.venv-b1-gpu` hiện có, không cần tải thư viện/weights mới.

```bash
python3 scripts/local_postgres.py start
export DATAMATE_PG_DSN="$(python3 scripts/local_postgres.py dsn)"

.venv-b1-gpu/bin/python scripts/prepare_pipeline.py \
  --partition train \
  --output-dir outputs/data_pipeline/train_new

.venv-data/bin/python scripts/summarize_semantic.py \
  --run-dir outputs/data_pipeline/train_new

.venv-data/bin/python scripts/validate_pipeline.py \
  --run-dir outputs/data_pipeline/train_new \
  --output outputs/data_pipeline/train_new/database_validation.json
```

Chọn output chưa tồn tại. Bản hiện tại nằm tại `outputs/data_pipeline/train` và đã bao gồm refinement có giới hạn; không còn các bản train đối chiếu/backup trong outputs.

## Bản đã chốt

115 tests passed; 18 doanh nghiệp, 80 bảng, 2.040.280 dòng nguồn đã nạp và đối chiếu PostgreSQL. Semantic: existing_mapping=464, applied_candidate=18, described_other=168, unresolved=15, role_conflict=6. Chưa đo QA accuracy.

Xem [refinement](BOUNDED_REFINEMENT_EXPERIMENT.md), [validation](DATA_PIPELINE_VALIDATION.json) và [bộ báo cáo nhỏ để commit Git](../artifacts/data_pipeline_final/README.md). Dữ liệu full nằm local và được Git ignore. Các PostgreSQL schema cũ được giữ nguyên; dọn output không DROP database objects.
