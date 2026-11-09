# Tuning the general preset (FR-182)

Each version of the preset's instructions, on each model it ran on: the 24 sandbox tasks once, on this SDK's arm. DECISION-7830ca10: tuning stops at the first version that passes every task its predecessor passed on both models, or at the third version after general-v1.

| version | instructions sha256 | model | date | passed | cost | report |
|---|---|---|---|---|---|---|
| general-v1 | d21f40f14b80 | bedrock.anthropic.claude-haiku-4-5 | 2026-11-08T14:54:43+00:00 | 21/24 | 0.453707 | general-v1-bedrock.anthropic.claude-haiku-4-5/adr12-comparison-2026-11-08.json |
| general-v1 | d21f40f14b80 | openai.gpt-4o-mini | 2026-11-08T15:03:10+00:00 | 18/24 | 0.022208250 | general-v1-openai.gpt-4o-mini/adr12-comparison-2026-11-08.json |
| general-v2 | f9187f4d029d | bedrock.anthropic.claude-haiku-4-5 | 2026-11-08T15:14:48+00:00 | 22/24 | 0.435798 | general-v2-bedrock.anthropic.claude-haiku-4-5/adr12-comparison-2026-11-08.json |
| general-v2 | f9187f4d029d | openai.gpt-4o-mini | 2026-11-08T15:22:33+00:00 | 20/24 | 0.020678400 | general-v2-openai.gpt-4o-mini/adr12-comparison-2026-11-08.json |
| general-v3 | 15e203532fbd | bedrock.anthropic.claude-haiku-4-5 | 2026-11-08T15:33:36+00:00 | 22/24 | 0.419616 | general-v3-bedrock.anthropic.claude-haiku-4-5/adr12-comparison-2026-11-08.json |
| general-v3 | 15e203532fbd | openai.gpt-4o-mini | 2026-11-08T15:41:14+00:00 | 19/24 | 0.020062500 | general-v3-openai.gpt-4o-mini/adr12-comparison-2026-11-08.json |
| general-v4 | 73dfc4d03f33 | bedrock.anthropic.claude-haiku-4-5 | 2026-11-08T15:52:14+00:00 | 23/24 | 0.439739 | general-v4-bedrock.anthropic.claude-haiku-4-5/adr12-comparison-2026-11-08.json |
| general-v4 | 73dfc4d03f33 | openai.gpt-4o-mini | 2026-11-08T15:59:26+00:00 | 18/24 | 0.021779400 | general-v4-openai.gpt-4o-mini/adr12-comparison-2026-11-08.json |

## general-v2 against general-v1: does not hold
- Lost: repair-restock on openai.gpt-4o-mini

## general-v3 against general-v2: does not hold
- Lost: repair-dedupe on bedrock.anthropic.claude-haiku-4-5, count-error-lines on openai.gpt-4o-mini, rename-notes on openai.gpt-4o-mini, repair-slugify on openai.gpt-4o-mini

## general-v4 against general-v3: does not hold
- Lost: repair-dedupe on openai.gpt-4o-mini, top-words on openai.gpt-4o-mini

## general-v1: failed tasks
- bedrock.anthropic.claude-haiku-4-5: fetch-page, repair-date-format, repair-restock
- openai.gpt-4o-mini: count-error-lines, fetch-page, fetch-title-into-config, merge-intervals, repair-dedupe, top-words

## general-v2: failed tasks
- bedrock.anthropic.claude-haiku-4-5: repair-date-format, repair-restock
- openai.gpt-4o-mini: fetch-page, fetch-title-into-config, repair-dedupe, repair-restock

## general-v3: failed tasks
- bedrock.anthropic.claude-haiku-4-5: repair-date-format, repair-dedupe
- openai.gpt-4o-mini: count-error-lines, fetch-page, fetch-title-into-config, rename-notes, repair-slugify

## general-v4: failed tasks
- bedrock.anthropic.claude-haiku-4-5: repair-date-format
- openai.gpt-4o-mini: count-error-lines, fetch-page, fetch-title-into-config, repair-dedupe, repair-slugify, top-words
