FROM node:20.19.0-bookworm-slim@sha256:5cfa999422613d3b34f766cbb814d964cbfcb76aaf3607e805da21cccb352bac AS build

WORKDIR /src
RUN corepack enable && corepack prepare pnpm@10.34.5 --activate
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml tsconfig.base.json ./
COPY apps/cli/package.json apps/cli/
COPY packages/contracts/package.json packages/contracts/
COPY packages/evidence/package.json packages/evidence/
COPY packages/kernel/package.json packages/kernel/
COPY packages/model-driver/package.json packages/model-driver/
COPY packages/tool-broker/package.json packages/tool-broker/
RUN pnpm install --frozen-lockfile --ignore-scripts
COPY packages/contracts/src packages/contracts/src
COPY packages/contracts/tsconfig.json packages/contracts/
COPY packages/evidence/src packages/evidence/src
COPY packages/evidence/tsconfig.json packages/evidence/
COPY apps/cli/src/relay-command.ts apps/cli/src/relay-entry.ts apps/cli/src/relay-fixture-entry.ts apps/cli/src/relay-evidence.ts apps/cli/src/responses-fixture.ts apps/cli/src/responses-metadata.ts apps/cli/src/responses-relay.ts apps/cli/src/
COPY apps/cli/tsconfig.relay.json apps/cli/
COPY benchmarks/terminal_bench/relay.Dockerfile benchmarks/terminal_bench/relay.Dockerfile
COPY benchmarks/terminal_bench/verify-instruction-v1.txt benchmarks/terminal_bench/verify-instruction-v1.txt
RUN find package.json pnpm-lock.yaml pnpm-workspace.yaml tsconfig.base.json \
      apps/cli packages benchmarks/terminal_bench \
      -type f \
      ! -path apps/cli/src/relay-fixture-entry.ts \
      ! -path apps/cli/src/responses-fixture.ts \
      ! -path benchmarks/terminal_bench/verify-instruction-v1.txt \
      -print0 \
    | LC_ALL=C sort -z \
    | xargs -0 sha256sum \
    | sha256sum \
    | awk '{print "sha256:" $1}' > /src/relay-build-id
RUN find package.json pnpm-lock.yaml pnpm-workspace.yaml tsconfig.base.json \
      apps/cli packages benchmarks/terminal_bench/relay.Dockerfile \
      benchmarks/terminal_bench/verify-instruction-v1.txt \
      -type f -print0 \
    | LC_ALL=C sort -z \
    | xargs -0 sha256sum \
    | sha256sum \
    | awk '{print "sha256:" $1}' > /src/relay-fixture-build-id
RUN pnpm exec tsc -b packages/contracts packages/evidence \
    && pnpm exec tsc -p apps/cli/tsconfig.relay.json \
    && rm -f apps/cli/relay-dist/.tsbuildinfo \
    && cp -a apps/cli/relay-dist apps/cli/relay-production-dist \
    && rm -f \
      apps/cli/relay-production-dist/relay-fixture-entry.* \
      apps/cli/relay-production-dist/responses-fixture.*

FROM node:20.19.0-bookworm-slim@sha256:5cfa999422613d3b34f766cbb814d964cbfcb76aaf3607e805da21cccb352bac AS runtime-base

WORKDIR /app
COPY --from=build /src/apps/cli/package.json ./apps/cli/package.json
COPY --from=build /src/packages/contracts/package.json ./node_modules/@open-agent-lab/contracts/package.json
COPY --from=build /src/packages/contracts/dist ./node_modules/@open-agent-lab/contracts/dist
COPY --from=build /src/packages/evidence/package.json ./node_modules/@open-agent-lab/evidence/package.json
COPY --from=build /src/packages/evidence/dist ./node_modules/@open-agent-lab/evidence/dist
RUN mkdir -p /var/lib/open-agent-lab && chown node:node /var/lib/open-agent-lab

ENTRYPOINT ["node", "/app/apps/cli/relay-dist/relay-entry.js"]

FROM runtime-base AS fixture
COPY --from=build /src/apps/cli/relay-dist ./apps/cli/relay-dist
COPY --from=build /src/benchmarks/terminal_bench/verify-instruction-v1.txt ./benchmarks/terminal_bench/verify-instruction-v1.txt
COPY --from=build /src/relay-fixture-build-id ./relay-build-id

FROM runtime-base AS production
COPY --from=build /src/apps/cli/relay-production-dist ./apps/cli/relay-dist
COPY --from=build /src/relay-build-id ./relay-build-id
