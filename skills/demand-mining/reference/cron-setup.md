# Scheduling the daily EOD run

The skill's tasks are declared in `automation/tasks.json` at the repository root
(`DemandMiningEOD` daily at 21:40, `DemandMiningDaemon` at logon) and installed with
background-task-core `install-skill`. Machine overrides live in the companion's
`automation/tasks.local.json`. The EOD task invokes wrapper.ps1, which runs scheduled.py with the
selected Python interpreter. The installed llmcall agent interface proposes
candidates. Local code validates the handoff and owns artifact, receipt and
watermark persistence. No separate provider CLI is needed.

Configure product_id and an IANA timezone before registration. Initialization
chooses UTC explicitly. The wrapper and runner share the zone, so day boundaries
and DST are independent of the host timezone. The scheduled source window ends
at a frozen collection cutoff and resumes from the previous completed cutoff.
Pending runs are resumed before starting a new day. Historical digest registration is
not proof that missed source messages were collected.

Install the tasks after the config doctor and scheduled.py --preflight succeed. The
declaration passes -Python with the interpreter that has llmcall installed; the
Windows Store alias is refused. Installation is an explicit operator action; running a
doctor never creates a task or sends a notification.

The daemon task starts `daemon_supervisor.py` windowless at logon with no execution
limit; the supervisor restarts the daemon after an ordinary crash. Shadow mode captures
only. To run it `live` or `dry`, change `--mode` in the manifest's argv and re-run
`install-skill`.
The scheduled caller reports incomplete status through its exit code and private
caller state. It does not issue a second message to report a failed send.

A confirmed adapter receipt, bound to the logical run and actual sent content,
suppresses replay. An ambiguous send remains pending reconciliation without
another automatic delivery. Backup retry uses the same saved handoff and receipt.
A private index for inspection and path-limited commits preserve unrelated staged edits. Every Git
result is checked. See docs/runtime-contract.md in the repository root for the
handoff, destination, receipt and backup requirements.
