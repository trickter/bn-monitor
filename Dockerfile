FROM python:3.12-slim
ARG PIP_INDEX_URL=https://pypi.org/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY . .
RUN pip install --no-cache-dir --timeout 120 --retries 10 -i "$PIP_INDEX_URL" .
CMD ["bn-monitor", "run"]
