# Baseline calculation plan (opt-in, đọc output pipeline v5)

Triển khai §11–§12 của [thiết kế](DATA_PIPELINE_REVISION_DESIGN.md). Trạng thái: **code + test synthetic và integration PostgreSQL đã chạy với fake model; chưa có inference Qwen thật, chưa có PA/VRAM/latency mới**. Baseline v2 (`scripts/run_baseline.py`, `configs/baseline_grounded_model.json`) giữ nguyên để đối chiếu.

## 1. Phiên bản và phạm vi

| Thành phần | Giá trị |
|---|---|
| Version | `baseline-calcplan-v1`, prompts `calcplan-prompts-v1` |
| Code | `src/datamate_baseline/calcplan/` (subpackage riêng; code hash v2 không đổi) |
| Config | `configs/baseline_calcplan.json` |
| CLI | `scripts/run_calcplan.py` |
| Đầu vào | Business có `manifest.json` với `status=published` (audit pass, đã nạp DB) từ [pipeline v5](SEMPLAN_PIPELINE.md) |

`PublishedContextLoader` kiểm tra hash của mọi artifact, so README/rulebook/calendar với snapshot đã publish, và kiểm revision giữa plan, quality và manifest khớp nhau. Nó từ chối output draft, prepared-only, audit-failed hoặc bị sửa sau publish. Manifest được pin khi bắt đầu run và kiểm lại trước mỗi câu.

## 2. Luồng một câu

```text
Câu gốc + as_of_date
→ understand: rewrite + analysis (1 lượt model, tối đa 1 retry contract, đều tính vào ngân sách)
→ thời gian: extractor + TimeResolver bằng Python (rulebook §2, calendar.csv cho Tết, cắt tại as_of_date)
→ bind: model gọi tool hoặc trả calculation plan / missing_data / ambiguous
→ validate plan + ground literal + profile JOIN chưa biết
→ quality + coverage gate
→ model sinh SQL từ plan đã chốt
→ AST gates (checked_sql) + plan–SQL checks → EXPLAIN (không ANALYZE) → thực thi read-only
→ kiểm shape/type/NULL → làm tròn theo rulebook §9 → serializer
```

- **Understand** luôn giữ câu gốc. Rewrite bị bỏ nếu đổi số, thêm năm, hay đổi chữ ngưỡng (trên/hơn/quá/dưới so với "từ … trở lên/ít nhất/tối đa"). Toán tử được sửa theo chữ của câu hỏi (rulebook §2) và có ghi chú. Ngưỡng không có trong câu hỏi là lỗi contract. Năm câu hỏi không nêu thì bị xóa để resolver áp luật "tháng/quý không ghi năm". Không còn bước `binding_candidates` theo alias.
- **Thời gian**: extractor theo mẫu (có dấu hoặc không dấu) thắng model khi cùng một cụm; model chỉ bổ sung cụm extractor không bắt được. Hỗ trợ: ngày, hôm nay/hôm qua/hôm kia, từ X đến Y (gồm 2 đầu), đến hết ngày X, N ngày gần nhất, tuần này/trước, tháng (có/không năm, này/trước), quý, năm, từ đầu năm, Tết năm Y và trước Tết. Câu so sánh có thêm kỳ trước (`T1P`). "Tháng này so với tháng trước" dùng cùng số ngày. Kỳ hoàn toàn sau `as_of_date` → `AMBIGUOUS_TIME`; kỳ chưa hỗ trợ → `UNSUPPORTED_TIME`. Không dùng đồng hồ máy.
- **Ground literal**: lọc trạng thái dùng nhãn trong từ vựng của domain (kể cả nhãn chưa xuất hiện trong dữ liệu); nhãn raw đã map được đổi sang nhãn chuẩn. Literal text (tên hàng/khách) phải khớp chính xác hoặc khớp duy nhất sau chuẩn hóa hoa thường/dấu, tra trên toàn bộ distinct (tối đa 20.000 giá trị). Candidate fuzzy/contains không bao giờ được coi là khớp: có candidate → `AMBIGUOUS_LITERAL`; đã quét hết mà không có → `LITERAL_NOT_FOUND`.
- **Quality gate** gọi `quality.assess` trên các dependency suy ra từ plan (cột, operation, khung thời gian). Nếu mọi khung thời gian nằm hoàn toàn ngoài khoảng ngày của bảng → `unanswerable` (rulebook §7, B59). Nếu một issue blocking có thể ảnh hưởng → abstain `QUALITY_BLOCKED`.
- **Kết quả**: chỉ khi aggregate là sum/count, dữ liệu phủ kỳ hỏi và gate không chặn thì scalar NULL mới thành 0. NULL của avg/min/max là lỗi. Percent làm tròn 2 chữ số HALF_UP. Số tiền lẻ (kể cả trung bình tiền) làm tròn về đồng; trung bình khác làm tròn 2 chữ số.

## 3. Calculation plan

Ví dụ "Tháng 3/2026 có bao nhiêu đơn có doanh thu từ 55.000 đồng trở lên?":

```json
{"answer": {"type": "number", "shape": "scalar", "columns": null},
 "stages": [
  {"id": "s1", "from": "sales_clean", "joins": [], "time": [{"interval": "T1", "column": "ngay"}],
   "filters": [{"column": "trang_thai", "op": "not_in", "value": ["cancelled", "returned"], "evidence": ["B3"]}],
   "group_by": ["ma_don"], "aggregates": [{"name": "order_total", "fn": "sum", "column": "thanh_tien"}],
   "having": [{"aggregate": "order_total", "op": ">=", "value": 55000}]},
  {"id": "s2", "from": "s1", "joins": [], "time": [], "filters": [], "group_by": [],
   "aggregates": [{"name": "value", "fn": "count", "column": "*"}], "having": []}],
 "output": {"select": [{"name": "value", "expr": "s2.value"}], "order_by": [], "limit": null, "round": null},
 "evidence": ["B3"], "feasibility": {"status": "feasible", "missing": []}}
```

Bound thời gian luôn lấy từ resolver theo ID interval; model không tự viết ngày. Validator báo lỗi theo mã:

- tham chiếu sai: `PLAN_UNKNOWN_COLUMN`, `PLAN_UNKNOWN_SOURCE`, `PLAN_UNKNOWN_INTERVAL`, `PLAN_TIME_COLUMN_INVALID`, `PLAN_FILTER_VALUE_TYPE`, `PLAN_UNKNOWN_OUTPUT_REF`, `PLAN_UNKNOWN_EVIDENCE`;
- ngưỡng: `PLAN_THRESHOLD_NOT_PRESERVED` (thiếu ngưỡng câu hỏi nêu), `PLAN_THRESHOLD_LEVEL_MISMATCH` (điều kiện trên tổng mỗi đơn mà đặt thành bộ lọc dòng);
- tổng hợp sai: `PLAN_NON_ADDITIVE_SUM` (cộng đơn giá), `PLAN_REPEATED_GROUP_VALUE_SUM` (cộng giá trị lặp của đơn mà chưa gom theo đơn), `PLAN_FANOUT_RISK` (JOIN phía nhiều trước khi tổng hợp);
- JOIN: `SELF_JOIN_UNSUPPORTED`, `PLAN_JOIN_TYPE_MISMATCH`;
- shape: `PLAN_PROJECTION_COUNT`, `PLAN_SCALAR_FROM_GROUPED_STAGE`, `PLAN_SHAPE_TYPE_MISMATCH`.

`missing_data` chỉ được nhận khi không cột nào mang khái niệm đó có dữ liệu (kiểm inventory theo role/nghĩa/tên cột) **và** claim trích evidence README/rulebook có thật. Nếu không thỏa, claim bị trả về để sửa.

## 4. Tools và ngân sách

| Tool | Ai gọi | Ghi chú |
|---|---|---|
| `describe_tables`, `get_source_context` | model | Từ context đã publish, không đọc DB |
| `get_column_values`, `resolve_value` | model / code | Distinct values kèm số đếm, coverage, cờ truncated, domain trạng thái |
| `preview_rows` | model | Tối đa 5 dòng; lấy mẫu theo nhóm khóa; có `sample_only` |
| `profile_keys_and_joins` | model / code | Uniqueness, orphans, cardinality, fanout bằng SQL cố định |
| `resolve_time`, `check_data_requirements` | code | Resolver; quality registry + coverage |
| `explain_sql`, `execute_sql` | code | AST gate, role SELECT-only, transaction READ ONLY, timeout, cap; lỗi thì rollback |

Ngân sách mỗi câu (đều là trần cứng; dùng hết là abstain `BUDGET_EXHAUSTED`):

| Ngân sách | Mức |
|---|---|
| Lượt gọi model (gồm retry contract) | 8 |
| Vòng sửa (dùng chung plan và SQL) | 2 |
| Lượt gọi tool logic (gồm cache hit và tool bị từ chối) | 12 |
| Lần thực thi SQL trả lời | 3 |
| Lượt bind | 5 |

Timeout SQL trả lời là 10 giây, tool metadata 5 giây, tối đa 1.000 dòng kết quả. `unverified_critical=abstain`: check quan trọng không kiểm được thì không bị coi là pass.

## 5. Sửa đúng tầng

| Lỗi | Hành động |
|---|---|
| Plan sai contract/tham chiếu/ngưỡng/aggregation/JOIN, literal mơ hồ, missing_data không đủ căn cứ | Sửa plan (kèm mã lỗi, evidence, tool results) |
| `SQL_GATE`, `PLAN_SQL_MISMATCH`, lỗi EXPLAIN/thực thi, `RESULT_SHAPE` | Giữ nguyên plan, chỉ sửa SQL (kèm SQL cũ và lỗi cụ thể) |
| Timeout (`57014`) | Abstain `QUERY_TIMEOUT`, không thử lại |
| Hết vòng sửa | Abstain với mã lỗi cuối cùng |

Plan–SQL checks (`plan_sql.py`) chấp nhận các dạng tương đương: `>=`/`<` hoặc `BETWEEN` (DATE), so sánh bị lật vế, `CAST`/`DATE '…'`, `NOT IN` và `NOT … IN`, `HAVING SUM(...)` hoặc `WHERE alias` ở truy vấn ngoài. Dạng ngoài phạm vi trả `unsupported`, không trả pass.

## 6. Lý do abstain và unanswerable

- **`unanswerable`**: `MISSING_DATA` (đã kiểm inventory và có evidence), `OUTSIDE_DATA_COVERAGE`.
- **`abstain`**: `AMBIGUOUS_QUESTION`, `AMBIGUOUS_TIME`, `UNSUPPORTED_TIME`, `AMBIGUOUS_LITERAL`, `LITERAL_NOT_FOUND`, `QUALITY_BLOCKED`, `PLAN_INVALID`, `PLAN_SQL_MISMATCH`, `SQL_GATE`, `SQL_ERROR`, `QUERY_TIMEOUT`, `RESULT_SHAPE`, `UNVERIFIED_CRITICAL_CHECK`, `SELF_JOIN_UNSUPPORTED`, `CAPABILITY_UNSUPPORTED`, `MODEL_CONTRACT`, `CONTEXT_BUDGET`, `BUDGET_EXHAUSTED`, `CONTEXT_CHANGED`.

## 7. Output và trace

Run cho ra cùng các file như v2 (`questions.jsonl`, `predictions.jsonl`, `timings.jsonl`, `traces.jsonl`, `run.json`), nên `evaluate_baseline_run.py`, `summarize_baseline.py` và `compare_baseline_runs.py` dùng được ngay. `run.json` ghi thêm ngân sách, hash code calcplan và hash manifest của từng business.

Trace giữ các trường của v2 và thêm:

- `question_analysis`, `analysis_corrections`, `time_resolution`;
- `tool_calls` (tham số, kết quả rút gọn, cache, thời gian);
- `plan_attempts` (từng plan kèm issues), `schema_linking` (plan đã chốt), `literal_grounding`;
- `quality_gate`, `plan_sql_checks`, `explain`, `query_result`;
- `budgets` (đã dùng/giới hạn, danh sách vòng sửa theo tầng), `decision` (lý do và chi tiết).

## 8. Lệnh chạy

```bash
python3 scripts/local_postgres.py start
export DATAMATE_PG_DSN="$(python3 scripts/local_postgres.py dsn)"
# Role SELECT-only cho các view của output v5 (script v2 dùng được với output v5)
export DATAMATE_BASELINE_PG_DSN="$(.venv-b1-gpu/bin/python scripts/setup_baseline_readonly.py \
  --prepared-root outputs/semplan/development_v5 --print-dsn)"

# Một câu
.venv-b1-gpu/bin/python -u scripts/run_calcplan.py --prepared-root outputs/semplan/development_v5 \
  --business-id biz_118 --question '<câu hỏi>' --as-of-date <YYYY-MM-DD> --output-dir outputs/calcplan/single_01

# 5 câu đầu của 50 ID đã chốt, rồi đủ 50 câu
.venv-b1-gpu/bin/python -u scripts/run_calcplan.py --prepared-root outputs/semplan/development_v5 \
  --questions-file artifacts/data_split_v1/inputs/development/questions.jsonl \
  --question-ids artifacts/baseline_grounded_v2/question_ids.json --limit 5 --output-dir outputs/calcplan/dev50_smoke5
.venv-b1-gpu/bin/python -u scripts/run_calcplan.py --prepared-root outputs/semplan/development_v5 \
  --questions-file artifacts/data_split_v1/inputs/development/questions.jsonl \
  --question-ids artifacts/baseline_grounded_v2/question_ids.json --output-dir outputs/calcplan/development50

# Chấm, báo cáo, so với một run khác trên cùng 50 ID
.venv-b1-gpu/bin/python scripts/evaluate_baseline_run.py --run-dir outputs/calcplan/development50 --partition development --subset
.venv-b1-gpu/bin/python scripts/summarize_baseline.py --run-dir outputs/calcplan/development50
.venv-b1-gpu/bin/python scripts/compare_baseline_runs.py --before outputs/baseline/development50_v2_next \
  --after outputs/calcplan/development50 --output-dir outputs/calcplan/compare_v2_vs_calcplan

# Ablation 2 của thiết kế: baseline v2 trên dữ liệu v5 (qua sidecar tương thích)
.venv-b1-gpu/bin/python -u scripts/run_baseline.py --config configs/baseline_grounded_model.json \
  --prepared-root outputs/semplan/development_v5 \
  --questions-file artifacts/data_split_v1/inputs/development/questions.jsonl \
  --question-ids artifacts/baseline_grounded_v2/question_ids.json --output-dir outputs/baseline/development50_v2_on_v5
```

`compare_baseline_runs.py` cần cả hai run đã hoàn tất và đã chấm, trên cùng một tập câu hỏi.

## 9. Kiểm thử đã chạy

`tests/test_calcplan.py` có 32 test, gồm 4 integration PostgreSQL trên business v5 đã publish với role SELECT-only riêng. Các test bao phủ:

- resolver đối chiếu từng luật của §2, cắt tại as_of, Tết và trước Tết, so sánh cùng số ngày, câu không dấu;
- rewrite/ngưỡng strict-inclusive, ngưỡng tự bịa, năm tự thêm, extractor thắng model;
- validator plan: ngưỡng phải nằm trong HAVING, đơn giá và giá trị lặp không được cộng, fanout, self-join, interval lạ, shape;
- plan–SQL: dạng tương đương pass; sai bound, ngưỡng theo dòng, thừa cột, bảng lạ đều fail; check unsupported thì không pass;
- runner: đường chuẩn; sửa SQL giữ nguyên plan; sửa plan; ngân sách cứng (kể cả model gọi `execute_sql`); ground literal trạng thái/text; ngoài coverage và quality block; missing_data; NULL sum → 0 nhưng NULL avg thì không; timeout; ca sáng/chiều; làm tròn;
- integration: tool literal thật, EXPLAIN không thực thi (`1/(count-count)`), rollback sau lỗi, role không ghi được, trả lời end-to-end bằng SQL thật, `summarize_baseline.py` chạy được trên output batch.

## 10. Giới hạn còn lại

- Prompt/contract mới chỉ kiểm bằng fake model. Plan nhiều stage có thể khó với Qwen3-4B; cần smoke vài câu rồi xem `plan_attempts` trước khi chạy batch.
- Chưa hỗ trợ: lọc theo ca sáng/chiều (`CAPABILITY_UNSUPPORTED`), so với cùng kỳ năm trước, câu cần date parts của ngày partial (gate chặn khi có thể ảnh hưởng), semantics snapshot "tồn/nợ đến hết ngày X" ngoài khung thời gian.
- Plan–SQL checker chưa kiểm số học trong công thức output, thứ tự ORDER BY và tie-break theo mã; với các phần này, SQL trả lời chỉ được kiểm ở mức projection và LIMIT.
- `contains`/`is_null` trong bộ lọc là `unsupported`, nên dẫn tới abstain.
- Model gọi tool qua action JSON (tối đa 3 request mỗi lượt), chưa dùng function-calling native.
- Ngân sách và context mới là giá trị khởi đầu; chưa đo token/latency/VRAM thật. Payload bind gửi toàn bộ schema và README, có thể lớn với business nhiều bảng.
