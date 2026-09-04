FROM mcr.microsoft.com/dotnet/sdk:8.0-bookworm-slim AS continumail-build
ARG CONTINUMAIL_REF=v0.3.3
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/* \
    && git clone --depth 1 --branch "$CONTINUMAIL_REF" https://github.com/ContinuMail/continumail-converter.git /src/continumail \
    && dotnet publish /src/continumail/src/Mail2Pst.Cli/Mail2Pst.Cli.csproj -c Release -r linux-x64 --self-contained true -p:PublishSingleFile=true -o /out

FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client ca-certificates gosu libicu72 && rm -rf /var/lib/apt/lists/*
COPY --from=continumail-build /out /opt/continumail
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .
COPY . .
RUN chmod +x /opt/continumail/Mail2Pst.Cli && useradd -r -u 10001 archiver && mkdir -p /data/db /data/work /data/pst /data/nas && chown -R archiver:archiver /data /app && chmod +x /app/docker-entrypoint.sh
ENTRYPOINT ["/app/docker-entrypoint.sh"]
EXPOSE 8085
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8085"]
