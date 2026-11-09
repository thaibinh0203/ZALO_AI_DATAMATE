# Pipeline v5 — semantic plan thống nhất (opt-in)

Triển khai [thiết kế đã review](DATA_PIPELINE_REVISION_DESIGN.md). Trạng thái: **code + test synthetic/integration PostgreSQL đã chạy; chưa load model thật, chưa rebuild 18 train/6 development, chưa đo PA**. Pipeline v4 (`datamate_data/*.py`), baseline v2 và các output cũ không bị sửa.

## 1. Phiên bản và cách giữ v4/v2

| Thành phần | Giá trị |
|---|---|
| Pipeline | `postgres-pipeline-v5-semplan` (`src/datamate_data/semplan/`) |
| Semantic plan | `business-semantic-plan-v1` |
| Manifest publish | `published-business-v1` |
| Row decisions / checks / quality | `row-decisions-v1`, `scoped-checks-v1`, `quality-registry-v1` |
| Ngày / trạng thái | `partial-date-v1`, `scoped-status-v1` |
| Config | `configs/pipeline_semplan.json` |
| CLI | `scripts/prepare_semplan.py`, `scripts/inspect_semplan.py` |

Code mới nằm trong subpackage nên `implementation_hash()` của v4 (chỉ hash `datamate_data/*.py`) và `code_sha256` trong run v2 (chỉ `datamate_baseline/*.py`) không đổi. v5 dùng lại parser, adapter, `column_spec`/`normalize_value`, gate cột của v4 (`validate_column`, `refine_column`, collision guards) và registry quy đổi, nhưng không sửa chúng. Schema PostgreSQL mới có tiền tố `dm5_`; schema `dm_` cũ không bị đụng.

## 2. Luồng P0–P9 và module

| Bước | Việc | Module |
|---|---|---|
| P0 | Parse CSV/XLSX/SQLite, header/layout, giữ raw + vị trí + cell metadata | `staging.py` (dùng `parsers`, `adapters` v4) |
| P1 | Phân loại sơ bộ từng dòng; dòng chưa rõ là `candidate` | `rows.py` |
| P2 | Parse thử từng ô theo spec rule-based, ghi `attempt`; profile có scope | `typed.py`, `temporal.py`, `checks.py` |
| P3 | Review nhóm dòng candidate → nghĩa cột (contract v4) → table plan | `planner.py`, `prompts.py`, `model.py` |
| P4 | Tính lại checks khi row kind/spec đổi (revision mới) | `checks.py` |
| P5 | Tối đa **1** refinement cấp bảng khi checks mâu thuẫn proposal | `planner.refine` |
| P6 | Chốt row decisions, status mapping, policy (enabled/disabled/needs_review) | `planner.py`, `policies.py`, `status.py` |
| P7 | Chuẩn hóa cuối theo policy đã bật | `materialize.py` |
| P8 | Nạp schema `dm5_*` mới + audit | `postgres.py` |
| P9 | Manifest bất biến, sidecar tương thích v2, review | `publish.py`, `build.py` |

P0–P2 không fill-forward, không đánh dấu trùng, không quy đổi. Mỗi check ghi `dataset_stage`, `row_selection` và `semantic_revision`. Revision là hash của row decisions + column specs, nên đổi quyết định là checks cũ không được dùng lại.

## 3. Phân loại dòng

Luật chỉ quyết các cấu trúc rõ: dòng trống; header lặp chính xác; dòng CSV lệch cột (`quarantine`); nhãn tổng có ô định danh trống (`subtotal`, hoặc `grand_total` nếu nằm sau dòng data cuối của part); ghi chú/metadata có tiền tố rõ. Không loại dòng chỉ vì có chữ "tổng" hay thiếu số lượng. Ví dụ "Tổng hàng khuyến mãi" trong cột tên hàng mà đơn có mã vẫn là `data`; dòng phí không có số lượng vẫn là `data`.

Dòng chưa rõ (nhãn tổng nhưng còn định danh, dòng chỉ có số, ô dài đơn lẻ, header lặp một phần) là `candidate`. Chúng được gom nhóm theo rule/nhãn/mẫu ô có giá trị, mỗi nhóm kèm sample đa dạng (`S*`), rồi gửi model review theo nhóm. Validator chỉ nhận nhóm có thật, ngoại lệ phải là sample của chính nhóm đó, evidence phải nằm trong catalog. Model nói "data" cho dòng chỉ có số mà không có định danh thì bị bỏ qua. Nhóm không được quyết định (hoặc vượt `row_review_max_groups`) thành `unknown` với trạng thái `needs_review`. Dòng `unknown` vẫn nằm trong bảng raw, không vào view phân tích, chặn fill-forward đi xuyên qua, và được ghi vào quality registry kèm khoảng ngày nếu biết.

## 4. Semantic plan của business (`semantic_plan.json`)

Mỗi bảng gồm: `columns` (canonical/semantic role, kiểu, nghĩa, audit v4, `date_order`, `status_domain`), `grain` (`kind` theo vocabulary chuẩn; `other` + `semantic_kind` khi hiểu mà chưa có tên; `unknown` khi thiếu bằng chứng), `row_identity` (`supported_by_data_check` / `not_established` / `not_proposed`), `group_keys`, `measures` (`measure_scope`, `aggregation` ∈ sum / sum_once_per_group / weighted_average / average / snapshot_latest / min_max / none / unknown, `time_semantics`, `weights_column`), `joins` (kèm `data_checks` overlap/orphans/cardinality/fanout, `business_verified=false`), `status_mappings`, `policies`, `row_decisions`, `refinement`, `rejected_items` và `revision`.

Evidence là ID tham chiếu: `R<n>` (dòng README), `B<n>` (dòng bảng/đoạn rulebook), `S…` (dòng nguồn được lấy mẫu), `C…` (kết quả check). Code tự nhận diện các điều luật cần cho policy và trích ID của chúng: nhóm dòng để trống (B62), trạng thái đơn (B63), dòng trùng trong cùng đơn (B64), bảng không ghi theo đơn (B65), ngày không ghi năm (B66), đơn vị không quy đổi có hệ số 1 (B78), kỳ ngoài dữ liệu (B59), bản ghi sau hôm nay (B2). Số ID theo rulebook train hiện tại.

Checks có thể làm proposal bị coi là mâu thuẫn: identity bị trùng/null; group key luôn null; `sum` theo dòng nhưng giá trị lặp nguyên trong cùng đơn (gợi ý `sum_once_per_group`); `sum_once_per_group` nhưng giá trị khác nhau trong nhóm; fill-forward không có continuation hoặc có xung đột. Khi có mâu thuẫn, planner gửi đúng một request refinement (kèm check ID và sample theo nhóm). Mục nào còn mâu thuẫn sau refinement thì bị hạ cấp (`aggregation=unknown`, identity bị bỏ, join `rejected_by_checks`), không bị bỏ qua lặng lẽ. Đơn giá/giá vốn đơn vị không bao giờ được `sum`.

## 5. Processing policy

| Operation | Bật khi | Không bật khi |
|---|---|---|
| `fill_forward` (mode `key`) | Có continuation thật (dòng sau để trống khóa/thuộc tính đơn), không có giá trị xung đột, dòng tiếp có ô dòng hàng, có evidence (B62 hoặc README) | Không có continuation → `disabled`; có xung đột/thiếu bằng chứng → `needs_review` |
| `mark_duplicates` | Dòng giống nhau ở mọi ô trong **cùng một đơn**, có B64 | Bảng không ghi theo đơn → `disabled` (B65); hai giao dịch giống nhau vẫn giữ cả hai |
| `derive_base_quantity` | Có số lượng + định danh hàng + bằng chứng quy đổi (README/bảng quy đổi/B78) | Thiếu operand/bằng chứng; xung đột quy đổi để `NULL` và ghi registry |
| `apply_status_mapping` | Xác định được domain của cột trạng thái | Không rõ domain → `needs_review`, giữ raw |
| `resolve_source_year` | Đúng một năm từ sheet (ưu tiên theo B66), dòng tiêu đề hoặc tên file | Không có năm hoặc các nguồn mâu thuẫn |

Fill-forward chỉ điền ô raw rỗng trong cùng nhóm. Nó reset khi đổi part, gặp dòng trống, gặp dòng không phải data/unknown, hoặc gặp khóa nhóm mới. Không bao giờ chép giá trị để "sửa" ô parse lỗi. Mọi ô được điền đều có note `filled_within_group` kèm `source_row_id` và `policy_id`. Model chỉ được đề xuất `fill_forward`/`mark_duplicates`; code chạy detector theo rulebook rồi quyết định bằng checks.

## 6. Ngày partial

Cột DATE/TIMESTAMP chỉ chứa ngày đầy đủ. Các companion cho phép truy vấn những gì đã biết: `<col>__y`, `__m`, `__d`, `__p` (precision: `datetime`/`day`/`month_day`/`month`/`year`) và `__r` (resolution: `explicit`, `native`, `excel_serial`, `year_from_sheet|header|file`, `missing_year`, `conflicting_year`, `ambiguous_day_month`).

- `1/7` không có năm là `month_day`/`missing_year`. `29/2` chưa có năm vẫn là partial; nếu năm của sheet không có ngày 29/2 thì là lỗi. `31/2` là lỗi.
- Thứ tự dd/mm hay mm/dd quyết theo cả cột: README nêu định dạng → giá trị chỉ hợp lệ một chiều → mặc định dd/mm. Cột có cả hai chiều thì các giá trị mơ hồ thành `ambiguous_day_month`.
- Không bao giờ dùng năm của máy hay năm trong câu hỏi để sửa ngày nguồn.

## 7. Status theo domain

Domain là order/delivery/purchase/payment/product/customer, mỗi domain có vocabulary riêng (order = completed/cancelled/returned/delivering theo B63). Từ điển v4 chỉ áp dụng trong đúng domain: "Đã giao" của một sự kiện giao là `delivered`, không phải đơn `completed`. Domain do model đề xuất phải khớp cấu trúc (tên cột, grain, role hint), không khớp thì không áp dụng. Vocabulary lấy từ toàn cột kèm số đếm, giới hạn 200 giá trị và có cờ `truncated`. Giá trị lạ được model review với evidence README/rulebook; giá trị chưa rõ giữ raw. View có thêm `<col>__raw` chứa text gốc.

## 8. Quality registry (`quality_registry.json`)

Các loại issue: `unknown_rows`, `parse_errors`, `partial_dates`, `ambiguous_dates`, `unmapped_status`, `status_mapping_needs_review`, `base_quantity_unresolved`, `base_quantity_conflict`, `conversion_conflicts`, `potential_duplicates`, `fill_forward_needs_review`, `column_semantics_unresolved`. Mỗi issue có view/cột/số dòng/row refs, các operation bị ảnh hưởng, `known_bounds` (khoảng ngày, tháng của ngày partial) và `unknown_bounds`.

`quality.assess(registry, dependencies)` trả `pass` / `warn` / `block`. Một issue chỉ được bỏ qua khi chứng minh được nó không chạm cột/operation đang dùng, hoặc nằm ngoài cửa sổ thời gian. Nếu không loại trừ được thì kết quả là `cannot_exclude`, tức block.

## 9. Publication, audit, replay

- `manifest.json` được ghi cuối cùng, chứa hash mọi artifact, phiên bản code/config/model, nguồn, schema DB và trạng thái `published` / `audit_failed` / `prepared_not_loaded`. Chỉ `published` mới dùng được cho baseline. Các file được đặt read-only; `load_manifest()` từ chối nếu artifact bị sửa sau khi publish.
- Audit sau khi nạp gồm: số dòng theo kind, số dòng trong view, tổng NUMERIC chính xác (Decimal), tổng `__quantity_base`, tính nhất quán của companion ngày, định nghĩa view (lọc `data`, không duplicate, đúng danh sách cột, không lộ `__raw/__source/__quality`).
- `business_metadata/metadata.json` là sidecar định dạng v2 sinh từ plan, không chạy model mới. Nhờ đó `scripts/run_baseline.py` (v2) đọc được output v5 cho ablation "pipeline mới + baseline cũ".
- `--reuse-plan-root` (replay) chạy lại từ quyết định đã chốt mà không gọi model. Nguồn khác snapshot hoặc khác phiên bản thì bị từ chối. Run mới ghi `previous_run`. Chưa hỗ trợ file "yêu cầu sửa" thủ công.

## 10. Ngân sách model (`model_caps`)

Mỗi item có hạn mức riêng, gồm header 2, nhóm cột 2, refinement cột 1, review dòng 2, table plan 2, table refinement 2 (1 request + 1 retry contract) và status mapping 2, cộng tổng tối đa 600 lần/business. Mọi lần thử đều được tính, kể cả retry và cache hit. Lần bị chặn vì hết ngân sách vẫn ghi `model_calls.jsonl` (`sent=false`) và dùng fallback an toàn (unknown/unresolved). Cache khóa theo identity model + messages, nên đổi nguồn/revision/prompt là cache tự vô hiệu. Không có học alias (`alias_learning=false`).

## 11. Lệnh chạy

Chạy từ root repo trên máy có model/dữ liệu (ví dụ `.venv-b1-gpu`). Output luôn là thư mục mới.

```bash
python3 scripts/local_postgres.py start
export DATAMATE_PG_DSN="$(python3 scripts/local_postgres.py dsn)"

# 1) Thử nhanh 1 business, không model, không DB (không dùng được cho baseline)
.venv-b1-gpu/bin/python scripts/prepare_semplan.py --business-dir docs/train/data/biz_118 \
  --semantic-mode none --prepare-only --output-dir outputs/semplan/smoke_biz118

# 2) Development: model local + PostgreSQL + audit + publish
.venv-b1-gpu/bin/python -u scripts/prepare_semplan.py --partition development \
  --output-dir outputs/semplan/development_v5

# 3) Xem kết quả (không cần model/DB); review.md đọc bằng Markdown preview
.venv-b1-gpu/bin/python scripts/inspect_semplan.py --business-root outputs/semplan/development_v5/biz_118 --view manifest
.venv-b1-gpu/bin/python scripts/inspect_semplan.py --business-root outputs/semplan/development_v5/biz_118 --view plan
.venv-b1-gpu/bin/python scripts/inspect_semplan.py --business-root outputs/semplan/development_v5/biz_118 --view quality
.venv-b1-gpu/bin/python scripts/inspect_semplan.py --business-root outputs/semplan/development_v5/biz_118 \
  --view policies --table <tên bảng trong view plan>

# 4) Rebuild từ quyết định đã chốt, không inference
.venv-b1-gpu/bin/python -u scripts/prepare_semplan.py --partition development \
  --reuse-plan-root outputs/semplan/development_v5 --output-dir outputs/semplan/development_v5_replay
```

`run.json` ở root output ghi trạng thái từng business. `<biz>/model_calls.jsonl` có đủ prompt/raw/lỗi/cache của từng lần gọi.

## 12. Kiểm thử đã chạy

`tests/test_semplan_pipeline.py` (30 test, gồm 2 integration PostgreSQL) bao phủ: subtotal/grand total/header/note/metadata/ragged; sản phẩm chứa chữ "tổng"; dòng phí thiếu số lượng; ngày partial (thiếu năm, 29/2, 31/2, năm của sheet, năm mâu thuẫn, dd/mm mơ hồ); trạng thái đúng/sai domain; validator table plan (đơn giá, `other` + `semantic_kind`, mục sai bị loại); fill-forward reset tại unknown và không sửa lỗi parse; dòng giống nhau ở bảng không theo đơn được giữ; công thức thiếu cache; review dòng/trạng thái có validate; refinement đúng một lần và hạ cấp mục còn mâu thuẫn; hết ngân sách; replay không gọi model; check bị tính lại theo revision; cache/caps; manifest bất biến và phát hiện sửa đổi; version không khớp; nạp/audit/publish thật; baseline v2 đọc output v5; audit lỗi thì không publish.

Đo trên máy này (bảng tổng hợp 199.863 dòng, không model, không DB): khoảng 74 giây, RAM đỉnh khoảng 108 MB; `raw/` 113 MB và `final/` 218 MB. Thời gian và dung lượng cho dữ liệu thật và tập private chưa được đo.

## 13. Giới hạn còn lại

- Prompt và contract chỉ được kiểm bằng fake model; chưa biết Qwen3-4B có tuân thủ đến đâu. Output lỗi rơi về unknown/unresolved, nên độ phủ grain/measure khi chạy thật có thể thấp.
- Measure chưa có trường `currency` và `preconditions` có cấu trúc (danh sách để trống); `additive_dimensions` chưa có.
- Join chỉ được kiểm bằng dữ liệu (overlap/fanout); chưa có bằng chứng nghiệp vụ. Bảng nhiều store chưa tự ghép khóa store vào join model đề xuất.
- Duplicate chỉ xét "giống mọi ô trong cùng đơn" (B64). Bản xuất trùng giữa các file không có mã đơn thì giữ lại, và chỉ báo `potential_duplicates` khi policy ở `needs_review`.
- Fill-forward mode `block` (ví dụ ngày ghi một lần cho cả khối) chỉ chạy khi model đề xuất kèm evidence; không có detector tự động.
- Chưa có file "yêu cầu sửa rõ ràng" cho rebuild; replay chỉ dùng lại quyết định đã chốt.
- Staging/final JSONL khá lớn (khoảng 1,1 KB/dòng ở final). Cần đo dung lượng và thời gian cold-start trên dữ liệu thật trước khi đóng gói Docker.
