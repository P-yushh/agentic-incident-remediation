"""Allowlisted synthetic scenarios shared by the dashboard and reviewed runner."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal


class DemoScenario(StrEnum):
    OOM = "kubernetes_oom_killed"
    CRASH_LOOP = "kubernetes_crash_loop"
    TERRAFORM = "terraform_drift"


@dataclass(frozen=True)
class ScenarioSpec:
    scenario: DemoScenario
    label: str
    service: str
    title: str
    description: str
    validator: Literal["kubeconform", "terraform_validate"]
    artifact_kind: str
    extension: str

    def incident(self) -> dict[str, Any]:
        return {
            "type": self.scenario.value,
            "title": self.title,
            "description": "Synthetic demonstration. Prepare a concise generate/validate/review "
            "plan. " + self.description + " Do not apply anything.",
            "source": "reviewed-live-demo",
            "context": {"terraform_resource": "aws_security_group_rule"}
            if self.scenario is DemoScenario.TERRAFORM
            else {"k8s_kind": "Deployment", "k8s_api_version": "apps/v1"},
        }

    def public(self) -> dict[str, str]:
        return {
            "id": self.scenario.value,
            "label": self.label,
            "service": self.service,
            "title": self.title,
            "description": self.description,
            "validator": self.validator,
        }


SCENARIOS = {
    DemoScenario.OOM: ScenarioSpec(
        DemoScenario.OOM,
        "Kubernetes memory limit exceeded",
        "payment-api",
        "Payment API memory limit exceeded",
        "Propose a complete apps/v1 Deployment named payment-api in namespace demo, "
        "replicas 1, selector and Pod label app=payment-api, container name api, "
        "image example/payment-api:1.0. Increase memory request from 128Mi to 256Mi "
        "and limit from 256Mi to 512Mi.",
        "kubeconform",
        "kubernetes_manifest",
        "yaml",
    ),
    DemoScenario.CRASH_LOOP: ScenarioSpec(
        DemoScenario.CRASH_LOOP,
        "Kubernetes CrashLoop / incorrect configuration reference",
        "queue-worker",
        "Worker cannot find its configuration",
        "The queue-worker Deployment references nonexistent ConfigMap worker-config-old. "
        "The synthetic scenario assumes worker-config exists; no live lookup is performed. "
        "Propose a complete apps/v1 Deployment named queue-worker in namespace demo, replicas 1, "
        "selector and Pod label app=queue-worker, container name worker, image "
        "example/queue-worker:1.0, envFrom configMapRef name worker-config. "
        "Schema validation does not prove that the workload recovers.",
        "kubeconform",
        "kubernetes_manifest",
        "yaml",
    ),
    DemoScenario.TERRAFORM: ScenarioSpec(
        DemoScenario.TERRAFORM,
        "Terraform drift / public ingress",
        "checkout",
        "Security group allows public ingress",
        "Propose only an aws_security_group_rule resource named service_https for ingress "
        'TCP port 443, security_group_id sg-demo123. Change cidr_blocks from ["0.0.0.0/0"] '
        'to ["10.20.0.0/16"]. No provider configuration, backend, credentials or apply. '
        "The synthetic IDs and private CIDR are scenario inputs, not observed AWS resources.",
        "terraform_validate",
        "terraform_configuration",
        "tf",
    ),
}


def scenario_for_manifest(manifest: dict[str, Any]) -> DemoScenario:
    """Legacy v1 bundles are OOM-only; v2 binds the explicit scenario to the incident."""
    if type(manifest.get("version")) is not int:
        raise ValueError("unsupported review manifest")
    if manifest.get("version") == 1:
        scenario = DemoScenario.OOM
    elif manifest.get("version") == 2:
        scenario = DemoScenario(manifest["scenario"])
    else:
        raise ValueError("unsupported review manifest")
    if manifest["state"]["incident"]["type"] != scenario.value:
        raise ValueError("review scenario does not match its incident")
    return scenario
