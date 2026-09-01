FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY semantic_cache ./semantic_cache

RUN pip install --no-cache-dir .

EXPOSE 8000

CMD ["uvicorn", "semantic_cache.main:app", "--host", "0.0.0.0", "--port", "8000"]
