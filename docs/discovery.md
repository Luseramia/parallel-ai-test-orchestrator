# Milestone 0 Discovery Report

## Repositories inspected

- `k8s-project-helm`: clean `main` at `e36b64f`, aligned with `origin/main`.
- `ai-orchestrator`: inspected `origin/main` at `040b83d`. Local `main` was one commit behind and had user changes in `.env.example` and `app/chains/llm.py`; they were not used or modified.
- This workspace initially contained only `parallel-ai-test-orchestrator-plan.md` and was not initialized as Git.

## Existing Codex deployment

`k8s-project-helm/codex-workspace` is a Helm chart deployed in namespace `codex`.

- Image: `registry.registry.svc.cluster.local:5000/codex-workspace:6`.
- Base: `node:22-bookworm`; the Dockerfile pins Codex CLI `0.154.0`.
- Runtime: one `Recreate` replica because workspace, home, and sensitive ChatGPT-auth PVCs use ReadWriteOnce.
- Gateway: bearer-authenticated `codex-gateway.codex.svc.cluster.local:8080`, one in-memory slot, fixed read-only sandbox, no ServiceAccount token.
- NetworkPolicy: admits the configured namespace and Pod label `app.kubernetes.io/name: ai-orchestrator`.
- Delivery: Jenkins builds with Kaniko, pushes to the internal registry, commits the tag into Helm values, requests Argo CD sync, and notifies n8n.

The existing gateway is unsuitable as the durable runner. It has no queue, database, idempotency, exact-SHA checkout, writable test workspace, JSONL artifact capture, or patch output.

## Existing AI orchestrator and n8n

`ai-orchestrator` is a Python 3.13 FastAPI application with synchronous `/generate`, no durable job state, and no artifact store. Its CI uses `pip`, `unittest`, Kaniko, immutable build-number tags, a commit to `k8s-project-helm/ai-orchestrator/deployment.yaml`, and Argo CD reconciliation.

n8n is referenced at `n8n.n8n.svc.cluster.local:443`; an external webhook hostname and a ServiceMonitor for `app.kubernetes.io/name: n8n` also exist. No n8n Deployment/StatefulSet, Helm values, Argo CD Application, database config, or workflow export is present in the deployment repository. Its version, install method, queue mode, persistence, and webhook authentication are therefore not established by source control.

## Gap analysis

| Area | Current evidence | Required for MVP |
| --- | --- | --- |
| Durable state | synchronous endpoints only | PostgreSQL state machine, attempts, events, reconciliation |
| Idempotency | request IDs are correlation only | unique deliveries, replay window, transition guards |
| Git | no push workflow/footer parser | signature, footer, reachability, exact-SHA verification |
| Jobs/RBAC | no job launcher | namespace-scoped launcher and isolated templates |
| Codex | singleton read-only gateway | prepare/generate runner, JSONL, output schemas |
| Tests | no credential-free runner | patch apply, command allowlist, result/JUnit parser |
| Artifacts | none configured | approved S3 endpoint/bucket and filesystem fake |
| n8n | no workflow exports | three versioned workflows and import/runbook |
| Determinism | target repo requirements are unpinned | lock/constraints before production execution |
| Local tooling | Node/npm only | Python, Docker, Helm, kubectl via CI/container or approved install |

## Repository and test conventions

- Initial Git provider: GitHub, repositories under `Luseramia/*`.
- Initial real policy target: `github.com/Luseramia/ai-orchestrator` on `main`.
- Existing package/test conventions: `pip` and `unittest`.
- Generated patches may add only allowlisted tests and fixtures. Production code, scripts, images, CI, environment, dependency, and lock files are denied.
- The policy is staging-only until the target repository has pinned dependencies.

## Proposed Milestone 1-3 files

```text
gateway/
  app/{api,domain,repositories,security,services}/
  migrations/
  tests/
  Dockerfile
  requirements.in
  requirements.lock
codex-runner/
  src/prepare.ts
  src/callback.ts
  prompts/prepare.md
  schemas/test-draft.schema.json
  tests/
  Dockerfile
  package.json
  package-lock.json
schemas/{test-plan,test-draft,runner-callback}.schema.json
deploy/local/compose.yaml
scripts/submit-test-plan.{sh,ps1}
docs/operations.md
```

The later GitOps change belongs in `k8s-project-helm/parallel-ai-test-orchestrator/` and should contain the Argo CD Application, gateway Service/Deployment/ServiceAccount, runner RBAC, NetworkPolicies, ConfigMaps, and Secret references. Secret values must not be committed.

## Verification and open operational facts

Milestone 0 inspected the implementation plan, Codex chart/image/gateway/tests/Jenkins/PVC/NetworkPolicy, `ai-orchestrator` source and deployment at `origin/main`, n8n references, and current official Codex non-interactive guidance. No runtime file or reference repository was modified.

These facts do not block a fake-runner POC but must be resolved before production: the existing n8n install/backup/auth mechanism, approved PostgreSQL and S3 services, real Codex workload identity versus managed ChatGPT authentication, GitHub webhook and clone identity, and the intended Git remote for this workspace.
