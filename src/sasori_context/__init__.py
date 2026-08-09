"""Optional, dependency-free context projection for Sasori models."""

from .budget import (
    BoundedContextModel,
    ContextBudget,
    ContextBudgetExceeded,
    ContextProjection,
    ContextProjectionError,
    ContextProjector,
    ContextStructureError,
    ProtectedContextMessage,
    default_message_units,
)
from .semantic import (
    SemanticCompactionError,
    SemanticCompactionModel,
    SemanticCompactionPolicy,
    SemanticCompactionRecord,
    SemanticSummaryCache,
)

__all__ = [
    "BoundedContextModel",
    "ContextBudget",
    "ContextBudgetExceeded",
    "ContextProjection",
    "ContextProjectionError",
    "ContextProjector",
    "ContextStructureError",
    "ProtectedContextMessage",
    "SemanticCompactionError",
    "SemanticCompactionModel",
    "SemanticCompactionPolicy",
    "SemanticCompactionRecord",
    "SemanticSummaryCache",
    "default_message_units",
]
