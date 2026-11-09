# Thiết kế điều chỉnh data pipeline và baseline sau v2

**Trạng thái: thiết kế đã thống nhất để triển khai; code hiện tại vẫn là v2.**

Tài liệu này thay thế bản ghi chú tổng hợp trước đó, giải quyết các điểm review về thứ tự xử lý, điều kiện cho phép biến đổi, chất lượng dữ liệu và kiểm tra plan–SQL. Ví dụ JSON là minh họa, không phải output model đã chạy. Tên API có thể đổi khi triển khai nhưng không được bỏ các điều kiện về dữ liệu và kiểm chứng dưới đây.

## 1. Mục tiêu và giới hạn

- Giữ nguyên dữ liệu nguồn và provenance; tránh biến đổi theo một table-role heuristic chưa đủ bằng chứng.
- Tạo một semantic plan thống nhất cho mỗi business; baseline dùng phiên bản plan đã chốt để hiểu câu hỏi và lập cách tính trước khi sinh SQL.
- Giảm lỗi phân loại dòng, grain/scope, thời gian, trạng thái, chọn nguồn, JOIN và SQL triển khai sai plan.
- Không fine-tune, embedding/reranker, persistent alias learning hoặc agent framework trong đợt triển khai này. Các phương án đó chỉ xem xét sau khi đo baseline mới.
- Model local hiện có được tái sử dụng; không gọi AI API bên ngoài, không tự mở cloud trả phí, không thay driver/global Python.
- Không đưa questions/answers vào pipeline ingest; labels chỉ do evaluator đọc sau inference. Không học mapping toàn cục từ development/test.
- Giữ output train/v2 đã có. Việc thay code không tự cho phép overwrite output, rebuild dữ liệu thật hoặc chạy inference thật.
- Một semantic plan chung không đồng nghĩa một prompt/model call duy nhất.

## 2. Ba lớp dữ liệu và ranh giới trách nhiệm

| Lớp | Nội dung | Không được hiểu nhầm thành |
|---|---|---|
| Nguồn/staging ban đầu | Raw values, ô Excel, vị trí, kiểu nguồn, parse thử và lỗi | Dữ liệu đã qua mọi biến đổi nghiệp vụ |
| Semantic plan của business | Column roles/meaning, grain, scope, keys, JOIN, policy, evidence/checks | Đáp án hoặc kế hoạch của một câu hỏi riêng |
| Calculation plan của câu hỏi | Chọn nguồn, population, các phép tính, grouping, time, JOIN, output | Quyền sửa semantic plan hoặc nguồn trong inference |

Trong semantic plan cần phân biệt:

- Column role: cột có ý nghĩa gì, ví dụ order_id/quantity/line_amount.
- Table grain: một dòng đại diện cho gì.
- Measure scope: giá trị của một cột thuộc cấp nào; có thể khác grain bảng.
- Group key: gom nhiều dòng về thực thể/sự kiện; được phép lặp.
- Row identity: khóa ứng viên xác định một dòng; cần kiểm tra riêng, có thể chưa tìm được.
- Processing policy: phép biến đổi cụ thể được cho phép và các điều kiện để áp dụng.

## 3. Thứ tự pipeline: giải quyết vòng phụ thuộc grain và phân loại dòng

```text
P0. Parser + xác định header/layout + giữ raw/provenance
→ P1. Phân loại sơ bộ các dòng rõ ràng, đánh dấu candidate/unknown
→ P2. Mapping rule-based + parse thử + profile có ghi phạm vi
→ P3. Model xây draft semantic plan từ schema/docs/samples theo nhóm
       Đồng thời xem lại các row candidates cần ngữ cảnh
→ P4. Tính/recompute checks bị ảnh hưởng bởi thay đổi mapping hoặc row kind
→ P5. Tối đa một refinement cấp bảng/grain cho mỗi bảng, rồi validate/recheck
→ P6. Chốt row decisions, mappings và processing policies được phép
→ P7. Normalize cuối + fill-forward/derived/deduplicate theo policy
→ P8. Nạp PostgreSQL mới và audit dữ liệu cuối
→ P9. Publish một phiên bản hoàn chỉnh gồm data + catalog + semantic plan + quality
→ Baseline đọc phiên bản published
```

### 3.1. Quy tắc staging ban đầu

- P0–P2 chưa fill-forward, chưa loại duplicate hoặc biến đổi phụ thuộc grain.
- Được parse thử từng ô để thu bằng chứng; giữ raw, kết quả, lỗi và partial date. Parse thử không làm mất giá trị gốc.
- Có thể dùng Python hoặc một staging schema PostgreSQL riêng để lấy thống kê; không được bắt buộc final clean views phải có trước khi suy grain.
- Dòng rõ ràng như blank/header lặp được ghi nhận trước khi profile. Dòng chưa rõ được giữ, không giả định là data để rồi quên uncertainty.
- Samples/checks luôn ghi dataset stage, row selection và semantic revision. Phân biệt all-source, provisional-data, unknown-only và final-data.
- Nếu row classification hoặc column type thay đổi, invalidate và tính lại checks phụ thuộc. Không lấy thống kê cũ làm bằng chứng cho plan mới.
- Model dùng schema và profiles để đề xuất, nhưng các con số chính xác do code/DB tính trên phạm vi được ghi rõ, không suy từ vài samples.

### 3.2. Không tạo vòng lặp mở

Một lần dựng plan có budget refinement cấp bảng/grain, được giữ xuyên các phase. Proposal cột/row candidates, retries JSON/contract và refinement cột nếu giữ từ code cũ phải có caps riêng, ghi trong config/manifest và tính mọi actual attempt; không ẩn thêm calls trong một tool. Một vòng sửa grain có thể chia thành requests giới hạn theo context nhưng phải khai báo hard cap, không chỉ đếm vòng. Recompute checks không reset budget. Nếu audit cuối phát hiện thay đổi làm policy cũ không còn đúng, không publish output đó như usable; giữ failed/needs_review cùng audit.

Rebuild là run mới có liên kết đến run trước, chỉ dùng plan/policy đã chốt hoặc yêu cầu sửa rõ ràng. Không tự tạo chuỗi rebuild để xin thêm model calls vô hạn.

## 4. Phân loại dòng và xử lý unknown

Các loại dự kiến: data/subtotal/grand_total/repeated_header/note/metadata/blank/quarantine/unknown.

`data` nghĩa là record nguồn có ý nghĩa phân tích theo grain bảng: có thể là product/customer/snapshot, không chỉ là giao dịch bán hàng. Dòng tổng trong báo cáo không bị xóa vật lý; nếu muốn dùng nó như nguồn aggregate độc lập phải khai báo scope rõ và tránh cộng lặp với chi tiết.

### 4.1. Bằng chứng phân loại

Kết hợp nhãn, vị trí ô/dòng, cấu trúc ID/quantity/amount, context trước/sau, công thức/merged cells và mô tả nguồn. Đối chiếu số chỉ hỗ trợ: tổng có thể gồm thuế/chiết khấu/làm tròn hoặc là tổng tích lũy.

- Không loại dòng chỉ vì có chữ tổng hoặc thiếu quantity.
- Rule-based xử lý cấu trúc rõ; gom candidate tương tự cho SLM với samples và những ngoại lệ đáng chú ý.
- Mapping cho một nhóm candidate phải nêu phạm vi áp dụng và điều kiện ngoại lệ, không lan truyền kết luận từ một dòng sang mọi dòng cùng nhãn một cách mù quáng.
- Model đề xuất; validator kiểm row IDs/phạm vi/evidence và loại quyết định. Không dùng confidence do model tự sinh làm proof.
- Giữ raw, vị trí và row decisions; blank/header cũng có dấu vết ở raw staging hoặc source-layout index, không cần COPY tất cả vào final analytic view.

Ví dụ unknown:

```json
{
  "kind": "unknown",
  "candidate_kind": "subtotal",
  "decision_status": "needs_review",
  "evidence_refs": ["source:row_123:label"]
}
```

### 4.2. Hành vi cuối

- Unknown không tự vào analytic data view và không bị xóa khỏi raw/candidate store.
- Không fill-forward qua unknown hoặc ranh giới nguồn/nhóm đã quy định.
- Baseline nhận quality contract ở mục 9 để biết view đang loại các dòng chưa rõ; không coi view rỗng hoặc SUM một phần là câu trả lời đầy đủ.
- Candidate/unknown không tự đồng nghĩa thiếu dữ liệu nguồn: uncertainty thường dẫn tới abstain nếu có thể ảnh hưởng câu hỏi.

## 5. Semantic plan thống nhất cho business

Plan gồm business/source IDs, schema version, source/code/config/model fingerprints, tables/columns, row decisions hoặc references, status/date mappings, quality registry và audit. Không có sidecar nghiệp vụ thứ hai được model suy luận độc lập rồi cạnh tranh với plan.

Có thể chia calls theo bảng/chunk để vừa context. Các đề xuất đều tham chiếu cùng inventory/source revision, sau đó validator kiểm consistency xuyên bảng.

### 5.1. Column semantics

Giữ tên nguồn/tên SQL, SQL type, canonical_role/semantic_role, meaning, unit và availability đã có. Rule-based là mapping ban đầu; SLM ưu tiên khớp ý nghĩa canonical trước khi tạo semantic role. Canonical bị đặt nhầm ở semantic_role được normalize và kiểm lại.

Mapping deterministic không được âm thầm bị model ghi đè. Nếu README/data cho thấy mâu thuẫn, ghi conflict/evidence và chưa cho policy dựa vào role ấy chạy. Mọi cơ chế thay mapping đã có cần quyết định có audit, không tự thêm alias toàn cục.

Nâng SQL type/operational role phải có kiểm tra toàn cột và bằng chứng theo policy parser, không chỉ vài samples parse được. IDs/leading zero được bảo toàn. Empty columns không có value evidence để ép kiểu; partial date được biểu diễn riêng thay vì giả mọi giá trị parse thành DATE đầy đủ. Giữ audit khi proposal bị reject và không bỏ cột nguồn.

### 5.2. Grain và semantic_kind

- `table_role_hint` từ role_of chỉ là hint; không tự kích hoạt biến đổi.
- `grain.kind` dùng vocabulary chuẩn nếu phù hợp.
- Hiểu grain nhưng vocabulary chưa có: kind=other và semantic_kind cụ thể, snake_case hợp lệ.
- Chưa đủ bằng chứng: kind=unknown; khác với other.
- Description/evidence giải thích một dòng là gì; identity_columns chỉ là proposal, không bắt buộc có khóa duy nhất mới được mô tả grain.
- Không tự mở rộng dictionary hoặc cấp quyền thực thi chỉ vì có semantic_kind mới.

```json
{
  "table_role_hint": "sale_lines",
  "grain": {
    "kind": "other",
    "semantic_kind": "delivery_item_per_lot",
    "description": "Một mặt hàng theo lô trong một đợt giao",
    "identity_columns": ["ma_don_hang", "lan_giao", "ma_hang", "ma_lo"],
    "status": "candidate",
    "evidence_refs": ["readme:delivery_definition"]
  }
}
```

### 5.3. Dữ liệu để suy grain và kiểm tra

Cung cấp README/rulebook, schema/meaning, samples đa dạng, các dòng cùng khóa ứng viên, related tables và JOIN candidates. Tools trả total/null/distinct, distribution số dòng mỗi nhóm, measure repetition, overlap/orphans/cardinality và phạm vi thống kê.

Một hai dòng đầu chưa đủ thấy một đợt giao trải trên nhiều mặt hàng. Samples không thay full-data statistics. Group key lặp là bình thường ở order_line; chỉ mâu thuẫn khi proposal khẳng định nó là row identity hoặc mỗi row là một event duy nhất.

### 5.4. Measure scope, aggregation và điều kiện áp dụng

Mỗi measure cần scope và quy tắc tổng hợp có điều kiện, không chỉ một nhãn sum.

| Trường hợp | Điều kiện cần biểu diễn |
|---|---|
| Tiền dòng hàng | Cộng trên population hợp lệ, tránh JOIN nhân dòng, cùng currency |
| Tổng tiền đơn lặp | Lấy một lần theo đúng khóa đơn; kiểm mâu thuẫn giá trị trong nhóm |
| Số lượng | Cùng đơn vị hoặc có conversion hợp lệ; không cộng kg với viên |
| Đơn giá | Không additive như amount; average/weighted average cần ý nghĩa và weights phù hợp |
| Snapshot tồn/công nợ | Phân biệt cộng giữa entities và cộng lặp qua nhiều thời điểm |
| Tỷ lệ/phần trăm | Cần numerator/denominator và populations; không tự cộng/average tỷ lệ |

Các trường có thể là measure_scope, aggregation, additive_dimensions, required_group_keys, unit/currency, time_semantics và preconditions; contract phải được schema hóa. Chưa rõ điều kiện thì ghi unknown, không đoán công thức.

### 5.5. JOIN và group keys

Giữ identifiers, scope cửa hàng/business, purpose, evidence, observed cardinality/orphans/fanout và checks revision. Không suy JOIN đúng nghĩa chỉ từ tên cột bằng nhau hoặc zero orphans. Phân biệt row keys, entity keys và grouping keys.

## 6. Processing policy: điều kiện để thực sự biến đổi

Grain có thể vẫn là candidate trong khi một thao tác cụ thể đủ bằng chứng để thực hiện. Policy được kiểm riêng, không chờ proof tuyệt đối về mọi semantics và cũng không chỉ tin tên grain.

Mỗi policy có operation, columns/key scope, preconditions, evidence/check refs, decision status và method. Model chỉ chọn phép biến đổi nằm trong implementation allowlist; không cung cấp code/SQL normalization tùy ý.

Trạng thái policy: proposed/enabled/disabled/needs_review. Chỉ enabled mới chạy; validator của code quyết định enabled theo contract và bằng chứng. Đây là chấp nhận thao tác theo các điều kiện được kiểm, không tuyên bố business_verified cho mọi metadata.

### 6.1. Fill-forward

Chỉ bật cho những trường được xác định là thuộc nhóm và nguồn thể hiện continuation. Khai báo:

- Các cột được điền, khóa/phạm vi nhóm và thông tin nguồn làm căn cứ.
- Reset khi đơn/store/source part đổi, gặp blank/header/non-data/unknown hoặc ranh giới nhóm khác được khai báo.
- Chỉ điền raw cell rỗng; không dùng giá trị trước để chữa parse error hoặc giá trị mâu thuẫn.
- Giữ raw, source row đã cung cấp giá trị và quality note.

Không mặc định mọi bảng có order_id/quantity đều cho fill-forward.

### 6.2. Duplicate handling

- Mặc định không loại chỉ vì hai raw rows giống nhau hoặc cùng order/product/amount.
- Phải có bằng chứng về duplicate export/record identity hoặc policy nguồn cụ thể, cùng phạm vi áp dụng.
- Hai giao dịch hợp lệ giống nhau vẫn là hai records nếu không có bằng chứng trùng.
- Đánh dấu duplicate_of kèm căn cứ, không xóa raw. Policy chưa rõ thì giữ và báo potential duplicates trong quality để baseline đánh giá.

### 6.3. Derived quantities và conversions

Dùng đúng operands, unit/store/product scope và conversion evidence. Conflict/thiếu conversion giữ unresolved; supplied base quantity và computed không khớp phải được ghi audit. Rule factor 1 chỉ áp khi rulebook/source cho phép và không có bằng chứng ngược.

## 7. Partial date: giữ thông tin đã biết và phân biệt nguồn với câu hỏi

```json
{
  "raw": "1/7",
  "normalized_date": null,
  "date_parts": {"day": 1, "month": 7, "year": null},
  "precision": "month_day",
  "resolution": "missing_year",
  "evidence_refs": ["source:date_format_day_month"]
}
```

- DATE PostgreSQL chỉ chứa ngày đầy đủ; lưu các thành phần/precision/resolution trong các cột companion có type hoặc một temporal sidecar có thể truy vấn bằng tools. Chọn và tài liệu hóa một representation chính trước code; không chỉ cất vào prose.
- Cho baseline thấy physical representation và predicates hợp lệ. Không dùng CAST tùy tiện làm mất partial date hoặc COALESCE sang năm giả.
- Bổ sung năm nguồn chỉ từ README/file/sheet/header/cột năm hoặc liên kết nguồn được xác nhận có scope. Bằng chứng xung đột thì chưa resolve.
- Phân biệt missing year, day/month ambiguity và invalid date. 31/2 không hợp lệ; 29/2 thiếu năm có thể là partial date, chưa được ép vào một năm bất kỳ.
- Không dùng năm hiện tại hoặc năm câu hỏi để sửa dữ liệu nguồn thiếu năm.
- Riêng thời gian trong câu hỏi được resolve bằng câu gốc, as_of_date và rulebook. Ví dụ as_of_date giúp hiểu tháng này, không chứng minh mọi dòng 1/7 thuộc năm đó.
- Baseline dùng date parts khi câu hỏi thực sự chỉ cần phần đó và đủ coverage; câu hỏi phụ thuộc năm chưa biết phải ghi uncertainty, không bỏ NULL dates rồi trả tổng một phần.

## 8. Status mapping theo đối tượng và scope

```text
Xác định cột/đối tượng mang trạng thái
→ Chọn vocabulary và mappings phù hợp domain/scope
→ Áp mappings đã xác nhận trong scope đó
→ SLM kiểm tra giá trị chưa biết và mapping có mâu thuẫn ngữ cảnh
→ Validator → áp mapping hoặc giữ unresolved
```

- Phân biệt alias tên cột với mapping giá trị; domain có thể là order/delivery/product/customer/khác.
- Mapping có sẵn không tự đúng cho mọi domain: đã giao của một delivery event chưa chứng minh order completed.
- Lấy distinct values có counts/coverage, không gọi model từng dòng. Vocabulary bị cap/truncated phải ghi rõ; không coi giá trị ngoài mẫu là không tồn tại.
- SLM dùng docs/table/column/related context, đề xuất mapping và evidence. Không ép mọi domain vào bốn trạng thái đơn.
- Validator kiểm target vocabulary, scope, evidence và consistency; confidence tự sinh không là proof. Chưa rõ giữ raw + unresolved.
- Lưu mapping business/table/column/domain/source fingerprint, version và provenance; không tự sửa dictionary toàn cục hoặc học xuyên development/test.
- Profile/final checks dùng cùng normalization revision để tránh raw và canonical values bị so lẫn.

## 9. Quality contract: pipeline truyền uncertainty cho baseline

Một analytic view loại unknown không có nghĩa nó đủ dữ liệu. Cần quality registry có thể truy vấn, không chỉ summary tổng số lỗi.

Mỗi issue cần: issue ID/kind, business/table/columns/row references hoặc phạm vi nguồn, stage/revision, affected operands/operations, known time/entity bounds nếu có, unknown bounds và evidence. Không được loại bỏ raw uncertainty chỉ vì không biểu diễn được đầy đủ trong predicate.

### 9.1. Gate theo câu hỏi

1. Calculation plan khai báo sources, operands, filters, grouping, unit/time và operations.
2. Tool chất lượng đối chiếu dependencies với issues trên cùng published revision.
3. Nếu chứng minh issue không ảnh hưởng target population/operation thì cho phép tiếp tục và ghi lý do.
4. Nếu có ảnh hưởng, hoặc không loại trừ được ảnh hưởng, thì resolve bằng tools trong budget hoặc abstain.

Ví dụ unknown amount thiếu ngày không được tự xem là nằm ngoài tháng đang hỏi. Ngược lại, lỗi đơn vị ở bảng không được query không làm mọi câu hỏi business phải abstain. Impact phải xét cả COUNT/existence/JOIN/filter, không chỉ SUM trên cột lỗi.

### 9.2. Phân biệt rỗng và thiếu dữ liệu

- Không có giao dịch match có thể cho 0/empty list nếu nguồn và population đủ rõ.
- Literal chưa tìm thấy qua fuzzy/sample lookup không chứng minh thiếu nguồn; có thể cần exact lookup hoặc xác nhận coverage.
- Parse NULL/partial dates/unknown statuses có thể khiến query trả một phần mà SQL vẫn thành công.
- Không tự đổi NULL thành 0 hoặc trả kết quả một phần như đáp án đầy đủ. COALESCE chỉ hợp lệ khi ý nghĩa zero đã được xác định trong plan và quality gate.
- Counts/sums staging khớp DB chỉ chứng minh faithful loading, không chứng minh phân loại dòng/metric đúng.

## 10. Versioning, publication và migration từ v2

```text
Draft semantic plan
→ Checks/refinement
→ Frozen processing decisions
→ Materialize vào output/schema mới
→ Final audit
→ Published manifest tham chiếu plan + catalog + staging/DB + quality
```

- Draft không được baseline dùng. Published artifact bất biến; thay đổi tạo revision/fingerprint mới.
- Dữ liệu và plan phải cùng snapshot. Baseline pin manifest khi bắt đầu run, không đổi metadata giữa các câu.
- Manifest có schema/semantic/policy version, source/code/config/model identity, hashes, data paths/DB schema và publication status.
- Audit cuối chỉ thêm record kết quả trước publish; nếu thay đổi decision ảnh hưởng materialization thì cần rebuild và audit lại.
- Cache theo business/source/model/prompt/schema/stage revision; thay dependency phải invalidate cache/checks liên quan.
- Giữ v2 config/reader và output đang có để đối chiếu. Thêm version/config mới để opt-in, không âm thầm trộn old catalog với new plan. Old read path có thể làm compatibility adapter, không chạy model tạo metadata độc lập mới.
- Pipeline và baseline cập nhật context contract cùng nhau, gồm các trường partial date/quality mới và phạm vi cột tools được phép đọc.

## 11. Baseline: từ câu hỏi đến đáp án

### 11.1. Luồng

```text
Câu hỏi gốc + published pipeline context
→ Rewrite/cách diễn giải + structured analysis
→ Model chọn nguồn, lấy thêm bằng chứng bằng tools khi cần
→ Calculation plan có cấu trúc + quality/feasibility checks
→ Model sinh SQL
→ SQL gates + plan–SQL checks + EXPLAIN không ANALYZE
→ Execute SELECT chỉ đọc
→ Result shape/type/quality checks
→ Final JSON hoặc quyết định có reason
```

Không tạo compiler tổng quát thay model sinh SQL trong đợt này. Model không tự cộng kết quả từ preview/retrieval. Các bước logic có thể gộp theo những contract đã kiểm thử để giảm calls.

### 11.2. Rewrite và analysis

- Luôn giữ câu gốc; context giúp hiểu viết tắt/thiếu dấu và mô tả nghiệp vụ.
- Không đổi trên thành từ ... trở lên, thêm năm/đơn vị/trạng thái thiếu bằng chứng, hoặc đổi metric sang cái dễ tính hơn.
- Rewrite không chắc thì giữ câu gốc và uncertainty; chưa rewrite được không tự động abstain nếu vẫn phân tích được câu gốc.
- Giữ canonical/semantic roles và alias metadata của pipeline. Bỏ bước binding_candidates rule-based của câu hỏi khỏi đường chọn nguồn; không match alias vẫn được model chọn nguồn theo nghĩa/context.
- Analysis gồm target/entity, metric/property, operation, filters, temporal expression/bounds, units, grouping, answer type/shape và unresolved.
- Thời gian có biểu thức gốc, as_of_date, rule refs, resolved bounds và trạng thái. Dùng resolver/validator Python cho các phép lịch được hỗ trợ; không chỉ kiểm ISO/start<end như v2. Không hỗ trợ hoặc còn ambiguity thì không tự đoán.
- Năm thiếu trong câu hỏi có thể suy theo rulebook/as_of khi được phép; khác việc thêm năm cho nguồn partial date.

### 11.3. Source binding và literal grounding

Model dùng schema/description/roles/grain/scope/keys/JOIN/quality/docs. Không buộc tên role phải giống chữ trong câu hỏi.

Cần phân biệt tên hiển thị và mã định danh, raw status và canonical status, unit/currency. Dùng vocabulary/exact lookup để kiểm literals; fuzzy candidates không tự trở thành binding đã xác nhận. Truncated vocabulary phải được xử lý như coverage hạn chế.

Không match một lần không đồng nghĩa missing_data. Không JOIN chỉ để loại trùng đơn; joins=[] hợp lệ. Self-join nếu capability chưa hỗ trợ thì báo cụ thể, không chặn vì một mã Invalid join chung rồi gọi đó là thiếu dữ liệu.

### 11.4. Calculation plan có cấu trúc

Không chỉ prose reasoning. Contract tối thiểu:

| Thành phần | Yêu cầu |
|---|---|
| References | request ID, original question, published semantic/data revision |
| Answer | type, target, units, grouping/granularity, scalar/list/table shape |
| Inputs | tables/columns, intended roles/scopes, required operands |
| Population | Structured filters, operators, literals hoặc tool refs, time intervals |
| Stages | IDs, dependencies, grain vào/ra, grouping, aggregate expressions hoặc operation specification rõ |
| JOIN | source/target refs, keys, join type, purpose, cardinality assumptions |
| Formula | Các phép tính, numerator/denominator và populations tương ứng |
| Output | projections, order, tie-break, limit và rounding theo rulebook |
| Evidence/quality | Source/rule/tool refs, checks, unresolved và feasibility status |

Ví dụ đếm đơn đạt ngưỡng: lọc hợp lệ → gom đúng khóa đơn → SUM amount từng đơn → lọc >= ngưỡng → đếm. Phải giữ sự khác nhau giữa lọc dòng amount và lọc tổng amount của đơn.

### 11.5. Kiểm tra plan và SQL: không hứa proof toàn phần

Mỗi check có status pass/fail/not_checked hoặc unsupported, evidence và phạm vi kiểm chứng. Không gọi mọi plan “đã đúng” chỉ vì JSON hợp lệ.

- Structural checks: keys/types/IDs/dependencies/allowed tables/columns, references giữa stages.
- Data checks: keys/JOIN/vocabulary/quality/units/time coverage trên đúng snapshot.
- AST checks có thể triển khai: source/columns, predicates/time bounds/thresholds, grouping, aggregation/projection và các mẫu công thức/JOIN trong phạm vi hỗ trợ.
- Model review có thể hỗ trợ phát hiện mâu thuẫn nhưng không là verifier độc lập chứng minh đúng.
- Không yêu cầu SQL phải trùng một canonical string duy nhất nếu có biểu diễn tương đương; các trường hợp checker chưa hiểu phải báo rõ. Với điều kiện quan trọng chưa xác minh được, dùng thêm checks/refinement hoặc abstain theo policy, không âm thầm coi pass.
- Không viết SQL/đáp án riêng theo question ID hoặc chọn rules để khớp gold.
- EXPLAIN kiểm planner, không chứng minh logic, kết quả hay completeness. Giữ read-only role, AST gates, timeout và result cap của baseline hiện có.

### 11.6. Refinement đúng tầng và budget

| Vấn đề | Hành động |
|---|---|
| Plan sai nguồn/khóa/công thức/population | Sửa plan theo evidence, kiểm lại dependencies và sinh lại SQL |
| Plan đủ rõ nhưng SQL lệch plan | Giữ plan, sửa SQL |
| SQL syntax/name/type/runtime lỗi | Gửi lỗi cụ thể; đổi plan chỉ khi xác định vấn đề nằm ở plan |
| Result shape/NULL/quality bất thường | Xem bindings/plan/SQL; không lấy cột đầu hoặc đổi NULL thành zero để qua gate |

Gửi previous output/plan/SQL + error + evidence/checks + những điều phải giữ nguyên. Không ép sửa chỉ để có answer. Tách reason cụ thể, ví dụ SELF_JOIN_UNSUPPORTED/UNKNOWN_COLUMN/AMBIGUOUS_LITERAL/PLAN_SQL_MISMATCH.

Default budget triển khai ban đầu, có cấu hình và ghi manifest:

- Tối đa 2 vòng repair/refinement cho mỗi câu, dùng chung các stage.
- Tối đa 8 model calls/câu, gồm tool-decision calls và retries; không bắt buộc dùng hết.
- Tối đa 12 logical tool calls/câu, kể cả validate/explain/execute và cache-hit requests; tool có thể xử lý batch identifiers với cap riêng.
- Tối đa 3 answer SQL execution attempts/câu: một lần đầu và tối đa hai lần có sửa trong budget. Query preview/profiles là tool calls riêng có giới hạn; không tính là answer attempts.
- Mỗi SQL trả lời timeout tối đa 10 giây, cap 1.000 result rows như cấu hình v2. Metadata/profiling tools có timeout/row/sample caps riêng, phải khai báo.
- Mỗi vòng sửa có thể cần hơn một model call; mọi call vẫn trừ hard caps. Hết bất kỳ budget liên quan thì dừng, ghi reason và abstain.

Các số này là giới hạn ban đầu để kiểm thử, không phải số đo chất lượng/latency đã đạt. Pipeline grain refinement một lượt/bảng là budget khác và không được baseline dùng để thay đổi published metadata.

### 11.7. Abstain và unanswerable

| Tình huống | Kết quả |
|---|---|
| Hiểu câu hỏi, có evidence thiếu nguồn/operand cần thiết | unanswerable + requirements/evidence |
| Ambiguity ảnh hưởng answer, chưa giải quyết được | abstain với semantic reason |
| Unknown data/partial date/status có thể ảnh hưởng metric | resolve trong budget hoặc abstain với quality reason |
| Không match alias/cột/literal ở lần thử đầu | Tiếp tục kiểm context/tools, chưa kết luận thiếu dữ liệu |
| SQL/context/timeout/budget/capability failure | abstain với technical/capability reason, không giả missing_data |
| Query rỗng nhưng nguồn/filters/coverage được xác nhận | Trả zero/empty theo contract nếu thực sự là nghĩa của câu hỏi |

Không bắt buộc README phải có nguyên câu “không có dữ liệu” mới nhận unanswerable: evidence có thể gồm source inventory/availability và yêu cầu đã hiểu. Tuy nhiên không tìm thấy qua sample hoặc retrieval không chứng minh không tồn tại. Lưu căn cứ để review.

## 12. Tools và context adapter

Một lớp context adapter đọc published output, kiểm hashes/versions, dựng prompt và execution allowlist. Có thể gộp ContextLoader/SemanticContextLoader về một API cho version mới; không bỏ chức năng kiểm tra hoặc đưa nguyên mọi log/raw row vào prompt.

| Tool dự kiến | Trách nhiệm |
|---|---|
| describe_tables | Schema/semantics và keys của nguồn trong business |
| get_source_context | README/rules/evidence theo refs; giữ original source text |
| preview_rows | Samples có scope, giới hạn và provenance; có grouped sampling |
| get_column_values / resolve_value | Vocabulary, exact matches và candidates, có coverage/truncation |
| profile_keys_and_joins | Counts/uniqueness/repetition/overlap/cardinality/fanout |
| resolve_time | Time question theo as_of/rules, ghi supported/ambiguous/unsupported |
| check_data_requirements | Đối chiếu dependencies của plan với quality registry và source availability |
| validate_plan / validate_sql | Contract/identifiers/dependencies và các checks có phạm vi rõ |
| explain_sql | Planner check không ANALYZE |
| execute_sql | Answer SELECT đã qua gates, read-only, timeout/cap |
| validate_result | Output shape/type và quality conditions của plan |

Tên API có thể gộp/tách nhưng mọi logical request phải log stage/arguments/source revision/cache/result/error/timing. Không bắt buộc mọi tool cho mọi câu. Tools chỉ truy cập business/columns được cấp, không cho model code execution tùy ý hoặc sửa nguồn/metadata. Không nới SQL privileges để đọc candidate store: metadata/quality tools lấy evidence qua API có kiểm soát.

Context dài được chia theo inventory và evidence refs, tải phần cần bằng tools. Đo token trước generate; không cắt rule/condition quan trọng âm thầm. Không tăng context hoặc đổi model mặc định để che contract quá dài; ghi giới hạn chưa chạy thật.

## 13. Kiểm thử và đánh giá theo từng lớp

### 13.1. Fixtures dữ liệu

- Subtotal đa dạng, header lặp, note, merged/formula, sản phẩm chứa tổng, dòng phí thiếu quantity.
- Grain inference trước/sau row decisions: checks bị invalidate/recompute đúng; samples theo nhóm thay vì chỉ first rows.
- Other + semantic_kind có evidence khác unknown; group keys lặp khác invalid row identity.
- Fill-forward ranh giới/unknown/raw parse error; legitimate identical rows không bị drop như duplicate.
- Price/amount/snapshot/unit/currency và conditional aggregation; không SUM đơn giá như doanh thu.
- Partial date có/thiếu/conflicting year, ambiguous day/month, 29/2 chưa có năm, invalid 31/2.
- Status đã biết ở sai domain, status mới, vocabulary truncated và unresolved.
- Quality impact liên quan/không liên quan đến metric; unknown date không được coi ngoài filter.
- Refinement hết budget, audit fail, rebuild mới, immutable publication, cache invalidation và old/new version mismatch.

### 13.2. Fixtures baseline

- Rewrite giữ câu gốc/ngưỡng strict-inclusive/units/time, fallback khi không rewrite được.
- Không match alias vẫn chọn đúng nguồn; names khác IDs, role giống nhưng scope khác.
- Plan đúng/SQL sai và ngược lại; grouping trước threshold, numerator/denominator, JOIN fanout, final shape.
- Time resolved theo as_of/rulebook không theo host clock; không lấy query year sửa source year.
- Unanswerable có evidence khác SQL lỗi/quality uncertainty; query empty/zero không bị gán thiếu dữ liệu máy móc.
- Tool permissions/caps, SQL READ ONLY, EXPLAIN không ANALYZE, rollback sau lỗi và budgets áp mọi route.
- Mock model/tool tests cho cả malformed outputs/refinement fail; tests mock pass chưa chứng minh local model thực tuân contract.

### 13.3. Ablations và thực nghiệm model

Giữ cùng model/pinned revision/question IDs để so:

1. V2 làm mốc; ghi rõ run cũ hiện interrupted 34/50, không gọi là full 50.
2. Pipeline mới với khả năng baseline tương đương qua compatibility path.
3. Rewrite/analysis/binding mới.
4. Calculation plan + bounded repairs.
5. Tools/evidence lookup bổ sung.

Đo cả cuối cùng và theo stage: PA/correct/wrong/abstain, answered accuracy/coverage, lỗi phân loại dòng và false exclusions, quality unresolved, grain/scope review, JSON/SQL/shape errors, model/tool calls/tokens/cache/latency/VRAM. Không suy PA tăng chỉ vì lỗi JSON giảm.

Chạy synthetic/code checks trước; model smoke nhỏ khi được yêu cầu, rồi mới batch lớn. Development đã inspect là diagnostic/regression set; giữ frozen split, không dùng calibration/holdout hoặc gold làm prompt/học aliases. Evaluator là local approximation, ghi rõ giới hạn so scorer chính thức.

## 14. Thứ tự triển khai và tiêu chí hoàn tất

1. Chốt schemas/contracts/versioning/fixtures, không bắt đầu bằng prompt prose dài.
2. Raw/provisional staging + preliminary row classification + scoped profiles.
3. Unified semantic plan + checks/refinement + processing policies.
4. Partial dates, scoped statuses và queryable quality registry.
5. Materialization/audit/publication và compatibility loaders.
6. Baseline context-aware understanding/binding/calculation plan.
7. Read-only tools, plan–SQL checks, bounded repairs, serializer/logs/reports.
8. Synthetic/integration checks, docs/lệnh chạy và báo phần chưa kiểm chứng bằng model.

Hoàn tất code khi các contract/quality/budget paths có tests có ý nghĩa, old v2 còn chạy được, source/output cũ không bị overwrite, không có gold/per-question SQL hard-code, logs phân biệt checks thực hiện với chưa thực hiện, và có lệnh reproducible cho smoke/eval. Code hoàn tất không đồng nghĩa đã chứng minh cải thiện model trên dữ liệu thật.

Tài liệu đọc code hiện tại: [Data pipeline](DATA_PIPELINE.md), [Pipeline từng hàm](DATA_PIPELINE_WALKTHROUGH.md), [Baseline v2](BASELINE_GROUNDED.md), [Baseline từng hàm](BASELINE_V2_WALKTHROUGH.md), [Evaluator](EVALUATOR.md).

Prompt triển khai: [PIPELINE_BASELINE_IMPLEMENTATION_PROMPT.md](PIPELINE_BASELINE_IMPLEMENTATION_PROMPT.md).
