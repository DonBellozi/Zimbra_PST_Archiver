FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client ca-certificates gosu git && rm -rf /var/lib/apt/lists/*
ARG EML2PST_REF=main
RUN git clone --depth 1 --branch "$EML2PST_REF" https://github.com/igrbtn/EDB_Explorer.git /opt/EDB_Explorer \
    && rm -rf /opt/EDB_Explorer/.git
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .
COPY . .
RUN useradd -r -u 10001 archiver && mkdir -p /data/db /data/work /data/pst /data/nas && chown -R archiver:archiver /data /app && chmod +x /app/docker-entrypoint.sh
ENTRYPOINT ["/app/docker-entrypoint.sh"]
EXPOSE 8085
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8085"]
