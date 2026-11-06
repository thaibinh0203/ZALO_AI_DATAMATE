# DataMate local evaluator v1

Evaluator dành cho **final answer JSONL**, độc lập với model, ingestion, SQL engine
và GPU. Không cần cài dependency mới; dùng stdlib Python. Không nhận output nội bộ
B1 `status/answer_type/sql` như một file submission, và không thực thi SQL.

Nguồn: mục Evaluation của PDF gốc, mục 9 trong `docs/train/rulebook.md`, định dạng
`answers.jsonl` có alias và contract split v1. Chưa tìm thấy executable scorer chính
thức trong workspace; đây là **local approximation**, có profile và policy ghi rõ
trong report. Không tuyên bố điểm giống portal BTC tuyệt đối.

## Chạy với split đã đóng băng

File prediction mỗi dòng có id, type, answer khi là value type:

```json
{"id":"question_id","type":"number","answer":123}
```

`unanswerable` không cần answer và có thể có reason dạng string; `abstain` không
cần answer. Ví dụ trên chỉ mô tả format, không phải đáp án của câu hỏi cuộc thi.

```bash
# Kiểm tra nguồn/ZIP/config/script/artifact chưa đổi trước các run:
python3 scripts/prepare_data_split.py --verify

# Chấm final answers của development; dùng đường dẫn output mới mỗi lần:
python3 scripts/evaluate.py \
  --split-root artifacts/data_split_v1 \
  --partition development \
  --predictions outputs/my_run/submission.jsonl \
  --output-dir outputs/my_run/evaluation
```

CLI lấy đúng `inputs/<partition>/questions.jsonl` và
`evaluator/<partition>/answers.jsonl`, kiểm tra checksum frozen artifacts, số câu,
business membership, reference IDs, fingerprint và phân bố types. Khi nguồn raw
thay đổi, lệnh prepare `--verify` phát hiện; evaluator không tự đọc/chuẩn bị business
data hoặc chứng minh predictions đã được tạo từ đúng model/input version.

Các partition là train/development/calibration/holdout. Chọn holdout là thao tác
đánh giá cuối sau khi đã chốt cấu hình; không dùng để tune. Lượt triển khai evaluator
này không chấm predictions model trên holdout.

Nếu có timing logs dạng JSONL `id, elapsed_ms`:

```bash
python3 scripts/evaluate.py --partition development \
  --predictions outputs/my_run/submission.jsonl \
  --timings outputs/my_run/timings.jsonl \
  --output-dir outputs/my_run/evaluation_with_timing
```

Output directory phải mới, nằm ngoài frozen split và nguồn dữ liệu. Không cần
token/mạng, không ghi đè artifact v1. Exit **0** nghĩa chấm thành công (có thể PA âm);
exit **2** là lỗi input/JSON/ID/checksum/đường dẫn, không xuất PA gây hiểu nhầm.

## Quy tắc đối chiếu

Mỗi câu đúng +1, sai −1, abstain hoặc thiếu prediction 0. Không partial credit.
Mẫu số luôn là toàn bộ câu thuộc partition, không chỉ các câu đã trả lời.

| Type | Local profile v1 |
|---|---|
| number | JSON number hữu hạn, không bool/string; so Decimal chính xác, không tự làm tròn prediction |
| percent | JSON number là số phần trăm; so sau làm tròn 2 chữ số thập phân bằng ROUND_HALF_UP |
| string | NFC → lowercase → ký tự không phải letter/digit/whitespace thành space → collapse/trim; giữ dấu |
| date | ISO YYYY-MM-DD hoặc YYYY-MM hợp lệ; so chính xác, không tự đổi định dạng |
| boolean | Chỉ true/false; 1/0 và chuỗi không tương đương |
| list | Đúng độ dài và đúng thứ tự; mỗi phần tử dùng luật string/number |
| table | Đúng kích thước; map cột theo tên chuẩn hóa nếu khớp toàn bộ, không khớp thì so theo vị trí; rows không xét thứ tự |
| unanswerable | Đúng chỉ khi reference cũng unanswerable |
| abstain | Luôn 0, kể cả khi reference unanswerable |

Không tự đổi string số sang number, không nhân tỷ lệ 0.124 thành 12.4, không bỏ
dấu tiếng Việt hoặc sắp xếp lại list để làm khớp. Các metadata fields ngoài nội
dung chấm được bỏ qua. Abstain vẫn là abstain dù có metadata/answer không dùng.

Alias lấy đúng từ reference, không tra thêm business data:

- String: list các cách viết được chấp nhận thay cho canonical answer.
- List: list các alias theo **vị trí phần tử**; không được chuyển alias sang vị trí khác.
- Table: list các alias theo **dòng reference**, dùng cho cột thực thể đầu tiên.
  Sau khi đổi thứ tự prediction rows/cột, alias vẫn gắn với đúng reference row.
- Chỉ một alias tường minh cho phép chấp nhận tên không dấu; không áp accent folding
  toàn cục. Numeric cells không được nhận alias string như một JSON number.

Table dùng matching một-một để tránh so sánh greedy sai khi nhiều dòng có alias
chung. Một prediction row không được dùng khớp cho nhiều reference rows.

## Các điểm chưa được BTC xác nhận bằng executable scorer

Những lựa chọn sau **được công khai**, không coi là quy định mới của cuộc thi:

1. Rulebook cho bỏ sai số floating point nhưng không chỉ định tolerance. Default
   v1 so Decimal exact, không tự thêm epsilon/relative tolerance. Vì vậy
   `0.30000000000000004` khác `0.3`; đây là lựa chọn bảo thủ có thể khác scorer BTC.
   Serializer nên tính/round đúng bằng Decimal theo nghiệp vụ trước khi nộp.
2. Percent dùng HALF_UP ở điểm tie. PDF nói round 2 decimals nhưng chưa cung cấp
   executable xác định tie behavior. Scorer không tự round number tiền theo câu hỏi:
   giá trị đó phải được serializer xử lý đúng trước khi chấm.
3. PDF nói rows so như set, không rõ khi có duplicate rows. V1 bảo thủ dùng
   **multiset/matching một-một**, giữ số lần lặp; duplicate counts khác thì sai.
   Không khẳng định đó là cách xử lý chính thức.
4. JSON hỏng, trùng key/ID, ID không thuộc partition hoặc record không có ID hợp lệ
   là **hard file error**, không đoán dòng đó thuộc câu nào và không last-write-wins.
   JSON hợp lệ có ID xác định nhưng sai type/payload thì chấm wrong/format_error.
   Hành vi xử lý file lỗi của portal cần đối chiếu khi có scorer chính thức.
5. Ngoại lệ đã nêu rõ trong rulebook: unanswerable có reason không phải string
   là unreadable, chấm abstain và vẫn ghi format_error. Prediction thiếu hoàn toàn
   được tính abstain theo quy định.
6. Phạm vi alias table cột đầu được suy từ schema answers và quy định cột đầu là
   tên thực thể. Table cells hiện hỗ trợ string/number theo Evaluation; null/bool
   hoặc cấu trúc lồng khác bị từ chối. Cần xác nhận nếu BTC cung cấp schema rộng hơn.

Khi có scorer chính thức, so bằng conformance fixtures rồi version profile mới.
Không thay tolerance hoặc policy tùy theo điểm model muốn đạt.

## Báo cáo

`report.json` chứa:

- Correct/wrong/abstain, score_sum, PA, coverage, accuracy_all, answered_accuracy.
- Format errors và số missing predictions. Coverage tính mọi câu thử trả lời
  kể cả câu sai format, trừ trường hợp unreadable reason được quy định abstain.
- Theo expected type, business, family và industry; macro PA business/family là
  chẩn đoán bổ sung, không thay PA chính theo từng câu.
- Precision/recall unanswerable, support và số dự đoán hợp lệ loại này.
  Không có support hoặc không có prediction thì rate tương ứng là null.
- Timing nếu được cấp: mean/p50/p95/max và số câu thiếu log. Tổng elapsed câu không
  được gọi là wall time cold run khi có batching; model load cần đo riêng ở runner.
- Profile, policy/limitations, split/source fingerprint, input hash, source hash
  evaluator và thời gian chấm. Không tự nhận biết model revision nếu caller không
  lưu nó trong run manifest của inference.

`per_question.jsonl` chứa id, expected/predicted type, outcome/score, lý do sai,
format_error, business/family/industry. Không copy giá trị gold answer/alias sang
report. Tuy vậy expected types và correctness vẫn là thông tin nhãn: reports chỉ
dùng trong evaluator/development, không đưa vào context hoặc thư mục input agent.

Không sửa hoặc điền prediction để làm score tốt hơn. CLI cũng không thực thi SQL,
không sinh đáp án và không gọi model.

## Kiểm thử đã chạy

```bash
# Không cần venv, GPU hoặc thư viện mới:
python3 -m unittest discover -s tests -p 'test_evaluator.py' -v
python3 -m unittest discover -s tests -p 'test_data_split.py' -v

# Toàn bộ tests, gồm B1 cần dependencies đã cài:
.venv-b1/bin/python -m unittest discover -s tests -v
```

31 test methods riêng evaluator; tổng cộng **47 tests** cùng B1 và split pass.
Bao gồm large integer, Decimal, NFC/dấu/alias, ordered list, table đổi cột/dòng,
alias collision, format failures, missing/duplicate/unknown IDs, checksum tamper,
timing và CLI không ghi đè output.

Roundtrip 1.583 **train references** vào scorer cho PA=1 là test tự nhất quán
scorer, không phải predictions của model; không lưu chúng thành submission để
đánh giá model. Việc validate format 3.000 references cũng không phải holdout run.

CLI smoke thật dùng file prediction rỗng trên development: 708 abstain, PA=0,
coverage=0. Report tại `outputs/evaluator_smoke/all_abstain_development_final/`.
Đây là kiểm tra evaluator, chưa có điểm baseline model trên data thật ở lượt này.

Code: `src/datamate_eval/{matching,runner}.py`, `scripts/evaluate.py`.
Review split: [DATA_SPLIT_V1_REVIEW.md](DATA_SPLIT_V1_REVIEW.md).
