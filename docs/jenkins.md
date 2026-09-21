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

The same Pod uses Vault Agent injection with role `kaniko` and renders each
runtime value into a separate ephemeral file from `dev-secrets/data/ai`. Add
these fields to that Vault secret:

- `AI_TEST_DATABASE_URL`
- `AI_TEST_API_TOKEN`
- `AI_TEST_CODEX_RUNNER_TOKEN`
- `AI_TEST_TEST_RUNNER_TOKEN`
- `AI_TEST_ARTIFACT_SIGNING_KEY`
- `AI_TEST_COMPLETION_WEBHOOK_SECRET`
- `AI_TEST_GITHUB_READ_TOKEN`
- `AI_TEST_OPENAI_API_KEY`

After manifest validation, Jenkins builds the Kubernetes Secrets directly from
those injected files and applies `ai-test-gateway` in
`ai-test-system` plus the two callback Secrets and `ai-test-codex-auth` in
`ai-test-runners`. Secret values stay in the Vault-injected files and are not
copied into Groovy variables, console output, Git, or image layers. The
`kaniko` ServiceAccount therefore needs namespaced Secret write access in both
namespaces. The deployment repository supplies that access through
`jenkins-secret-rbac.yaml`; Jenkins does not need permission to read Namespace
objects or any cluster-wide role. During the first deployment, the Secret stage
retries for up to ten minutes while Argo CD creates the two namespaces and
applies that RBAC.

## What it publishes

Every successful build pushes one immutable tag (`BUILD_NUMBER`) for:

- `parallel-ai-test-gateway`
- `parallel-ai-test-codex-runner`
- `parallel-ai-test-runner`

It updates the five runtime image references in
`k8s-project-helm/parallel-ai-test-orchestrator` (gateway Deployment, database
migration hook, reconciler CronJob, Codex runner, and credential-free test
runner), validates the plain manifests, and pushes a `[skip ci]` commit. Argo
CD observes that commit through its automated sync policy; Jenkins does not
need an Argo CD API token.

## Before the first run

1. Apply `argocd/parallel-ai-test-orchestrator.yaml` from `k8s-project-helm`
   once. The bootstrap manifest is outside the managed application directory,
   so the Argo CD Application never reconciles or prunes itself.
2. Populate the required Vault fields above. Argo CD manages the non-secret
   repository-policy ConfigMap.
3. Confirm the Jenkins `kaniko` ServiceAccount can push to
   `registry.registry.svc.cluster.local:5000`.
4. Review the fixed repository URLs, branches, registry, and n8n webhook at
   the top of the `Jenkinsfile`.

The pipeline deliberately does not place PostgreSQL, GitHub, runner, Codex, or
webhook secrets in image build arguments or GitOps manifests.
