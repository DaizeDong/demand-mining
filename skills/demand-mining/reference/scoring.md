# scoring — three orthogonal axes (Step 4)

Three indicators, three frameworks, **kept orthogonal** (never merged into one opaque number —
that hides the trade-off). The LLM proposes each axis's inputs at **temperature 0** with anchored
1/3/5 rubric samples + a one-line `because` + bound evidence; `scripts/score.py` (pure) disposes them
into a reproducible ordering. No hand-math, no LLM ranking.

## A) Demand strength = Opportunity / ODI (Ulwick)

`Opportunity = Importance + max(Importance − Satisfaction, 0)` (importance double-weighted).
Importance ≈ mention frequency × pain (`intensity` + `external_origin_count` proxy); Satisfaction =
how well the current product/competitor already solves it. High importance + low satisfaction = the
#1 gap. (`lib.opportunity`.)

## B) Ordering = RICE (Intercom, anchored scale)

`RICE = (Reach × Impact × Confidence) / Effort`.
- **Reach** = distinct `author_hash` × cross-lane source-breadth (real count, not estimate).
- **Impact** ∈ {3 massive, 2 high, 1 medium, 0.5 low, 0.25 minimal}; `pain_workaround` /
  `competitor_compare` / half-built workaround = implicit-strong weighting.
- **Confidence** = **mechanical** function of source-tier × cross-validation (`lib.confidence_from_evidence`):
  internal explicit + ≥2 independent = 1.0; single internal cluster ≥3 mentions = 0.8; single
  implicit / single external = 0.5; unverified frontier = 0.3. ≥2-independent-sources gate maps
  straight to the high band — never a guess.
- **Effort** = person-weeks; **clamped to a floor** (`effort_min`), TBD → neutral default
  (`effort_tbd_default`). Never a small divisor (anti-pattern: score explosion).
`final_score` = bounded saturating map `100·r/(r+k)` of `rice_raw` (ordering metric + push threshold).

## C) Urgency = WSJF / velocity (independent layer)

`Urgency = (UserBusinessValue + TimeCriticality + RiskReduction) / JobSize` (Fibonacci 1/2/3/5/8/13).
TimeCriticality anchors: 13 = competitor shipped / active churn / contract window; 8 = competitor
building / quarter window; 3 = time preference; 1 = no deadline. velocity from trend-pulse
`get_trend_velocity`/`lifecycle_prediction` or cross-day score jump. RICE systematically
under-weights time-sensitive needs, so urgency is a **separate axis**, never folded in.

## D) Kano gate (orthogonal nature, adds not replaces)

Must-be (missing → churn) / Performance (linear) / Delighter (excitement) / Indifferent (noise,
cut) / Reverse. No survey → LLM light Kano proxy from tone.

## Deterministic tiering (`score.assign_tier`, replay-safe)

- **Step0 Kano floor**: Indifferent/Reverse → **cut**; Must-be AND currently missing/broken →
  **Tier0** (immediate stop-the-bleed), decoupled from the score.
- **Step1 2D matrix**: high demand × high urgency → **Tier1** (this week); high demand × low →
  **Tier2** (this month); low demand × high urgency → **Tier2** (quick fix); low × low → **backlog**.
- **Step2** within a tier: Opportunity/RICE desc. **Step3** normalize into bands (80+/60-80/40-60/
  <40); argue tier boundaries, never single points (anti false-precision); `depends-on` topo-fix.

## Reproducibility (the命门)

Pure aggregation = byte-identical reruns (`test_score.py::test_score_byte_identical_reruns`).
Weights/thresholds/maps live in `priority.json` (change data, not code). The weight-regression gate
(`score.weight_regression_gate`) re-ranks a golden set under a proposed weight vector WITHOUT
re-evaluating (the breakdown is persisted) and rules auto_pass / needs_review / block by Kendall-tau
drift + push-floor churn. override budget ≤20%/day; golden-set drift >1 band pauses scoring.
