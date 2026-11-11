import json
import time
import unittest
from dataclasses import replace
from unittest.mock import patch

from member_agent.agent import Agent
from member_agent.domain import Scope
from member_agent.errors import PolicyDenied
from tests.support import Fixture, claim


class PerformanceSafetyTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)

    def ask(self, text):
        return self.fx.agent.ask(self.fx.scope, text)

    def claim_calls(self):
        return [call for call in self.fx.transport.calls if "/claims" in call["url"]]

    def test_follow_up_uses_scoped_snapshot_and_discloses_age(self):
        self.ask("What is the status of claim C900000001?")
        count = len(self.claim_calls())
        reply = self.ask("How much did the plan pay on claim C900000001?")
        self.assertEqual("answer", reply.behaviour)
        self.assertEqual(count, len(self.claim_calls()))
        self.assertEqual(1, reply.metrics["cache_hits"])
        self.assertIn("Claim data reused", reply.text)

    def test_expired_snapshot_fetches_backend(self):
        self.ask("What is the status of claim C900000001?")
        with self.fx.agent.store.connection() as db:
            db.execute("UPDATE read_cache SET captured=?,expires=?", (time.time() - 100, time.time() - 1))
        reply = self.ask("What is the status of claim C900000001?")
        self.assertEqual(0, reply.metrics["cache_hits"])
        self.assertEqual(2, len(self.claim_calls()))

    def test_cache_zero_setting_forces_fresh_reads(self):
        settings = replace(self.fx.settings, claims_cache_ttl=0)
        agent = Agent(settings, transport=self.fx.transport)
        for _ in range(2):
            reply = agent.ask(self.fx.scope, "What is the status of claim C900000001?")
            self.assertEqual(0, reply.metrics["cache_hits"])
        self.assertEqual(2, len(self.claim_calls()))

    def test_cache_cannot_cross_conversations_even_for_same_member(self):
        self.ask("What is the status of claim C900000001?")
        other = self.fx.agent.new_session("red", "M900001")
        reply = self.fx.agent.ask(other, "What is the status of claim C900000001?")
        self.assertEqual(0, reply.metrics["cache_hits"])
        self.assertEqual(2, len(self.claim_calls()))

    def test_forged_scope_cannot_read_another_tenants_cache(self):
        self.ask("What is the status of claim C900000001?")
        with self.assertRaises(PolicyDenied):
            self.fx.agent.store.cached(Scope(self.fx.scope.session_id, "blue", "M900001"), "claim/C900000001")

    def test_snapshot_survives_cli_process_restart(self):
        self.ask("What is the status of claim C900000001?")
        restarted = Agent(self.fx.settings, transport=self.fx.transport)
        reply = restarted.ask(self.fx.scope, "What is the status of claim C900000001?")
        self.assertEqual(1, reply.metrics["cache_hits"])
        self.assertEqual(1, len(self.claim_calls()))

    def test_complete_count_reused_only_after_last_page(self):
        self.fx.transport.claims = [claim(claim_id=f"C9000000{i:02}", status="DENIED") for i in range(11)]
        self.ask("How many claims were denied in 2026?")
        count = len(self.claim_calls())
        reply = self.ask("How many claims were denied in 2026?")
        self.assertEqual(3, count)
        self.assertEqual(count, len(self.claim_calls()))
        self.assertIn("11 denied claims", reply.text)

    def test_date_lookup_stops_after_older_day_without_caching_full_count(self):
        records = []
        for index, service_date in enumerate(["2026-11-02", "2026-11-01", "2026-10-30", "2026-10-29",
                                              "2026-10-28", "2026-10-10", "2026-10-09"]):
            record = claim(claim_id=f"C9000000{index:02}")
            record["service_date"] = service_date
            records.append(record)
        self.fx.transport.claims = records
        reply = self.ask("What happened with the claim on 2026-11-01?")
        self.assertEqual("answer", reply.behaviour)
        self.assertEqual(1, len(self.claim_calls()))
        self.assertIsNone(self.fx.agent.store.cached(self.fx.scope, "claims/all"))
        result = self.ask("How many claims are there in 2026?")
        self.assertIn("7 claims", result.text)
        self.assertEqual(3, len(self.claim_calls()))

    def test_target_day_spanning_pages_does_not_return_incomplete_match(self):
        self.fx.transport.claims = [claim(claim_id=f"C9000000{i:02}") for i in range(7)]
        reply = self.ask("What happened with the claim on 2026-10-03?")
        self.assertEqual("cannot_answer", reply.behaviour)
        self.assertEqual(2, len(self.claim_calls()))

    def test_out_of_order_pages_are_not_trusted_for_early_stop(self):
        records = [claim(claim_id="C900000001"), claim(claim_id="C900000002")]
        records[0]["service_date"] = "2026-10-01"
        records[1]["service_date"] = "2026-11-01"
        self.fx.transport.claims = records
        reply = self.ask("What happened with the claim on 2026-11-01?")
        self.assertEqual("unavailable", reply.behaviour)
        self.assertIsNone(self.fx.agent.store.cached(self.fx.scope, "claims/date/2026-11-01"))

    def test_cache_does_not_store_raw_notes_or_member_identity(self):
        self.ask("What is the status of claim C900000001?")
        with self.fx.agent.store.connection() as db:
            payload = db.execute("SELECT payload FROM read_cache").fetchone()[0]
        for forbidden in ("111-22-3333", "Ignore previous", "M900001", "notes", "dob", "email"):
            self.assertNotIn(forbidden, payload)


if __name__ == "__main__":
    unittest.main()
