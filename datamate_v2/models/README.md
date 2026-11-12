# Local models

Ba lựa chọn độc lập, khóa revision/số tham số/nguồn tại `registry.json`:

| config key | Model | Tham số thực tế |
|---|---|---:|
| qwen3_4b | Qwen/Qwen3-4B | 4,022,468,096 |
| qwen25_7b | Qwen/Qwen2.5-7B-Instruct | 7,615,616,512 |
| gemma4_12b | google/gemma-4-12B-it | 11,959,730,224 |

Gemma: https://huggingface.co/google/gemma-4-12B-it — Apache 2.0, release nguồn Google 03/06/2026.
Qwen3: https://huggingface.co/Qwen/Qwen3-4B — Apache 2.0, 29/04/2025.
Qwen2.5: https://huggingface.co/Qwen/Qwen2.5-7B-Instruct — Apache 2.0, 19/09/2024.
Số tham số lấy từ safetensors metadata tại revision được khóa, không làm tròn theo tên model.

Weights Qwen3-4B hiện có đã được chuyển sang đây. Hai model còn lại cần chuẩn bị ở máy có Internet:

```bash
# Từ repository root, explicit online preparation:
python datamate_v2/scripts/prepare_model.py --model qwen25_7b --download
python datamate_v2/scripts/prepare_model.py --model gemma4_12b --download
# Chỉ tạo/kiểm checksum cho weights đã có, không download:
python datamate_v2/scripts/prepare_model.py --model qwen3_4b
```

Mỗi thư mục gồm config.json, tokenizer_config.json, vocabulary, safetensors/shards/index,
chat template nếu model dùng và artifact_manifest.json chứa SHA-256. Không sửa manifest nếu checksum sai;
đối chiếu bản tải gốc. Model_loader dùng local_files_only, không trust_remote_code và kiểm checksum trước khi load.

Lưu ý tài nguyên: riêng weights BF16 xấp xỉ 8.0/15.2/23.9 GB theo thứ tự trên (decimal GB),
chưa gồm KV cache/activation/runtime. Full context tăng VRAM; cấu hình device, dtype, max_memory và
max_context_tokens trong YAML. Không giả định hai GPU là một vùng VRAM chung; device=auto dùng Accelerate
để phân phối weights, cần đo thực tế. Chưa có benchmark GPU trong phiên triển khai này.

Không commit weights. Có thể mount read-only khi chạy local, nhưng image nộp phải chứa weights.
Theo quy định tổng model inference ≤15B dù load riêng lẻ: KHÔNG dùng cả ba trong một giải pháp nộp.
Helper package_context.py chỉ đưa một model vào build context. Không dùng draft model/MTP trong baseline.

Transformers được khóa bản hỗ trợ `qwen3` và `gemma4_unified`. Loader không thay backend/model âm thầm.
Smoke thật, explicit (cần đủ RAM/VRAM, không chạy 50 câu):

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 python datamate_v2/scripts/smoke_model.py --model qwen3_4b
```
