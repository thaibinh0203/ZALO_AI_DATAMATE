# Synthetic semantic mapping demo — không phải dữ liệu cuộc thi

Mỗi dòng trong `export.csv` là một dòng sản phẩm bán ra của một đơn hàng.

- Số tham chiếu là mã đơn hàng, phải giữ số 0 ở đầu.
- Thời điểm chốt là ngày bán, ghi theo YYYY-MM-DD.
- Mặt hàng xuất là tên sản phẩm.
- Lượng thực xuất là số lượng theo đơn vị bán của dòng.
- Tiền ròng là thành tiền của dòng sau chiết khấu, đơn vị VND.
- Chỉ số nội bộ là một số đo riêng; chưa có công thức nghiệp vụ trong rulebook.

Các tên cột được cố ý viết khác alias để thử semantic pipeline. Không có câu hỏi
hay đáp án mong đợi trong fixture này.
