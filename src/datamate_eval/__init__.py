"""Local evaluation only. Never import reference labels into inference."""

from .matching import score_answer
from .runner import evaluate

__all__ = ["score_answer", "evaluate"]
