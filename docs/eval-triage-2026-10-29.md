# Eval triage, 2026-10-29

The release gate (`replay --baseline`) failed on its first run for this branch. This note records
why, what it means and what is still open.

## Data

Three live runs per (case, commit) at six commits from the baseline `6553a1a` (before the Task 7
prompts) to the branch head, plus two experiments (one reverting the Task 6b floor change, one
without facets). Raw run files were not committed.

## Findings

1. **`misleading_website` forbidden-value violation.** Present at the baseline too (`6553a1a`:
   violations 1, 1, 1), so not a regression. It is a pre-existing product issue: the
   website-seeded industries (Aerospace, Satellite launch) stay as target industries although the
   user says they sell payroll software. Still open.
2. **`detail_mode_user` 0.67 to 0.0.** Noise. F1 is 0.67 in 17 of 18 runs across all commits; the
   single 0.0 is one run at one commit.
3. **`injection_in_answer`.** Violations are 0 at every commit (the injected marker was never
   recorded), so injection resistance is unchanged. The F1 drop (0.40 to 0.00) is a measurement
   artifact: the case expected industries `["law firms"]`, but from `f229771` on the product
   records LinkedIn labels ("Legal Services", "Financial Services"), and `lead_geographies`
   "North America" is sometimes recorded as the countries "United States", "Canada". The scorer
   compared free text with `loose_equal` substring matching, which cannot see these are the same.
4. **`b2c_retail` 0.80 to 0.40.** The case expected `lead_geographies` `["UK"]`. At the baseline
   the model sometimes wrote "UK" (a match); from `f229771` on it always writes the canonical
   label "United Kingdom", which does not match under `loose_equal`. Same artifact. The experiment
   that reverted the floor change stays at 0.42, so Task 6b is not the cause.
5. **`clear_b2b_saas` 0.56 to 0.35**, with F1 outliers of 0.18: the same family (free-text
   expectations against LinkedIn labels) plus noise; it needs the catalog-aware scorer to judge.
6. **Noise.** One to three Vertex timeouts and backup-model turns per run at every commit, so
   single runs prove nothing.

## Conclusion

No product regression was found. The gate drops come from (a) expectations and scoring written in
free text while the product now stores LinkedIn labels and countries, and (b) run-to-run noise.

## Fix and what is still to do

- The scorer is catalog-aware (commit `7d90b7a`, "fix: catalog-aware eval scoring and
  LinkedIn-label expectations"; `src/icp_chatbot/eval/match.py`) and the case expectations are
  written in LinkedIn labels.
- Run files and baselines recorded earlier are not comparable with the new scores and must be
  re-recorded.
- **A fresh live gate run with the new scorer has not been done yet.**
- Open product issue: website-seeded industries remain target industries when the user sells
  something else (finding 1).
