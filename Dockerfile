# ---- Stage 1: Build frontend ----
FROM node:20-alpine AS frontend-build
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --prefer-offline
COPY frontend/ ./
RUN node scripts/community_assets.mjs && npm run build

# ---- Stage 2: Build the CPU-only Python environment ----
FROM python:3.13-slim AS python-deps
ARG PYTORCH_VERSION=2.13.0
ARG PYTORCH_CPU_INDEX_URL=https://download.pytorch.org/whl/cpu
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="${VIRTUAL_ENV}/bin:${PATH}"

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ && \
    rm -rf /var/lib/apt/lists/*

RUN python -m venv "${VIRTUAL_ENV}"
COPY backend/requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir \
        --index-url "${PYTORCH_CPU_INDEX_URL}" \
        "torch==${PYTORCH_VERSION}" && \
    python -m pip install --no-cache-dir -r /tmp/requirements.txt && \
    python -m pip check && \
    python -c "import torch; assert torch.__version__.split('+', 1)[0] == '${PYTORCH_VERSION}'; assert torch.version.cuda is None; assert not torch.cuda.is_available()" && \
    python -c "from importlib import metadata; names={str(d.metadata.get('Name') or '').lower().replace('_','-') for d in metadata.distributions()}; bad=sorted(n for n in names if n == 'nvidia' or n.startswith('nvidia-') or 'triton' in n); assert not bad, bad"

# ---- Stage 3: Production image ----
FROM python:3.13-slim AS runtime
ARG GROWTHLOG_VERSION=0.0.0-dev
ARG GROWTHLOG_RELEASE_ID=local
LABEL org.opencontainers.image.title="GrowthLog" \
      org.opencontainers.image.version="${GROWTHLOG_VERSION}" \
      org.opencontainers.image.revision="${GROWTHLOG_RELEASE_ID}"
ENV GROWTHLOG_VERSION="${GROWTHLOG_VERSION}"
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="${VIRTUAL_ENV}/bin:${PATH}"
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-chi-sim \
    tesseract-ocr-eng \
    libgl1 \
    libglib2.0-0 \
    libgomp1 \
    libsm6 \
    libxext6 \
    libxrender1 && \
    rm -rf /var/lib/apt/lists/*

COPY --from=python-deps /opt/venv /opt/venv
COPY backend/ ./
COPY scripts/community_privacy.py /app/scripts/community_privacy.py

RUN python -c "from pathlib import Path; import rapidocr, sys; root=Path(rapidocr.__file__).resolve().parent/'models'; req=['PP-OCRv6_det_small.onnx','PP-OCRv6_rec_small.onnx','ch_ppocr_mobile_v2.0_cls_mobile.onnx']; missing=[n for n in req if not (root/n).is_file()]; missing and sys.exit('RapidOCR exact small models missing: %s' % missing); assert all('medium' not in n.lower() for n in req); print('rapidocr_models_ok', req)"

# RapidOCR uses exact package models (PP-OCRv6_*_small + cls_mobile).
# E5 is mounted read-only at /app/models/multilingual-e5-base in production.
# Runtime model downloads are forbidden by docker-compose.prod.yml.

# Drop any host/history static assets copied with backend/; image UI must come
# solely from the frontend-build stage output.
RUN rm -rf /app/static && mkdir -p /app/static
COPY --from=frontend-build /app/backend/static /app/static

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
