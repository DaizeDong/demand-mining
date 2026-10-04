# demand-mining, Design Philosophy

## Separate interpretation from reproducible decisions

Community feedback includes explicit requests and implied jobs, and a model can help interpret
both. The local pipeline owns scoring, evidence admission, deduplication and delivery state.
This makes those decisions reproducible and prevents a fluent proposal from becoming a confirmed
demand merely because it sounds confident. Verbatim evidence and observation identity remain necessary.

## Protect data before interpreting it

Collection applies structured redaction and HMAC author pseudonyms before model processing and
persistence. Regex and entropy checks have limits: they cannot promise to remove every name or
sensitive passage. Inputs must respect those limits. Real observations, demand pools and reports
belong in a PRIVATE versioned companion, and egress checks run again before delivery.

## Own the demand workflow and use established interfaces

The shipped tap and daemon own demand-specific Discord collection. Model work uses installed
`llmcall` routing; reminder operations use the scheduler CLI instead of directly changing its database.
The daemon's own private pool is a separate persistence surface. Hotspot and competitor research
remain deferred integrations, so their absence must stay visible rather than be implied by delegation prose.

## Keep ranking questions distinct

RICE orders effort-adjusted opportunities, Opportunity represents unmet need, WSJF represents
urgency, and Kano describes need type. Distinct-author counts reduce repeated-message inflation.
These calculations preserve their separate meanings; combining them does not turn uncertain
inputs into calibrated forecasts. Realized outcomes are needed before changing calibration.

## Accept an empty result when evidence is insufficient

A daily cadence does not require a nonempty digest. Grounding and admission checks can leave no
qualified new demand, which the output reports explicitly. Implicit-demand recall remains a quality
question to measure with separate evaluation material. Synthetic regression cases establish local
behavior, while collection access, actual delivery and model effectiveness require their own evidence.
