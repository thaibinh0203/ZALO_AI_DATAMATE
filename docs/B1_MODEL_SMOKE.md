# B1 — Local model smoke test, sinh SQL offline

Triển khai riêng phần B theo mục 20 của `DATAMATE_PROJECT_BRIEF.md`.
Đây là công cụ sinh và kiểm tra **kế hoạch SQL**, không phải runner nộp bài,
không thực thi SQL, chưa nối BusinessContext/QueryTool của A.

Cập nhật sau khi triển khai [data pipeline PostgreSQL](DATA_PIPELINE.md): CLI/prompt
hiện mặc định `--dialect postgres`. Các kết quả smoke test lịch sử bên dưới dùng
DuckDB; để tái lập chúng, truyền `--dialect duckdb`. Thay đổi dialect chưa phải
bằng chứng model đã được kiểm tra lại trên database thật.

## Khảo sát thực tế ngày 05/11/2026

- Workspace Linux/WSL2, Ubuntu 26.04, kernel `6.6.87.2-microsoft-standard-WSL2`.
- Global Python 3.14.4; Python 3.12.13 đã có sẵn, được dùng tạo `.venv-b1` và
  `.venv-b1-gpu` riêng; Python 3.12 global ban đầu không có torch/Transformers.
  Không sửa global Python, driver hay code A/C. Không tìm thấy `AGENTS.md` áp dụng.
  Workspace ban đầu chỉ có tài liệu; `git status`/`git rev-parse` báo không phải Git
  repository; thư mục `.git` hiện không cung cấp metadata Git sử dụng được.
- RAM tổng 9,71 GiB, available khoảng 7,4–8,3 GiB tùy thời điểm; swap 16 GiB.
  Đĩa trống trước chuẩn bị khoảng 936 GiB, sau chuẩn bị khoảng 916 GiB.
- Trong sandbox: `nvidia-smi` báo `GPU access blocked by the operating system`,
  không thấy `/dev/dxg`. **Ngoài sandbox** đã xác minh `/dev/dxg` và GPU
  **RTX 4080 SUPER, 16.376 MiB VRAM, trống khoảng 14.488–14.547 MiB, driver 610.88**.
  Giới hạn ban đầu thuộc sandbox; cần chạy kiểm tra và inference GPU với quyền truy cập host phù hợp.
- PyTorch kiểm tra trong venv: `2.7.1+cpu`, `torch.version.cuda = None`,
  `torch.cuda.is_available() = False`, device_count=0. Đây là CPU build phục vụ
  kiểm tra tooling, không phải kiểm chứng tương thích CUDA trên máy này.
- Venv GPU đã kiểm tra thành công: `torch==2.7.1+cu128`, CUDA runtime 12.8,
  available=true, device_count=1, tensor_check=true; đã load/generate BF16 trên GPU.
- `/tmp` là tmpfs riêng chỉ 4,9 GiB: cache cài GPU đã từng làm đầy /tmp.
  Cache được chuyển sang `.cache/uv` trên ổ dự án; hướng dẫn dưới đây dùng vị trí đó.
  Script chẩn đoán báo cả đĩa dự án và temporary_disk.
- Mạng sandbox ban đầu lỗi DNS. Sau khi được phép chạy bước chuẩn bị có mạng,
  đã cài dependencies và tải đủ weights/tokenizer/config/license từ Hugging Face.

Chọn **Qwen/Qwen3-4B**, revision
`1cfa9a7208912126459214e8b04321603b3df60c`, chưa chọn 8B.
Metadata safetensors tại revision này ghi **4.022.468.096 tham số**;
index ghi weights BF16 **8.044.936.192 bytes ≈7,49 GiB**.
KV cache, activations, CUDA kernels và bộ nhớ tạm còn cần thêm; không thể lấy
kích thước weights làm tổng VRAM. Mốc dự trù GPU 12–16 GiB cho context 4096,
batch 1 chỉ là ước lượng cần đo lại trên GPU thật. CPU float32 riêng weights
đã khoảng 14,98 GiB, vượt RAM hiện tại; không ép load hoặc dùng swap để thử 4B.
4B BF16 phù hợp với VRAM đã xác minh để thử context 4096, batch 1.
8B BF16 riêng weights gần hết 16 GiB; không chọn 8B làm mặc định.

Cấu hình đã chạy: BF16, không quantization, `cuda:0`, context tổng 4096 tokens,
output tối đa 512 tokens, batch 1, seed 42, greedy (`do_sample=False`),
`enable_thinking=False`, attention SDPA. Greedy được chọn theo brief để dễ so
sánh baseline; đây không phải thông số sampling khuyến nghị của model card.
Seed/greedy không bảo đảm GPU tuyệt đối deterministic.

## Nguồn đã đọc và quyết định dependencies

Đã đọc brief và ba PDF trong `docs`: Description/Rules/Evaluation (4 trang,
gồm trang Evaluation dạng ảnh), Docker guideline (10 trang), thể lệ tiếng Việt
(15 trang). Không có training package, rulebook hoặc calendar thật trong workspace.
Quy tắc trong `examples/b1_rules.md` chỉ dùng demo synthetic. Các tài liệu có
quy định quota/tie-break ở mức chung và mức DataMate khác nhau; B1 không triển
khai nộp bài, không suy diễn thành luật demo.

- [Qwen3-4B model card](https://huggingface.co/Qwen/Qwen3-4B): hỗ trợ từ
  Transformers 4.51.0, cách dùng chat template và `enable_thinking=False`.
  Chọn Transformers 4.51.3, accelerate 1.6.0, huggingface-hub 0.30.2.
- [Qwen3 release](https://qwenlm.github.io/blog/qwen3/): ngày phát hành gốc
  29/04/2025, Apache 2.0. LICENSE tại revision đã được tải và hash trong manifest.
- [PyTorch previous versions](https://pytorch.org/get-started/previous-versions/#v271):
  PyTorch 2.7.1 có wheel CPU/cu128. Docker guide của cuộc thi cũng nêu 2.7.1+cu128;
  đã kiểm chứng cu128 trên RTX 4080 SUPER, chưa kiểm chứng RTX 5090/Docker cuối.
- SQLGlot 26.16.4 được dùng parse AST DuckDB và đối chiếu schema, không chạy engine.
  Không cài vLLM, flash-attn, embedding hoặc framework agent.

`requirements-b1.txt` khóa dependencies trực tiếp. `configs/b1-environment.freeze.txt`
ghi toàn bộ phiên bản của venv đã kiểm tra, gồm pypdf/Pillow chỉ dùng đọc tài liệu;
hf-transfer chỉ phục vụ download. Snapshot GPU đầy đủ (gồm NVIDIA libraries) ở
`configs/b1-gpu-environment.freeze.txt`. Các snapshot này là bằng chứng môi trường,
không phải lock production đa nền tảng. Python 3.12 được chọn vì đã
có sẵn và đã xác nhận import stack này; không cần tải Python 3.11 hay đổi global.

## Chuẩn bị environment

Chạy tại thư mục gốc dự án. Các lệnh cài/download cần mạng; inference bên dưới
không cần mạng. `uv` đã có sẵn trên máy. Cache nằm trong workspace để tránh lỗi
quyền ghi vào home.

```bash
export UV_CACHE_DIR="$PWD/.cache/uv"
uv venv --python python3.12 .venv-b1
uv pip install --python .venv-b1/bin/python torch==2.7.1 --index-url https://download.pytorch.org/whl/cpu
uv pip install --python .venv-b1/bin/python -r requirements-b1.txt
.venv-b1/bin/python scripts/check_environment.py --output outputs/b1/environment.json
uv pip check --python .venv-b1/bin/python
```

Để chạy model thật trên một GPU đã truy cập được, tạo **venv GPU riêng** và
dùng wheel cu128 thay cho CPU, không thay driver hoặc torch của environment khác:

```bash
uv venv --python python3.12 .venv-b1-gpu
uv pip install --python .venv-b1-gpu/bin/python torch==2.7.1 --index-url https://download.pytorch.org/whl/cu128
uv pip install --python .venv-b1-gpu/bin/python -r requirements-b1.txt
.venv-b1-gpu/bin/python scripts/check_environment.py --output outputs/b1/environment-gpu.json
```

Nếu index PyTorch dẫn dependency tới `pypi.nvidia.com` và bị timeout,
có thể tách tải wheel rồi cài **đầy đủ** dependencies từ PyPI (không dừng sau
`--no-deps`):

```bash
uv pip install --python .venv-b1-gpu/bin/python torch==2.7.1 --index-url https://download.pytorch.org/whl/cu128 --no-deps
uv pip install --python .venv-b1-gpu/bin/python -r requirements-b1.txt
uv pip check --python .venv-b1-gpu/bin/python
```

Chỉ tiếp tục load CUDA khi báo cáo có `available=true`, tensor_check thành công
và đủ VRAM. BF16 cần GPU hỗ trợ; có thể chọn `--dtype float16` trên GPU phù hợp.
Không tự động fallback CPU. `--device cpu --dtype float32` là lựa chọn rõ ràng
cho máy khác đủ RAM, không phù hợp với RAM của workspace đã khảo sát.

## Chuẩn bị model có mạng

Lệnh metadata-only **đã chạy thành công**, không tải trọng số:

```bash
HF_HOME="$PWD/.cache/huggingface" .venv-b1/bin/python scripts/download_model.py \
  --repo-id Qwen/Qwen3-4B \
  --revision 1cfa9a7208912126459214e8b04321603b3df60c \
  --model-path models/qwen3-4b --metadata-only
```

Nếu HTTP thường quá chậm, backend chuẩn bị `hf-transfer==0.1.9` đã được thử
thành công để tải weights song song. Chỉ cài trong venv chuẩn bị và thêm
`HF_HUB_ENABLE_HF_TRANSFER=1` trước lệnh download:

```bash
uv pip install --python .venv-b1/bin/python hf-transfer==0.1.9
HF_HOME="$PWD/.cache/huggingface" HF_HUB_ENABLE_HF_TRANSFER=1 .venv-b1/bin/python scripts/download_model.py \
  --repo-id Qwen/Qwen3-4B --revision 1cfa9a7208912126459214e8b04321603b3df60c \
  --model-path models/qwen3-4b
```

Không cần hf-xet cho cấu hình này: thử Xet ở mạng hiện tại không tiến triển;
hub 0.30.2 chưa hỗ trợ biến HF_HUB_DISABLE_XET nên nếu đã cài hf-xet mà muốn
dùng HTTP, gỡ riêng package đó khỏi venv chuẩn bị. Không tác động runtime GPU.

Weights **đã tải đầy đủ** cùng revision bằng script này; manifest đã có checksum
cả ba shard. Lệnh tải đầy đủ để chạy lại: Có thể dùng venv CPU để download vì đây chỉ
là bước tải file, sau đó dùng venv GPU inference.

```bash
HF_HOME="$PWD/.cache/huggingface" .venv-b1/bin/python scripts/download_model.py \
  --repo-id Qwen/Qwen3-4B \
  --revision 1cfa9a7208912126459214e8b04321603b3df60c \
  --model-path models/qwen3-4b
```

Script tải tokenizer/config/chat template, README/LICENSE và safetensors;
không tải PyTorch `.bin`, GGUF hoặc weights định dạng trùng lặp. Mọi tải đều
dùng resolved commit SHA. Tham số revision bắt buộc; muốn resolve nhánh/tag
trong bước online phải thêm `--resolve-revision`, ví dụ `--revision main`.
Không có resolve từ remote trong inference.

`models/qwen3-4b/manifest.json` ghi repo, revision, nguồn, license, ngày phát hành,
dtype/quantization, tổng tham số/cách xác định, đường dẫn local, versions của bước chuẩn bị,
trạng thái weights, size và SHA-256 từng file. Trong quá trình chuẩn bị bị ngắt,
manifest giữ `weights_downloaded=false`; có thể chạy lại cùng revision.
Thư mục có model/revision khác bị từ chối để tránh trộn snapshots.
Không tự coi model name “4B” là số tham số chính xác.

Bản manifest đầy đủ được lưu ở `docs/B1_MODEL_MANIFEST.json` để review;
manifest dùng thực tế luôn là file trong thư mục model. `.gitignore` loại models,
venv, cache, weights và outputs. Không có commit Git được tạo trong phiên này.

## Chạy offline

Sau khi tải đầy đủ và xác nhận GPU, chạy cả ba câu trong **một process**, load một lần:

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv-b1-gpu/bin/python scripts/smoke_sql.py \
  --model-path models/qwen3-4b \
  --schema examples/b1_schema.sql --rules examples/b1_rules.md \
  --questions-file examples/b1_questions.jsonl \
  --device cuda:0 --dtype bfloat16 \
  --max-new-tokens 512 --max-context-tokens 4096 --seed 42 \
  --output outputs/b1/offline-smoke.jsonl
```

Hoặc nhập câu hỏi tùy ý, dùng ngày tham chiếu do người gọi cấp:

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv-b1-gpu/bin/python scripts/smoke_sql.py \
  --model-path models/qwen3-4b \
  --schema examples/b1_schema.sql --rules examples/b1_rules.md \
  --question 'Có bao nhiêu giao dịch hợp lệ hôm qua?' --as-of-date 2026-03-04 \
  --device cuda:0 --dtype bfloat16 --output outputs/b1/custom.jsonl
```

Với JSONL, `--as-of-date` nếu có sẽ override ngày của mọi câu. Schema/rules/model
path đều nhận đường dẫn local, không hard-code ổ Windows hoặc username.
CLI từ chối ghi đè output/run log đã tồn tại; dùng tên mới khi chạy lại.
Exit code: 0 = mọi output qua validation; 1 = ít nhất một lỗi generation/parse/
contract; 2 = lỗi chuẩn bị/load (argparse cũng dùng 2 cho đối số không hợp lệ).
Lỗi model không bị đổi thành câu trả lời `unanswerable` giả.

Mỗi dòng log JSONL chứa câu hỏi/ngày, messages thực tế, raw_output nguyên trạng,
parsed_json sau decode JSON, parsed_result sau validation, parse_error riêng với
contract_error và generation_error, token counts, generation/elapsed_ms, model
revision/config/versions và prompt_version. JSON hợp lệ nhưng sai contract vẫn giữ
parsed_json, còn parsed_result=null và valid=false. `reasoning_generated=null` ở chế độ
non-thinking, `executed_code=null`, `output=null` vì chưa chạy SQL.
File `<output>.run.json` ghi cấu hình chung, hash input, load_ms riêng, tổng thời gian,
số lỗi và trạng thái hoàn tất. Lỗi load được lưu dù chưa tạo JSONL. Log này là
tooling B1, không phải schema log final của C.

## Luồng schema và câu hỏi thành SQL

1. CLI đọc DDL, quy tắc demo và câu hỏi/ngày; kiểm tra câu hỏi và ISO date. JSONL
   có trường phụ/nhãn sẽ không được chuyển vào prompt. ID chỉ dùng log.
2. `build_messages()` ghép system contract với user JSON chứa schema, rules,
   question, as_of_date. Không có few-shot SQL hoặc đáp án mong đợi.
3. Tokenizer local áp dụng **chat template Qwen3** với non-thinking. Văn bản được
   đổi thành token IDs; kiểm tra tổng prompt + output budget, không cắt prompt ngầm.
4. `LocalModel.load()` nạp một lần từ local path, `local_files_only=True`,
   `trust_remote_code=False`, safetensors. Cờ offline được đặt trước import
   Transformers; `generate()` dùng inference mode và chỉ decode tokens mới.
5. Model tự chọn bảng/cột, metric, join, ngày và bộ lọc từ nội dung đã cho.
   Nếu thiếu dữ kiện bắt buộc thì đề xuất `unanswerable`; nếu chưa đủ tin cậy
   thì `abstain`. Không có nhánh Python nhận diện ba câu và trả SQL có sẵn.
6. Parser nhận đúng một JSON object với bốn keys. `sql` cần answer_type hợp lệ,
   SQL không rỗng và không có missing_requirements. Hai trạng thái còn lại cần
   sql=null, answer_type trùng trạng thái; unanswerable phải nêu dữ kiện thiếu.
   Reject duplicate keys, NaN/Infinity, Markdown fence, prose thừa và JSON truncated.
7. SQLGlot parse DuckDB AST, chỉ nhận một SELECT/CTE/set query, đối chiếu bảng/cột,
   từ chối mutation, ngày máy và table function đọc file. Không chỉ nhìn tiền tố
   SELECT. Kiểm tra này **không phải sandbox, không chứng minh SQL đúng nghiệp vụ**;
   SQL không được thực thi trong B1. Output chạm token budget bị đánh lỗi bảo thủ.

Ngày “tuần rồi” được model suy từ as_of_date theo quy tắc thứ Hai trong prompt.
Test có calendar oracle độc lập xác nhận `2026-03-04` ứng với tuần từ
`2026-02-23` đến trước `2026-03-02`; các mốc đáp án này không nằm trong prompt.
Không có dữ liệu chi phí/COGS trong fixture; không cho phép đổi lợi nhuận thành
doanh thu. Chạy thật cho thấy model còn lỗi thời gian/tổng hợp; không coi calendar
oracle của test là bằng chứng model đã tự tính đúng kỳ.

## Checks đã chạy và phần chưa kiểm chứng

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv-b1-gpu/bin/python -m unittest discover -s tests -v
.venv-b1-gpu/bin/python -m compileall -q src scripts tests
uv pip check --python .venv-b1-gpu/bin/python
```

- 13 tests pass: strict JSON/contract, giữ parsed JSON khi contract sai,
  ngày không hợp lệ, calendar boundary,
  SQL tương đương/join/CTE, SQL lỗi/mutation/file/unknown columns, prompt không lộ
  nhãn, token budget, lỗi generation, lỗi load và log, load một lần cho nhiều câu.
- Test stack tùy chọn dùng tokenizer Qwen3 thật + model **cực nhỏ khởi tạo ngẫu nhiên**,
  không tải weights; thực sự chạy local load/generate trên CPU, chặn socket connect,
  xác nhận template non-thinking và chỉ decode token mới. Đây chỉ là kiểm tra API,
  không là Qwen3-4B smoke test hoặc bằng chứng khả năng sinh SQL.
  Test này tự skip ở checkout chưa có tokenizer metadata local.
- Import torch/Transformers/Qwen3ForCausalLM/accelerate/SQLGlot pass;
  compileall pass; uv pip check pass.
- Online metadata và **toàn bộ weights** download thành công tại SHA cố định;
  đã ghi checksum SHA-256 các file trong manifest.
- Đã gọi CLI offline trên ba câu với snapshot metadata-only bằng venv CPU:
  exit 2, `Manifest is metadata-only; download weights first`, lưu
  `outputs/b1/smoke.jsonl.run.json`. Không tạo raw outputs/parsed answers giả.
- **Đã chạy inference thật ba lần**, mỗi process load một model cho cả ba câu;
  prompt v1/v2/v3 không chứa SQL hoặc ngày đáp án kỳ vọng. Giữ nguyên mọi output.
  Lần cuối dùng `b1-v3`: **3/3 strict JSON, 2/3 qua contract**, CLI exit 1 đúng thiết kế.
  Câu doanh thu và unanswerable về lợi nhuận phù hợp khi đọc kế hoạch; chưa chạy SQL.
- Lần v3: load 8,53 giây; generation ba câu lần lượt 3,12 / 5,89 / 3,27 giây;
  khối load/generate/log đo 23,25 giây. Peak PyTorch allocated 7,67 GiB, reserved 8,22 GiB;
  đây là thống kê allocator, không phải tổng VRAM process/device.
- **Lỗi còn lại ở câu “tuần rồi”**: dùng DATE_SUB không được policy DuckDB chấp nhận,
  dùng 7 ngày gần nhất thay tuần lịch trước, nhóm theo sale_date làm sai tổng hợp;
  bị validator từ chối, parsed_result=null. Không sửa tay SQL hoặc raw output.
- v1 còn dùng CURRENT_DATE; v2 đổi sai lợi nhuận thành doanh thu dù JSON/AST qua được.
  v2 không được giữ làm prompt mặc định. v3 vẫn chưa giải quyết câu tuần trước.
  Output: `outputs/b1/gpu-smoke.jsonl`, `gpu-smoke-v2.jsonl`, `gpu-smoke-v3.jsonl`,
  kèm `.run.json`. v2/v3 lưu messages trực tiếp; v1 có snapshot prompt được dựng lại
  ngay từ input/prompt v1 chưa sửa ở `outputs/b1/b1-v1-prompt.json` và được ghi rõ nguồn.
- **Chưa kiểm chứng**: thực thi/giá trị kết quả SQL, full competition semantic accuracy,
  RTX 5090, tái lập hai run cùng prompt trên GPU, container network-none, PA hoặc runtime
  toàn test. Offline flags/local_files_only đã dùng; chặn socket được thử riêng với
  tiny model, không tuyên bố đã chạy container cô lập mạng.

Báo cáo môi trường và kết quả kiểm tra tóm tắt được lưu trong
`docs/B1_VALIDATION_REPORT.json`; output chạy tạm nằm trong `outputs/b1/` (ignored).

## Xử lý lỗi

- GPU access blocked/thiếu `/dev/dxg`: trong phiên này GPU bị che bởi sandbox và
  dùng được khi chạy ngoài sandbox với quyền đã cấp. Với môi trường khác, kiểm tra
  quyền GPU của phiên WSL/host; cài CUDA wheel không tự chữa giới hạn truy cập.
- `CUDA unavailable`: kiểm tra lại báo cáo từ **venv GPU**. CPU wheel không có
  CUDA; không diễn giải `cuda=False` của CPU build thành bằng chứng thiếu driver.
- OOM: giảm context/output budget, batch giữ 1; đo free VRAM trước load. Không
  chuyển thẳng sang 8B hoặc float32 CPU; quantization chưa thuộc cấu hình đã kiểm.
- `KeyError: qwen3`: dùng đúng Transformers đã pin; không dùng bản <4.51.0.
- Metadata-only/thiếu local files: chạy download đầy đủ khi có mạng/phần cứng,
  dùng cùng SHA và thư mục; inference không tự tải để chữa lỗi.
- DNS/cache read-only: bước cài/download cần quyền mạng, đặt UV_CACHE_DIR/HF_HOME
  như trên. Không đặt token vào source hoặc log.
- `No space left on device`: kiểm tra riêng `df -h /tmp .`; đặt UV_CACHE_DIR
  trên ổ dự án, không đưa stack CUDA nhiều GB vào tmpfs 4,9 GiB. Không cần xóa weights.
- JSON lỗi/truncated: xem raw_output/parse_error/contract_error, điều chỉnh prompt hoặc output
  budget trong lần chạy mới. Không sửa tay output để làm pass; không xem JSON
  hợp lệ là chứng minh câu trả lời đúng.

Bước tiếp theo sau B1 là nhận BusinessContext và nối QueryTool từ A, rồi kiểm tra
ngữ nghĩa/thực thi cùng evaluator của C. Phần đó chưa được triển khai ở đây.

## File bàn giao

Tạo mới 20 file trong workspace (không sửa brief/PDF gốc):

| Nhóm | File |
|---|---|
| Chuẩn bị/chạy | `scripts/check_environment.py`, `scripts/download_model.py`, `scripts/smoke_sql.py` |
| Phần B | `src/datamate_b/__init__.py`, `model.py`, `prompts.py`, `contracts.py` |
| Synthetic | `examples/b1_schema.sql`, `examples/b1_rules.md`, `examples/b1_questions.jsonl` |
| Kiểm thử | `tests/test_b1.py`, `tests/test_b1_stack.py` |
| Cấu hình | `configs/b1_model.json`, `configs/b1-environment.freeze.txt`, `configs/b1-gpu-environment.freeze.txt` |
| Dependency/Git | `requirements-b1.txt`, `.gitignore` |
| Tài liệu/bằng chứng | `docs/B1_MODEL_SMOKE.md`, `docs/B1_MODEL_MANIFEST.json`, `docs/B1_VALIDATION_REPORT.json` |

Ngoài ra có assets local trong `models/qwen3-4b`, hai venv, `.cache` và logs trong
`outputs/b1`, đều được ignore. Không tạo commit hoặc thay metadata Git hiện có.
