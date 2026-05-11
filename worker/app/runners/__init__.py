"""Role-specific runners. Each runner is selected by the worker's pool."""
from app.runners.base import BaseRunner, RunnerContext, RunnerResult
from app.runners.coder import CoderRunner
from app.runners.docs import DocsRunner
from app.runners.planner import PlannerRunner
from app.runners.refactorer import RefactorerRunner
from app.runners.researcher import ResearcherRunner
from app.runners.reviewer import ReviewerRunner
from app.runners.tester import TesterRunner

RUNNERS: dict[str, type[BaseRunner]] = {
    "planner": PlannerRunner,
    "coder": CoderRunner,
    "reviewer": ReviewerRunner,
    "tester": TesterRunner,
    "refactorer": RefactorerRunner,
    "docs": DocsRunner,
    "researcher": ResearcherRunner,
}

__all__ = ["RUNNERS", "BaseRunner", "RunnerContext", "RunnerResult"]
