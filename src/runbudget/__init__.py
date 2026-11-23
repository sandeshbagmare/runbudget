"""A per-run token and cost budget, with a warning and a clean stop.

Summarization bounds how long the *context* may get. It says nothing about what the run
*costs*: forty model calls with a 30k-token prompt and a 2k-token answer is a real number,
and nobody checks it until the invoice arrives.

    >>> from runbudget import Budget, RunBudget
    >>> budget = RunBudget(budget=Budget(max_cost=0.05))
    >>> budget.add("gpt-4o-mini", input_tokens=10_000, output_tokens=1_000).action
    'ok'
    >>> budget.spent
    0.0021
    >>> budget.add("gpt-4o", input_tokens=10_000, output_tokens=1_000).action
    'ok'
    >>> budget.add("gpt-4o", input_tokens=10_000, output_tokens=1_000).action
    'stop'

Prices are per million tokens and are yours to override: this library knows the
arithmetic, not today's price list. Standard library only, Python 3.9+.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["Budget", "BudgetExceeded", "ModelPrice", "RunBudget", "Status"]

__version__ = "0.1.0"

# A small, deliberately conservative table. Override it with prices= to stay current.
DEFAULT_PRICES = {
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "claude-opus": (15.00, 75.00),
    "claude-sonnet": (3.00, 15.00),
    "claude-haiku": (0.80, 4.00),
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-2.5-flash": (0.30, 2.50),
    "llama": (0.00, 0.00),
}


@dataclass(frozen=True)
class ModelPrice:
    """US dollars per million tokens, for input and output."""

    input: float
    output: float

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        cost = input_tokens * self.input + output_tokens * self.output
        return round(cost / 1_000_000, 6)


@dataclass(frozen=True)
class Budget:
    """The limits. Any field left as ``None`` is unlimited."""

    max_cost: float | None = None
    max_tokens: int | None = None
    max_steps: int | None = None


@dataclass(frozen=True)
class Status:
    """The verdict after a step: ``ok``, ``warn`` or ``stop``."""

    action: str
    reason: str | None = None
    spent: float = 0.0
    tokens: int = 0
    steps: int = 0

    @property
    def allowed(self) -> bool:
        return self.action != "stop"

    @property
    def stopped(self) -> bool:
        return self.action == "stop"

    def __bool__(self) -> bool:
        return self.allowed

    def __str__(self) -> str:
        return self.action if self.reason is None else f"{self.action}: {self.reason}"


@dataclass(frozen=True)
class Step:
    """One recorded model call."""

    model: str
    input_tokens: int
    output_tokens: int
    cost: float

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class BudgetExceeded(RuntimeError):
    """Raised by :meth:`RunBudget.enforce` when the budget is spent."""

    def __init__(self, message: str, status: Status) -> None:
        super().__init__(message)
        self.status = status
        self.action = status.action
        self.reason = status.reason


@dataclass
class RunBudget:
    """Track spend across one run and answer "may I make another call?".

    ``prices`` maps a model name (or a prefix of one) to a :class:`ModelPrice` or a
    ``(input, output)`` tuple. Unknown models raise rather than silently costing zero.
    ``warn_at`` is the fraction of any limit at which :class:`Status` becomes ``warn``.
    """

    budget: Budget = field(default_factory=Budget)
    prices: dict[str, ModelPrice | tuple[float, float]] = field(default_factory=dict)
    warn_at: float = 0.8
    _steps: list[Step] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        if not 0 < self.warn_at <= 1:
            raise ValueError("warn_at must be in (0, 1]")

    # -- recording -------------------------------------------------------

    def add(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        *,
        cost: float | None = None,
    ) -> Status:
        """Record a model call and return the status after it."""
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("token counts must not be negative")
        if cost is None:
            cost = self._price(model).cost(input_tokens, output_tokens)
        self._steps.append(
            Step(model=model, input_tokens=input_tokens, output_tokens=output_tokens, cost=cost)
        )
        return self.status()

    def add_cost(self, cost: float, model: str = "manual") -> Status:
        """Record a charge that is not a model call, such as a paid tool."""
        if cost < 0:
            raise ValueError("cost must not be negative")
        return self.add(model, 0, 0, cost=cost)

    def reset(self) -> None:
        """Start a new run on the same object."""
        self._steps.clear()

    # -- reading ---------------------------------------------------------

    @property
    def spent(self) -> float:
        return round(sum(step.cost for step in self._steps), 6)

    @property
    def tokens(self) -> int:
        return sum(step.tokens for step in self._steps)

    @property
    def steps(self) -> int:
        return len(self._steps)

    def status(self) -> Status:
        """Where the run stands against its limits."""
        spent, tokens, steps = self.spent, self.tokens, self.steps
        checks = [
            ("cost", spent, self.budget.max_cost, lambda value: f"${value:.4f}"),
            ("tokens", tokens, self.budget.max_tokens, str),
            ("steps", steps, self.budget.max_steps, str),
        ]
        for name, value, limit, show in checks:
            if limit is None:
                continue
            if value >= limit:
                return Status(
                    action="stop",
                    reason=f"{name} budget reached: {show(value)} of {show(limit)}",
                    spent=spent,
                    tokens=tokens,
                    steps=steps,
                )
        for name, value, limit, show in checks:
            if limit is None:
                continue
            if value >= limit * self.warn_at:
                return Status(
                    action="warn",
                    reason=f"{name} budget at {value / limit:.0%}: {show(value)} of {show(limit)}",
                    spent=spent,
                    tokens=tokens,
                    steps=steps,
                )
        return Status(action="ok", spent=spent, tokens=tokens, steps=steps)

    def check(self) -> None:
        """Raise :class:`BudgetExceeded` if the budget is spent."""
        status = self.status()
        if status.action == "stop":
            raise BudgetExceeded(status.reason or "budget exceeded", status)

    def enforce(self) -> None:
        """Alias for :meth:`check`, for reading at a call site."""
        self.check()

    @property
    def remaining(self) -> Budget:
        """What is left of each limit, as a :class:`Budget`."""
        spent, tokens, steps = self.spent, self.tokens, self.steps
        return Budget(
            max_cost=None
            if self.budget.max_cost is None
            else round(max(self.budget.max_cost - spent, 0.0), 6),
            max_tokens=None
            if self.budget.max_tokens is None
            else max(self.budget.max_tokens - tokens, 0),
            max_steps=None
            if self.budget.max_steps is None
            else max(self.budget.max_steps - steps, 0),
        )

    def breakdown(self) -> dict[str, dict[str, float]]:
        """Totals per model, for a report at the end of a run."""
        totals: dict[str, dict[str, float]] = {}
        for step in self._steps:
            entry = totals.setdefault(
                step.model, {"steps": 0, "input_tokens": 0, "output_tokens": 0, "cost": 0.0}
            )
            entry["steps"] += 1
            entry["input_tokens"] += step.input_tokens
            entry["output_tokens"] += step.output_tokens
            entry["cost"] = round(entry["cost"] + step.cost, 6)
        return totals

    def _price(self, model: str) -> ModelPrice:
        table = {**DEFAULT_PRICES, **self.prices}
        if model in table:
            return _as_price(table[model])
        for name in sorted(table, key=len, reverse=True):
            if model.startswith(name):
                return _as_price(table[name])
        message = (
            f"no price for model {model!r}; pass prices={{{model!r}: (input, output)}} "
            "in dollars per million tokens"
        )
        raise KeyError(message)


def _as_price(value: ModelPrice | tuple[float, float]) -> ModelPrice:
    if isinstance(value, ModelPrice):
        return value
    input_price, output_price = value
    return ModelPrice(float(input_price), float(output_price))
