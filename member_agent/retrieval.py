import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .domain import Citation
from .errors import AgentError
from .privacy import safe_evidence


@dataclass(frozen=True)
class Document:
    source: str
    tenant: str
    product: str
    year: int
    effective: date
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class Evidence:
    values: tuple[str, ...]
    citation: Citation


class KnowledgeBase:
    def __init__(self, pack_dir: Path):
        self.root = (Path(pack_dir) / "kb").resolve()
        self._documents: dict[str, tuple[Document, ...]] = {}
        self._issues: dict[str, list[dict]] = {}

    def _load(self, tenant):
        if tenant in self._documents:
            return
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", tenant):
            raise AgentError("invalid_tenant")
        folder = self.root / tenant
        documents, issues = [], []
        for path in sorted(folder.glob("*.md")):
            try:
                if path.is_symlink() or folder.is_symlink() or path.resolve().parent != folder:
                    raise ValueError("untrusted_source_path")
                if path.stat().st_size > 250_000:
                    raise ValueError("document_too_large")
                text = path.read_text(encoding="utf-8")
                if not text.startswith("---\n"):
                    raise ValueError("invalid_front_matter")
                parts = text.split("---", 2)
                if len(parts) != 3:
                    raise ValueError("invalid_front_matter")
                metadata = {}
                for line in parts[1].strip().splitlines():
                    key, separator, value = line.partition(":")
                    if not separator or key.strip() in metadata:
                        raise ValueError("invalid_front_matter")
                    metadata[key.strip()] = value.strip()
                if metadata.get("tenant") != tenant:
                    raise ValueError("tenant_metadata_mismatch")
                year = int(metadata["plan_year"])
                effective = date.fromisoformat(metadata["effective_date"])
                product = metadata["product_code"]
                if not re.fullmatch(r"[A-Z0-9_]{1,20}", product) or effective.year != year:
                    raise ValueError("invalid_document_scope")
                rows = []
                for line in parts[2].splitlines():
                    if not line.strip().startswith("|"):
                        continue
                    cells = tuple(cell.strip() for cell in line.strip().strip("|").split("|"))
                    if len(cells) < 2 or re.fullmatch(r"[-: ]+", cells[0]):
                        continue
                    rows.append(cells)
                documents.append(Document(f"kb/{tenant}/{path.name}", tenant, product,
                                          year, effective, tuple(rows)))
            except (ValueError, KeyError, OSError) as exc:
                code = str(exc) if str(exc) in {
                    "tenant_metadata_mismatch", "untrusted_source_path", "document_too_large",
                    "invalid_front_matter", "invalid_document_scope"} else "invalid_document"
                issues.append({"source": f"kb/{tenant}/{path.name}", "code": code})
        self._documents[tenant] = tuple(documents)
        self._issues[tenant] = issues

    def issues(self, tenant):
        self._load(tenant)
        return list(self._issues[tenant])

    def lookup(self, tenant, product, year, label, effective_on: date) -> Evidence:
        self._load(tenant)
        matches = []
        for doc in self._documents[tenant]:
            if (doc.product not in {product, "ALL"} or doc.year != year
                    or doc.effective > effective_on):
                continue
            for row in doc.rows:
                if row[0].casefold() == label.casefold():
                    values = tuple(safe_evidence(value) for value in row[1:])
                    if all(values):
                        rank = (doc.effective, doc.product == product)
                        matches.append((rank, values, doc))
        if not matches:
            raise AgentError("evidence_unavailable")
        best_rank = max(item[0] for item in matches)
        best = [item for item in matches if item[0] == best_rank]
        if len({item[1] for item in best}) != 1:
            raise AgentError("ambiguous_evidence")
        _, values, doc = best[0]
        return Evidence(values, Citation(doc.source, year, label))
