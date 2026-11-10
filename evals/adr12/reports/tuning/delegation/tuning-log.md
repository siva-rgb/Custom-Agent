# Tuning the general preset (FR-182)

Each version of the preset's instructions, on each model it ran on: its set's tasks once (the 24 sandbox tasks in reports/tuning/, the 6 delegation tasks in reports/tuning/delegation/), on this SDK's arm. DECISION-7830ca10: tuning stops at the first version that passes every task its predecessor passed on both models, or at the third version after general-v1.

| version | instructions sha256 | model | date | passed | cost | report |
|---|---|---|---|---|---|---|
| general-v5 | 4cbee96d6def | bedrock.anthropic.claude-haiku-4-5 | 2026-11-10T03:45:22+00:00 | 6/6 | 0.346477 | general-v5-bedrock.anthropic.claude-haiku-4-5/adr12-comparison-2026-11-10.json |
| general-v5 | 4cbee96d6def | openai.gpt-4o-mini | 2026-11-10T03:49:14+00:00 | 4/6 | 0.011278200 | general-v5-openai.gpt-4o-mini/adr12-comparison-2026-11-10.json |

## general-v5: failed tasks
- bedrock.anthropic.claude-haiku-4-5: none
- openai.gpt-4o-mini: csv-totals, word-counts
