# Data pipeline final — canonical-first + bounded refinement

Bản refinement đã được chấp nhận. Bộ này đủ nhỏ để commit Git: schema không có giá trị dòng/profile, review từng business, thống kê kiểm tra và so sánh lịch sử. Dữ liệu staging đầy đủ, weights, virtualenv và raw model logs không nằm ở đây.

- [Validation](validation.json)
- [Schema 18 doanh nghiệp](schema_catalog.json)
- [So sánh refinement trước khi xóa baseline](refinement_comparison.md)
- [Đối chiếu PostgreSQL](database_validation.json)
- [Review](business_reviews/)

Output local hiện tại: `outputs/data_pipeline/train`. Muốn truy vấn trên máy khác, chuẩn bị lại dữ liệu nguồn, model local và PostgreSQL theo `docs/DATA_PIPELINE.md`; không dùng snapshot schema như dữ liệu giao dịch. Các tên schema PostgreSQL ở snapshot là của lần kiểm chứng này.
