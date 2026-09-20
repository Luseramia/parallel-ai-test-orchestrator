# Build from the repository root so the JSON Schemas ship with the service:
#   docker build -f gateway/Dockerfile -t parallel-ai-test-gateway:<build> .
# `app.services.job_service.default_contract_path()` resolves to /contracts,
# which is why the app lives at /app/app and the schemas at /contracts.
FROM python:3.13-slim-bookworm

RUN useradd --create-home --uid 10001 gateway

WORKDIR /app
COPY gateway/requirements.lock ./requirements.lock
RUN pip install --no-cache-dir -r requirements.lock

COPY gateway/app ./app
COPY gateway/migrations ./migrations
COPY gateway/alembic.ini ./alembic.ini
COPY contracts /contracts

USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
