class ArchGateError(Exception):
    """Base class for all ArchGate errors."""


class RequirementsError(ArchGateError):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("invalid requirements: " + "; ".join(problems))


class KnowledgeBaseError(ArchGateError):
    """The rule base or component catalog is malformed."""


class IllegalTransition(ArchGateError):
    """A pipeline step tried to move to a state it is not allowed to reach."""


class BudgetExceeded(ArchGateError):
    """A run used up its LLM call or time budget."""


class LLMError(ArchGateError):
    """The model backend failed or returned something unusable."""
