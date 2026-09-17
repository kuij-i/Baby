"""Deterministic default verifier for BABY foundation flows."""

from baby.core import AgentResult, VerificationResult
from baby.verification.base import Verifier


class ResultStatusVerifier(Verifier):
    """Verify results using explicit execution success/error status."""

    async def verify(self, result: AgentResult) -> VerificationResult:
        issues: list[str] = []
        if not result.success:
            issues.append(result.error or "Agent execution reported failure")
        elif result.error:
            issues.append(result.error)

        return VerificationResult(
            agent_result_id=result.id,
            verified=not issues,
            verification_method="agent_result_status",
            issues=issues,
        )


verifier = ResultStatusVerifier()
