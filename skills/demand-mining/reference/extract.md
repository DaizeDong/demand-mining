# extract, Discord demand extraction (Step 2)

Input = the already-redacted message stream (Step 1 ran first). Output = grounded, dual-track
opinion-units the pool/score layers consume. The interpretive work is the LLM's; `scripts/extract.py`
is the deterministic frame the proposal must satisfy.

## Stage A, intent (near-real-time, per message)

Classify each message into the **frozen 8-label mutually-exclusive enum** (a message may carry >1):
`feature_request · bug_complaint · pain_workaround · competitor_compare · pricing_objection ·
how_to_question · praise · chitchat`. `chitchat` is the logged-then-dropped bucket; `how_to_question`
is support (not a demand); `pain_workaround` is the **strongest implicit signal** (user already built
a half-fix). **Do NOT keyword-filter chitchat**, "April/Penny"-style ambiguity + sarcasm need
context LLM judgement. `extract.normalize_intents()` clamps proposals to the enum; `is_demand()`
decides demand vs noise.

## Stage B, session-level (batch)

1. **Disentangle first.** A channel interleaves many conversations. **Never time-window slice**
   (anti-pattern #11). Use Discord-native structure for session edges: `thread` / `message.reference`
   reply chains + same-author bursts (<5min) + `@mention` chains + light semantic similarity. Feed the
   extractor the *clustered segment*, not the raw channel stream.
2. **JTBD four forces.** Recover Push (struggle w/ status quo), Pull (attraction to new), Anxiety
   (switching fear), Habit (old habit to drop). Push+Pull = "is there a real demand"; **Anxiety+Habit
   = the implicit goldmine** (what the user wants but did not say).
3. **Three-layer translation.** literal feature (the user's own proposed solution) → the job/pain
   behind it → the emotion/value layer. **Never排期 the literal feature**, force an inferred job +
   5-Whys, tag confidence (anti-pattern #5, Feature Factory).
4. **Opinion-unit extraction (ABSA).** Per session: `{intents, aspect, polarity/intensity, quote,
   message_id}`.
5. **Verbatim grounding (`extract.verbatim_grounding`).** Every extracted demand's quote MUST be
   locatable in the redacted source, a quote that is not is **REJECTED** (omission ≈ 2× fabrication,
   ~7.7% of quotes unfindable). EOD runs a second "missed-pass" to catch contradiction/weak signals
   that the coherence filter dropped.

## Dual-track record

`build_unit()` emits an explicit-pool unit (directly stated) **or** an implicit-pool unit (inferred,
with evidence chain + confidence). Both kept, 宁全勿漏. The unit's `canonical_key` is built from the
inferred **job** (+ aspect entities), never the literal feature, so different literal asks for the
same job collapse correctly downstream.
