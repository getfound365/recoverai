"""The atomicity tests.

The claim and a mode flip both write the same client_autonomy_settings row, and that row
carries mode_version. Both transactions therefore serialize, and a claim records the
version it read, which makes the guarantee checkable:

  * a claim that read version V committed strictly before the flip that produced V+1,
    so the send was authorised under the mode that was actually in force at claim time
  * a claim that starts after the flip has committed reads the new mode and is refused

So the invariant is: no claimed contact may carry mode_version_at_claim >= F, where F is
the version at which the client's mode became hitl. This test hammers the two operations
together with a barrier so they start in the same instant, over and over.
"""
from __future__ import annotations

import datetime
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from engine import clock
from engine.orchestrator import BLOCKED_DNC, BLOCKED_MODE_HITL, CLAIMED
from tests.base import EngineTestCase

#: How many times each direction of the STOP-vs-send race is asserted, per run. Both
#: directions are forced (alternating), so neither can be the one that never executes.
DIRECTIONS_PER_RACE = 4
#: The repo root the mutation check copies into a scratch tree.
ROOT = Path(__file__).resolve().parent.parent


class TestModeFlipRaceWithClaim(EngineTestCase):
    ITERATIONS = 12

    def _reset(self) -> None:
        self.engine.store.close()
        for suffix in ("", "-wal", "-shm"):
            path = self.db_path.parent / (self.db_path.name + suffix)
            if path.exists():
                path.unlink()
        self.engine = self.new_engine()

    def _prepare_race(self):
        """A client in full_auto with a valid sign-off, and an in-window debtor."""
        self.add_debtor("D-1", phone="(212) 555-0143", aging_state="60", balance=1000.0)
        self.sign_off()
        self.engine.switch_mode("CLI-TEST", "full_auto", "signer", "signed")
        self.assertEqual(self.engine.settings("CLI-TEST")["mode_version"], 1)
        return self.new_engine(), self.new_engine()

    def state(self, engine) -> dict:
        return {
            "claims": engine.store.query("SELECT * FROM outbound_contact_logs WHERE claim_token IS NOT NULL"),
            "hitl_versions": [r["mode_version"] for r in engine.timeline("CLI-TEST") if r["new_mode"] == "hitl"],
            "attempts": engine.contacts(),
        }

    def test_send_started_first_is_authorized_even_if_the_flip_lands_a_moment_later(self):
        """Order one: the claim commits first, the flip follows. The send stands."""
        sender, flipper = self._prepare_race()
        try:
            outcome = sender.send("D-1", "voice", who="orchestrator", reason="claim first, no dispatch",
                                  dispatch=False)
            self.assertEqual(outcome["decision"], CLAIMED)
            flip = flipper.switch_mode("CLI-TEST", "hitl", "owner@client", "flip lands after the claim")
            self.assertTrue(flip["changed"])
            state = self.state(sender)
            self.assertEqual(len(state["claims"]), 1)
            self.assertEqual(state["claims"][0]["mode_version_at_claim"], 1)
            self.assertEqual(state["hitl_versions"], [2])
            self.assertLess(state["claims"][0]["mode_version_at_claim"], state["hitl_versions"][0])
            # and the flip stops anything after it
            self.assertEqual(sender.send("D-1", "voice", reason="after", dispatch=False)["decision"],
                             BLOCKED_MODE_HITL)
        finally:
            sender.store.close()
            flipper.store.close()

    def test_flip_committed_first_leaves_no_claim_possible(self):
        """Order two: the flip commits first. The send that follows cannot claim."""
        sender, flipper = self._prepare_race()
        try:
            flip = flipper.switch_mode("CLI-TEST", "hitl", "owner@client", "flip lands first")
            self.assertTrue(flip["changed"])
            outcome = sender.send("D-1", "voice", who="orchestrator", reason="after the flip",
                                  dispatch=False)
            self.assertEqual(outcome["decision"], BLOCKED_MODE_HITL)
            self.assertFalse(outcome["claimed"])
            state = self.state(sender)
            self.assertEqual(state["claims"], [], "a claim was written under a mode that had already been flipped")
            self.assertEqual(state["hitl_versions"], [2])
        finally:
            sender.store.close()
            flipper.store.close()

    def test_no_claim_is_written_after_a_hitl_flip_has_committed(self):
        claim_won = 0
        flip_won = 0
        blocked = 0
        for iteration in range(self.ITERATIONS):
            self._reset()
            self.add_debtor("D-1", phone="(212) 555-0143", aging_state="60", balance=1000.0)
            self.sign_off()
            self.engine.switch_mode("CLI-TEST", "full_auto", "signer", "signed")
            self.assertEqual(self.engine.settings("CLI-TEST")["mode_version"], 1)

            sender = self.new_engine()
            flipper = self.new_engine()
            barrier = threading.Barrier(2)
            outcome: dict = {}

            def do_send():
                barrier.wait()
                outcome["send"] = sender.send("D-1", "voice", who="orchestrator",
                                              reason=f"race iteration {iteration}", dispatch=False)

            def do_flip():
                barrier.wait()
                outcome["flip"] = flipper.switch_mode("CLI-TEST", "hitl", "owner@client",
                                                      "halting automation now")

            threads = [threading.Thread(target=do_send), threading.Thread(target=do_flip)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
            self.assertFalse(any(t.is_alive() for t in threads), "a race thread hung")

            send_result = outcome["send"]
            flip_result = outcome["flip"]
            self.assertTrue(flip_result["changed"], "the flip must have taken effect")
            self.assertEqual(flip_result["new_mode"], "hitl")

            timeline = sender.timeline("CLI-TEST")
            hitl_versions = [r["mode_version"] for r in timeline if r["new_mode"] == "hitl"]
            self.assertEqual(hitl_versions, [2], "the flip to hitl is version 2")
            flip_version = hitl_versions[0]

            claims = sender.store.query(
                "SELECT * FROM outbound_contact_logs WHERE claim_token IS NOT NULL")
            self.assertLessEqual(len(claims), 1, "at most one claim may survive one race")
            for claim in claims:
                self.assertLess(claim["mode_version_at_claim"], flip_version,
                                "a claim was written under a mode version the flip had already replaced")
                self.assertEqual(claim["operating_mode_at_claim"], "full_auto")
                self.assertEqual(claim["decision"], CLAIMED)
            if claims:
                self.assertEqual(send_result["decision"], CLAIMED)
                claim_won += 1
            else:
                self.assertEqual(send_result["decision"], BLOCKED_MODE_HITL)
                self.assertIn("not human-approved", send_result["reason_detail"])
                if send_result["claimed"] is False:
                    blocked += 1
                flip_won += 1

            # after the flip has committed, nothing more may be claimed for this client
            later = sender.send("D-1", "voice", who="orchestrator", reason="after the flip",
                               dispatch=False)
            self.assertEqual(later["decision"], BLOCKED_MODE_HITL)
            self.assertEqual(len(sender.store.query(
                "SELECT * FROM outbound_contact_logs WHERE claim_token IS NOT NULL")), len(claims))

            sender.store.close()
            flipper.store.close()

        self.assertEqual(claim_won + flip_won, self.ITERATIONS)
        print(f"\n[race] iterations={self.ITERATIONS} claim-before-flip={claim_won} "
              f"flip-before-claim={flip_won} refused_claims={blocked}")

    def test_dnc_ordered_both_ways(self):
        """The two orderings of an opt-out against an in-window send, deterministically."""
        # opt-out first: no claim, ever
        self.add_debtor("D-5", phone="(212) 555-0143", aging_state="60", approval_status="Approved")
        self.engine.handle_inbound({"debtor_id": "D-5", "content": "Stop calling"})
        after_stop = self.engine.send("D-5", "voice", dispatch=False)
        self.assertEqual(after_stop["decision"], "BLOCKED_DNC")
        self.assertEqual(self.claim_rows("D-5"), [])
        # send first: the claim stands, and every later attempt is refused
        self.add_debtor("D-6", phone="(212) 555-0143", aging_state="60", approval_status="Approved")
        before_stop = self.engine.send("D-6", "voice", dispatch=False)
        self.assertEqual(before_stop["decision"], CLAIMED)
        inbound = self.engine.handle_inbound({"debtor_id": "D-6", "content": "unsubscribe please"})
        self.assertTrue(inbound["dnc_set"])
        self.assertEqual(len(self.claim_rows("D-6")), 1, "the claim that committed first stands")
        self.assertEqual(self.engine.send("D-6", "voice", dispatch=False)["decision"], "BLOCKED_DNC")
        self.assertEqual(len(self.claim_rows("D-6")), 1)

    def test_the_dnc_opt_out_and_a_send_serialise_in_both_directions(self):
        """The two orders of an inbound STOP against an in-window send, CONTROLLED.

        What actually runs, exactly:

        * the interleaving is injected through the documented seam
          `engine.orchestrator.CLAIM_INTERLEAVE_HOOK`, not left to chance. That hook is None
          in every non-test run, is called with a snapshot copy, and cannot disable a guard.
        * DNC-FIRST iteration: the hook runs BEFORE the claim transaction opens and waits
          for the STOP to commit, so the claim's last-millisecond re-read genuinely sees
          dnc_flag = 1. Asserted: the STOP is recorded, BLOCKED_DNC is returned, `claimed`
          is False, NO claim row exists, and the opt-out committed before the send returned.
        * CLAIM-FIRST iteration: the hook runs INSIDE the claim transaction, after the
          re-read and before the claim row is written. The STOP is released FROM THAT HOOK
          ONLY - never from the pre-transaction one - so the claim transaction is already
          open and holding the store's write lock when the STOP's write arrives, and the
          claim commits first while that write waits. Asserted: exactly one claim exists, it
          is CLAIMED, it carries `dnc_flag_at_claim = 0`, the STOP had NOT completed while
          the claim transaction was open (so the halt took effect after the claim), and the
          opt-out's recorded instant is strictly later than the claim's.

        The two recorded instants are millisecond UTC and the harness advances the fake clock
        between them, so the ordering comparison is evaluated on two instants that really do
        differ - the old version pinned both to one whole second, where the comparison had no
        discriminating power, and released the STOP before the transaction opened, so the
        claim-first direction never ran at all (0/12). Both directions now run; the counts
        are printed below and each is asserted to be non-zero.
        """
        import engine.orchestrator as orchestrator
        counts = {"dnc_first": 0, "claim_first": 0}
        for iteration in range(2 * DIRECTIONS_PER_RACE):
            direction = "dnc_first" if iteration % 2 == 0 else "claim_first"
            observed = self._race_once(direction, gap_ms=250 if direction == "claim_first" else 0)
            counts[observed] += 1          # counted from what ran, not from what was asked for
        self.assertEqual(counts, {"dnc_first": DIRECTIONS_PER_RACE,
                                  "claim_first": DIRECTIONS_PER_RACE})
        # Each direction must EXECUTE, not merely be requested: a direction that never runs
        # is not tested at all, and the claim-first direction spent this whole harness's
        # first life at 0 of 12 exactly that way.
        self.assertGreaterEqual(counts["claim_first"], 1,
                                "the claim-first direction never actually occurred")
        self.assertGreaterEqual(counts["dnc_first"], 1,
                                "the STOP-first direction never actually occurred")
        print(f"\n[race] dnc-first segments asserted={counts['dnc_first']} "
              f"claim-first segments asserted={counts['claim_first']} "
              f"overlapping_segments_with_an_unasserted_order=0")
        self.assertIsNone(orchestrator.CLAIM_INTERLEAVE_HOOK,
                          "the seam must be uninstalled after the race")

    def _race_once(self, direction: str, gap_ms: int = 0) -> str:
        """One controlled interleaving. See the test above for what each direction asserts.

        Returns the direction that ACTUALLY occurred, read off the store's claim rows - the
        caller counts that value, so a segment that silently ran as the other direction is
        visible instead of being counted as coverage it never had.
        """
        import engine.orchestrator as orchestrator
        self._reset()
        self.add_debtor("D-9", phone="(212) 555-0143", aging_state="60",
                        approval_status="Approved", balance=1000.0)
        sender = self.new_engine()
        subscriber = self.new_engine()
        stop_go = threading.Event()
        stop_attempted = threading.Event()
        stop_done = threading.Event()
        facts: dict = {"stop_done_inside_claim": None}

        def do_stop():
            stop_go.wait(20)
            facts["stop_attempted_at"] = time.monotonic()
            stop_attempted.set()
            if direction == "claim_first":
                # Park on the very lock the claim transaction holds: this empty BEGIN
                # IMMEDIATE cannot complete until that transaction COMMITs, and the claim's
                # millisecond `claimed_at` is written before it does. So by the time this
                # returns, the claim's instant is already fixed, and the clock is moved
                # below only then - which is what makes the two recorded instants a real
                # ordering comparison instead of two identical strings off one pinned
                # instant.
                with subscriber.store.tx(immediate=True):
                    pass
            if gap_ms:
                # The harness moves the clock between the two instants, so the recorded
                # order is a real comparison instead of two identical strings.
                clock.set_fake_now(clock.now_utc() + datetime.timedelta(milliseconds=gap_ms))
            facts["dnc"] = subscriber.handle_inbound({"debtor_id": "D-9", "content": "STOP"})
            facts["dnc_committed_at"] = time.monotonic()
            stop_done.set()

        def hook(event):
            stage = event["stage"]
            if stage == "before_claim_transaction":
                if direction == "dnc_first":
                    # Released BEFORE the claim transaction opens, so the claim's
                    # last-millisecond re-read genuinely sees dnc_flag = 1.
                    stop_go.set()
                    self.assertTrue(stop_done.wait(20), "the STOP never committed")
                else:
                    # CLAIM-FIRST: the STOP is deliberately NOT released here. Releasing it
                    # before BEGIN IMMEDIATE let its write commit first, which turned the
                    # iteration into a second dnc-first segment wearing a claim-first label
                    # - the defect that held this direction at 0/12. It is released below,
                    # from inside the claim transaction, once the claim holds the lock.
                    self.assertFalse(stop_attempted.is_set(),
                                     "the STOP started before the claim transaction opened, "
                                     "so this segment is not the claim-first direction")
            elif stage == "inside_claim_transaction":
                if direction == "claim_first":
                    # The transaction is open and holds the store's write lock, the re-read
                    # has happened and the claim row has not been written. Release the STOP
                    # HERE: its write can now only land once this transaction commits.
                    stop_go.set()
                    self.assertTrue(stop_attempted.wait(20), "the STOP never started")
                    time.sleep(0.05)      # let the STOP's write actually block on the lock
                # Sampled at the moment the claim row is about to be written: True means the
                # STOP had already committed before the transaction opened (dnc-first),
                # False means its write was still parked behind this transaction's lock
                # (claim-first).
                facts["stop_done_inside_claim"] = stop_done.is_set()
            else:                                    # pragma: no cover - future stages
                raise AssertionError(f"unknown interleave stage {stage!r}")

        stop_thread = threading.Thread(target=do_stop)
        orchestrator.CLAIM_INTERLEAVE_HOOK = hook
        try:
            stop_thread.start()
            outcome = sender.send("D-9", "voice", who="orchestrator",
                                  reason=f"race with an inbound STOP ({direction})", dispatch=False)
            facts["send_returned_at"] = time.monotonic()
            stop_thread.join(timeout=20)
            self.assertFalse(stop_thread.is_alive(), "the STOP thread hung")
        finally:
            orchestrator.CLAIM_INTERLEAVE_HOOK = None

        self.assertTrue(facts["dnc"]["dnc_set"], "the STOP must be recorded either way")
        debtor = sender.debtor("D-9")
        self.assertEqual(debtor["dnc_flag"], 1)
        dnc_at = debtor["dnc_set_at"]
        self.assertTrue(dnc_at, "the opt-out must be recorded with its instant")
        claims = sender.store.query(
            "SELECT * FROM outbound_contact_logs WHERE claim_token IS NOT NULL")
        self.assertLessEqual(len(claims), 1, "at most one claim may survive one race")

        if direction == "dnc_first":
            self.assertEqual(facts["stop_done_inside_claim"], True,
                             "the STOP must have committed before the claim transaction opened")
            self.assertLessEqual(facts["dnc_committed_at"], facts["send_returned_at"],
                                 "the opt-out had not committed before the send returned")
            self.assertEqual(claims, [],
                             "a claim was written although the opt-out had already committed")
            self.assertEqual(outcome["decision"], BLOCKED_DNC)
            self.assertFalse(outcome["claimed"])
        else:
            self.assertEqual(facts["stop_done_inside_claim"], False,
                             "the STOP completed while the claim transaction was open: the "
                             "opt-out's write was not serialized behind the claim")
            self.assertEqual(len(claims), 1, "the claim that committed first must stand")
            claim = claims[0]
            self.assertEqual(claim["decision"], CLAIMED)
            self.assertEqual(outcome["decision"], CLAIMED)
            self.assertEqual(claim["dnc_flag_at_claim"], 0,
                             "a claim recorded the debtor as suppressed at claim time")
            self.assertEqual(claim["operating_mode_at_claim"], "hitl")
            self.assertNotEqual(dnc_at, claim["claimed_at"],
                                "the two instants are the same string: the ordering assertion "
                                "below would have no discriminating power")
            self.assertGreater(clock.parse_ts(dnc_at), clock.parse_ts(claim["claimed_at"]),
                               f"the opt-out at {dnc_at} does not read as taking effect after "
                               f"the claim at {claim['claimed_at']}")

        # Which direction really ran is decided by what the STORE shows, never by the label
        # this iteration asked for. A direction that never executes is not tested, and that
        # is the exact defect this harness exists to prevent.
        observed = "claim_first" if len(claims) == 1 else "dnc_first"
        self.assertEqual(observed, direction,
                         f"asked for the {direction} direction, but the store shows {observed}")

        # whatever happened, the opt-out now blocks everything for this debtor
        after = sender.send("D-9", "voice", who="orchestrator", reason="after the opt-out",
                            dispatch=False)
        self.assertEqual(after["decision"], BLOCKED_DNC)
        self.assertEqual(len(sender.store.query(
            "SELECT * FROM outbound_contact_logs WHERE claim_token IS NOT NULL")), len(claims))
        sender.store.close()
        subscriber.store.close()
        return observed

    def test_the_race_assertion_can_fail_when_the_last_millisecond_re_read_is_removed(self):
        """A test that cannot fail is not a test: prove this one can.

        The same race test is run against a SCRATCH COPY of the engine in which the send
        path is check-then-fire - the DNC re-read is moved out of the claim transaction, so
        a STOP that commits between the read and the claim is ignored. That is the exact
        defect the ordering assertion exists to catch. The copy is under a temp directory;
        nothing in the working tree is modified.
        """
        work = Path(tempfile.mkdtemp(prefix="recoverai-mutation-"))
        try:
            for name in ("engine", "tests", "mocks", "config"):
                shutil.copytree(ROOT / name, work / name,
                                ignore=shutil.ignore_patterns("__pycache__"))
            target = work / "engine" / "orchestrator.py"
            src = target.read_text(encoding="utf-8")
            anchor = (
                """        if CLAIM_INTERLEAVE_HOOK is not None:
            CLAIM_INTERLEAVE_HOOK({"stage": "before_claim_transaction", "debtor_id": debtor_id,
                                   "client_id": client_id})

        # ---- ONE transaction: re-check everything, then write the claim ----
        with self.store.tx(immediate=True):
            settings_row = self._settings_locked(client_id)  # contended row
            live = self.store.query_one("SELECT * FROM debtor_records WHERE debtor_id = ?", (debtor_id,))
""")
            mutated = (
                """        # MUTATION (check-then-fire): the DNC/balance read now happens OUTSIDE the claim
        # transaction, so an opt-out that commits after it is ignored by the claim.
        live = self.store.query_one("SELECT * FROM debtor_records WHERE debtor_id = ?", (debtor_id,))
        if CLAIM_INTERLEAVE_HOOK is not None:
            CLAIM_INTERLEAVE_HOOK({"stage": "before_claim_transaction", "debtor_id": debtor_id,
                                   "client_id": client_id})

        # ---- ONE transaction: re-check everything, then write the claim ----
        with self.store.tx(immediate=True):
            settings_row = self._settings_locked(client_id)  # contended row
""")
            self.assertEqual(src.count(anchor), 1,
                             "the mutation anchor moved: update this test with the new source")
            target.write_text(src.replace(anchor, mutated), encoding="utf-8")

            result = subprocess.run(
                [sys.executable, "-m", "unittest", "-v",
                 "tests.test_race.TestModeFlipRaceWithClaim."
                 "test_the_dnc_opt_out_and_a_send_serialise_in_both_directions"],
                cwd=str(work), capture_output=True, text=True, timeout=280)
            output = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0,
                                "the mutation SURVIVED: the race assertion cannot fail. "
                                f"output was:\n{output[-1500:]}")
            self.assertIn("a claim was written although the opt-out had already committed",
                          output, "the mutation failed, but not on the assertion under test")
        finally:
            shutil.rmtree(work, ignore_errors=True)

if __name__ == "__main__":
    unittest.main()
