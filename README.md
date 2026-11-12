# DataMate V2

Pipeline đọc CSV/Excel/SQLite, chuẩn hóa dữ liệu và trả lời câu hỏi nghiệp vụ tiếng Việt bằng PostgreSQL và SLM.

Flow QA hiện tại: Call 1 đọc Rulebook/README và đưa ra quyết định → Python phân giải entity, khoảng ngày và nguồn dữ liệu → Call 2 viết plan/SQL → PostgreSQL chỉ đọc → serializer xuất JSONL. Cấu hình flow này là `datamate_v2/configs/pipeline_decisions.yaml`.

Xem [hướng dẫn cài đặt và chạy](datamate_v2/README.md), [model và weights](datamate_v2/models/README.md), [implementation prompt](docs/DataMate_V2_2Call_Implementation_Prompt.md).

Repo chứa source, prompt, config và unit/integration tests. Weights, dữ liệu train/test, PostgreSQL runtime, cache, output, bản rollback và harness thử nghiệm 50 câu được giữ ngoài Git. Cần bổ sung dữ liệu chính thức vào `docs/train` khi chạy trên máy mới.
