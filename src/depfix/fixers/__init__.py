"""Fix generators — call LLMs to produce migration patches."""

from depfix.fixers.bedrock import BedrockFixGenerator
from depfix.fixers.gemini import FixGenerator
from depfix.fixers.ollama import OllamaFixGenerator

__all__ = ["BedrockFixGenerator", "FixGenerator", "OllamaFixGenerator"]
