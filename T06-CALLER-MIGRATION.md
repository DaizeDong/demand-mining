# Caller migration

Calls use one llmcall request with inherited model, effort and routing defaults.
Explicit user selections remain constraints. Timeouts, uncertain effects and
invalid outputs never cause a second business model call. Synthetic tests do
not certify production capabilities.

The existing PowerShell entrypoint uses the CONFIG compatibility shell through
its existing AGENT_RUNNER binding. Deployment must supply RequiredMcp and
AgentDataRoots (including the shared ledger). All SKILL-declared tools remain
required. Current llmcall cannot represent multiple writable roots and has no
verified Skill/Agent support. These requests fail explicitly. Do not broaden
Workspace, drop required MCP, or restore a provider CLI bypass. Demand-mining
also requires RequiredArtifact, the resolved current digest path; success needs
a fresh nonempty digest before archive publication. Existing daily-hotspots
run IDs, in-flight markers, logs and digest correctness gates remain.
