import json
import re
from dataclasses import dataclass

from .errors import AgentError


BENEFITS = {
    "specialist": "Specialist visit",
    "primary_care": "Primary care visit",
    "urgent_care": "Urgent care",
    "emergency": "Emergency room",
    "telehealth": "Telehealth (video) visit",
    "behavioural": "Behavioral health outpatient visit (in person or virtual)",
    "generic": "Generic drugs (Tier 1, 30-day retail)",
    "physical_therapy": "Physical therapy",
    "deductible": "Deductible (individual / family)",
    "out_of_pocket": "Out-of-pocket maximum (individual / family)",
    "preventive": "Preventive care",
    "imaging": "Advanced imaging (MRI, CT, PET)",
    "inpatient": "Inpatient hospital stay",
    "surgery": "Outpatient surgery",
}
INTENTS = ("benefit", "deductible_remaining", "oop_remaining", "pt_remaining", "claim",
           "claims_count", "appeal", "grievance_deadline", "providers", "prior_auth", "unknown")


@dataclass(frozen=True)
class Intent:
    kind: str
    benefit: str = ""


def parse_model_intent(text):
    try:
        value = json.loads(text)
        if not isinstance(value, dict) or set(value) != {"intent", "benefit"}:
            raise ValueError
        if value["intent"] not in INTENTS or value["benefit"] not in {"", *BENEFITS}:
            raise ValueError
        if value["intent"] == "benefit" and not value["benefit"]:
            raise ValueError
        if value["intent"] != "benefit" and value["benefit"]:
            raise ValueError
        return Intent(value["intent"], value["benefit"])
    except (ValueError, KeyError, TypeError):
        raise AgentError("invalid_model_action") from None


def confirmation(text):
    normal = " ".join(text.casefold().split()).strip()
    if re.search(r"\b(cancel|stop|wait|no|not|hold off|don't|do not|change|edit|instead)\b", normal):
        return "cancel" if re.search(r"\b(cancel|stop|wait|hold off|don't|do not)\b", normal) else "ambiguous"
    approved = (
        r"(?:yes[, ]*|confirmed?[, ]*|go ahead and )?file it[.!]?",
        r"(?:yes|confirm|confirmed)[.!]?",
        r"yes[, ]+i read it back to the member and the member agrees[.!]?\s*go ahead and file it[.!]?",
        r"confirmed\.\s*the member agreed to the summary\.\s*file it[.!]?",
    )
    return "confirm" if any(re.fullmatch(pattern, normal) for pattern in approved) else "ambiguous"


def is_write_request(text):
    return bool(re.search(r"(?i)\b(file|submit|create|open|lodge|raise)\b", text)
                and re.search(r"(?i)\b(grievance|complaint)\b", text))


def is_full_ssn_request(text):
    has_ssn = re.search(r"(?i)\b(ssn|social security)\b", text)
    last_four = re.search(r"(?i)\blast (four|4)\b", text)
    disclosure = re.search(r"(?i)\b(read|show|reveal|disclose|print|full|entire|complete)\b.{0,40}"
                           r"\b(ssn|social security)\b", text)
    return bool(has_ssn and not last_four and (not is_write_request(text) or disclosure))


def claim_reference(text):
    matches = set(re.findall(r"\bC\d{6,20}\b", text, re.I))
    if len(matches) > 1:
        raise AgentError("ambiguous_claim_reference")
    return next(iter(matches)).upper() if matches else None


def rule_intent(text):
    q = text.casefold()
    if "grievance" in q and any(word in q for word in ("deadline", "resolution", "past", "overdue")):
        return Intent("grievance_deadline")
    if "appeal" in q:
        return Intent("appeal")
    if ("prior auth" in q or "preauthorization" in q) and any(word in q for word in ("approved", "status", "decision")):
        return Intent("prior_auth")
    if any(word in q for word in ("dermatolog", "find a provider", "find an in-network", "provider near")):
        return Intent("providers")
    if "claim" in q and re.search(r"how many|count|number of", q):
        return Intent("claims_count")
    if claim_reference(text) or ("claim" in q and any(word in q for word in ("status", "denied", "owe", "happened", "recent"))):
        return Intent("claim")
    if any(word in q for word in ("owe", "owed", "happened", "plan pay", "plan paid")) and re.search(r"\d{4}-\d{2}-\d{2}", q):
        return Intent("claim")
    if "physical therapy" in q and re.search(r"how many|left|still use|remaining|visits.*used", q):
        return Intent("pt_remaining")
    if "out-of-pocket" in q or "out of pocket" in q:
        if re.search(r"how far|left|remaining|how much more|still.*pay", q):
            return Intent("oop_remaining")
        return Intent("benefit", "out_of_pocket")
    aliases = (
        (r"behavioral|behavioural|virtual therapy|mental health", "behavioural"),
        (r"specialist", "specialist"), (r"primary care|pcp visit", "primary_care"),
        (r"urgent care", "urgent_care"), (r"emergency|\ber\b", "emergency"),
        (r"telehealth|video visit|virtual doctor", "telehealth"),
        (r"generic|tier 1", "generic"), (r"physical therapy", "physical_therapy"),
        (r"preventive|preventative", "preventive"), (r"mri|imaging|\bct\b", "imaging"),
        (r"inpatient|hospital stay", "inpatient"), (r"surgery", "surgery"),
    )
    for pattern, benefit in aliases:
        if re.search(pattern, q):
            return Intent("benefit", benefit)
    if "deductible" in q:
        if re.search(r"left|remaining|still|more|before|meet|met", q):
            return Intent("deductible_remaining")
        return Intent("benefit", "deductible")
    return Intent("unknown")


# Only this fixed vocabulary crosses the model boundary. Identifiers, values,
# free-form notes, names, locations, and the conversation transcript stay local.
MODEL_WORDS = frozenset("""
    what how many much far still remaining left before after next current year renews
    january family individual in network out of pocket maximum deductible met meet
    specialist primary care urgent emergency room video telehealth virtual behavioral
    behaviour health mental therapy physical visits visit generic drug retail pharmacy
    prescription preventive imaging mri ct hospital inpatient surgery cost pay paid plan
    owes owe status claim claims denied denial appeal window deadline grievance
    resolution received processed prior authorization approved dermatologist dermatology
    provider providers find near count total happened stand enroll enrolled copay
    coinsurance thirty day supply free sessions available limit remaining
""".split())


def model_features(text):
    words = [word for word in re.findall(r"[a-z]+", text.casefold()) if word in MODEL_WORDS]
    return {"words": words[:160], "has_claim_reference": bool(claim_reference(text)),
            "has_service_date": bool(re.search(r"\b20\d{2}-\d{2}-\d{2}\b", text))}
