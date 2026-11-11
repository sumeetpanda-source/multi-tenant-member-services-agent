import unittest
from decimal import Decimal

from evaluation.scoring import contains_fact, regression_gate
from member_agent.budgets import Budget
from member_agent.domain import money
from member_agent.errors import BudgetExceeded

from tests.support import Fixture, claim, write_document


class BehaviourTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)

    def ask(self, message):
        return self.fx.agent.ask(self.fx.scope, message)

    def test_current_benefit_has_source(self):
        reply = self.ask("What does an in-network specialist visit cost?")
        self.assertEqual("answer", reply.behaviour)
        self.assertIn("$47 copay", reply.text)
        self.assertEqual(2026, reply.citations[0].plan_year)

    def test_renewal_selects_requested_year(self):
        write_document(self.fx.root, name="next.md", year=2027, specialist=63)
        reply = self.ask("The plan renews January 1. What is the specialist copay after that?")
        self.assertIn("$63 copay", reply.text)
        self.assertEqual(2027, reply.citations[0].plan_year)

    def test_future_document_not_applied_to_current_year(self):
        write_document(self.fx.root, name="next.md", year=2027, specialist=63)
        self.assertIn("$47 copay", self.ask("What is the specialist copay?").text)

    def test_conflicting_same_version_fails_closed(self):
        write_document(self.fx.root, name="conflict.md", specialist=63)
        self.assertEqual("cannot_answer", self.ask("What is the specialist copay?").behaviour)

    def test_decimal_remaining_deductible(self):
        self.assertIn("$878.91", self.ask("How much is left on the deductible?").text)

    def test_family_limit_and_remaining_oop(self):
        self.assertIn("$10,400", self.ask("What is the family out-of-pocket maximum?").text)
        self.assertIn("$4,400", self.ask("How far from the individual out-of-pocket maximum?").text)

    def test_physical_therapy_remaining(self):
        self.assertIn("5 physical therapy visits remain", self.ask("How many physical therapy visits are left?").text)

    def test_no_future_accumulator_guess(self):
        self.assertEqual("cannot_answer", self.ask("How much deductible remains in 2027?").behaviour)

    def test_out_of_network_column_selected(self):
        reply = self.ask("What does an out-of-network specialist visit cost?")
        self.assertIn("45% coinsurance", reply.text)
        self.assertNotIn("$47", reply.text)

    def test_terminated_coverage_not_answered_as_active(self):
        self.fx.transport.coverage_termination = "2026-09-30"
        reply = self.ask("What is the urgent care copay?")
        self.assertEqual("coverage_terminated", reply.behaviour)
        self.assertIn("2026-09-30", reply.text)
        self.assertNotIn("$57", reply.text)

    def test_prior_auth_is_explicitly_unavailable(self):
        reply = self.ask("Has the prior authorization for the MRI been approved?")
        self.assertEqual("cannot_answer", reply.behaviour)
        self.assertIn("Utilization Management", reply.text)

    def test_claim_count_follows_all_pages_and_retries_throttle(self):
        self.fx.transport.claims = [claim(claim_id=f"C9000000{index:02}", status="DENIED" if index % 2 else "PAID")
                                    for index in range(13)]
        self.fx.transport.throttle_next = True
        reply = self.ask("How many 2026 claims were denied?")
        self.assertEqual("answer", reply.behaviour)
        self.assertIn("6 denied claims", reply.text)
        calls = [call for call in self.fx.transport.calls if "/claims?" in call["url"]]
        self.assertEqual(4, len(calls))
        self.assertEqual(1, sum(event["event"] == "http.retry" for event in self.fx.agent.store.events(self.fx.scope)))

    def test_direct_claim_lookup_avoids_pagination(self):
        reply = self.ask("What is the status of claim C900000001?")
        self.assertIn("PAID", reply.text)
        self.assertFalse(any("/claims?" in call["url"] for call in self.fx.transport.calls))

    def test_appeal_date_uses_processed_date(self):
        item = claim(status="DENIED")
        self.fx.transport.claims = [item]
        reply = self.ask("Can the member appeal claim C900000001?")
        self.assertIn("Yes.", reply.text)
        self.assertIn("2027-01-08", reply.text)

    def test_multiple_date_matches_require_reference(self):
        self.fx.transport.claims = [claim(), claim(claim_id="C900000002")]
        reply = self.ask("What happened with the claim on 2026-10-03?")
        self.assertEqual("cannot_answer", reply.behaviour)

    def test_grievance_deadline_is_tenant_rule(self):
        self.fx.transport.records = [{"member_id": "M900001", "grievance_id": "G26-800000",
                                      "received_date": "2026-10-01", "category": "billing", "status": "OPEN"}]
        reply = self.ask("Is grievance G26-800000 past the resolution deadline?")
        self.assertIn("Yes.", reply.text)
        self.assertIn("2026-10-21", reply.text)

    def test_money_formats_without_binary_rounding(self):
        self.assertEqual("$1,234.57", money(Decimal("1234.567")))
        self.assertEqual("$40", money(40))


class BudgetTests(unittest.TestCase):
    def test_exhausted_steps_stop_without_partial_answer(self):
        fx = Fixture(max_steps=4)
        self.addCleanup(fx.close)
        reply = fx.agent.ask(fx.scope, "What is the specialist copay?")
        self.assertEqual("budget_exceeded", reply.behaviour)
        self.assertNotIn("$47", reply.text)

    def test_budget_persists_across_turns(self):
        fx = Fixture(max_steps=14)
        self.addCleanup(fx.close)
        one = fx.agent.ask(fx.scope, "What is the specialist copay?")
        two = fx.agent.ask(fx.scope, "What is the specialist copay?")
        self.assertEqual("answer", one.behaviour)
        self.assertEqual("budget_exceeded", two.behaviour)
        self.assertGreater(two.metrics["steps"], one.metrics["steps"])

    def test_retry_cannot_exceed_remaining_time(self):
        fx = Fixture()
        self.addCleanup(fx.close)
        budget = Budget(fx.settings, fx.agent.store, fx.scope)
        with self.assertRaises(BudgetExceeded):
            budget.pause(100)

    def test_latency_budget_excludes_human_wait(self):
        fx = Fixture()
        self.addCleanup(fx.close)
        first = fx.agent.ask(fx.scope, "What is the specialist copay?")
        with fx.agent.store.connection() as db:
            db.execute("UPDATE sessions SET created=0 WHERE id=?", (fx.scope.session_id,))
        second = fx.agent.ask(fx.scope, "What is the specialist copay?")
        self.assertEqual("answer", second.behaviour)
        self.assertLess(second.metrics["active_seconds"], 5)

    def test_latency_deadline_stops_steps(self):
        fx = Fixture()
        self.addCleanup(fx.close)
        budget = Budget(fx.settings, fx.agent.store, fx.scope)
        budget.started -= 100
        with self.assertRaises(BudgetExceeded):
            budget.step()


class ScoringTests(unittest.TestCase):
    def test_facts_match_sentence_punctuation(self):
        self.assertTrue(contains_fact("Member cost is $40 copay.", "$40 copay"))
        self.assertTrue(contains_fact("No. Deadline 2026-11-10.", "2026-11-10"))

    def test_money_and_counts_cannot_match_wrong_numbers(self):
        self.assertFalse(contains_fact("$400 copay", "$40"))
        self.assertFalse(contains_fact("$40.50", "$40"))
        self.assertFalse(contains_fact("13 claims in 2026", "3"))
        self.assertTrue(contains_fact("$1,200.00 copay", "$1200 copay"))

    def test_identity_polarity_is_checked(self):
        self.assertFalse(contains_fact("The supplied digits do not match.", "match"))
        self.assertTrue(contains_fact("The supplied digits match.", "match"))

    def test_safety_failure_fails_gate_even_when_quality_threshold_passes(self):
        good = {"passed": True, "invariants": {"no_unexpected_write": True},
                "behaviour_passed": True, "expected_behaviour": "answer"}
        bad = {**good, "invariants": {"no_unexpected_write": False}}
        self.assertFalse(regression_gate([good] * 39 + [bad])["passed"])

    def test_quality_regression_fails_gate(self):
        good = {"passed": True, "invariants": {"safe": True}, "behaviour_passed": True,
                "expected_behaviour": "answer"}
        self.assertFalse(regression_gate([good] * 18 + [{**good, "passed": False}] * 2)["passed"])
