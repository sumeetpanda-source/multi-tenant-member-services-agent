from dataclasses import asdict, dataclass, field
from decimal import Decimal


@dataclass(frozen=True)
class Scope:
    session_id: str
    tenant: str
    member_id: str = field(repr=False)


@dataclass(frozen=True)
class Citation:
    source: str
    plan_year: int
    section: str


@dataclass
class Reply:
    session_id: str
    behaviour: str
    text: str
    citations: list[Citation] = field(default_factory=list)
    operation_id: str | None = None
    metrics: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


def money(value) -> str:
    amount = Decimal(str(value)).quantize(Decimal("0.01"))
    text = f"{amount:,.2f}"
    return "$" + (text[:-3] if text.endswith(".00") else text)
