# Python 3.12 기반 AI API 이미지. KoELECTRA 모델은 외부 읽기 전용 볼륨으로 연결한다.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    UVICORN_WORKERS=2 PTEXT_BATCH_SIZE=8 \
    OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 TOKENIZERS_PARALLELISM=false \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
    PTEXT_MODEL_PATH=/models/ptext-koelectra-v1-2epoch-20260929
WORKDIR /service
COPY requirements.txt requirements-ml.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-ml.txt \
    && useradd --create-home --uid 10001 aiuser
COPY main.py .
COPY ai ./ai
COPY app ./app
RUN mkdir -p /service/data && chown aiuser:aiuser /service/data
USER aiuser
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
