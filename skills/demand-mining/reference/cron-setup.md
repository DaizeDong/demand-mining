# Scheduling the daily EOD run

Windows Task Scheduler invokes wrapper.ps1, which runs scheduled.py with the
selected Python interpreter. The installed llmcall agent interface proposes
candidates. Local code validates the handoff and owns artifact, receipt and
watermark persistence. No separate provider CLI is needed.

Configure product_id and an IANA timezone before registration. Initialization
chooses UTC explicitly. The wrapper and runner share the zone, so day boundaries
and DST are independent of the host timezone. The scheduled source window ends
at a frozen collection cutoff and resumes from the previous completed cutoff.
Pending runs are resumed before starting a new day. Historical digest registration is
not proof that missed source messages were collected.

Register with register-task.ps1 at an appropriate local trigger time after the
config doctor and scheduled.py --preflight succeed. Registration fixes the
interpreter: pass -Python with the python.exe that has llmcall installed, or the
script records the python your shell resolves; the Windows Store alias is refused. Registration is an explicit
operator action; running a doctor never creates a task or sends a notification.
The scheduled caller reports incomplete status through its exit code and private
caller state. It does not issue a second message to report a failed send.

A confirmed adapter receipt, bound to the logical run and actual sent content,
suppresses replay. An ambiguous send remains pending reconciliation without
another automatic delivery. Backup retry uses the same saved handoff and receipt.
A private index for inspection and path-limited commits preserve unrelated staged edits. Every Git
result is checked. See docs/runtime-contract.md in the repository root for the
handoff, destination, receipt and backup requirements.
