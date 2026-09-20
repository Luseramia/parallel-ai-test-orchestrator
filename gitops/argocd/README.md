# Argo CD bootstrap

This directory contains the one-time Argo CD Application bootstrap manifest.
It makes Argo CD watch the GitOps repository and path that Jenkins updates:

```console
kubectl apply -f gitops/argocd/application.yaml
```

Before applying it, add `https://github.com/Luseramia/k8s-project-helm.git` to
Argo CD with read access and provision the runtime Secrets and repository-policy
ConfigMap described in [../../k8s/README.md](../../k8s/README.md). Do not commit
secret values to this directory or the GitOps repository.

The application starts with manual sync. Jenkins invokes an Argo CD sync after
it commits immutable image references to
`k8s-project-helm/parallel-ai-test-orchestrator/base`.
