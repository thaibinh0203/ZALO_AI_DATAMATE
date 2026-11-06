# DataMate — dữ liệu nguồn và split v1

Đã tạo từ `docs/train`: **30 doanh nghiệp, 3.000 câu hỏi và 3.000 đáp án**.
Dữ liệu nguồn giữ nguyên. Không đọc public test, không chạy model, không train,
không sinh đáp án hoặc SQL trong bước này.

## Kết quả

| Tập | Doanh nghiệp | Câu hỏi | Business ID |
|---|---:|---:|---|
| Train | 18 | 1.583 | 101, 102, 103, 104, 106, 107, 108, 109, 110, 113, 114, 119, 120, 121, 122, 125, 126, 128 |
| Development | 6 | 708 | 112, 115, 116, 118, 124, 130 |
| Calibration | 3 | 355 | 105, 117, 123 |
| Holdout | 3 | 354 | 111, 127, 129 |

Các số ID trong bảng có tiền tố `biz_`, ví dụ `101` là `biz_101`.
Mọi câu hỏi của một doanh nghiệp thuộc đúng một tập; không random từng câu.

## Căn cứ phân nhóm

Đọc 30 README, header của 20 CSV, schema của SQLite (17 bảng), metadata và ba
dòng đầu của **mọi sheet trong 107 workbook Excel**. Đây là khảo sát layout,
chưa kiểm tra chất lượng toàn bộ ô hoặc tính đúng các công thức.

Nhóm theo cấu trúc lõi và cách nối dữ liệu, không coi đổi tên/thứ tự cột hoặc
đổi cách đóng gói sheet/file là một template mới. Đây là phân nhóm thủ công có
căn cứ, không phải clustering đã được benchmark. Chọn thành viên theo cấu trúc,
ngành và phân bố loại đáp án, không theo điểm model hay giá trị đáp án.

| Nhóm layout | Doanh nghiệp | Tập |
|---|---|---|
| CSV phẳng, từng dòng hàng, có mã đơn | 101, 114, 125 | Train |
| Excel một sheet, không mã đơn/khách | 107, 113, 126 | Train |
| DonHang_ChiTiet + bảng quy đổi; gồm biến thể nhà thuốc | 102, 103, 104, 121, 122 | Train |
| Hóa đơn + chi tiết + danh mục POS/phần mềm; gồm biến thể lô/bàn | 106, 108, 109, 119, 120, 128 | Train |
| CSV chỉ có hóa đơn, không dòng hàng | 110 | Train |
| Sổ chi tiết bán hàng + danh mục; gồm báo cáo giao hàng/hạn nợ | 112, 115, 116, 118, 130 | Development |
| Excel phần mềm + sổ tay + CSV | 124 | Development |
| Sổ tay tháng + nhập/tồn/bảng giá, trả hàng bằng dòng âm | 105, 117 | Calibration |
| SQLite POS, khóa theo cửa hàng | 123 | Calibration |
| Năm CSV quan hệ: đơn, dòng hàng, hàng, khách, thanh toán | 111, 127, 129 | Holdout |

Nhóm được giữ nguyên giữa các tập. Một file quy đổi giống hoàn toàn giữa
102 và 103 được phát hiện bằng SHA-256; cả hai đều ở train. Không có file dữ liệu
kinh doanh giống hoàn toàn bị đặt ở hai tập khác nhau.

## Phân bố ngành

| Ngành suy ra từ dữ liệu | Train | Development | Calibration | Holdout |
|---|---:|---:|---:|---:|
| Tạp hóa | 6 | 0 | 0 | 0 |
| Cà phê | 2 | 1 | 1 | 1 |
| Vật liệu xây dựng | 2 | 3 | 1 | 0 |
| Nhà thuốc | 4 | 1 | 1 | 0 |
| Nhà hàng/quán ăn | 3 | 1 | 0 | 2 |
| Chưa xác định | 1 | 0 | 0 | 0 |

Ngành là annotation suy ra từ README, mặt hàng/quy đổi và câu hỏi, không phải nhãn
ngành chính thức. `biz_110` chỉ có hóa đơn, chưa đủ căn cứ xác định ngành.
Chi tiết và lý do từng nhóm: [cấu hình split](../configs/data_split_v1.json).

## Phân bố loại đáp án

Chỉ dùng trường `type` để thống kê; không dùng `answer`/`alias` để quyết định split.

| Loại | Train | Development | Calibration | Holdout |
|---|---:|---:|---:|---:|
| number | 1.042 | 420 | 202 | 220 |
| percent | 283 | 156 | 65 | 74 |
| list | 86 | 40 | 36 | 31 |
| date | 77 | 36 | 13 | 19 |
| string | 45 | 18 | 21 | 4 |
| boolean | 11 | 7 | 5 | 0 |
| table | 7 | 3 | 3 | 6 |
| unanswerable | 32 | 28 | 10 | 0 |

Calibration có đủ tám loại đáp án và ba ngành để chọn ngưỡng; các lớp hiếm vẫn có
ít mẫu. Development giữ họ sổ báo cáo và gói trộn nhiều nguồn để phát hiện lỗi đọc
layout và nối dữ liệu.

Holdout giữ riêng họ CSV nhiều bảng để kiểm tra khả năng xử lý layout chưa dùng
phát triển. Tập này chỉ có cà phê/nhà hàng, **không có boolean hoặc unanswerable**.
Không dùng điểm holdout một mình để kết luận chất lượng trên toàn bài toán. Tạp hóa
chỉ ở train do các ràng buộc nhóm; chưa có phép đo trực tiếp trên shop tạp hóa mới.
Chỉ một shop SQLite tồn tại và được đưa vào calibration, nên train không có SQLite.

Các tập vẫn chia sẻ thuật ngữ, tên hàng, mẫu câu phổ biến và một số quan hệ nghiệp
vụ tương tự; phân nhóm layout không loại hết mọi loại tương đồng. Nếu cần phép đo
bổ sung, tạo grouped cross-validation bên trong train, giữ nguyên holdout v1.

## Artifact và cách dùng

- [source_manifest.json](../artifacts/data_split_v1/source_manifest.json): đường dẫn tương đối với `docs/train`, kích thước, SHA-256, vai trò từng file, phiên bản nội dung rulebook/calendar và đối chiếu ZIP.
- [split_manifest.json](../artifacts/data_split_v1/split_manifest.json): membership, question IDs, nhóm layout, thống kê và giới hạn.
- [layout_audit.json](../artifacts/data_split_v1/layout_audit.json): header CSV, schema SQLite, sheet và ba dòng đầu Excel, chỉ phục vụ khảo sát.
- `artifacts/data_split_v1/inputs/<tập>/questions.jsonl`: chỉ có `id`, `business_id`, `question`, `as_of_date`.
- `artifacts/data_split_v1/evaluator/<tập>/answers.jsonl`: đáp án nguyên bản, chỉ cấp cho evaluator; train được phép dùng cho supervision của train.
- `artifacts/data_split_v1/artifact_checksums.json`: checksum các artifact sinh ra.

Không sao chép 193 MB dữ liệu kinh doanh sang bốn thư mục. Runner dùng danh sách
`business_ids` của tập để mở đúng thư mục `docs/train/data/<business_id>`. Cả bốn
tập nội bộ dùng cùng `docs/train/rulebook.md` và `docs/train/calendar.csv` đã khóa
checksum; không lấy rulebook public thay thế.

Tách đường dẫn labels giúp tổ chức dữ liệu, **chưa phải sandbox quyền truy cập**.
Khi nối runtime, chỉ cấp câu hỏi, business package và luật cho inference; không cấp
thư mục evaluator, split statistics hay toàn bộ `docs/train` chứa labels.

Train dùng cho few-shot/supervision/fine-tuning; development để sửa và so model;
calibration để chọn ngưỡng; holdout chỉ đánh giá sau khi chốt cấu hình.
Không trộn lại development/calibration/holdout vào dữ liệu tạo supervision.

## Kiểm chứng và tái lập

162 file thuộc ZIP train gốc đều khớp SHA-256 với bản giải nén. File bổ sung ngoài
ZIP được liệt kê trong manifest; `.Rhistory` và `:Zone.Identifier` được hash để
theo dõi nhưng loại khỏi dữ liệu nghiệp vụ. Script không thực thi các file này.
Phiên bản rulebook/calendar là SHA-256 nội dung, không tự bịa số release của BTC.

```bash
# Kiểm tra source, ZIP, cấu hình, script và artifact chưa đổi:
python3 scripts/prepare_data_split.py --verify

# Tái tạo sang thư mục mới; script từ chối ghi đè split đã có:
python3 scripts/prepare_data_split.py --output /tmp/datamate-split-replay

# Kiểm tra ranh giới tập, labels và phát hiện source drift:
python3 -m unittest discover -s tests -p 'test_data_split.py' -v
```

Script: [prepare_data_split.py](../scripts/prepare_data_split.py).
Không cần cài thêm thư viện hoặc sử dụng GPU.
