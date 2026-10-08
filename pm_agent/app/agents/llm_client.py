"""
Provider-agnostic LLM client adapter.

MVP uses OpenAI only. The adapter pattern allows adding Groq, Anthropic,
Gemini, etc. later by implementing the LLMClient interface.
"""

import json
from abc import ABC, abstractmethod

from pm_agent.app.config import get_settings
from pm_agent.app.logging_config import get_logger

logger = get_logger("llm_client")


class LLMClient(ABC):
    """Abstract base class for LLM providers."""

    @abstractmethod
    async def generate_json(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: dict | None = None,
    ) -> dict:
        """
        Generate a structured JSON response from the LLM.

        Args:
            system_prompt: System-level instructions
            user_prompt: User-level input (transcript, question, etc.)
            schema: Optional JSON schema for structured output

        Returns:
            Parsed JSON dict from the LLM response
        """
        ...

    @abstractmethod
    async def generate_text(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        """Generate a plain text response from the LLM."""
        ...

    @abstractmethod
    async def embed(self, text: str) -> list[float]:
        """Generate an embedding vector for the given text."""
        ...


class OpenAIClient(LLMClient):
    """OpenAI LLM client implementation."""

    def __init__(self, api_key: str, model: str, embedding_model: str):
        from openai import AsyncOpenAI

        self.client = AsyncOpenAI(api_key=api_key)
        self.model = model
        self.embedding_model = embedding_model

    async def generate_json(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: dict | None = None,
    ) -> dict:
        """Generate structured JSON using OpenAI with response_format."""
        kwargs = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }

        response = await self.client.chat.completions.create(**kwargs)
        content = response.choices[0].message.content

        try:
            result = json.loads(content)
            return result
        except json.JSONDecodeError as e:
            logger.error(f"LLM returned invalid JSON: {e}\nContent: {content[:500]}")
            raise ValueError(f"LLM returned invalid JSON: {e}")

    async def generate_text(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        """Generate plain text response."""
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
        )
        return response.choices[0].message.content

    async def embed(self, text: str) -> list[float]:
        """Generate embedding using OpenAI embedding model."""
        response = await self.client.embeddings.create(
            model=self.embedding_model,
            input=text,
        )
        return response.data[0].embedding


# ── Factory ──


_client_instance: LLMClient | None = None


def get_llm_client() -> LLMClient:
    """
    Get or create the LLM client based on settings.
    Cached as a singleton.
    """
    global _client_instance

    if _client_instance is not None:
        return _client_instance

    settings = get_settings()
    provider = settings.llm_provider.lower()

    if provider == "openai":
        _client_instance = OpenAIClient(
            api_key=settings.openai_api_key,
            model=settings.llm_model,
            embedding_model=settings.embedding_model,
        )
    else:
        raise ValueError(
            f"Unsupported LLM provider: {provider}. "
            f"MVP supports: openai. "
            f"Add provider adapter to llm_client.py for others."
        )

    logger.info(f"LLM client initialized: provider={provider}, model={settings.llm_model}")
    return _client_instance
