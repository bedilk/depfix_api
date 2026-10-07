"""The Week 6 fleet orchestrator: scan -> fix -> verify -> PR across many
repos, gated by each repo's own opt-in ``.depfix.yml`` and an idempotency
ledger."""

from depfix.orchestrator.models import ChangeOutcome, OrchestratorOutcome, RepoOutcome
from depfix.orchestrator.plan import PlanArtifact
from depfix.orchestrator.policy import SkipReason, decide, effective_max_changes_per_run
from depfix.orchestrator.runner import Orchestrator

__all__ = [
    "ChangeOutcome",
    "Orchestrator",
    "OrchestratorOutcome",
    "PlanArtifact",
    "RepoOutcome",
    "SkipReason",
    "decide",
    "effective_max_changes_per_run",
]
