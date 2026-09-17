"""Verification interfaces and default implementation for BABY."""

from baby.verification.base import Verifier
from baby.verification.default import ResultStatusVerifier, verifier

__all__ = ["Verifier", "ResultStatusVerifier", "verifier"]
