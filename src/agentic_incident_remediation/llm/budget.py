"""A process-local request ceiling for explicitly opted-in demonstrations."""

from typing import TypeVar

from pydantic import BaseModel

from agentic_incident_remediation.llm.gateway import ModelGatewayError, StructuredModel

ResponseModel = TypeVar("ResponseModel", bound=BaseModel)


class BudgetedModel:
    """Count attempted invocations, including failures, without retrying the delegate."""

    def __init__(self, delegate: StructuredModel, *, maximum: int = 2) -> None:
        if maximum < 1:
            raise ValueError("model invocation budget must be positive")
        self.delegate = delegate
        self.maximum = maximum
        self.calls = 0

    def generate_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_model: type[ResponseModel],
    ) -> ResponseModel:
        if self.calls >= self.maximum:
            raise ModelGatewayError("live smoke-test request budget exhausted; no request sent")
        self.calls += 1
        return self.delegate.generate_structured(
            system_prompt=system_prompt, user_prompt=user_prompt, response_model=response_model
        )
