# Quy tắc SYNTHETIC cho demo B1, KHÔNG phải rulebook cuộc thi

- Mỗi dòng sales là một sale line; sale_id duy nhất. product_id nối sang products.
- Chỉ giao dịch có status = 'completed' là hợp lệ.
- Doanh thu là tổng amount_vnd, đơn vị đồng; số lượng bán là tổng quantity.
- Tuần bắt đầu thứ Hai. “Tuần rồi” là tuần lịch ngay trước tuần chứa as_of_date;
  khoảng thời gian gồm đầu kỳ và không gồm đầu kỳ tiếp theo.
- “Bán nhiều nhất” dùng tổng quantity, không dùng doanh thu; nếu bằng nhau,
  sắp product_id tăng dần để chọn sản phẩm đầu tiên. Trả tên sản phẩm khi hỏi “là gì”.
- “Tháng này” dùng tháng lịch chứa as_of_date.
- Chỉ các bảng/cột trong DDL có sẵn. Fixture không có chi phí hoặc COGS.
