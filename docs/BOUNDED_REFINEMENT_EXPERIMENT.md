# Refinement có giới hạn — bản đã chấp nhận

## Phạm vi

Chỉ thêm refinement cho cột đã có mô tả hữu ích và bằng chứng nguồn nhưng role bị thiếu/chung chung (`text`, `other`, `unknown` hoặc null). Không xây registry, không thêm alias, không fine-tune model và không đọc câu hỏi/đáp án.

- Tối đa một yêu cầu model bổ sung cho mỗi cột đủ điều kiện; không JSON retry hoặc contract retry bên trong refinement.
- Chỉ yêu cầu lại đúng cột đó, giữ context README/rulebook và profile nguồn. Header evidence chỉ chứa cột mục tiêu để tránh nhầm ID; các ID không bị đổi tên.
- Giữ kiểu mà model đề xuất ở lượt đầu; mọi kiểu áp dụng vẫn phải qua validator toàn cột. Không cho model đổi kiểu trong lượt refinement.
- Candidate vẫn phải qua kiểm tra role, bằng chứng và collision. Nếu không đạt, giữ kết quả trước refinement cùng mô tả đã có.
- Không được làm mất mapping hợp lệ của cột khác. Nếu refinement tạo xung đột role, bỏ đề xuất refinement và lưu lý do.
- Audit lưu initial_proposal, initial_checks, candidate_proposal/checks, accepted và reason; raw prompt/output/timing nằm trong model_calls.jsonl.

## Kết quả đã kiểm chứng

Đã chạy 18 doanh nghiệp, 80 bảng, 2.040.280 dòng nguồn và đối chiếu PostgreSQL thành công. 115 tests passed, không skip.

So sánh lịch sử trước khi dọn output: 18 yêu cầu refinement, 7 được chấp nhận, 11 giữ kết quả cũ; 3 yêu cầu lỗi parse không retry. Unresolved 22 → 15; không mất/đổi role vốn hợp lệ. Staging 80 bảng và các output phân tích ban đầu giống baseline. Đây là độ phủ metadata, chưa phải QA accuracy. Hai mô tả “Vị trí”, “Thương hiệu” ở biz_122 vẫn cần review.

Bản refinement đã được chọn làm `outputs/data_pipeline/train`. Baseline, snapshot rollback và các backup đã xóa theo yêu cầu; không còn lệnh rollback dựa vào snapshot. Tham khảo [so sánh lịch sử](../artifacts/data_pipeline_final/refinement_comparison.md) và [validation cuối](../artifacts/data_pipeline_final/validation.json).

Code hiện tại dùng semantic-v4 / canonical-first-alias-meaning-v8 và payload refinement bounded-role-refinement-v1. Giữ prompt phân tích ban đầu để so sánh; payload refinement có version riêng. Không xây registry/alias mới.

## Chạy lại

Xem [DATA_PIPELINE.md](DATA_PIPELINE.md), dùng output mới như `outputs/data_pipeline/train_new`. Nếu cần so sánh hai run tương lai:

```bash
.venv-b1-gpu/bin/python scripts/compare_refinement_runs.py \
  --baseline outputs/data_pipeline/train \
  --candidate outputs/data_pipeline/train_new \
  --output-dir outputs/data_pipeline/comparison_new
```

Có thể cung cấp `--baseline-snapshot <file.json>` chứa relative path → SHA256 để kiểm tra baseline không đổi. Không cung cấp thì trường baseline_unchanged là null; script vẫn đối chiếu business/source/schema/role. Không so wall time giữa lượt có cache và lượt inference mới như benchmark tốc độ.
