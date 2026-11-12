# Build needs Internet; runtime works with --network none and a local Unix socket.
ARG BASE_IMAGE=pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime
FROM ${BASE_IMAGE}
ARG MODEL_DIR=qwen3-4b
ARG MODEL_KEY=qwen3_4b
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1     HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_DATASETS_OFFLINE=1     TOKENIZERS_PARALLELISM=false DATAMATE_MODEL=${MODEL_KEY}
RUN apt-get update && apt-get install -y --no-install-recommends postgresql postgresql-client     && rm -rf /var/lib/apt/lists/* && useradd --create-home --uid 1000 datamate
WORKDIR /code
COPY requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/requirements.txt
COPY --chown=datamate:datamate __init__.py cli.py common.py contracts.py progress.py requirements.txt /code/datamate_v2/
COPY --chown=datamate:datamate configs/ /code/datamate_v2/configs/
COPY --chown=datamate:datamate data_pipeline/ /code/datamate_v2/data_pipeline/
COPY --chown=datamate:datamate database/ /code/datamate_v2/database/
COPY --chown=datamate:datamate evaluation/ /code/datamate_v2/evaluation/
COPY --chown=datamate:datamate inference/ /code/datamate_v2/inference/
COPY --chown=datamate:datamate metadata/ /code/datamate_v2/metadata/
COPY --chown=datamate:datamate prompts/ /code/datamate_v2/prompts/
COPY --chown=datamate:datamate qa_pipeline/ /code/datamate_v2/qa_pipeline/
COPY --chown=datamate:datamate scripts/ /code/datamate_v2/scripts/
COPY --chown=datamate:datamate models/README.md models/registry.json /code/datamate_v2/models/
# Copy only selected weights, once. No combined-model image layers.
COPY --chown=datamate:datamate models/${MODEL_DIR}/ /code/datamate_v2/models/${MODEL_DIR}/
COPY --chown=datamate:datamate scripts/predict.sh /code/predict.sh
COPY --chown=datamate:datamate README.md /code/README.md
COPY --chown=datamate:datamate training_code/ /code/training_code/
RUN mkdir -p /result /code/training_code && chown datamate:datamate /result /code/training_code     && chmod +x /code/predict.sh
USER datamate
ENTRYPOINT ["bash","/code/predict.sh"]
