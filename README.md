# DataMate

Pipeline đọc dữ liệu doanh nghiệp CSV/Excel/SQLite, chuẩn hóa có provenance và
quality flags, hỗ trợ suy luận ngữ nghĩa cột bằng SLM local, rồi nạp PostgreSQL.
Repository cũng có công cụ chia tập theo doanh nghiệp và evaluator đáp án JSONL.

## Bắt đầu với baseline v2

Code hiện tại dùng data pipeline canonical-first với bounded refinement và baseline
v2 phân tích câu hỏi → liên kết schema/grain/rule → sinh SQL → PostgreSQL chỉ đọc
→ chuẩn hóa đáp án → evaluator. Hướng dẫn chạy đầy đủ:
[Baseline v2](docs/BASELINE_GROUNDED.md).

Output local được giữ sau khi dọn:

```text
outputs/
  data_pipeline/
    train/                    # 18 doanh nghiệp train, 80 bảng
    development_v1/           # Dữ liệu development được baseline v2 sử dụng
    release.json              # Manifest pipeline train đã chấp nhận
    README.md
  business_metadata/
    development_v2/           # Grain, measure scope, group keys và JOIN candidates
  baseline/
    development50_v2/         # Run bị ngắt: hoàn tất 34/50 câu
    grounded_v2_frozen.json   # SHA-256 và cấu hình đã chốt của v2
```

Tên `development_v1` là phiên bản **output data pipeline**, không phải phiên bản
baseline. Baseline v2 dùng chính thư mục này cùng metadata `development_v2`.
Các output thử nghiệm và backup đã được xóa. Model local, virtualenv, dữ liệu nguồn
và PostgreSQL local vẫn cần để chạy; chúng được Git ignore. Output giao dịch và
predictions được giữ local; Git chứa code, hướng dẫn và artifacts nhỏ.

## Thành phần

| Đường dẫn | Nội dung |
|---|---|
| `src/datamate_data/` | Parsers, adapters, normalization, semantic mapping, catalog và PostgreSQL store |
| `src/datamate_b/` | Local model wrapper và contract/prompt; được semantic pipeline sử dụng |
| `src/datamate_eval/` | Evaluator local cho final answers |
| `src/datamate_baseline/` | Baseline v2: hiểu câu hỏi, liên kết schema, SQL read-only và serializer |
| `scripts/` | CLI chuẩn bị dữ liệu, quản lý PostgreSQL local, kiểm tra và đánh giá |
| `configs/` | Cấu hình model/pipeline/split và dependency snapshots |
| `tests/` | Kiểm thử synthetic và integration tùy chọn |
| `examples/` | Fixture synthetic nhỏ có thể chạy độc lập với dữ liệu cuộc thi |
| `docs/` | Tài liệu dự án, quy định, kế hoạch và báo cáo |

## Tài liệu nên đọc

- [Baseline v2](docs/BASELINE_GROUNDED.md): luồng hiện tại, lệnh chạy 50 câu và chấm kết quả.
- [Data pipeline](docs/DATA_PIPELINE.md): kiến trúc, cài đặt, PostgreSQL và semantic mode.
- [Split v1](docs/DATA_SPLIT_V1.md): membership, fingerprint và cách dùng bốn tập.
- [Review split](docs/DATA_SPLIT_V1_REVIEW.md): phạm vi và giới hạn phép đánh giá.
- [Evaluator](docs/EVALUATOR.md): format, luật so sánh, alias và cách chạy.
- [Kế hoạch triển khai](docs/DATAMATE_IMPLEMENTATION_PLAN.md).

## Môi trường

Python 3.12 được dùng trong các environment đã ghi nhận. Các nhóm dependencies:

- `requirements-data.txt`: chuẩn bị dữ liệu và PostgreSQL, không yêu cầu model.
- `requirements-data-semantic.txt`: data pipeline với SLM local.
- `requirements-b1.txt`: local model/SQL generation.

Hướng dẫn cài PyTorch CUDA và phiên bản đã thử nằm trong `docs/DATA_PIPELINE.md`.
Không đưa virtualenv, cache, database local hoặc trọng số model vào Git.

Markdown/PDF/HTML, rulebook và README doanh nghiệp được giữ trong Git. Dữ liệu
kinh doanh CSV/XLSX/SQLite, questions/answers, calendar và ZIP nguồn được giữ ngoài
Git theo `.gitignore`; đặt lại gói dữ liệu được cấp vào `docs/train` và
`docs/public_test` khi cần chạy trên dữ liệu thật. Không đổi nội dung nguồn.

## Chuẩn bị fixture không cần GPU/PostgreSQL

Sau khi cài `requirements-data.txt` trong môi trường riêng:

```bash
python scripts/prepare_pipeline.py \
  --business-dir examples/semantic_business \
  --source-root docs/train \
  --semantic-mode none \
  --prepare-only \
  --output-dir outputs/synthetic_prepare_01
```

Lệnh cần `docs/train/rulebook.md` và `docs/train/calendar.csv` từ gói nguồn.
Output phải là đường dẫn mới. Chế độ semantic local cần tải model trước; inference
không tải model qua mạng. Script tải model: `scripts/download_model.py --help`.
Revision và tham số được ghi trong `configs/b1_model.json`,
`configs/semantic_model.json` và `docs/B1_MODEL_MANIFEST.json`.

## Kiểm thử

```bash
python -m unittest discover -s tests -p 'test_data_pipeline.py' -v
python -m unittest discover -s tests -p 'test_semantic_pipeline.py' -v
```

Kiểm thử synthetic không cần weights. Một số integration tests cần PostgreSQL
hoặc dependencies riêng và sẽ skip khi điều kiện không có. Tests split/evaluator
trên dữ liệu thật cần gói nguồn và frozen artifacts đúng checksum; xem tài liệu.

## Đầu ra

Mỗi doanh nghiệp có staging JSONL, `catalog.json`, error log và semantic review.
Nạp PostgreSQL tạo `postgres_catalog.json`, bảng typed và clean views. Catalog giữ
tên cột/thứ tự/kiểu; mỗi record giữ raw, typed, provenance và quality.

Các mapping SLM vẫn là candidates. Clean view không đồng nghĩa mọi ô đều hợp lệ;
cần kiểm tra quality và ngữ nghĩa trước khi dùng để tính metric.
