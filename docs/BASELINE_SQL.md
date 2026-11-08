# Baseline SQL chạy local

Bản có grain và question grounding: [BASELINE_GROUNDED.md](BASELINE_GROUNDED.md). Config v1 vẫn giữ luồng một lượt model; config grounded bật analysis/linking.

Baseline đã nối dữ liệu pipeline → PostgreSQL → Qwen3-4B → SQL → đáp án JSON → evaluator. Model chạy offline, load một lần cho mỗi batch. Mỗi câu chỉ có một lần gọi model và tối đa một query; chưa có repair, embedding, rerank, query rewriting hay fine-tuning.

## Tình trạng kiểm chứng

Đã chạy 133 tests (pass, không skip), smoke một câu và smoke 20 câu train. Smoke train: 0 đúng, 10 sai, 10 abstain, PA -0,50, không lỗi format. Batch development được dừng theo yêu cầu người dùng sau 169/708 câu; giữ tại `outputs/baseline/development_v1_partial`, chưa chấm điểm full. Bạn có thể tự chạy batch mới theo lệnh bên dưới; không có resume tự động.

## Luồng hoạt động

1. Nhận `id`, `business_id`, `question`, `as_of_date`. File câu hỏi chỉ được có bốn trường này; runner từ chối labels và ID trùng.
2. `ContextLoader` đọc catalog đúng doanh nghiệp, tên clean views/cột SQL, kiểu, canonical/semantic role, mô tả và cảnh báo chất lượng. README và rulebook được đối chiếu hash với pipeline. Metadata cache theo doanh nghiệp; mỗi câu có messages mới.
3. Model nhận toàn bộ schema gọn, README, rulebook, câu hỏi và mốc ngày. Model quyết định dùng bảng nào, điều kiện, phép tổng hợp, JOIN và kiểu đáp án; sinh JSON có `status`, `answer_type`, `sql`, `missing_requirements`. Không có đáp án tham chiếu trong prompt.
4. Python parse JSON và kiểm tra contract. SQLGlot kiểm tra SELECT/CTE, bảng/cột, hàm, thời gian và giới hạn truy cập. QueryTool dùng role chỉ đọc, bind schema đúng doanh nghiệp và chạy SQL trong transaction riêng.
5. PostgreSQL tính trên toàn bộ dòng thỏa điều kiện. Python chuyển kết quả sang `id/type/answer`, giữ Decimal chính xác; không gọi thêm model để viết lại số liệu. Lỗi kỹ thuật hoặc NULL không hợp lệ trở thành `abstain`, không tự thay bằng 0.
6. Khi inference xong, evaluator riêng mới đọc đáp án tham chiếu và chấm kết quả. Trang HTML cho phép xem câu hỏi, SQL, prediction, expected và lỗi sau chấm.

`unanswerable` là model kết luận câu hỏi thiếu dữ liệu theo ngữ nghĩa; `abstain` là hệ thống không đưa được đáp án đáng tin, gồm lỗi SQL, giới hạn context, timeout và capability chưa hỗ trợ. Hai trạng thái có ý nghĩa và điểm khác nhau.

## Model và giới hạn cố định

Cấu hình ở `configs/baseline_model.json`: `Qwen/Qwen3-4B`, revision `1cfa9a7208912126459214e8b04321603b3df60c`, CUDA BF16, seed 42, greedy, non-thinking. Tổng context 16.384 token, output tối đa 1.024 token. Giữ 4B vì GPU RTX 4080 SUPER 16 GB hiện có đủ chỗ chạy model và context; 8B BF16 riêng weights gần hết VRAM.

Query timeout 10 giây, kết quả tối đa 1.000 dòng. Cursor đọc thêm một dòng để phát hiện vượt cap; không thêm LIMIT vào đầu vào của SUM/COUNT. Kết quả vượt cap sẽ abstain. `SELECT *` được mở rộng theo danh sách cột đã expose, loại cột raw/internal. Role database không có quyền đọc raw tables hoặc ghi dữ liệu.

Preview tắt; chưa có tool tra lịch âm/ngày lễ. Schema vượt token budget sẽ abstain, không cắt bảng/rule âm thầm. Alias/semantic role và quan hệ SLM đề xuất vẫn cần được hiểu cùng README/rulebook; không phải bằng chứng mọi JOIN đều đúng. Một SQL chạy được vẫn có thể tính sai metric.

## Chuẩn bị trên workspace này

Chạy từ thư mục gốc repository. Model, venv, PostgreSQL và dữ liệu đã có sẵn nên không cần tải lại. Xem [B1_MODEL_SMOKE.md](B1_MODEL_SMOKE.md) nếu cần tải model đúng revision và [DATA_PIPELINE.md](DATA_PIPELINE.md) nếu cần dựng PostgreSQL/dữ liệu từ đầu.

Nếu tạo environment mới, dùng Python 3.12 và các dependencies đã khóa. Cài đặt cần mạng, inference không cần mạng:

```bash
export UV_CACHE_DIR="$PWD/.cache/uv"
uv venv --python python3.12 .venv-b1-gpu
uv pip install --python .venv-b1-gpu/bin/python torch==2.7.1 --index-url https://download.pytorch.org/whl/cu128
uv pip install --python .venv-b1-gpu/bin/python -r requirements-data-semantic.txt
uv pip check --python .venv-b1-gpu/bin/python
```

Không chạy lại `uv venv` trên environment đang dùng. `requirements-data-semantic.txt` gộp model và PostgreSQL/parser dependencies; baseline không thêm dependency mới. Nếu tạo máy mới, cần chuẩn bị model local và cluster PostgreSQL theo các tài liệu liên kết trước khi chạy.

Khởi động PostgreSQL local và tạo/cập nhật role baseline có quyền SELECT trên clean views:

```bash
python3 scripts/local_postgres.py start
export DATAMATE_PG_DSN="$(python3 scripts/local_postgres.py dsn)"
export DATAMATE_BASELINE_PG_DSN="$(.venv-b1-gpu/bin/python scripts/setup_baseline_readonly.py \
  --prepared-root outputs/data_pipeline/train \
  --prepared-root outputs/data_pipeline/development_v1 --print-dsn)"
```

`DATAMATE_PG_DSN` là tài khoản quản trị cluster dùng cho setup; runner chỉ nhận `DATAMATE_BASELINE_PG_DSN`. Không đưa DSN vào prompt/file kết quả. Nếu dùng PostgreSQL khác, đặt DSN quản trị phù hợp cho setup. Script từ chối ghi đè role cùng tên không có marker do dự án tạo.

Hai prepared roots dùng ở đây: `train` là 18 doanh nghiệp đã chốt; `development_v1` là 6 doanh nghiệp development chuẩn bị riêng bằng cùng pipeline. `docs/train` là thư mục package nguồn chung, không có nghĩa cả 30 doanh nghiệp đều thuộc partition train. Calibration và holdout chưa dùng.

Nếu chưa có prepared root development, tạo một lần trước khi cấp grants và chạy baseline (không chạy cùng lúc với model baseline):

```bash
.venv-b1-gpu/bin/python scripts/prepare_pipeline.py \
  --partition development --output-dir outputs/data_pipeline/my_development_01
.venv-b1-gpu/bin/python scripts/summarize_semantic.py \
  --run-dir outputs/data_pipeline/my_development_01
.venv-b1-gpu/bin/python scripts/validate_pipeline.py \
  --run-dir outputs/data_pipeline/my_development_01 \
  --output outputs/data_pipeline/my_development_01/database_validation.json
```

Sau đó dùng cùng đường dẫn mới ở `setup_baseline_readonly.py` và `run_baseline.py`. Dữ liệu hiện tại đã kiểm tra đủ 6 doanh nghiệp/29 bảng: row counts, tổng numeric và derived quantity khớp staging. Validation này xác nhận nạp dữ liệu trung thực, chưa khẳng định mọi metric nghiệp vụ đều tính được.

## Thử một câu

Dùng output directory mới mỗi lần; runner từ chối ghi đè kết quả cũ.

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
.venv-b1-gpu/bin/python scripts/run_baseline.py \
  --prepared-root outputs/data_pipeline/train \
  --source-root docs/train \
  --model-path models/qwen3-4b \
  --business-id biz_107 \
  --question 'Bảng sobanhang_48d2074d_clean có bao nhiêu dòng dữ liệu?' \
  --as-of-date 2026-07-22 \
  --output-dir outputs/baseline/my_single_01

cat outputs/baseline/my_single_01/predictions.jsonl
.venv-b1-gpu/bin/python scripts/inspect_baseline.py \
  --run-dir outputs/baseline/my_single_01 --id manual_0001
```

Đây là câu kiểm tra kết nối/thực thi, không phải câu đo PA cuộc thi. Smoke đã lưu ở `outputs/baseline/single_v1`; model sinh SQL và trả 43.605, khớp số dòng clean view đã kiểm chứng. Không có SQL/đáp án hard-code theo câu hỏi.

## Smoke train và chạy đủ development

Smoke 20 câu đầu train, mẫu số chấm chỉ là 20 câu đã chọn:

```bash
.venv-b1-gpu/bin/python scripts/run_baseline.py \
  --prepared-root outputs/data_pipeline/train \
  --questions-file artifacts/data_split_v1/inputs/train/questions.jsonl \
  --limit 20 --output-dir outputs/baseline/my_train_smoke_01

.venv-b1-gpu/bin/python scripts/evaluate_baseline_run.py \
  --run-dir outputs/baseline/my_train_smoke_01 --partition train --subset
.venv-b1-gpu/bin/python scripts/summarize_baseline.py \
  --run-dir outputs/baseline/my_train_smoke_01
```

Chạy toàn bộ 708 câu development, không dùng `--limit` hoặc `--subset`:

```bash
.venv-b1-gpu/bin/python -u scripts/run_baseline.py \
  --prepared-root outputs/data_pipeline/development_v1 \
  --questions-file artifacts/data_split_v1/inputs/development/questions.jsonl \
  --output-dir outputs/baseline/my_development_01

.venv-b1-gpu/bin/python scripts/evaluate_baseline_run.py \
  --run-dir outputs/baseline/my_development_01 --partition development
.venv-b1-gpu/bin/python scripts/summarize_baseline.py \
  --run-dir outputs/baseline/my_development_01
```

Evaluator kiểm tra run đã complete, đủ ID, nội dung câu hỏi khớp split cố định; subset chỉ được chấm khi bật rõ `--subset`. Chấm điểm dùng lại `datamate_eval`, không tự đổi công thức/tolerance. Có thể dùng evaluator gốc cho full partition với output directory khác:

```bash
.venv-b1-gpu/bin/python scripts/evaluate.py \
  --partition development \
  --predictions outputs/baseline/my_development_01/predictions.jsonl \
  --timings outputs/baseline/my_development_01/timings.jsonl \
  --output-dir outputs/baseline/my_development_01/evaluation_original
```

Không so sánh PA subset với PA full partition như cùng một bộ đánh giá. `train_smoke_v1` là run debug trước sửa SQL gate AND/OR, không phải kết quả cấu hình cuối; `train_smoke_v2` là smoke hợp lệ sau sửa, không được chọn lại câu theo kết quả.

## Xem kết quả

Trong mỗi run:

| File | Nội dung |
|---|---|
| `summary.md` | Tổng quan runtime, số SQL, lỗi và điểm |
| `report.html` | Mở trực tiếp trong browser, lọc ID/business/outcome/câu hỏi; xem SQL, raw output, expected/prediction |
| `predictions.jsonl` | Đáp án cuối `id/type/answer`, input evaluator |
| `questions.jsonl` | Chính xác các câu đã chạy, không có labels |
| `timings.jsonl` | Thời gian từng câu, chưa cộng load dùng chung |
| `traces.jsonl` | Prompt, raw output, parsed plan, SQL gốc/SQL thực thi, query result, lỗi, timing |
| `run.json` | Model/revision/config, code/context hashes, trạng thái, startup/load/tổng wall time |
| `runtime_summary.json` | Số JSON/plan/SQL hợp lệ và phân loại lỗi kỹ thuật |
| `evaluation/report.json` | PA, correct/wrong/abstain, coverage, format, unanswerable, breakdown và latency |
| `evaluation/diagnostics.jsonl` | Câu hỏi, expected/prediction/outcome, chỉ tạo sau inference |
| `evaluation/per_question.jsonl` | Kết quả chấm từng ID |

Trang HTML tự chứa, không cần server hoặc CDN. `traces.jsonl` có thể lớn vì giữ full prompt; dùng `inspect_baseline.py` để đọc một câu. `outputs/` bị Git ignore vì chứa dữ liệu và kết quả local.

Trong lúc đang chạy có thể xem tiến độ ở terminal khác, không gọi thêm model:

```bash
wc -l outputs/baseline/development_v1/predictions.jsonl
tail -n 5 outputs/baseline/development_v1.log
```

PA sử dụng +1 đúng, -1 sai, 0 abstain; chia cho tổng số câu của phạm vi chấm. Cần xem đồng thời coverage và answered accuracy: abstain nhiều có thể tránh điểm âm nhưng không tạo đáp án đúng. `unanswerable` chỉ đúng khi khớp tham chiếu, không phải cách né câu khó. Runtime toàn run gồm setup trong CLI và model load; thời gian mỗi câu không gồm load dùng chung. Timer bắt đầu ở `main()`, chưa gồm import Python hoặc khởi tạo shell; đây chưa phải benchmark cold-start container của cuộc thi.

## Kiểm tra code

```bash
DATAMATE_TEST_PG_DSN="$DATAMATE_PG_DSN" PYTHONPATH=src \
  .venv-b1-gpu/bin/python -m unittest discover -s tests -v
```

Nếu không đặt `DATAMATE_TEST_PG_DSN`, các test tích hợp database sẽ skip. Lần kiểm tra sau khi dọn v2 có 140 tests đạt và 7 database tests skip. Test dùng fixture riêng, không ghi vào bảng doanh nghiệp. Các kiểm tra gồm SQL gate và bảng/cột, scope CTE, chặn ghi/cross-schema/system time, rollback sau timeout, role owner bị từ chối, read-only/raw access, tổng hợp không mất dòng vì cap, Decimal, shape/NULL, runner chỉ load một lần, một query, lỗi không repair và evaluator subset/nội dung câu hỏi.

Các logs/báo cáo v1 cũ đã được dọn. Hướng dẫn và trạng thái hiện tại nằm tại [Baseline v2](BASELINE_GROUNDED.md). Chưa kiểm chứng Docker/thiết bị chấm chính thức hoặc dữ liệu test mới; đây là baseline local để xác định điểm yếu trước khi thêm kỹ thuật khác.

## Xử lý lỗi

- Không tìm thấy catalog/README hash khác: kiểm tra đúng prepared root và source root; không thay metadata bằng file doanh nghiệp khác.
- Từ chối DSN owner: dùng role do `setup_baseline_readonly.py` tạo; không nới quyền runner.
- CUDA unavailable/OOM: kiểm tra đúng venv GPU và quyền truy cập GPU; không chạy hai inference cùng lúc. Không tự fallback model sang CPU vì RAM máy hiện tại không phù hợp.
- Context budget/output token limit: trace ghi lý do, final abstain; đánh giá nhu cầu rút gọn schema/retrieval ở phiên bản tiếp theo.
- SQL unknown column/function/current date: xem raw output, SQL và lỗi; baseline không tự sửa. Ngày tương đối phải dựa trên `as_of_date`.
- SQL đúng cú pháp nhưng sai đáp án: so definition/source/status/JOIN/unit với README/rulebook và diagnostics sau chấm; lỗi này không được giải quyết chỉ bằng việc query chạy thành công.
- Run failed hoặc bị ngắt: các dòng đã flush vẫn đọc được nhưng evaluator wrapper từ chối gọi đó là full run. Dùng output directory mới khi chạy lại; chưa có resume tự động.
