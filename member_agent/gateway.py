import hmac
import hashlib
import json
import re
import time
from datetime import date
from urllib.parse import parse_qs, urlencode, urlsplit

from .errors import AgentError, PolicyDenied, UpstreamError
from .privacy import project_benefits, project_claim, redact_sensitive, safe_evidence
from .routing import BENEFITS
from .transport import JsonTransport


TOOL_PARAMETERS = {
    "business_date": set(), "benefits": set(), "claim": {"claim_id"},
    "claims": set(), "claims_on_date": {"service_date"}, "grievances": set(), "verify_last_four": {"last_four"},
    "providers": {"specialty"}, "retrieve": {"year", "label", "effective_on"},
    "draft_grievance": {"category", "description", "expedite_requested"},
    "file_grievance": {"operation_id"},
}
RULE_LABELS = {"Physical therapy visit limit", "Claim appeal filing window", "Grievance resolution timeframe",
               *BENEFITS.values(), "CO-16", "CO-18", "CO-27", "CO-50", "CO-96", "CO-197"}
CATEGORIES = {"billing", "quality_of_care", "access", "customer_service", "other"}
SPECIALTIES = {"dermatology": "Dermatology", "cardiology": "Cardiology",
               "primary care": "Primary Care", "pediatrics": "Pediatrics"}


class Gateway:
    def __init__(self, settings, store, knowledge, scope, budget, transport=None):
        self.settings, self.store, self.knowledge = settings, store, knowledge
        self.scope, self.budget = scope, budget
        self.transport = transport or JsonTransport()
        self._benefits_cache = None
        self.cache_hits = 0
        self.http_requests = 0
        self.max_data_age = 0.0

    def call(self, tool, **arguments):
        started = time.monotonic()
        valid_tool = tool if isinstance(tool, str) and tool in TOOL_PARAMETERS else "unknown"
        try:
            self.store.usage(self.scope)
            self.budget.step()
            if self.scope.tenant not in self.settings.tenant_tokens:
                raise PolicyDenied("unknown_tenant")
            if not isinstance(tool, str) or tool not in TOOL_PARAMETERS or set(arguments) != TOOL_PARAMETERS[tool]:
                raise PolicyDenied("invalid_tool_arguments")
            self.store.event(self.scope, "gateway.decision", tool=valid_tool, decision="allow")
            result = getattr(self, "_tool_" + tool)(**arguments)
            self.budget.check()
            self.store.event(self.scope, "tool.end", tool=valid_tool, status="completed",
                             elapsed_ms=round((time.monotonic() - started) * 1000))
            return result
        except AgentError as exc:
            self.store.event(self.scope, "gateway.decision", tool=valid_tool, decision="deny", code=exc.code)
            self.store.event(self.scope, "tool.end", tool=valid_tool, status="failed",
                             elapsed_ms=round((time.monotonic() - started) * 1000))
            raise

    def _http(self, path, *, body=None, key=None, directory=False):
        if directory:
            headers = {"X-Api-Key": self.settings.directory_key}
        else:
            headers = {"Authorization": "Bearer " + self.settings.tenant_tokens[self.scope.tenant]}
        if body is not None:
            headers["Content-Type"] = "application/json"
            headers["Idempotency-Key"] = key
        for attempt in range(self.settings.max_retries + 1):
            self.budget.step()
            self.http_requests += 1
            self.store.event(self.scope, "http.attempt", attempt=attempt + 1)
            try:
                result = self.transport.request(self.settings.api_url + path, headers=headers,
                                                body=body, timeout=self.budget.remaining)
                if not isinstance(result, dict):
                    raise UpstreamError(502)
                return result
            except UpstreamError as exc:
                self.store.event(self.scope, "http.error", status=exc.status, attempt=attempt + 1)
                if exc.status not in {429, 503} or attempt >= self.settings.max_retries:
                    raise
                self.store.event(self.scope, "http.retry", attempt=attempt + 1)
                self.budget.pause(max(exc.retry_after, 0.05))
        raise UpstreamError(503)

    @property
    def member_path(self):
        # Scope identifiers were validated by the session store, never by a model.
        return f"/tenants/{self.scope.tenant}/members/{self.scope.member_id}"

    def _check_member(self, raw):
        if not isinstance(raw, dict):
            raise UpstreamError(502)
        if raw.get("member_id") != self.scope.member_id:
            raise PolicyDenied("member_scope_mismatch")

    def _tool_business_date(self):
        try:
            return date.fromisoformat(self._http("/health")["clock"])
        except (KeyError, TypeError, ValueError):
            raise UpstreamError(502) from None

    def _tool_benefits(self):
        if self._benefits_cache is None:
            try:
                raw = self._http(self.member_path + "/benefits")
            except UpstreamError as exc:
                if exc.status == 404:
                    raise AgentError("member_not_found") from None
                raise
            self._check_member(raw)
            self._benefits_cache = project_benefits(raw)
        return self._benefits_cache

    def _tool_claim(self, claim_id):
        if not isinstance(claim_id, str) or not re.fullmatch(r"C\d{6,20}", claim_id):
            raise PolicyDenied("invalid_claim_id")
        cached = self._cached("claim/" + claim_id)
        if cached is not None:
            return cached
        cached_all = self._cached("claims/all")
        if cached_all is not None:
            found = next((item for item in cached_all if item["claim_id"] == claim_id), None)
            if found:
                return found
        captured = time.time()
        try:
            raw = self._http(f"/tenants/{self.scope.tenant}/claims/{claim_id}")
        except UpstreamError as exc:
            if exc.status == 404:
                raise AgentError("claim_not_found") from None
            raise
        self._check_member(raw)
        if raw.get("claim_id") != claim_id:
            raise PolicyDenied("claim_scope_mismatch")
        result = project_claim(raw)
        self.store.cache(self.scope, "claim/" + claim_id, result, captured, self.settings.claims_cache_ttl)
        return result

    def _validated_next(self, path):
        if not isinstance(path, str):
            raise PolicyDenied("invalid_pagination_path")
        parsed = urlsplit(path)
        if (parsed.scheme or parsed.netloc or parsed.fragment or parsed.path != self.member_path + "/claims"
                or "%" in path or "\\" in path):
            raise PolicyDenied("pagination_scope_mismatch")
        query = parse_qs(parsed.query, keep_blank_values=True)
        if set(query) != {"cursor", "page_size"}:
            raise PolicyDenied("invalid_pagination_path")
        if any(len(values) != 1 or not re.fullmatch(r"\d{1,8}", values[0]) for values in query.values()):
            raise PolicyDenied("invalid_pagination_path")
        if not 1 <= int(query["page_size"][0]) <= 5:
            raise PolicyDenied("invalid_pagination_path")
        return path

    def _tool_claims(self):
        cached = self._cached("claims/all")
        return cached if cached is not None else self._claims_pages()

    def _tool_claims_on_date(self, service_date):
        if not isinstance(service_date, str):
            raise PolicyDenied("invalid_service_date")
        try:
            parsed = date.fromisoformat(service_date)
        except ValueError:
            raise PolicyDenied("invalid_service_date") from None
        if parsed.isoformat() != service_date:
            raise PolicyDenied("invalid_service_date")
        cached = self._cached("claims/all")
        if cached is not None:
            return [item for item in cached if item["service_date"] == service_date]
        cached = self._cached("claims/date/" + service_date)
        return cached if cached is not None else self._claims_pages(service_date)

    def _cached(self, resource):
        if self.settings.claims_cache_ttl == 0:
            return None
        cached = self.store.cached(self.scope, resource)
        if cached is None:
            return None
        payload, age = cached
        # A stricter freshness setting also applies to entries from earlier processes.
        if age >= self.settings.claims_cache_ttl:
            return None
        self.cache_hits += 1
        self.max_data_age = max(self.max_data_age, age)
        self.store.event(self.scope, "cache.hit", count=1)
        return payload

    def _claims_pages(self, service_date=None):
        path = self.member_path + "/claims?page_size=5"
        seen_paths, claims = set(), {}
        captured, last_date = time.time(), None
        complete = False
        while path:
            if path in seen_paths:
                raise PolicyDenied("pagination_cycle")
            seen_paths.add(path)
            raw = self._http(path)
            if not isinstance(raw.get("data"), list):
                raise UpstreamError(502)
            for item in raw["data"]:
                self._check_member(item)
                claim = project_claim(item)
                if not isinstance(claim["claim_id"], str) or not re.fullmatch(r"C\d{6,20}", claim["claim_id"]):
                    raise UpstreamError(502)
                try:
                    current_date = date.fromisoformat(claim["service_date"]).isoformat()
                except (TypeError, ValueError):
                    raise UpstreamError(502) from None
                if last_date is not None and current_date > last_date:
                    raise UpstreamError(502)
                if claim["claim_id"] in claims:
                    raise PolicyDenied("duplicate_claim_page")
                last_date = current_date
                claims[claim["claim_id"]] = claim
            next_path = raw.get("next")
            path = self._validated_next(next_path) if next_path is not None else None
            if path is None:
                complete = True
            # Claims are ordered newest first. Read through the entire target day,
            # including page boundaries, before stopping at an older service date.
            if service_date and last_date is not None and last_date < service_date:
                break
        result = list(claims.values())
        if complete:
            self.store.cache(self.scope, "claims/all", result, captured, self.settings.claims_cache_ttl)
        if service_date:
            result = [item for item in result if item["service_date"] == service_date]
            self.store.cache(self.scope, "claims/date/" + service_date, result, captured, self.settings.claims_cache_ttl)
        return result

    def _tool_grievances(self):
        raw = self._http(self.member_path + "/grievances")
        if not isinstance(raw.get("data"), list):
            raise UpstreamError(502)
        items = []
        for item in raw.get("data", []):
            self._check_member(item)
            items.append({key: item.get(key) for key in ("grievance_id", "received_date", "status", "category")})
        return items

    def _tool_verify_last_four(self, last_four):
        if not isinstance(last_four, str) or not re.fullmatch(r"\d{4}", last_four):
            raise PolicyDenied("invalid_verification_input")
        raw = self._http(self.member_path)
        self._check_member(raw)
        stored = re.sub(r"\D", "", raw.get("ssn", ""))
        if len(stored) != 9:
            raise AgentError("verification_unavailable")
        return {"matches": hmac.compare_digest(stored[-4:], last_four)}

    def _tool_providers(self, specialty):
        if not isinstance(specialty, str) or specialty.casefold() not in SPECIALTIES:
            raise PolicyDenied("unsupported_specialty")
        if not self.settings.directory_key:
            raise AgentError("directory_not_configured")
        member = self._http(self.member_path)
        self._check_member(member)
        network = member.get("network_id", "")
        zip_code = member.get("address", {}).get("zip", "")
        if not re.fullmatch(r"[A-Z0-9-]{1,32}", network) or not re.fullmatch(r"\d{5}", zip_code):
            raise UpstreamError(502)
        specialty = SPECIALTIES[specialty.casefold()]
        # The vendor manifest is deliberately not consulted for security policy.
        query = urlencode({"query": specialty, "specialty": specialty, "zip": zip_code,
                           "network_id": network, "limit": 10})
        raw = self._http("/directory/v3/providers?" + query, directory=True)
        if not isinstance(raw.get("results"), list):
            raise UpstreamError(502)
        result = []
        for provider in raw.get("results", []):
            if not isinstance(provider, dict) or not isinstance(provider.get("address"), dict):
                raise UpstreamError(502)
            if (network not in provider.get("networks", [])
                    or provider.get("specialty", "").casefold() != specialty.casefold()
                    or provider.get("address", {}).get("zip", "")[:3] != zip_code[:3]):
                continue
            result.append({"name": safe_evidence(provider.get("name", "")),
                           "npi": provider.get("npi"), "accepting_new_patients": provider.get("accepting_new_patients")})
        return result

    def _tool_retrieve(self, year, label, effective_on):
        if (type(year) is not int or not 2000 <= year <= 2100 or not isinstance(label, str)
                or label not in RULE_LABELS or not isinstance(effective_on, date)):
            raise PolicyDenied("invalid_retrieval_arguments")
        benefits = self.call("benefits")
        return self.knowledge.lookup(self.scope.tenant, benefits["product_code"], year, label, effective_on)

    def _tool_draft_grievance(self, category, description, expedite_requested):
        if (not isinstance(category, str) or category not in CATEGORIES or not isinstance(description, str)
                or not description.strip() or len(description) > 2000 or type(expedite_requested) is not bool):
            raise PolicyDenied("invalid_grievance")
        self.call("benefits")
        body = {"category": category, "description": redact_sensitive(description),
                "expedite_requested": expedite_requested}
        if len(body["description"]) > 2000:
            raise PolicyDenied("invalid_grievance")
        return self.store.draft(self.scope, body)

    def _tool_file_grievance(self, operation_id):
        if not isinstance(operation_id, str) or not re.fullmatch(r"[a-f0-9]{32}", operation_id):
            raise PolicyDenied("invalid_operation_id")
        operation = self.store.operation(self.scope, operation_id)
        if operation["status"] == "COMPLETED":
            return json.loads(operation["result"])
        if operation["status"] not in {"APPROVED", "IN_FLIGHT"}:
            raise PolicyDenied("confirmation_required")
        digest = hashlib.sha256(operation["body"].encode("utf-8")).hexdigest()
        if not hmac.compare_digest(digest, operation["approved_hash"] or ""):
            raise PolicyDenied("approved_body_changed")
        if operation["status"] == "APPROVED" and time.time() - operation["approved"] > self.settings.approval_ttl:
            raise PolicyDenied("approval_expired")
        self.store.mark_in_flight(self.scope, operation_id)
        raw = self._http(self.member_path + "/grievances", body=operation["body"].encode("utf-8"), key=operation_id)
        self._check_member(raw)
        body = json.loads(operation["body"])
        if raw.get("category") != body["category"] or not re.fullmatch(r"G\d{2}-\d{6,20}", raw.get("grievance_id", "")):
            raise UpstreamError(502)
        result = {key: raw.get(key) for key in ("grievance_id", "status", "category", "received_date")}
        # Persist acknowledgement before any post-call budget check can fail.
        self.store.complete(self.scope, operation_id, result)
        return result
