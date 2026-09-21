FROM node:22-bookworm AS build

WORKDIR /runner
COPY codex-runner/package.json codex-runner/package-lock.json codex-runner/tsconfig.json ./
RUN npm ci
COPY codex-runner/src ./src
COPY codex-runner/tests ./tests
RUN npm run build

FROM node:22-bookworm

ARG CODEX_CLI_VERSION=0.154.0
RUN npm install --global "@openai/codex@${CODEX_CLI_VERSION}"

WORKDIR /runner
COPY codex-runner/package.json codex-runner/package-lock.json ./
RUN npm ci --omit=dev && npm cache clean --force
COPY --from=build /runner/dist ./dist
COPY codex-runner/prompts ./prompts
COPY runner-common/clone-repository.sh ./clone-repository.sh
# The runner validates Codex output against these schemas at /contracts, which
# is why this image builds from the repository root:
#   docker build -f codex-runner/Dockerfile -t parallel-ai-test-codex-runner:<build> .
COPY contracts /contracts

USER node
ENTRYPOINT ["node", "/runner/dist/src/prepare.js"]

