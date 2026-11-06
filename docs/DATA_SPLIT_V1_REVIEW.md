# Review split v1 — 05/11/2026

**Kết luận:** có thể giữ nguyên v1 để phát triển và đo khả năng tổng quát sang
**họ layout chưa dùng train**. Chưa nên dùng điểm holdout này làm đại diện cho mọi
ngành, mọi định dạng và mọi loại câu. Không chỉnh membership trong lượt review này.

| Tập | Doanh nghiệp | Câu hỏi | Nhận xét |
|---|---:|---:|---|
| Train | 18 | 1.583 | 5 họ layout, có đủ 8 loại đáp án |
| Development | 6 | 708 | 590 câu thuộc detailed_sales_report, 118 mixed_sources |
| Calibration | 3 | 355 | monthly_handwritten và SQLite; ít mẫu lớp hiếm |
| Holdout | 3 | 354 | Chỉ relational_csv, cà phê/nhà hàng; không boolean/unanswerable |

## Các kiểm tra đã chạy

- `scripts/prepare_data_split.py --verify`: checksum nguồn, ZIP, config, script
  chuẩn bị và toàn bộ artifact hiện có đều khớp bản đã đóng băng.
- Đối chiếu lại 162 file trong ZIP với bản giải nén: khớp nội dung.
- Tính lại từ records thực: 3.000 câu được phủ đúng một lần; business và family
  không giao nhau giữa các tập; câu hỏi/đáp án materialized giữ nguyên bản nguồn.
- Question inputs chỉ có id, business_id, question, as_of_date; không có answer/alias.
- Tính lại số câu, ngành và loại đáp án của từng business/partition: khớp manifest.
- Các alias và kiểu dữ liệu của cả 3.000 reference records hợp lệ với evaluator
  local. Đây chỉ là kiểm tra format, **không phải chấm model trên holdout**.
- File quy đổi giống byte giữa biz_102 và biz_103 cùng nằm ở train. Không có file
  business data giống byte vượt ranh giới tập theo checksum đã kiểm tra.

Chi tiết máy đọc được: [DATA_SPLIT_V1_REVIEW.json](DATA_SPLIT_V1_REVIEW.json).
Không đọc public test trong lượt review/triển khai evaluator này.

## Giới hạn và cách sử dụng hợp lý

1. **Holdout là một phép thử hẹp theo layout.** Nó không đo khả năng phát hiện
   unanswerable, boolean, nhà thuốc, vật liệu xây dựng hoặc shop tạp hóa mới.
   Khi type không có support, evaluator xuất rate=null, không gán accuracy=0.
2. **Development nghiêng về một họ layout.** Xem cả PA tổng, PA từng family,
   từng business và từng type. Macro PA family giúp nhìn thấy độ lệch này;
   PA chuẩn vẫn là trung bình theo câu hỏi.
3. **SQLite chỉ nằm ở calibration.** Xây adapter bằng spec chung và SQLite
   synthetic fixtures. Không dùng lỗi/nhãn calibration để liên tục sửa model,
   parser hoặc pipeline rồi vẫn gọi nó là calibration độc lập. Nếu đã làm vậy,
   cần ghi nhận contamination và chuẩn bị phép đo độc lập mới có version.
4. **Calibration ít nhãn hiếm:** 10 unanswerable, 5 boolean, 3 table. Ngưỡng chọn
   từ đây có thể nhiễu; dùng thêm grouped validation bên trong train khi cần,
   không tune bằng holdout.
5. **Không có tạp hóa ngoài train.** Cần phép đo bổ sung từ train theo business
   nếu muốn kết luận riêng về ngành này, giữ nguyên holdout v1.
6. **Family không đồng nghĩa độc lập ngữ nghĩa.** Có 84 nhóm câu có cùng wording
   sau chuẩn hóa xuất hiện ở nhiều partition. Cùng wording trên dữ liệu business
   khác nhau không tự chứng minh rò rỉ đáp án; nhưng v1 không đo “câu chữ hoàn toàn
   mới”. Checksum cũng không chứng minh không có dữ liệu tương đồng sau biến đổi.

Những giới hạn chính đã được người tạo split ghi rõ trong manifest/docs. Tôi đồng
ý giữ v1 và bổ sung báo cáo theo nhóm, thay vì đổi split chỉ để có phân bố đẹp hơn
hoặc đổi membership sau khi nhìn điểm model.

## Ranh giới dữ liệu

Labels chỉ cấp cho evaluator và việc tạo supervision của train partition. Runtime
inference chỉ nhận question inputs, đúng business package, rulebook và calendar.
Không cấp toàn bộ docs/train, evaluator directory, split label statistics hoặc
report chứa expected types cho model.

Đường dẫn tách biệt chưa phải sandbox quyền đọc. Khi dựng runner/Docker, cần
thực thi ranh giới này ở mount/path allowlist/process, không chỉ dựa vào prompt.

## Các lệnh kiểm tra lại

```bash
python3 scripts/prepare_data_split.py --verify
python3 -m unittest discover -s tests -p 'test_data_split.py' -v
python3 -m unittest discover -s tests -p 'test_evaluator.py' -v
```

Không tái tạo hoặc ghi đè thư mục artifact v1 để thêm report. Report evaluator
được ghi sang `outputs/` riêng; CLI sẽ từ chối output nằm trong frozen split.
