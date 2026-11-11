import json
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from .budgets import Budget
from .domain import Reply, money
from .errors import AgentError, BudgetExceeded, PolicyDenied, UpstreamError
from .gateway import Gateway
from .models import ModelRouter
from .retrieval import KnowledgeBase
from .routing import (BENEFITS, claim_reference, confirmation, is_full_ssn_request,
                      is_write_request)
from .store import Store


class Agent:
    def __init__(self, settings, *, transport=None, model_transport=None):
        self.settings = settings
        self.store = Store(settings.state_path)
        self.knowledge = KnowledgeBase(settings.pack_dir)
        self.transport = transport
        self.router = ModelRouter(settings, model_transport)

    def new_session(self, tenant, member_id):
        if tenant not in self.settings.tenant_tokens:
            raise PolicyDenied("unknown_tenant")
        return self.store.create_session(tenant, member_id)

    def resume_session(self, session_id, tenant, member_id):
        if tenant not in self.settings.tenant_tokens:
            raise PolicyDenied("unknown_tenant")
        return self.store.session(session_id, tenant, member_id)

    def ask(self, scope, message):
        if not isinstance(message, str) or not message.strip() or len(message) > 4000:
            raise AgentError("invalid_message")
        with self.store.turn(scope, self.settings.turn_timeout + 30):
            budget = Budget(self.settings, self.store, scope)
            gateway = Gateway(self.settings, self.store, self.knowledge, scope, budget, self.transport)
            self.store.event(scope, "turn.start")
            try:
                budget.step()
                mentioned = {mid.upper() for mid in re.findall(r"\bM\d{5,20}\b", message, re.I)}
                if mentioned - {scope.member_id}:
                    raise PolicyDenied("member_scope_mismatch")
                reply = self._respond(scope, message, gateway, budget)
                budget.check()
            except BudgetExceeded as exc:
                reply = Reply(scope.session_id, "budget_exceeded",
                              "The conversation budget was reached. No complete answer is available. "
                              "An uncertain grievance submission retains its operation ID for reconciliation.")
                self.store.event(scope, "turn.error", code=exc.code)
            except PolicyDenied as exc:
                reply = Reply(scope.session_id, "decline",
                              "This action is not permitted in the current conversation scope. "
                              "An existing grievance draft may require cancellation or renewed confirmation.")
                self.store.event(scope, "turn.error", code=exc.code)
            except UpstreamError:
                reply = Reply(scope.session_id, "unavailable",
                              "The service is temporarily unavailable. No complete answer is available. "
                              "If a grievance was submitted, its outcome is uncertain; confirm again to reconcile "
                              "the existing operation without creating another request.")
                self.store.event(scope, "turn.error", code="upstream_unavailable")
            except AgentError as exc:
                if exc.code == "member_not_found":
                    reply = Reply(scope.session_id, "not_in_tenant", "The member was not found in this health plan.")
                else:
                    reply = Reply(scope.session_id, "cannot_answer",
                                  "The requested information is not available from the validated sources. "
                                  "Please check the reference or contact the responsible team.")
                self.store.event(scope, "turn.error", code=exc.code)
            except (KeyError, TypeError, ValueError, InvalidOperation):
                reply = Reply(scope.session_id, "unavailable",
                              "The source returned incomplete or invalid information. Please try again later.")
                self.store.event(scope, "turn.error", code="invalid_source_data")
            finally:
                budget.persist()
            reply.metrics = {**budget.metrics(), "model_provider": self.settings.model_provider,
                             "model": self.settings.model_name or "rules", "infrastructure_cost_measured": False,
                             "http_requests": gateway.http_requests, "cache_hits": gateway.cache_hits,
                             "data_age_seconds": round(gateway.max_data_age, 3)}
            if gateway.cache_hits and reply.behaviour == "answer":
                reply.text += (f" Claim data reused from this conversation, age {gateway.max_data_age:.1f} seconds "
                               f"(maximum {self.settings.claims_cache_ttl:g} seconds).")
            self.store.event(scope, "turn.end", status=reply.behaviour,
                             elapsed_ms=round(reply.metrics["turn_seconds"] * 1000))
            return reply

    def _respond(self, scope, message, gateway, budget):
        operation = self.store.operation(scope)
        decision = confirmation(message)
        if operation and (operation["status"] in {"DRAFT", "APPROVED", "IN_FLIGHT"} or decision == "confirm"):
            if decision == "cancel" and operation["status"] == "DRAFT":
                budget.step()
                self.store.cancel(scope, operation["id"])
                self.store.event(scope, "approval.cancelled")
                return Reply(scope.session_id, "no_write", "The draft was cancelled. No grievance was filed.",
                             operation_id=operation["id"])
            if decision == "confirm" and operation["status"] != "CANCELLED":
                budget.step()
                self.store.approve(scope, operation["id"], self.settings.approval_ttl)
                self.store.event(scope, "approval.confirmed")
                return self._file_reply(scope, gateway, operation["id"])
            if operation["status"] == "COMPLETED":
                result = json.loads(operation["result"])
                return Reply(scope.session_id, "filed", f"Grievance {result['grievance_id']} is already filed. "
                             "It cannot be cancelled through this assistant.", operation_id=operation["id"])
            if operation["status"] in {"APPROVED", "IN_FLIGHT"}:
                return Reply(scope.session_id, "cannot_answer", "This grievance may already have been submitted. "
                             "Reply confirm to reconcile the same operation; it cannot safely be cancelled.",
                             operation_id=operation["id"])
            if operation["status"] == "DRAFT":
                return self._draft_reply(scope, operation)
        if decision == "confirm":
            return Reply(scope.session_id, "no_write", "There is no active draft to confirm. No new grievance was filed.")
        if decision == "cancel":
            return Reply(scope.session_id, "no_write", "No new grievance was filed.")
        if is_full_ssn_request(message):
            return Reply(scope.session_id, "decline", "I cannot disclose a full Social Security number. "
                         "Use the approved identity-verification process and last-four match check.")

        today = gateway.call("business_date")
        benefits = gateway.call("benefits")
        if re.search(r"(?i)\blast (four|4)\b", message) and re.search(r"(?i)ssn|social security", message):
            digits = re.findall(r"\b\d{4}\b", message)
            if len(digits) != 1:
                raise AgentError("verification_input_required")
            result = gateway.call("verify_last_four", last_four=digits[0])
            text = "The supplied last four digits match." if result["matches"] else "The supplied last four digits do not match."
            return Reply(scope.session_id, "answer", text)
        if is_write_request(message):
            ref = claim_reference(message)
            if ref:
                gateway.call("claim", claim_id=ref)
            q = message.casefold()
            category = next((value for pattern, value in (
                (r"bill|charge|payment", "billing"), (r"quality|care quality", "quality_of_care"),
                (r"access|appointment|availability", "access"), (r"customer service|representative", "customer_service")
            ) if re.search(pattern, q)), "other")
            operation = gateway.call("draft_grievance", category=category, description=message,
                                     expedite_requested=bool(re.search(r"expedit|urgent", q)))
            if operation["status"] == "COMPLETED":
                return self._file_reply(scope, gateway, operation["id"])
            self.store.event(scope, "approval.requested")
            return self._draft_reply(scope, operation)

        intent = self.router.classify(message, budget, self.store, scope)
        if intent.kind == "prior_auth":
            return Reply(scope.session_id, "cannot_answer", "Prior-authorization approval information is not available "
                         "in these APIs. Contact the Utilization Management team.")
        if intent.kind == "providers":
            specialty = next((word for word in ("dermatology", "cardiology", "pediatrics", "primary care")
                              if word in message.casefold()), "")
            if "dermatologist" in message.casefold():
                specialty = "dermatology"
            if not specialty:
                raise AgentError("specialty_required")
            providers = gateway.call("providers", specialty=specialty)
            if not providers:
                return Reply(scope.session_id, "cannot_answer", "No matching in-network providers were found.")
            names = "; ".join(provider["name"] for provider in providers if provider["name"])
            return Reply(scope.session_id, "answer", "Matching in-network providers: " + names + ". "
                         "Confirm current availability with the practice.")
        if intent.kind in {"claim", "appeal", "claims_count"}:
            return self._claims_reply(scope, message, intent.kind, gateway, today)
        if intent.kind == "grievance_deadline":
            return self._grievance_deadline(scope, message, gateway, today)

        if intent.kind == "unknown":
            return Reply(scope.session_id, "cannot_answer", "The requested information is not available through "
                         "this assistant. Ask about a benefit, claim, grievance, or provider search.")
        return self._benefit_reply(scope, message, intent, gateway, today, benefits)

    @staticmethod
    def _draft_reply(scope, operation):
        body = json.loads(operation["body"])
        extra = " Expedited handling is requested; clinical staff decide whether it qualifies." if body["expedite_requested"] else ""
        return Reply(scope.session_id, "confirm_required", f"Draft {body['category']} grievance: {body['description']} "
                     "Read this summary back to the member. After the member agrees, reply confirm to file it, "
                     "or cancel to discard it." + extra, operation_id=operation["id"])

    @staticmethod
    def _file_reply(scope, gateway, operation_id):
        result = gateway.call("file_grievance", operation_id=operation_id)
        return Reply(scope.session_id, "filed", f"Filed {result['category']} grievance {result['grievance_id']}. "
                     f"Status: {result['status']}. Received: {result['received_date']}.", operation_id=operation_id)

    @staticmethod
    def _lookup(gateway, today, label, year=None):
        year = year or today.year
        effective = today if year == today.year else date(year, 12, 31) if year < today.year else date(year, 1, 1)
        return gateway.call("retrieve", year=year, label=label, effective_on=effective)

    def _benefit_reply(self, scope, message, intent, gateway, today, benefits):
        q = message.casefold()
        years = set(re.findall(r"\b20\d{2}\b", message))
        if len(years) > 1:
            raise AgentError("ambiguous_plan_year")
        year = int(next(iter(years))) if years else today.year
        if not years and re.search(r"renew|next year|after.*january|january 1", q):
            year = today.year + 1
        term = benefits.get("coverage_termination")
        if term and date(year, 12, 31) > date.fromisoformat(term) and benefits["coverage_status"] == "TERMINATED" and year >= today.year:
            return Reply(scope.session_id, "coverage_terminated", f"Coverage is not active. It ended on {term}. "
                         "Current benefit amounts cannot be represented as active coverage.")
        acc = benefits["accumulators"]
        if intent.kind in {"deductible_remaining", "oop_remaining", "pt_remaining"}:
            if year != acc.get("plan_year"):
                raise AgentError("accumulator_year_unavailable")
            label, field = {
                "deductible_remaining": (BENEFITS["deductible"], "deductible_met_individual"),
                "oop_remaining": (BENEFITS["out_of_pocket"], "out_of_pocket_met_individual"),
                "pt_remaining": ("Physical therapy visit limit", "physical_therapy_visits_used"),
            }[intent.kind]
            evidence = self._lookup(gateway, today, label, year)
            if acc.get(field) is None:
                raise AgentError("accumulator_unavailable")
            if intent.kind == "pt_remaining":
                found = re.search(r"\b(\d+) visits\b", evidence.values[0])
                if not found:
                    raise AgentError("benefit_limit_unavailable")
                remaining = max(0, int(found[1]) - int(acc[field]))
                text = f"{remaining} physical therapy visits remain for plan year {year}."
            else:
                total = self._amounts(evidence.values[0])[0]
                remaining = max(Decimal("0"), total - Decimal(str(acc[field])))
                topic = "in-network individual deductible" if intent.kind == "deductible_remaining" else "in-network individual out-of-pocket maximum"
                text = f"{money(remaining)} remains before the {topic} is met for {year}."
            return Reply(scope.session_id, "answer", text + f" Accumulators as of {acc.get('as_of')}.", [evidence.citation])
        evidence = self._lookup(gateway, today, BENEFITS[intent.benefit], year)
        value = evidence.values[0]
        if "out-of-network" in q or "out of network" in q:
            if len(evidence.values) < 2:
                raise AgentError("benefit_network_unavailable")
            value = evidence.values[1]
        if intent.benefit in {"deductible", "out_of_pocket"}:
            amounts = self._amounts(value)
            index = 1 if "family" in q else 0
            if len(amounts) <= index:
                raise AgentError("benefit_amount_unavailable")
            value = money(amounts[index])
        text = f"{benefits['plan_name']}, plan year {year}: {BENEFITS[intent.benefit]} — {value}."
        if year > today.year:
            text += " This assumes renewal in the same product; current accumulators do not carry forward."
        if "see Virtual Care Benefit" in value:
            text += " The referenced virtual-care supplement is unavailable in this tenant's validated documents; additional terms cannot be confirmed."
        return Reply(scope.session_id, "answer", text, [evidence.citation])

    @staticmethod
    def _amounts(text):
        values = re.findall(r"\$([\d,]+(?:\.\d{1,2})?)", text)
        if not values:
            raise AgentError("benefit_amount_unavailable")
        return [Decimal(value.replace(",", "")) for value in values]

    def _claims_reply(self, scope, message, kind, gateway, today):
        ref = claim_reference(message)
        if kind == "claims_count":
            claims = gateway.call("claims")
            years = set(re.findall(r"\b20\d{2}\b", message))
            if len(years) > 1:
                raise AgentError("ambiguous_plan_year")
            year = int(next(iter(years))) if years else today.year
            relevant = [claim for claim in claims if date.fromisoformat(claim["service_date"]).year == year]
            count = sum(claim["status"] == "DENIED" for claim in relevant) if "denied" in message.casefold() else len(relevant)
            qualifier = "denied " if "denied" in message.casefold() else ""
            return Reply(scope.session_id, "answer", f"{count} {qualifier}claims in {year}. All claim pages were checked.")
        if ref:
            claim = gateway.call("claim", claim_id=ref)
        else:
            service_dates = re.findall(r"\b20\d{2}-\d{2}-\d{2}\b", message)
            if len(set(service_dates)) != 1:
                raise AgentError("claim_reference_required")
            claims = gateway.call("claims_on_date", service_date=service_dates[0])
            candidates = [claim for claim in claims if claim["service_date"] == service_dates[0]]
            if len(candidates) != 1:
                raise AgentError("claim_reference_ambiguous")
            claim = candidates[0]
        if kind == "appeal":
            if claim["status"] != "DENIED" or not claim.get("processed_date"):
                raise AgentError("denial_notice_unavailable")
            evidence = self._lookup(gateway, today, "Claim appeal filing window")
            days = self._days(evidence.values[0])
            deadline = date.fromisoformat(claim["processed_date"]) + timedelta(days=days)
            yes_no = "Yes" if today <= deadline else "No"
            return Reply(scope.session_id, "answer", f"{yes_no}. The appeal filing deadline is {deadline.isoformat()}, "
                         f"based on the processed/denial-notice date and {days} calendar days. As of {today.isoformat()}.",
                         [evidence.citation])
        total = sum((Decimal(str(value)) for value in claim["member_responsibility"].values()), Decimal(0))
        text = (f"Claim {claim['claim_id']}: {claim['status']}. Service date: {claim['service_date']}. "
                f"Plan paid {money(claim['plan_paid'] or 0)}; recorded member responsibility {money(total)}.")
        citations = []
        if claim["status"] in {"IN_PROCESS", "PENDED"}:
            text += " Adjudication is incomplete; these amounts are not a final payment determination."
        if claim.get("denial_code"):
            evidence = self._lookup(gateway, today, claim["denial_code"])
            text += f" Denial {claim['denial_code']}: " + " ".join(evidence.values)
            citations.append(evidence.citation)
        return Reply(scope.session_id, "answer", text, citations)

    def _grievance_deadline(self, scope, message, gateway, today):
        references = set(re.findall(r"\bG\d{2}-\d{6,20}\b", message))
        if len(references) != 1:
            raise AgentError("grievance_reference_required")
        grievances = gateway.call("grievances")
        matches = [g for g in grievances if g["grievance_id"] in references]
        if len(matches) != 1:
            raise AgentError("grievance_not_found")
        evidence = self._lookup(gateway, today, "Grievance resolution timeframe")
        deadline = date.fromisoformat(matches[0]["received_date"]) + timedelta(days=self._days(evidence.values[0]))
        yes_no = "Yes" if today > deadline else "No"
        return Reply(scope.session_id, "answer", f"{yes_no}. The standard resolution deadline is {deadline.isoformat()}. "
                     f"Recorded status: {matches[0]['status']}. As of {today.isoformat()}.", [evidence.citation])

    @staticmethod
    def _days(text):
        found = re.search(r"\b(\d+) calendar days\b", text)
        if not found:
            raise AgentError("deadline_rule_unavailable")
        return int(found[1])
