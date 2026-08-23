"""Stable, task-agnostic classes for scored Harbor/Codex failures.

These labels are descriptive only. They never authorize a rerun or remove an
attempt from the official denominator.
"""

from enum import StrEnum


class FailureClass(StrEnum):
    AGENT_TIMEOUT = "agent_timeout"
    AGENT_RUNTIME = "agent_runtime"
    PROVIDER_CONFIGURATION = "provider_configuration"
    PROVIDER_QUOTA = "provider_quota"
    PROVIDER_AVAILABILITY = "provider_availability"
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_TRANSPORT = "provider_transport"
    UNKNOWN_API = "unknown_api"
    MODEL_BUDGET = "model_budget"
    SAFETY_REFUSAL = "safety_refusal"


_FAILURE_CLASS_BY_EXCEPTION = {
    "AgentTimeoutError": FailureClass.AGENT_TIMEOUT,
    "NonZeroAgentExitCodeError": FailureClass.AGENT_RUNTIME,
    "AgentAuthenticationError": FailureClass.PROVIDER_CONFIGURATION,
    "ApiProviderResourceNotFoundError": FailureClass.PROVIDER_CONFIGURATION,
    "ModelNotFoundError": FailureClass.PROVIDER_CONFIGURATION,
    "ApiRateLimitError": FailureClass.PROVIDER_QUOTA,
    "ApiUsageLimitError": FailureClass.PROVIDER_QUOTA,
    "ApiInternalServerError": FailureClass.PROVIDER_AVAILABILITY,
    "ApiOverloadedError": FailureClass.PROVIDER_AVAILABILITY,
    "UnknownApiError": FailureClass.UNKNOWN_API,
    "ApiConnectionClosedError": FailureClass.PROVIDER_TRANSPORT,
    "ApiResponseStalledError": FailureClass.PROVIDER_TIMEOUT,
    "NetworkConnectionError": FailureClass.PROVIDER_TRANSPORT,
    "ContextWindowExceededError": FailureClass.MODEL_BUDGET,
    "OutputTokenExceededError": FailureClass.MODEL_BUDGET,
    "AgentSafetyRefusalError": FailureClass.SAFETY_REFUSAL,
}
CLASSIFIED_EXCEPTION_TYPES = frozenset(_FAILURE_CLASS_BY_EXCEPTION)


def classify_failure(exception_type: str) -> FailureClass:
    """Return the predeclared class, rejecting unknown exception types."""
    try:
        return _FAILURE_CLASS_BY_EXCEPTION[exception_type]
    except (KeyError, TypeError) as error:
        raise ValueError("scored exception type is not classified") from error
