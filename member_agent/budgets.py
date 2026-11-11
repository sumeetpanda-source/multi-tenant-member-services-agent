import time
from decimal import Decimal

from .errors import BudgetExceeded


class Budget:
    def __init__(self, settings, store, scope):
        self.settings, self.store, self.scope = settings, store, scope
        self.initial = store.usage(scope)
        self.steps = self.initial["steps"]
        self.tokens = self.initial["model_tokens"]
        self.cost = Decimal(self.initial["cost"])
        self.started = time.monotonic()

    @property
    def remaining(self):
        elapsed = time.monotonic() - self.started
        return min(self.settings.turn_timeout - elapsed,
                   self.settings.max_active_seconds - self.initial["active_seconds"] - elapsed)

    def check(self):
        if self.remaining <= 0:
            raise BudgetExceeded("latency_budget_exceeded")
        if self.steps > self.settings.max_steps:
            raise BudgetExceeded("step_budget_exceeded")
        if self.tokens > self.settings.max_model_tokens:
            raise BudgetExceeded("token_budget_exceeded")
        if self.cost > self.settings.max_cost:
            raise BudgetExceeded("cost_budget_exceeded")

    def step(self):
        self.check()
        if self.steps >= self.settings.max_steps:
            raise BudgetExceeded("step_budget_exceeded")
        self.steps += 1
        self.persist()

    def reserve_model(self, input_bound, output_bound):
        self.step()
        reservation = ((Decimal(input_bound) * self.settings.input_rate
                        + Decimal(output_bound) * self.settings.output_rate) / Decimal(1_000_000))
        if self.cost + reservation > self.settings.max_cost:
            raise BudgetExceeded("cost_budget_exceeded")
        if self.tokens + input_bound + output_bound > self.settings.max_model_tokens:
            raise BudgetExceeded("token_budget_exceeded")
        self.cost += reservation
        self.tokens += input_bound + output_bound
        self.persist()
        return reservation, input_bound + output_bound

    def settle_model(self, reserved, input_tokens, output_tokens):
        if min(input_tokens, output_tokens) < 0:
            raise BudgetExceeded("invalid_model_usage")
        cost = ((Decimal(input_tokens) * self.settings.input_rate
                 + Decimal(output_tokens) * self.settings.output_rate) / Decimal(1_000_000))
        self.cost += cost - reserved[0]
        self.tokens += input_tokens + output_tokens - reserved[1]
        self.persist()
        self.check()
        return cost

    def pause(self, seconds):
        self.check()
        if seconds < 0 or seconds >= self.remaining:
            raise BudgetExceeded("retry_exceeds_latency_budget")
        time.sleep(seconds)

    def metrics(self):
        return {"steps": self.steps, "model_tokens": self.tokens, "cost_usd": str(self.cost),
                "active_seconds": round(self.initial["active_seconds"] + time.monotonic() - self.started, 4),
                "turn_seconds": round(time.monotonic() - self.started, 4)}

    def persist(self):
        self.store.save_usage(self.scope, {"steps": self.steps, "model_tokens": self.tokens,
                              "cost": self.cost, "active_seconds": self.metrics()["active_seconds"]})
