"""The supervisor survives a transient admission refusal and records why it exits.

A resident supervisor that exits on one failed PRIVATE proof stays down until the next
logon, and under pythonw.exe its reason is lost with stderr. These tests run the real
daemon_supervisor module in the in-memory Review19 harness (synthetic paths, no Git, no
network) and check that:

- a refusal during child output stops the child, writes nothing while refused, and the
  supervisor restarts the child once a fresh admission succeeds, logging the cause;
- a refusal that outlasts --admission-retry-seconds still stops the supervisor, with
  bounded backoff and no restart while refused;
- a fatal exit leaves its reason in supervisor.log when that log is admitted, and in a
  local note outside the companion when it is not.
"""
import unittest

from test_review19_repairs import CASES, ControlledStop, Harness

NOTE_BASE = "/synthetic-local-state"
NOTE = NOTE_BASE + "/demand-mining/supervisor-exit.log"


class RetryHarness(Harness):
    """Adds a clock that sleep advances and a per-user state directory outside the companion."""

    def __init__(self, behavior, *, retry_seconds):
        super().__init__(behavior)
        h = self
        self.args.admission_retry_seconds = retry_seconds
        self.clock = [1000.0]
        self.sleeps = []

        def sleep(seconds):
            h.sleeps.append(seconds)
            h.clock[0] += seconds
            if h.behavior == "refuse-then-recover" and h.visibility == "PUBLIC":
                h.visibility = "PRIVATE"
                return
            if h.behavior == "refuse-persistently":
                return
            raise ControlledStop()

        def read_with_revocation(original):
            def read(size):
                if pipe_state["index"] == 1 and h.behavior in {"refuse-then-recover", "refuse-persistently"}:
                    h.visibility = "PUBLIC"
                pipe_state["index"] += 1
                return original(size)
            pipe_state = {"index": 0}
            return read

        self.time.time = lambda: h.clock[0]
        self.time.sleep = sleep
        self.os.environ["LOCALAPPDATA"] = NOTE_BASE
        self.os.getpid = lambda: 4242
        popen = self.subprocess.Popen

        def popen_with_revocation(argv, **kwargs):
            child = popen(argv, **kwargs)
            child.stdout.read = read_with_revocation(child.stdout.read)
            return child

        self.subprocess.Popen = popen_with_revocation

    def companion_writes_while_refused(self):
        return [name for name, _, state in self.writes
                if state != "PRIVATE" and name.startswith(self.root + "/")]

    def companion_opens_while_refused(self):
        return [name for name, state in self.opens
                if state != "PRIVATE" and name.startswith(self.root + "/")]


def supervisor_log(h):
    return h.files.get(h.root + "/pool/logs/supervisor.log", b"").decode()


class TransientRefusalTests(unittest.TestCase):
    def test_refusal_during_output_recovers_and_restarts_the_child(self):
        h = RetryHarness("refuse-then-recover", retry_seconds=3600.0)
        supervisor, _ = h.logging()
        # The second launch is the restart; the harness ends the run there.
        self.assertEqual(h.invoke(supervisor.main), "controlled-stop")
        self.assertEqual(len(h.children), 2, "the supervisor must relaunch after a fresh admission")
        self.assertTrue(h.children[0].terminated or h.children[0].killed)
        self.assertEqual(h.companion_writes_while_refused(), [])
        self.assertEqual(h.companion_opens_while_refused(), [])
        # Only the chunk admitted before the refusal reached the daemon log.
        self.assertEqual(h.files[h.root + "/pool/logs/daemon-2026-01-15.log"],
                         CASES["child_chunks"][0].encode())
        text = supervisor_log(h)
        self.assertIn("admission recovered after", text)
        self.assertIn("synthetic repository visibility is not PRIVATE", text)

    def test_persistent_refusal_still_stops_without_restart(self):
        h = RetryHarness("refuse-persistently", retry_seconds=100.0)
        supervisor, _ = h.logging()
        self.assertEqual(h.invoke(supervisor.run), "stopped")
        self.assertEqual(len(h.children), 1, "nothing is relaunched while admission is refused")
        self.assertEqual(h.companion_writes_while_refused(), [])
        self.assertEqual(h.companion_opens_while_refused(), [])
        self.assertGreaterEqual(sum(h.sleeps), 100.0)
        self.assertLessEqual(max(h.sleeps), h.args.max_backoff)
        self.assertGreater(len([p for p in h.proofs if p[1] == "PUBLIC"]), 2, "admission is re-proved")
        # The log is refused, so the reason goes to the local note outside the companion.
        note = h.files[NOTE].decode()
        self.assertIn("giving up", note)
        self.assertIn("synthetic repository visibility is not PRIVATE", note)
        self.assertNotIn(CASES["child_chunks"][0], note)


class FatalExitRecordTests(unittest.TestCase):
    def failing_main(self, h, supervisor, exc):
        def main():
            supervisor._SUPERVISOR_LOG = h.root + "/pool/logs/supervisor.log"
            raise exc
        supervisor.main = main

    def test_fatal_exit_reason_goes_to_the_admitted_supervisor_log(self):
        h = RetryHarness("normal", retry_seconds=0.0)
        supervisor, _ = h.logging()
        self.failing_main(h, supervisor, RuntimeError("synthetic fatal supervisor fault"))
        with self.assertRaisesRegex(RuntimeError, "synthetic fatal supervisor fault"):
            supervisor.run()
        self.assertIn("supervisor exiting: RuntimeError: synthetic fatal supervisor fault", supervisor_log(h))
        self.assertNotIn(NOTE, h.files)

    def test_fatal_exit_reason_goes_to_the_local_note_when_the_log_is_refused(self):
        h = RetryHarness("normal", retry_seconds=0.0)
        supervisor, _ = h.logging()
        h.visibility = "PUBLIC"
        self.failing_main(h, supervisor, RuntimeError("synthetic fatal supervisor fault"))
        with self.assertRaisesRegex(RuntimeError, "synthetic fatal supervisor fault"):
            supervisor.run()
        self.assertEqual(h.companion_writes_while_refused(), [])
        self.assertEqual(h.companion_opens_while_refused(), [])
        self.assertIn("supervisor exiting (pid 4242): RuntimeError: synthetic fatal supervisor fault",
                      h.files[NOTE].decode())

    def test_exit_note_needs_a_per_user_state_directory(self):
        h = RetryHarness("normal", retry_seconds=0.0)
        supervisor, _ = h.logging()
        del h.os.environ["LOCALAPPDATA"]
        h.visibility = "PUBLIC"
        self.failing_main(h, supervisor, RuntimeError("synthetic fatal supervisor fault"))
        with self.assertRaises(RuntimeError):
            supervisor.run()
        self.assertEqual([name for name in h.files if not name.startswith(h.root + "/")], [])


if __name__ == "__main__":
    unittest.main()
