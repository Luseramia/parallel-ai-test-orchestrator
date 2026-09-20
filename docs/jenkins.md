# Jenkins pipeline

The repository-root `Jenkinsfile` follows the existing `codex-workspace`
delivery pattern: a Kubernetes Jenkins agent runs tests, Kaniko builds images,
the pipeline commits immutable image references to `k8s-project-helm`, requests
an Argo CD sync, and sends a best-effort n8n notification.

## Jenkins credentials

Create these credentials in Jenkins; do not commit their values:

| Credential ID | Type | Use |
| --- | --- | --- |
| `github_key` | SSH username with private key | Clone the application and GitOps repositories, then push the GitOps commit |
| `argocd_token` | Secret text | Request sync of the `parallel-ai-test-orchestrator` Argo CD application |

The Kubernetes agent uses the existing `kaniko` ServiceAccount and expects its
registry authentication mechanism to populate `/kaniko/.docker` in the same
way as `codex-workspace`. The current internal registry is configured as
insecure because that is the existing cluster convention.

## What it publishes

Every successful build pushes one immutable tag (`BUILD_NUMBER`) for:

- `parallel-ai-test-gateway`
- `parallel-ai-test-codex-runner`
- `parallel-ai-test-runner`

It then replaces `k8s-project-helm/parallel-ai-test-orchestrator/base` and
`templates` with the reviewed manifests from this repository, updates the four
runtime image references (gateway Deployment, reconciler CronJob, Codex runner,
and credential-free test runner), validates the rendered Kustomize output, and
pushes a `[skip ci]` commit.

## Before the first run

1. Create the Argo CD application named `parallel-ai-test-orchestrator` and
   point it at `parallel-ai-test-orchestrator/base` in `k8s-project-helm`.
2. Provision the Secrets and repository-policy ConfigMap described in
   `k8s/README.md`.
3. Confirm the Jenkins `kaniko` ServiceAccount can push to
   `registry.registry.svc.cluster.local:5000`.
4. Review the fixed repository URLs, branches, registry, Argo CD URL, and n8n
   webhook at the top of the `Jenkinsfile`.

The pipeline deliberately does not place PostgreSQL, GitHub, runner, Codex, or
webhook secrets in image build arguments or GitOps manifests.
