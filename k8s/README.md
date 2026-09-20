# Kubernetes manifests

Written for operators deploying this system to a cluster. Day-two procedures
(retry, cancel, timeout, cleanup, secret rotation, staging verification) live in
[../docs/operations.md](../docs/operations.md).

## Layout

| Path | Purpose |
| --- | --- |
| `base/namespace.yaml` | `ai-test-system` and `ai-test-runners`, both enforcing the restricted Pod Security Standard |
| `base/service-account.yaml` | One API identity (`gateway`) plus two token-less runner identities |
| `base/role.yaml`, `base/role-binding.yaml` | Namespaced Job lifecycle rights for the gateway. There is no ClusterRole |
| `base/network-policy.yaml` | Default deny in both namespaces, then the minimum each component needs |
| `base/network-policy-test-runner-offline.yaml` | Opt-in replacement policy for `allow_network_during_tests: false` |
| `base/resource-quota.yaml` | Namespace quota and per-container defaults for runner Jobs |
| `base/artifact-pvc.yaml` | Artifact storage for the gateway and the reconciler |
| `base/gateway.yaml` | Gateway Deployment and Service |
| `base/reconciler-cronjob.yaml` | Five-minute cleanup and reconciliation sweep |
| `base/secrets.example.yaml` | Secret shapes with empty values. Never applied as-is |
| `templates/*.yaml` | Reference copies of the Jobs the gateway creates at runtime |

`base/kustomization.yaml` lists everything except the two files above that are
opt-in or examples.

## Job templates are checked, not decorative

The gateway builds runner Jobs in code
([kubernetes_launcher.py](../gateway/app/services/kubernetes_launcher.py)). The
files in `templates/` are the reviewable copy of that output, and
`gateway/tests/test_k8s_manifests.py` substitutes their placeholders and asserts
byte-equality with what the launcher POSTs. Change one side without the other
and the test suite fails.

## Prerequisites

1. A CNI that enforces NetworkPolicy. Without one, every egress rule here is
   documentation rather than a control.
2. Cluster Pod and Service CIDRs inside RFC1918. The runner egress rules deny
   those ranges wholesale to keep runners off the Kubernetes API, the metadata
   endpoint and in-cluster services. Verify before rollout:

   ```console
   kubectl cluster-info dump | grep -E 'cluster-cidr|service-cluster-ip-range'
   ```

3. A ReadWriteMany StorageClass for `ai-test-artifacts`, or a single-node
   arrangement for the gateway and the reconciler CronJob.
4. Namespace labels: the policies select peers by
   `kubernetes.io/metadata.name`, which kubelet sets automatically on modern
   clusters, for the `n8n`, `monitoring`, `kube-system` and `postgres`
   namespaces.

## Values to replace before applying

| File | Field | Why |
| --- | --- | --- |
| `base/network-policy.yaml` | `gateway-egress` API server `ipBlock` (`10.0.0.1/32`) | Must be the real control plane endpoint from `kubectl get endpoints kubernetes -n default` |
| `base/network-policy.yaml` | PostgreSQL peer | Namespace selector, or an `ipBlock` when the database is managed outside the cluster |
| `base/gateway.yaml`, `base/reconciler-cronjob.yaml` | `image:` | CI writes an immutable build-number tag; `latest` is a placeholder |
| `base/gateway.yaml` | `CALLBACK_ALLOWED_HOSTS` | The n8n host that may receive completion callbacks |
| `base/artifact-pvc.yaml` | `storage`, StorageClass | Sized for the retention window in `ARTIFACT_RETENTION_DAYS` |

## Images

All three images build from the repository root, because the gateway and the
Codex runner both need `contracts/` inside the image:

```console
docker build -f gateway/Dockerfile      -t parallel-ai-test-gateway:$BUILD .
docker build -f codex-runner/Dockerfile -t parallel-ai-test-codex-runner:$BUILD .
docker build -f test-runner/Dockerfile  -t parallel-ai-test-runner:$BUILD .
```

## Apply

For the Jenkins/Argo CD deployment, populate the Vault fields listed in
[`docs/jenkins.md`](../docs/jenkins.md#jenkins-credentials). Jenkins creates
the four runtime Secrets below from Vault, while Argo CD applies the remaining
resources from `k8s-project-helm`.

The commands below are the manual-development alternative:

```console
kubectl apply -f base/namespace.yaml
# Secrets first: the Deployment will not start without them.
kubectl create secret generic ai-test-gateway -n ai-test-system \
  --from-literal=database-url=... \
  --from-literal=api-token=... \
  --from-literal=codex-runner-token=... \
  --from-literal=test-runner-token=... \
  --from-literal=artifact-signing-key=... \
  --from-literal=completion-webhook-secret=... \
  --from-literal=github-read-token=...
kubectl create secret generic ai-test-codex-callback -n ai-test-runners \
  --from-literal=token=<same value as codex-runner-token>
kubectl create secret generic ai-test-test-callback -n ai-test-runners \
  --from-literal=token=<same value as test-runner-token>
kubectl create secret generic ai-test-codex-auth -n ai-test-runners \
  --from-literal=api-key=...
kubectl create configmap ai-test-repository-policies -n ai-test-system \
  --from-file=repository-policies.yaml=../config/repository-policies.yaml
kubectl apply -k base/
```

The ConfigMap is created from a file rather than generated by kustomize so the
policy can be reviewed and rolled forward on its own; kustomize would also
refuse to read a path outside its root.

Then run the post-deploy checks in
[../docs/operations.md](../docs/operations.md#after-every-deploy).
