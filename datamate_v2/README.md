# DataMate V2

## Flow đang sử dụng

Data pipeline: đọc CSV/Excel/SQLite → tìm header → chuẩn hóa → xác định grain/quan hệ → description và value catalog → PostgreSQL/cache.

QA với `configs/pipeline_decisions.yaml`:

1. Call 1 đọc câu hỏi, Rulebook, README, schema và dữ liệu mẫu, trả goal, rules_to_apply, aggregation bằng lời và time_requests.
2. Python tính ngày cụ thể, cung cấp CTE hợp nhất các bảng tháng tương thích và giá trị entity thật theo từng cột. Dictionary nunique <50 được chia sẻ và chọn trong ngân sách 2500 token.
3. Call 2 viết calculation_plan và PostgreSQL SQL trực tiếp. Không kiểm trích dẫn từng từ, không biên dịch aggregation bằng lời thành SQL.
4. Kiểm tra SQL chỉ đọc/phạm vi business và literal entity; thực thi bằng role chỉ đọc; serialize kết quả về JSONL. NULL hoặc kết quả không đáp ứng output contract sẽ abstain.

Các flow cũ vẫn có config riêng để tái lập; truyền rõ `--pipeline-config` để chọn flow decisions. Call 1 mặc định tối đa 2048 output token, Call 2 là 4096 theo task_max_new_tokens trong model config. Đây là giới hạn hai task khác nhau.

## Cài đặt

Chạy từ gốc repo. Cần Python 3.12, PostgreSQL binaries và PyTorch phù hợp GPU/CUDA.

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install torch==2.7.1 --index-url https://download.pytorch.org/whl/cu128
.venv/bin/python -m pip install -r datamate_v2/requirements-4bit.txt
```

Chuẩn bị weights online một lần, trước inference:

```bash
.venv/bin/python datamate_v2/scripts/prepare_model.py --model qwen25_7b --download
```

Các model được khai báo độc lập: qwen3_4b, qwen25_7b, gemma4_12b. Weights/revision/checksum ở [models/README.md](models/README.md) và registry.json; weights không nằm trên Git. Mỗi run dùng một model. Cấu hình so sánh NF4 4-bit: configs/model_compare_4bit.yaml.

## PostgreSQL local

```bash
.venv/bin/python datamate_v2/scripts/local_postgres.py start
source <(.venv/bin/python datamate_v2/scripts/local_postgres.py env)
```

Nếu chưa có PostgreSQL binaries, cài PostgreSQL trên máy hoặc truyền --bin-dir cho local_postgres.py. Cluster riêng dùng Unix socket, không cần mật khẩu; QA dùng role không sở hữu schema và transaction chỉ đọc. Không commit DSN/token.

## Dữ liệu và chạy

Đặt split chính thức trong docs/train hoặc truyền đường dẫn split khác. Split gồm rulebook.md, calendar.csv, questions.jsonl và data/<business_id>/ với README/dữ liệu của từng business. Không upload raw data/answers lên repo này.

Chạy file câu hỏi bất kỳ; đây không phải harness cố định 50 câu:

```bash
.venv/bin/python -m datamate_v2.cli run \
  --split docs/train --questions docs/train/questions.jsonl \
  --model qwen25_7b \
  --model-config datamate_v2/configs/model_compare_4bit.yaml \
  --pipeline-config datamate_v2/configs/pipeline_decisions.yaml \
  --output datamate_v2/runtime/my_run
```

Runner chuẩn bị mỗi biz một lần, sau đó chạy QA. Dùng một file câu hỏi nhỏ hơn để giới hạn business cần ingest. Output phải là thư mục mới.

QA-only từ ingestion trước (cần giữ cache/businesses chứa metadata.json, clean_rows.json; source split phải khớp):

```bash
.venv/bin/python -m datamate_v2.cli run \
  --split docs/train --reuse-ingestion-from PATH_TO_PRIOR_RUN \
  --cache PATH_TO_PRIOR_CACHE \
  --model qwen25_7b \
  --model-config datamate_v2/configs/model_compare_4bit.yaml \
  --pipeline-config datamate_v2/configs/pipeline_decisions.yaml \
  --output datamate_v2/runtime/qa_rerun
```

--reuse-ingestion-from dùng chính bộ câu hỏi đã lưu trong prior run. Không tự gọi model ingest lại. Metadata catalog thiếu được bổ sung bằng Python từ clean_rows, không đổi file metadata gốc.

Output gồm submission.jsonl, logs/questions.jsonl, run_manifest.json, progress.json và performance.json. Log giữ phân tích, rules_to_apply, aggregation, resolved_time, data_assembly, SQL thực thi và lỗi.

## Eval và tests

Eval đọc đáp án chuẩn riêng; pipeline inference không đọc answers.jsonl:

```bash
.venv/bin/python -m datamate_v2.cli evaluate \
  --run datamate_v2/runtime/my_run --references docs/train/answers.jsonl

.venv/bin/python -m pip install -r datamate_v2/requirements-dev.txt
.venv/bin/python -m pytest datamate_v2/tests -q
```

PostgreSQL integration tests cần DATAMATE_TEST_ADMIN_DSN trỏ tới database test riêng; không đặt biến này bằng database production. Local evaluator là proxy theo tài liệu, không phải executable scorer chính thức.

## Đóng gói

Dockerfile, docker-compose.yml, scripts/package_context.py và scripts/predict.sh phục vụ đóng gói offline. scripts/predict.sh hiện dùng config mặc định pipeline.yaml; nếu chọn flow decisions cho bản nộp, cần cấu hình entrypoint tương ứng và kiểm thử Docker đầy đủ trước khi nộp. Không coi smoke/unit tests là xác nhận Docker cuối đã đạt.

Các script/notebook test 50 câu, runtime, output và snapshot rollback chỉ giữ local, được loại khỏi Git. docs/train và docs/public_test không được phân phối lại trong repo; giữ tài liệu gốc ở docs/.
