# Jenkins pipeline

The repository-root `Jenkinsfile` follows the existing `codex-workspace`
delivery pattern: a Kubernetes Jenkins agent runs tests, Kaniko builds images,
the pipeline commits immutable image references to the application's existing
manifest directory in `k8s-project-helm`, and Argo CD self-heals from Git.

## Jenkins credentials

Create these credentials in Jenkins; do not commit their values:

| Credential ID | Type | Use |
| --- | --- | --- |
| `github_key` | SSH username with private key | Clone the application and deployment repositories, then push the image-tag commit |

The Kubernetes agent uses the existing `kaniko` ServiceAccount and expects its
registry authentication mechanism to populate `/kaniko/.docker` in the same
way as `codex-workspace`. The current internal registry is configured as
insecure because that is the existing cluster convention.

## What it publishes

Every successful build pushes one immutable tag (`BUILD_NUMBER`) for:

- `parallel-ai-test-gateway`
- `parallel-ai-test-codex-runner`
- `parallel-ai-test-runner`

It updates the four runtime image references in
`k8s-project-helm/parallel-ai-test-orchestrator` (gateway Deployment,
reconciler CronJob, Codex runner, and credential-free test runner), validates
the plain manifests, and pushes a `[skip ci]` commit. Argo CD observes that
commit through its automated sync policy; Jenkins does not need an Argo CD API
token.

## Before the first run

1. Apply `parallel-ai-test-orchestrator/argocd-app.yaml` from
   `k8s-project-helm` once. It points Argo CD at the application's directory
   and enables automated sync.
2. Provision the Secrets and repository-policy ConfigMap described in
   `k8s/README.md`.
3. Confirm the Jenkins `kaniko` ServiceAccount can push to
   `registry.registry.svc.cluster.local:5000`.
4. Review the fixed repository URLs, branches, registry, and n8n webhook at
   the top of the `Jenkinsfile`.

The pipeline deliberately does not place PostgreSQL, GitHub, runner, Codex, or
webhook secrets in image build arguments or GitOps manifests.
