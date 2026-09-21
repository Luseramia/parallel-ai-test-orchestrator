from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import httpx
import yaml

from app.config import Settings
from app.services.job_launcher import GenerateLaunch, PrepareLaunch, TestLaunch
from app.services.kubernetes_launcher import KubernetesApiJobLauncher

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
K8S_ROOT = REPOSITORY_ROOT / "k8s"
RUNNER_NAMESPACE = "ai-test-runners"
SYSTEM_NAMESPACE = "ai-test-system"
PRIVATE_RANGES = {
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "169.254.0.0/16",
}
JOB_ID = "atj_" + "a" * 32
CODE_SHA = "c" * 40
BASE_SHA = "b" * 40
PATCH_SHA256 = "d" * 64
PATCH_POLICY = {"allowed_paths": ["tests/**/*.py"], "max_files": 40}
RUNNER_POLICY = {"command_slots": {"unit": "python-unittest-all"}}


def load_documents(path: Path) -> list[dict[str, Any]]:
    return [
        document
        for document in yaml.safe_load_all(path.read_text(encoding="utf-8"))
        if document
    ]


def load_all() -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for path in sorted(K8S_ROOT.rglob("*.yaml")):
        if path.name == "kustomization.yaml":
            continue
        documents.extend(load_documents(path))
    return documents


class ManifestSecurityTests(unittest.TestCase):
    """The cluster-side half of the security boundary described in the ADR."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.documents = load_all()
        cls.by_kind: dict[str, list[dict[str, Any]]] = {}
        for document in cls.documents:
            cls.by_kind.setdefault(document["kind"], []).append(document)

    def test_every_manifest_parses_and_is_namespaced(self) -> None:
        self.assertTrue(self.documents)
        cluster_scoped = {"Namespace"}
        for document in self.documents:
            kind = document["kind"]
            metadata = document["metadata"]
            self.assertIn("name", metadata, kind)
            if kind in cluster_scoped:
                continue
            self.assertIn(
                metadata.get("namespace"),
                {SYSTEM_NAMESPACE, RUNNER_NAMESPACE},
                f"{kind}/{metadata['name']} is not in a project namespace",
            )

    def test_namespaces_enforce_the_restricted_pod_security_standard(self) -> None:
        names = set()
        for namespace in self.by_kind["Namespace"]:
            names.add(namespace["metadata"]["name"])
            labels = namespace["metadata"]["labels"]
            self.assertEqual("restricted", labels["pod-security.kubernetes.io/enforce"])
        self.assertEqual({SYSTEM_NAMESPACE, RUNNER_NAMESPACE}, names)

    def test_gateway_is_the_only_kubernetes_api_identity_and_stays_namespaced(
        self,
    ) -> None:
        self.assertNotIn("ClusterRole", self.by_kind)
        self.assertNotIn("ClusterRoleBinding", self.by_kind)

        roles = self.by_kind["Role"]
        self.assertEqual(1, len(roles))
        role = roles[0]
        self.assertEqual(RUNNER_NAMESPACE, role["metadata"]["namespace"])
        permitted = {
            ("batch", "jobs"): {"create", "get", "list", "watch", "delete"},
            ("", "pods"): {"get", "list", "watch"},
            ("", "pods/log"): {"get"},
        }
        for rule in role["rules"]:
            for group in rule["apiGroups"]:
                for resource in rule["resources"]:
                    allowed = permitted.get((group, resource))
                    self.assertIsNotNone(
                        allowed, f"unexpected RBAC resource {group}/{resource}"
                    )
                    self.assertTrue(
                        set(rule["verbs"]).issubset(allowed),
                        f"unexpected verbs on {group}/{resource}: {rule['verbs']}",
                    )

        bindings = self.by_kind["RoleBinding"]
        self.assertEqual(1, len(bindings))
        binding = bindings[0]
        self.assertEqual(RUNNER_NAMESPACE, binding["metadata"]["namespace"])
        self.assertEqual("Role", binding["roleRef"]["kind"])
        self.assertEqual(
            [
                {
                    "kind": "ServiceAccount",
                    "name": "gateway",
                    "namespace": SYSTEM_NAMESPACE,
                }
            ],
            binding["subjects"],
        )

    def test_runner_service_accounts_never_receive_an_api_token(self) -> None:
        runner_accounts = {
            account["metadata"]["name"]: account
            for account in self.by_kind["ServiceAccount"]
            if account["metadata"]["namespace"] == RUNNER_NAMESPACE
        }
        self.assertEqual({"codex-runner", "test-runner"}, set(runner_accounts))
        for name, account in runner_accounts.items():
            self.assertFalse(
                account["automountServiceAccountToken"],
                f"{name} would mount a Kubernetes API token",
            )

    def test_both_namespaces_default_to_deny(self) -> None:
        defaults = {
            policy["metadata"]["namespace"]: policy
            for policy in self.by_kind["NetworkPolicy"]
            if policy["metadata"]["name"] == "default-deny-all"
        }
        self.assertEqual({SYSTEM_NAMESPACE, RUNNER_NAMESPACE}, set(defaults))
        for namespace, policy in defaults.items():
            self.assertEqual({}, policy["spec"]["podSelector"], namespace)
            self.assertEqual(
                ["Ingress", "Egress"], policy["spec"]["policyTypes"], namespace
            )

    def test_runner_egress_cannot_reach_the_api_server_or_metadata_endpoint(
        self,
    ) -> None:
        runner_policies = [
            policy
            for policy in self.by_kind["NetworkPolicy"]
            if policy["metadata"]["namespace"] == RUNNER_NAMESPACE
            and policy["metadata"]["name"] != "default-deny-all"
        ]
        self.assertTrue(runner_policies)
        for policy in runner_policies:
            name = policy["metadata"]["name"]
            for rule in policy["spec"].get("egress", []):
                for peer in rule["to"]:
                    block = peer.get("ipBlock")
                    if block is None:
                        # Selector peers are namespace/pod scoped and listed
                        # explicitly; the assertions below cover them.
                        continue
                    self.assertEqual("0.0.0.0/0", block["cidr"], name)
                    self.assertTrue(
                        PRIVATE_RANGES.issubset(set(block.get("except", []))),
                        f"{name} leaves in-cluster addresses reachable",
                    )

    def test_runner_egress_selectors_only_reach_dns_and_the_gateway(self) -> None:
        allowed_namespaces = {"kube-system", SYSTEM_NAMESPACE}
        for policy in self.by_kind["NetworkPolicy"]:
            if policy["metadata"]["namespace"] != RUNNER_NAMESPACE:
                continue
            for rule in policy["spec"].get("egress", []):
                for peer in rule["to"]:
                    selector = peer.get("namespaceSelector")
                    if selector is None:
                        continue
                    self.assertIn(
                        selector["matchLabels"]["kubernetes.io/metadata.name"],
                        allowed_namespaces,
                        policy["metadata"]["name"],
                    )

    def test_offline_variant_replaces_the_permissive_test_runner_policy(self) -> None:
        offline = load_documents(
            K8S_ROOT / "base" / "network-policy-test-runner-offline.yaml"
        )
        self.assertEqual(1, len(offline))
        policy = offline[0]
        self.assertEqual("test-runner-egress", policy["metadata"]["name"])
        self.assertEqual(RUNNER_NAMESPACE, policy["metadata"]["namespace"])
        for rule in policy["spec"]["egress"]:
            for peer in rule["to"]:
                self.assertNotIn("ipBlock", peer, "offline policy allows raw egress")

    def test_runner_namespace_is_bounded_by_quota_and_limit_range(self) -> None:
        quotas = [
            document
            for document in self.by_kind["ResourceQuota"]
            if document["metadata"]["namespace"] == RUNNER_NAMESPACE
        ]
        self.assertEqual(1, len(quotas))
        hard = quotas[0]["spec"]["hard"]
        for key in ("pods", "count/jobs.batch", "requests.cpu", "limits.memory"):
            self.assertIn(key, hard)
        ranges = [
            document
            for document in self.by_kind["LimitRange"]
            if document["metadata"]["namespace"] == RUNNER_NAMESPACE
        ]
        self.assertEqual(1, len(ranges))
        container_limits = ranges[0]["spec"]["limits"][0]
        self.assertEqual("Container", container_limits["type"])
        self.assertIn("default", container_limits)
        self.assertIn("defaultRequest", container_limits)

    def test_gateway_workloads_are_hardened(self) -> None:
        pod_specs = [
            (
                deployment["metadata"]["name"],
                deployment["spec"]["template"]["spec"],
            )
            for deployment in self.by_kind["Deployment"]
        ] + [
            (
                cron["metadata"]["name"],
                cron["spec"]["jobTemplate"]["spec"]["template"]["spec"],
            )
            for cron in self.by_kind["CronJob"]
        ]
        self.assertEqual({"gateway", "reconciler"}, {name for name, _ in pod_specs})
        for name, spec in pod_specs:
            self.assertEqual("gateway", spec["serviceAccountName"], name)
            self.assertTrue(spec["securityContext"]["runAsNonRoot"], name)
            self.assertEqual(
                "RuntimeDefault",
                spec["securityContext"]["seccompProfile"]["type"],
                name,
            )
            container = spec["containers"][0]
            self.assertFalse(
                container["securityContext"]["allowPrivilegeEscalation"], name
            )
            self.assertTrue(
                container["securityContext"]["readOnlyRootFilesystem"], name
            )
            self.assertEqual(
                ["ALL"], container["securityContext"]["capabilities"]["drop"], name
            )
            self.assertIn("requests", container["resources"], name)
            self.assertIn("limits", container["resources"], name)

    def test_reconciler_cronjob_is_bounded_and_serialized(self) -> None:
        cron = self.by_kind["CronJob"][0]["spec"]
        self.assertEqual("Forbid", cron["concurrencyPolicy"])
        job_spec = cron["jobTemplate"]["spec"]
        self.assertEqual(0, job_spec["backoffLimit"])
        self.assertLessEqual(job_spec["activeDeadlineSeconds"], 900)
        self.assertIn("ttlSecondsAfterFinished", job_spec)
        self.assertEqual(
            ["python", "-m", "app.reconcile"],
            job_spec["template"]["spec"]["containers"][0]["command"],
        )

    def test_database_migration_is_a_hardened_sync_hook(self) -> None:
        jobs = [
            job
            for job in self.by_kind["Job"]
            if job["metadata"]["name"] == "gateway-database-migration"
        ]
        self.assertEqual(1, len(jobs))
        job = jobs[0]
        annotations = job["metadata"]["annotations"]
        self.assertEqual("Sync", annotations["argocd.argoproj.io/hook"])
        self.assertEqual("1", annotations["argocd.argoproj.io/sync-wave"])
        self.assertIn(
            "HookSucceeded",
            annotations["argocd.argoproj.io/hook-delete-policy"],
        )
        self.assertLessEqual(job["spec"]["activeDeadlineSeconds"], 300)

        deployment = self.by_kind["Deployment"][0]
        cron = self.by_kind["CronJob"][0]
        self.assertEqual(
            "2",
            deployment["metadata"]["annotations"][
                "argocd.argoproj.io/sync-wave"
            ],
        )
        self.assertEqual(
            "2", cron["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"]
        )

        pod = job["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertTrue(pod["securityContext"]["runAsNonRoot"])
        self.assertEqual(
            "RuntimeDefault",
            pod["securityContext"]["seccompProfile"]["type"],
        )
        container = pod["containers"][0]
        self.assertEqual(["alembic", "upgrade", "head"], container["command"])
        self.assertFalse(
            container["securityContext"]["allowPrivilegeEscalation"]
        )
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        self.assertEqual(
            ["ALL"], container["securityContext"]["capabilities"]["drop"]
        )
        self.assertIn("requests", container["resources"])
        self.assertIn("limits", container["resources"])

    def test_example_secrets_carry_no_values_and_keep_workloads_separate(self) -> None:
        secrets = {
            secret["metadata"]["name"]: secret
            for secret in load_documents(K8S_ROOT / "base" / "secrets.example.yaml")
        }
        for name, secret in secrets.items():
            self.assertEqual("Secret", secret["kind"])
            for key, value in secret.get("stringData", {}).items():
                self.assertEqual("", value, f"{name}.{key} carries a value")
            self.assertNotIn("data", secret, name)
        # The model credential and the two callback tokens are distinct objects
        # in the runner namespace; the gateway holds its own copy of the tokens.
        self.assertEqual(
            RUNNER_NAMESPACE, secrets["ai-test-codex-auth"]["metadata"]["namespace"]
        )
        self.assertEqual(
            RUNNER_NAMESPACE, secrets["ai-test-codex-callback"]["metadata"]["namespace"]
        )
        self.assertEqual(
            RUNNER_NAMESPACE, secrets["ai-test-test-callback"]["metadata"]["namespace"]
        )
        self.assertNotIn("api-key", secrets["ai-test-test-callback"]["stringData"])
        self.assertNotIn("api-key", secrets["ai-test-gateway"]["stringData"])

    def test_kustomization_excludes_the_opt_in_and_example_manifests(self) -> None:
        kustomization = yaml.safe_load(
            (K8S_ROOT / "base" / "kustomization.yaml").read_text(encoding="utf-8")
        )
        resources = set(kustomization["resources"])
        present = {
            path.name
            for path in (K8S_ROOT / "base").glob("*.yaml")
            if path.name != "kustomization.yaml"
        }
        self.assertEqual(
            {"secrets.example.yaml", "network-policy-test-runner-offline.yaml"},
            present - resources,
        )


class JobTemplateConformanceTests(unittest.TestCase):
    """The templates in k8s/templates must equal what the gateway POSTs.

    The gateway builds Job manifests in Python, so the checked-in templates are
    the only thing an operator can review. Comparing them here means a change to
    either side that weakens the runner sandbox fails the build.
    """

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.created: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            manifest = json.loads(request.content)
            self.created.append(manifest)
            return httpx.Response(
                201, json={"metadata": {"name": manifest["metadata"]["name"]}}
            )

        self.client = httpx.Client(
            base_url="https://kubernetes.example.test",
            transport=httpx.MockTransport(handler),
        )
        root = Path(self.temporary_directory.name)
        self.settings = Settings(
            database_url="sqlite+pysqlite:///:memory:",
            api_token="api-token-for-tests",
            runner_token="runner-token-for-tests",
            artifact_signing_key="artifact-signing-key-for-tests",
            artifact_root=root / "artifacts",
            repository_policy_path=root / "policy.yaml",
            kubernetes_namespace=RUNNER_NAMESPACE,
            codex_token_secret_name="ai-test-codex-callback",
            test_token_secret_name="ai-test-test-callback",
        )
        self.launcher = KubernetesApiJobLauncher(self.settings, client=self.client)

    def tearDown(self) -> None:
        self.client.close()
        self.temporary_directory.cleanup()

    def test_prepare_template_matches_the_launcher(self) -> None:
        self.launcher.launch_prepare(
            PrepareLaunch(
                job_id=JOB_ID,
                task_id="TASK-123",
                repository="github.com/Luseramia/ai-orchestrator",
                clone_url="https://github.com/Luseramia/ai-orchestrator.git",
                base_sha=BASE_SHA,
                plan_object_key="jobs/plan.json",
            )
        )
        self.assertManifestMatches("codex-prepare-job.yaml", self.created[0])

    def test_generate_template_matches_the_launcher(self) -> None:
        self.launcher.launch_generate(
            GenerateLaunch(
                job_id=JOB_ID,
                task_id="TASK-123",
                repository="github.com/Luseramia/ai-orchestrator",
                clone_url="https://github.com/Luseramia/ai-orchestrator.git",
                base_sha=BASE_SHA,
                code_sha=CODE_SHA,
                attempt=1,
                plan_object_key="jobs/plan.json",
                draft_object_key="jobs/draft.json",
                patch_policy=PATCH_POLICY,
            )
        )
        self.assertManifestMatches("codex-generate-job.yaml", self.created[0])

    def test_test_runner_template_matches_the_launcher(self) -> None:
        self.launcher.launch_test(
            TestLaunch(
                job_id=JOB_ID,
                task_id="TASK-123",
                repository="github.com/Luseramia/ai-orchestrator",
                clone_url="https://github.com/Luseramia/ai-orchestrator.git",
                base_sha=BASE_SHA,
                code_sha=CODE_SHA,
                attempt=1,
                patch_object_key="jobs/tests.patch",
                patch_sha256=PATCH_SHA256,
                runner_policy=RUNNER_POLICY,
            )
        )
        self.assertManifestMatches("test-runner-job.yaml", self.created[0])

    def test_only_codex_workloads_receive_the_model_credential(self) -> None:
        secret_names: dict[str, set[str]] = {}
        for template in ("codex-prepare-job", "codex-generate-job", "test-runner-job"):
            manifest = self.template(f"{template}.yaml")
            container = manifest["spec"]["template"]["spec"]["containers"][0]
            secret_names[template] = {
                variable["name"]
                for variable in container["env"]
                if "valueFrom" in variable
            }
        self.assertIn("OPENAI_API_KEY", secret_names["codex-prepare-job"])
        self.assertIn("OPENAI_API_KEY", secret_names["codex-generate-job"])
        self.assertNotIn("OPENAI_API_KEY", secret_names["test-runner-job"])

    def test_callback_secrets_are_separate_per_workload_type(self) -> None:
        codex = self.env_secret("codex-generate-job.yaml", "RUNNER_TOKEN")
        test = self.env_secret("test-runner-job.yaml", "RUNNER_TOKEN")
        self.assertEqual("ai-test-codex-callback", codex["name"])
        self.assertEqual("ai-test-test-callback", test["name"])
        self.assertNotEqual(codex["name"], test["name"])

    def env_secret(self, template_name: str, variable_name: str) -> dict[str, Any]:
        container = self.template(template_name)["spec"]["template"]["spec"][
            "containers"
        ][0]
        for variable in container["env"]:
            if variable["name"] == variable_name:
                return variable["valueFrom"]["secretKeyRef"]
        raise AssertionError(f"{variable_name} is missing from {template_name}")

    def template(self, name: str) -> dict[str, Any]:
        text = (K8S_ROOT / "templates" / name).read_text(encoding="utf-8")
        gateway_url = self.settings.gateway_internal_url
        substitutions = {
            "__JOB_NAME__": self.expected_name(name),
            "__JOB_ID__": JOB_ID,
            "__TASK_ID__": "TASK-123",
            "__CLONE_URL__": "https://github.com/Luseramia/ai-orchestrator.git",
            "__BASE_SHA__": BASE_SHA,
            "__CODE_SHA__": CODE_SHA,
            "__ATTEMPT__": "1",
            "__GATEWAY_URL__": gateway_url,
            "__ARTIFACT_GATEWAY_URL__": (
                f"{gateway_url}/internal/v1/test-jobs/{JOB_ID}/artifacts"
            ),
            "__CODEX_RUNNER_IMAGE__": self.settings.codex_runner_image,
            "__TEST_RUNNER_IMAGE__": self.settings.test_runner_image,
            "__PLAN_OBJECT_KEY__": "jobs/plan.json",
            "__DRAFT_OBJECT_KEY__": "jobs/draft.json",
            "__PATCH_OBJECT_KEY__": "jobs/tests.patch",
            "__PATCH_SHA256__": PATCH_SHA256,
            "__PATCH_POLICY_JSON__": json.dumps(
                PATCH_POLICY, sort_keys=True, separators=(",", ":")
            ),
            "__RUNNER_POLICY_JSON__": json.dumps(
                RUNNER_POLICY, sort_keys=True, separators=(",", ":")
            ),
            "__CODEX_CALLBACK_SECRET__": self.settings.codex_callback_secret_name,
            "__TEST_CALLBACK_SECRET__": self.settings.test_callback_secret_name,
            "__CODEX_SECRET__": self.settings.codex_secret_name,
            "__ACTIVE_DEADLINE_SECONDS__": str(
                self.settings.runner_active_deadline_seconds
            ),
            "__TTL_SECONDS__": str(self.settings.runner_ttl_seconds),
        }
        for placeholder, value in substitutions.items():
            text = text.replace(placeholder, value)
        self.assertNotIn("__", text, f"{name} has unsubstituted placeholders")
        return yaml.safe_load(text)

    @staticmethod
    def expected_name(template_name: str) -> str:
        prefix = {
            "codex-prepare-job.yaml": "codex-prepare",
            "codex-generate-job.yaml": "codex-generate",
            "test-runner-job.yaml": "test-runner",
        }[template_name]
        return f"{prefix}-{JOB_ID.removeprefix('atj_')[-20:]}-1"

    def assertManifestMatches(
        self, template_name: str, manifest: dict[str, Any]
    ) -> None:
        expected = self.template(template_name)
        namespace = expected["metadata"].pop("namespace")
        self.assertEqual(self.settings.kubernetes_namespace, namespace)
        self.assertEqual(expected, manifest)


if __name__ == "__main__":
    unittest.main()
