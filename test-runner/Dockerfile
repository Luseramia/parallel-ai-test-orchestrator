# Build from the repository root, like the other two images:
#   docker build -f test-runner/Dockerfile -t parallel-ai-test-runner:<build> .
# This image deliberately installs no model tooling and holds no credential.
FROM python:3.13-slim-bookworm

RUN apt-get update \
    && apt-get install --yes --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 runner
WORKDIR /runner
COPY test-runner/runner.py /runner/runner.py
COPY runner-common/clone-repository.sh /runner/clone-repository.sh
USER 10001:10001
ENTRYPOINT ["python", "/runner/runner.py"]

