"""NVIDIA model provider (NVIDIA API Catalog at build.nvidia.com, or a self-hosted NIM).

NVIDIA's endpoints implement the OpenAI chat completions API, including tool calling, so
this provider reuses ``OpenAIProvider`` and only changes where credentials, endpoint and
default model come from (``NVIDIA_API_KEY``, ``NVIDIA_BASE_URL``, ``NVIDIA_MODEL``).
"""

from typing import List, Optional

from baby.configuration import settings
from baby.providers.openai_provider import OpenAIProvider


class NvidiaProvider(OpenAIProvider):
    """OpenAI-compatible provider bound to NVIDIA's endpoint and model catalog."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        default_model: Optional[str] = None,
        supported_models: Optional[List[str]] = None,
    ) -> None:
        model = default_model or settings.nvidia_model
        super().__init__(
            api_key=api_key or settings.nvidia_api_key,
            base_url=base_url or settings.nvidia_base_url,
            default_model=model,
            supported_models=supported_models or [model],
        )

    @property
    def name(self) -> str:
        return "nvidia"
