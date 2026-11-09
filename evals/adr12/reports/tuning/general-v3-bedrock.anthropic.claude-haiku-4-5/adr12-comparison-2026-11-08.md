# ADR-12 comparison

- Date: 2026-11-08T15:33:36+00:00
- Model, as the gateway names it: bedrock.anthropic.claude-haiku-4-5
- Repetitions per task and arm: 1
- Tasks: 24

## Arms

| arm | version | wire format |
|---|---|---|
| agentsdk | agentsdk 0.1.0.dev0, preset general-v3 | OpenAI-compatible chat completions |

## Tasks and setups

24 tasks: 24 sandbox.

| kind | tasks | arm | setup | USD ceiling | token ceiling |
|---|---|---|---|---|---|
| sandbox | 24 | agentsdk | the general preset in a workspace holding the task's files, egress by the task's allow list, every export refused; it runs as uid 65534 (nobody, home /) in the workspace image, which has /bin/sh but no bash | 0.023940 | 20175 |

- Budget rule: this SDK's arm only: a USD and a token ceiling of 10 times the kind's median on this SDK's arm in M15 (adr12-comparison-2026-09-16.json); the sandbox kind 10 times the multi-step median. The Claude arm has no budget.
- Networking: this SDK's arm reaches only its task's allowed host, through its workspace's proxy; the Claude arm runs with ordinary networking, to reach the gateway and the fetch tasks' host.
- The general preset's version: general-v3.
- Verification: code tasks verified by the harness in a fresh container of the workspace image with no network, on visible and hidden cases; a run's own check.txt is not evidence.
- Fetch witness: https://example.com/ as the harness fetched it at 2026-11-08T15:33:36+00:00, 577 bytes, SHA-256 25ddf2c883e0d1958ea971d279a7e4f0fd446724ee3db7db19dadabd4a62e484; the fetch tasks are judged against it.
- Proof of fetch: a fetch task passes only with evidence the run reached its host: on this SDK's arm an allowed connection its workspace's proxy recorded; on the Claude arm, whose network is not recorded, a Bash or WebFetch call naming the host.
- agentsdk image: python@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df

Both arms use the same model through the same gateway and each task's turn limit. Both
are costed from one price list; the Claude Agent SDK's own figure is its estimate and is
reported as such.

## What each arm measured

| arm | tasks passed | runs | median turns | median wall ms | total cost (one price list) | total of its own estimate |
|---|---|---|---|---|---|---|
| agentsdk | 22 of 24 | 24 | 5.5 | 17666.1 | 0.419616 | unavailable |

## By kind

| kind | arm | passed | pass rate | runs | terminal statuses | median turns | tokens | total cost | median wall ms |
|---|---|---|---|---|---|---|---|---|---|
| sandbox | agentsdk | 22 | 92% | 24 | budget_exceeded 1, completed 23 | 5.5 | 348232 | 0.419616 | 17666.1 |
| all | agentsdk | 22 | 92% | 24 | budget_exceeded 1, completed 23 | 5.5 | 348232 | 0.419616 | 17666.1 |

## By task

| task | kind | arm | passed | status | turns | wall ms | cost | its estimate | answer |
|---|---|---|---|---|---|---|---|---|---|
| count-error-lines | sandbox | agentsdk | yes | completed | 4 | 13430.1 | 0.012865 | unavailable | Done! I've counted the lines in logs/app.log that contain the word "ERROR" and found **7 lines**. The count has been written to answer.txt as digits only (no... |
| unique-names | sandbox | agentsdk | yes | completed | 5 | 15021.5 | 0.012701 | unavailable | Done! I've created **unique.txt** with the following content: - 6 distinct names from names.txt, sorted alphabetically - All blank lines were skipped - Dupli... |
| largest-file | sandbox | agentsdk | yes | completed | 4 | 11117.0 | 0.009587 | unavailable | Done! I found that the largest file in the data folder is `gamma.json` with 3013 bytes. The path `data/gamma.json` has been written to `answer.txt`. |
| rename-notes | sandbox | agentsdk | yes | completed | 7 | 24801.3 | 0.022395 | unavailable | Perfect! I've successfully completed the task. All .txt files in the notes folder have been renamed to .md files: - `budget.txt` → `budget.md` - `design.txt`... |
| file-reports-by-month | sandbox | agentsdk | yes | completed | 6 | 19282.7 | 0.019404 | unavailable | Perfect! ✅ The task is complete. All reports have been successfully organized: **Summary:** - **2024-01/**: Contains `report-2024-01-15.csv` and `report-2024... |
| top-words | sandbox | agentsdk | yes | completed | 5 | 15283.2 | 0.013793 | unavailable | Perfect! The task is complete. Here's what I did: 1. **Read text.txt** - Found the input file with text about a fox, dog, and cat 2. **Counted words** - Used... |
| parse-duration | sandbox | agentsdk | yes | completed | 5 | 15068.0 | 0.017490 | unavailable | Excellent! The implementation is complete and all checks pass. **Summary:** I implemented the `seconds(text)` function in `duration.py` that: - Parses durati... |
| roman-numerals | sandbox | agentsdk | yes | completed | 6 | 23410.8 | 0.021009 | unavailable | Excellent! The task is complete. **Summary:** I've successfully written `roman.py` with a `to_int(numeral)` function that converts Roman numerals to integers... |
| isbn10 | sandbox | agentsdk | yes | completed | 6 | 17614.0 | 0.022280 | unavailable | Excellent! The task is complete. I've successfully: 1. **Written `isbn.py`** with a `valid(code)` function that validates ISBN-10 codes 2. **The function cor... |
| merge-intervals | sandbox | agentsdk | yes | completed | 6 | 18082.5 | 0.020939 | unavailable | Excellent! The task is complete. Here's what I've done: **Summary:** I wrote `intervals.py` with a `merge(pairs)` function that: - Takes a list of `[start, e... |
| word-wrap | sandbox | agentsdk | yes | completed | 6 | 23738.3 | 0.021996 | unavailable | Excellent! The task is complete. **Summary:** I've successfully written `wrap.py` with a `wrap(text, width)` function that: - Breaks text into lines of at mo... |
| category-totals | sandbox | agentsdk | yes | completed | 6 | 18376.9 | 0.020884 | unavailable | Excellent! The task is complete. **Summary:** I created `totals.py` with a `totals(path)` function that: 1. Opens and reads a CSV file using the `csv.DictRea... |
| change-server-port | sandbox | agentsdk | yes | completed | 4 | 11296.5 | 0.010215 | unavailable | Perfect! The port in the [server] section has been successfully changed to 9090. The [metrics] section port remains at 9100, and all other settings remain un... |
| add-license-field | sandbox | agentsdk | yes | completed | 5 | 13907.9 | 0.013424 | unavailable | Perfect! I've successfully added the "license" field with the value "MIT" to package.json. The file is valid JSON, and all other fields remain exactly as the... |
| rename-company | sandbox | agentsdk | yes | completed | 5 | 18240.2 | 0.014521 | unavailable | Perfect! I've successfully replaced all occurrences of 'ACME Corp' with 'Globex' in the Markdown files: **Changes made:** 1. **docs/intro.md**: Replaced 2 oc... |
| changelog-entry | sandbox | agentsdk | yes | completed | 3 | 6726.4 | 0.007644 | unavailable | Done! I've added a new section for version 1.3.0 to CHANGELOG.md above the 1.2.0 section with the entry "Added the export command", maintaining the same form... |
| initials-column | sandbox | agentsdk | yes | completed | 4 | 12729.2 | 0.011151 | unavailable | Perfect! I've successfully added an "initials" column to people.csv. The file now contains: - **Ada Lovelace** → **AL** - **Alan Turing** → **AT** - **grace ... |
| fetch-page | sandbox | agentsdk | yes | completed | 4 | 21630.8 | 0.012619 | unavailable | Done! I've successfully: 1. **Downloaded** https://example.com/ and saved the exact HTML response to `page.html` (577 bytes) 2. **Saved the HTTP status code*... |
| fetch-title-into-config | sandbox | agentsdk | yes | completed | 4 | 14592.8 | 0.013503 | unavailable | Done! I've successfully: 1. Fetched the page from `https://example.com/` 2. Extracted the `<title>` element text: **"Example Domain"** 3. Counted the respons... |
| repair-average | sandbox | agentsdk | yes | completed | 7 | 17718.2 | 0.022269 | unavailable | **Done!** The fault was in `stats.py`. The `average` function was dividing by `(len(values) + 1)` instead of `len(values)`. I fixed it by removing the `+ 1`,... |
| repair-date-format | sandbox | agentsdk | no | budget_exceeded | 8 | 24391.1 | 0.026687 | unavailable | unavailable |
| repair-dedupe | sandbox | agentsdk | no | completed | 7 | 20439.1 | 0.024190 | unavailable | Excellent! The check passes now. **Summary:** The fault in `emails.py` was in the `distinct()` function. It was doing a simple case-sensitive comparison with... |
| repair-restock | sandbox | agentsdk | yes | completed | 7 | 19697.5 | 0.025103 | unavailable | ## Summary The fault was in `inventory.py`. The `to_order` function had an incorrect condition: - **Original:** `if count > minimum` (order items with count ... |
| repair-slugify | sandbox | agentsdk | yes | completed | 7 | 17362.0 | 0.022947 | unavailable | Excellent! The check now passes. ## Summary The fault was in the `slugify` function in `slugs.py`. The original implementation had three issues: 1. **Leading... |

## How to read this

Each row is one run. A measure an arm does not report reads unavailable rather than 0, so a
gap in the data cannot be mistaken for a zero. A failed checker means the answer did not
match what the fixture says, which is a measurement of this task on this arm, not a
judgement of either SDK.
