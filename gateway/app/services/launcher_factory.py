from __future__ import annotations

from app.config import Settings
from app.services.job_launcher import FakeKubernetesJobLauncher, JobLauncher
from app.services.kubernetes_launcher import KubernetesApiJobLauncher


def create_launcher(settings: Settings) -> JobLauncher:
    """Build the launcher named by configuration.

    The API process and the reconciler both create Kubernetes Jobs, and both
    must be wrong in the same way when configuration is wrong, so they share
    this one factory.
    """

    if settings.job_launcher_backend == "fake":
        return FakeKubernetesJobLauncher()
    if settings.job_launcher_backend == "kubernetes":
        return KubernetesApiJobLauncher(settings)
    raise ValueError(
        f"unsupported job launcher backend {settings.job_launcher_backend!r}"
    )
