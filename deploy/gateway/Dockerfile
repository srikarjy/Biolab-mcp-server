FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml LICENSE README.md ./
COPY biolab ./biolab

RUN pip install --no-cache-dir .

ENV BIOLAB_HOST=0.0.0.0
ENV BIOLAB_PORT=8000
ENV BIOLAB_DB_PATH=/data/biolab.db

EXPOSE 8000

ENTRYPOINT ["python", "-m", "biolab.server"]
