"""Low-cardinality Prometheus metrics for the HTTP boundary."""

from prometheus_client import CollectorRegistry, Counter, generate_latest

from agentic_incident_remediation.models import ApprovalDecision, WorkflowStatus


class ApiMetrics:
    """Own an isolated metric registry so app factories remain test-safe."""

    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.http_requests = Counter(
            "remediation_http_requests_total",
            "HTTP requests handled by the remediation API.",
            ("method", "status"),
            registry=self.registry,
        )
        self.executions_started = Counter(
            "remediation_executions_started_total",
            "Remediation executions accepted by the workflow.",
            registry=self.registry,
        )
        self.approval_decisions = Counter(
            "remediation_approval_decisions_total",
            "Human approval decisions submitted to the workflow.",
            ("decision",),
            registry=self.registry,
        )
        self.workflow_outcomes = Counter(
            "remediation_workflow_outcomes_total",
            "Terminal workflow outcomes.",
            ("status",),
            registry=self.registry,
        )

    def record_request(self, method: str, status_code: int) -> None:
        self.http_requests.labels(method=method, status=str(status_code)).inc()

    def record_start(self, status: WorkflowStatus) -> None:
        self.executions_started.inc()
        self._record_terminal(status)

    def record_decision(self, decision: ApprovalDecision, status: WorkflowStatus) -> None:
        self.approval_decisions.labels(decision=decision.value).inc()
        self._record_terminal(status)

    def render(self) -> bytes:
        return generate_latest(self.registry)

    def record_outcome(self, status: WorkflowStatus) -> None:
        self._record_terminal(status)

    def _record_terminal(self, status: WorkflowStatus) -> None:
        if status in {
            WorkflowStatus.COMPLETED,
            WorkflowStatus.REJECTED,
            WorkflowStatus.FAILED,
            WorkflowStatus.TERMINATED,
        }:
            self.workflow_outcomes.labels(status=status.value).inc()
