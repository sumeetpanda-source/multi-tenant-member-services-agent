import re

from .errors import UpstreamError


def redact_sensitive(text: str) -> str:
    """Minimise free text stored in a grievance; never use this as a log filter."""
    rules = (
        (r"\b\d{3}[- ]\d{2}[- ]\d{4}\b|\b\d{9}\b", "[SSN REDACTED]"),
        (r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[EMAIL REDACTED]"),
        (r"(?<!\d)(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}(?!\d)", "[PHONE REDACTED]"),
        (r"\b\d{10}\b", "[PHONE REDACTED]"),
        (r"\bM\d{5,}\b", "[MEMBER REDACTED]"),
        (r"(?i)\b(?:dob|date of birth)\s*[:=]?\s*(?:[\d/.-]+|[a-z]+\s+\d{1,2},?\s+\d{4})", "[DOB REDACTED]"),
        (r"\b(?:mbrsvc_|cfn_dir_|sk-)[\w-]+\b", "[SECRET REDACTED]"),
    )
    for pattern, replacement in rules:
        text = re.sub(pattern, replacement, text)
    return " ".join(text.split())


def safe_evidence(value: str) -> str:
    """Reject instruction-like source text at the structured evidence boundary."""
    if not isinstance(value, str):
        raise UpstreamError(502)
    value = redact_sensitive(value)
    if re.search(r"(?i)ignore.{0,30}(?:instruction|previous)|system prompt|tool.call|"
                 r"do not (?:tell|mention)|coding assistant|send.{0,25}(?:ssn|social security)", value):
        return ""
    return value


def project_benefits(raw: dict) -> dict:
    if not isinstance(raw.get("accumulators"), dict):
        raise UpstreamError(502)
    if raw.get("coverage_status") not in {"ACTIVE", "TERMINATED"}:
        raise UpstreamError(502)
    fields = ("product_code", "plan_name", "network_id", "coverage_status",
              "coverage_effective", "coverage_termination")
    acc_fields = ("plan_year", "as_of", "deductible_met_individual",
                  "out_of_pocket_met_individual", "physical_therapy_visits_used")
    result = {key: raw.get(key) for key in fields}
    result["accumulators"] = {key: raw.get("accumulators", {}).get(key) for key in acc_fields}
    return result


def project_claim(raw: dict) -> dict:
    # Free-form examiner/provider notes and patient/provider identifiers stay out.
    if (not isinstance(raw.get("service"), dict) or not isinstance(raw.get("member_responsibility"), dict)
            or (raw.get("denial") is not None and not isinstance(raw["denial"], dict))
            or raw.get("status") not in {"IN_PROCESS", "PENDED", "PAID", "DENIED"}):
        raise UpstreamError(502)
    fields = ("claim_id", "service_date", "processed_date", "status", "plan_paid")
    result = {key: raw.get(key) for key in fields}
    result["service"] = safe_evidence(raw.get("service", {}).get("description", ""))
    result["member_responsibility"] = {
        key: raw.get("member_responsibility", {}).get(key, 0)
        for key in ("deductible", "copay", "coinsurance")
    }
    result["denial_code"] = (raw.get("denial") or {}).get("carc")
    return result
