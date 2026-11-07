> Kết quả lịch sử trước khi dọn output. Bản đối chiếu đã được xóa theo yêu cầu; bản refinement được chốt làm bản hiện tại.

# So sánh refinement có giới hạn

Metadata coverage comparison, not semantic correctness or QA accuracy. Candidate can reuse initial inference cache; full wall times are not a fair speed benchmark. No alias learning.

- Baseline nguyên vẹn: True; output phân tích ban đầu giống nhau: True.
- Role mới dùng được: 7; mất role trước đó: 0; thay đổi role vốn dùng được: 0.
- Refinement: {'extra_requests': 18, 'new_generations': 18, 'cache_hits': 0, 'parse_errors': 3, 'generation_ms': 83338.32, 'eligible_columns': 18, 'accepted_final': 7, 'kept_original': 11}.
- Trạng thái trước: {'existing_mapping': 464, 'unresolved': 22, 'described_other': 161, 'applied_candidate': 18, 'role_conflict': 6}.
- Trạng thái sau: {'existing_mapping': 464, 'unresolved': 15, 'described_other': 168, 'applied_candidate': 18, 'role_conflict': 6}.

| Business | Bảng | Cột | Role trước | Role sau | Trạng thái trước → sau | Ý nghĩa mới |
|---|---|---|---|---|---|---|
| biz_106 | danhsachkhachhang_496bf16d | Điện thoại | {'canonical': None, 'semantic': None} | {'canonical': None, 'semantic': 'customer_phone'} | unresolved → described_other | Số điện thoại của khách hàng |
| biz_106 | danhsachkhachhang_496bf16d | Loại hình khách | {'canonical': None, 'semantic': None} | {'canonical': None, 'semantic': 'khach_hang_loai_hinh'} | unresolved → described_other | Loại hình khách hàng |
| biz_106 | danhsachsanpham_e89704d9 | Thuộc tính | {'canonical': None, 'semantic': None} | {'canonical': None, 'semantic': 'product_attribute'} | unresolved → described_other | Thuộc tính của sản phẩm |
| biz_106 | danhsachsanpham_e89704d9 | Loại hàng hóa | {'canonical': None, 'semantic': None} | {'canonical': None, 'semantic': 'product_type'} | unresolved → described_other | Loại hàng hóa |
| biz_120 | danhsachkhachhang_7f5e2a72 | E-mail | {'canonical': None, 'semantic': None} | {'canonical': None, 'semantic': 'email'} | unresolved → described_other | E-mail không có dữ liệu |
| biz_122 | danhsachsanpham_6a9d2ed1 | Vị trí | {'canonical': None, 'semantic': None} | {'canonical': None, 'semantic': 'location'} | unresolved → described_other | Vị trí không có nghĩa rõ ràng từ tiêu đề hoặc mô tả |
| biz_122 | danhsachsanpham_6a9d2ed1 | Thương hiệu | {'canonical': None, 'semantic': None} | {'canonical': None, 'semantic': 'product_brand'} | unresolved → described_other | Thương hiệu không có nghĩa rõ ràng từ tiêu đề hoặc mô tả |
