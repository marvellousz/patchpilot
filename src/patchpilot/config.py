import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

# Both providers expose OpenAI-compatible chat completion endpoints.
PROVIDER_BASE_URLS = {
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "groq": "https://api.groq.com/openai/v1",
}

# Free-tier limits are per model, so rotating between models multiplies throughput.
DEFAULT_CHAIN = "groq:openai/gpt-oss-120b,groq:openai/gpt-oss-20b,groq:qwen/qwen3.8-27b,gemini:gemini-3.5-flash"


@dataclass(frozen=True)
class Endpoint:
    provider: str
    model: str
    base_url: str
    api_key: str

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.model}"


def llm_chain(spec: str | None = None) -> list[Endpoint]:
    """Parse `provider:model,provider:model,...` (from LLM_CHAIN) in order of preference."""
    endpoints = []
    for item in (spec or os.getenv("LLM_CHAIN") or DEFAULT_CHAIN).split(","):
        provider, model = item.strip().split(":", 1)
        if provider not in PROVIDER_BASE_URLS:
            raise ValueError(f"Unknown LLM provider: {provider}")
        api_key = os.getenv(f"{provider.upper()}_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError(f"{provider.upper()}_API_KEY is not set (needed for {item.strip()})")
        endpoints.append(Endpoint(provider, model, PROVIDER_BASE_URLS[provider], api_key))
    return endpoints
