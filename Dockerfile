# M10 — one image for both the API and the worker (different commands, same code).
#
# Pinning (guide §13.3): base image by tag AND digest, Python deps from requirements.lock
# (exact versions, generated with `pip freeze`). Rebuilding next month gives the same image.
FROM python:3.12.16-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first: this layer is cached until requirements.lock changes.
COPY requirements.lock .
RUN pip install -r requirements.lock

COPY app ./app
COPY scripts ./scripts

# Never run as root inside the container.
RUN useradd --create-home --uid 10001 kg \
 && mkdir -p documents/uploads extractions \
 && chown -R kg:kg /app
USER kg

EXPOSE 8000
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
