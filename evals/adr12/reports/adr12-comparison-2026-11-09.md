# ADR-12 comparison

- Date: 2026-11-09T07:13:43+00:00
- Model, as the gateway names it: bedrock.anthropic.claude-haiku-4-5
- Repetitions per task and arm: 3
- Tasks: 36

## Arms

| arm | version | wire format |
|---|---|---|
| agentsdk | agentsdk 0.1.0.dev0, preset general-v4 | OpenAI-compatible chat completions |
| claude-agent-sdk | claude-agent-sdk 0.2.152 with Claude Code CLI 2.1.267, in adr12-claude:cli-2.1.267 | Anthropic Messages through the Claude Code CLI |

## Tasks and setups

36 tasks: 1 decline, 3 long, 4 multi-step, 24 sandbox, 4 single-step.

| kind | tasks | arm | setup | USD ceiling | token ceiling |
|---|---|---|---|---|---|
| decline | 1 | agentsdk | read, glob and grep over a host copy of the fixture, as in M15 | 0.015310 | 9990 |
| decline | 1 | claude-agent-sdk | Read, Glob and Grep over a host copy of the fixture, as in M15 | none | none |
| long | 3 | agentsdk | read, glob and grep over a host copy of the fixture, as in M15 | 0.022370 | 19420 |
| long | 3 | claude-agent-sdk | Read, Glob and Grep over a host copy of the fixture, as in M15 | none | none |
| multi-step | 4 | agentsdk | read, glob and grep over a host copy of the fixture, as in M15 | 0.023940 | 20175 |
| multi-step | 4 | claude-agent-sdk | Read, Glob and Grep over a host copy of the fixture, as in M15 | none | none |
| sandbox | 24 | agentsdk | the general preset in a workspace holding the task's files, egress by the task's allow list, every export refused; it runs as uid 65534 (nobody, home /) in the workspace image, which has /bin/sh but no bash | 0.023940 | 20175 |
| sandbox | 24 | claude-agent-sdk | Read, Glob, Grep, Bash, Write and Edit in a disposable container holding the task's files; it runs as uid 1000 (agent, home /home/agent), and its image adds nodejs, npm and bash (for the Bash tool) to the workspace image's base | none | none |
| single-step | 4 | agentsdk | read, glob and grep over a host copy of the fixture, as in M15 | 0.021750 | 19250 |
| single-step | 4 | claude-agent-sdk | Read, Glob and Grep over a host copy of the fixture, as in M15 | none | none |

- Budget rule: this SDK's arm only: a USD and a token ceiling of 10 times the kind's median on this SDK's arm in M15 (adr12-comparison-2026-09-16.json); the sandbox kind 10 times the multi-step median. The Claude arm has no budget.
- Networking: this SDK's arm reaches only its task's allowed host, through its workspace's proxy; the Claude arm runs with ordinary networking, to reach the gateway and the fetch tasks' host.
- The general preset's version: general-v4.
- Verification: code tasks verified by the harness in a fresh container of the workspace image with no network, on visible and hidden cases; a run's own check.txt is not evidence.
- Fetch witness: https://example.com/ as the harness fetched it at 2026-11-09T07:13:43+00:00, 577 bytes, SHA-256 25ddf2c883e0d1958ea971d279a7e4f0fd446724ee3db7db19dadabd4a62e484; the fetch tasks are judged against it.
- Proof of fetch: a fetch task passes only with evidence the run reached its host, kept in each row's fetch_evidence: on this SDK's arm an allowed connection its workspace's proxy recorded, by id; on the Claude arm, whose network is not recorded, a call whose result was not an error, read as the shell would run it: WebFetch of the host's URL, or a Bash command whose program, after any wrapper, is curl or wget with the host among its arguments, or python or node running code that uses an HTTP client and names the host's URL (inline, a heredoc, or a script the run wrote). The Claude arm's evidence is the command the run gave, not the network: a URL computed at run time or a script edited after it was written is a fetch it does not count (pre-review F1, round-1 F1).
- agentsdk image: python@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df
- claude-agent-sdk image: adr12-claude:cli-2.1.267 sha256:9cf6ef1c38050aed0cca1abeefa337a986dad460da25e410553a20cdd206a4db

Both arms use the same model through the same gateway and each task's turn limit. Both
are costed from one price list; the Claude Agent SDK's own figure is its estimate and is
reported as such.

## What each arm measured

| arm | tasks passed | runs | median turns | median wall ms | total cost (one price list) | total of its own estimate |
|---|---|---|---|---|---|---|
| agentsdk | 103 of 108 | 108 | 5.0 | 16212.0 | 1.470586 | unavailable |
| claude-agent-sdk | 103 of 108 | 108 | 5.0 | 13945.0 | 1.31953930 | 1.3195393000000000380 |

## By kind

| kind | arm | passed | pass rate | runs | terminal statuses | median turns | tokens | total cost | median wall ms |
|---|---|---|---|---|---|---|---|---|---|
| decline | agentsdk | 3 | 100% | 3 | completed 3 | 1 | 2933 | 0.004273 | 2443.6 |
| decline | claude-agent-sdk | 2 | 67% | 3 | completed 3 | 1 | 12596 | 0.017912 | 4547.9 |
| long | agentsdk | 9 | 100% | 9 | completed 9 | 2 | 21570 | 0.025762 | 4029.8 |
| long | claude-agent-sdk | 9 | 100% | 9 | completed 9 | 4 | 85159 | 0.095123 | 7410.7 |
| multi-step | agentsdk | 12 | 100% | 12 | completed 12 | 2.0 | 26531 | 0.031879 | 3806.8 |
| multi-step | claude-agent-sdk | 12 | 100% | 12 | completed 12 | 2.0 | 106263 | 0.118459 | 4919.3 |
| sandbox | agentsdk | 67 | 93% | 72 | budget_exceeded 5, completed 67 | 6.0 | 1158446 | 1.382494 | 19486.5 |
| sandbox | claude-agent-sdk | 68 | 94% | 72 | completed 72 | 8.0 | 3962480 | 0.95921930 | 20377.2 |
| single-step | agentsdk | 12 | 100% | 12 | completed 12 | 2.0 | 23098 | 0.026178 | 4122.1 |
| single-step | claude-agent-sdk | 12 | 100% | 12 | completed 12 | 3.0 | 111890 | 0.128826 | 7023.7 |
| all | agentsdk | 103 | 95% | 108 | budget_exceeded 5, completed 103 | 5.0 | 1232578 | 1.470586 | 16212.0 |
| all | claude-agent-sdk | 103 | 95% | 108 | completed 108 | 5.0 | 4278388 | 1.31953930 | 13945.0 |

## By task

| task | kind | arm | passed | status | turns | wall ms | cost | its estimate | answer |
|---|---|---|---|---|---|---|---|---|---|
| version-from-readme | single-step | agentsdk | yes | completed | 2 | 3613.5 | 0.002158 | unavailable | 2.4.1 |
| version-from-readme | single-step | claude-agent-sdk | yes | completed | 3 | 9926.7 | 0.012155 | 0.012155000000000001 | 2.4.1 |
| retries-from-settings | single-step | agentsdk | yes | completed | 2 | 9337.3 | 0.002162 | unavailable | 5 |
| retries-from-settings | single-step | claude-agent-sdk | yes | completed | 3 | 6369.0 | 0.011630 | 0.01163 | 5 |
| tax-rate-constant | single-step | agentsdk | yes | completed | 2 | 2441.0 | 0.002196 | unavailable | 0.19 |
| tax-rate-constant | single-step | claude-agent-sdk | yes | completed | 4 | 6399.7 | 0.013038 | 0.013038000000000001 | 0.19 |
| entry-count | single-step | agentsdk | yes | completed | 2 | 2522.2 | 0.002188 | unavailable | 6 |
| entry-count | single-step | claude-agent-sdk | yes | completed | 3 | 9141.2 | 0.011508 | 0.011508000000000001 | 6 |
| where-apply-fee-lives | multi-step | agentsdk | yes | completed | 2 | 7804.9 | 0.002384 | unavailable | The function `apply_fee` is defined in **src/ledger.py**. |
| where-apply-fee-lives | multi-step | claude-agent-sdk | yes | completed | 2 | 4678.6 | 0.006389 | 0.006389000000000001 | The file that defines the `apply_fee` function is: **`src/ledger.py`** |
| test-that-covers-apply-fee | multi-step | agentsdk | yes | completed | 2 | 5208.9 | 0.002437 | unavailable | The test file that exercises `apply_fee` is: **tests/test_ledger.py** |
| test-that-covers-apply-fee | multi-step | claude-agent-sdk | yes | completed | 5 | 9058.1 | 0.016831 | 0.016831 | Perfect! The test file that exercises `apply_fee` is **`tests/test_ledger.py`**. |
| release-date-of-current-version | multi-step | agentsdk | yes | completed | 3 | 5367.7 | 0.003839 | unavailable | 2026-04-02 |
| release-date-of-current-version | multi-step | claude-agent-sdk | yes | completed | 6 | 6871.3 | 0.014308 | 0.014308000000000001 | The version named in README.md is **2.4.1**, and in docs/changelog.md, version 2.4.1 carries the date **2026-04-02**. 2026-04-02 |
| key-holding-the-region | multi-step | agentsdk | yes | completed | 2 | 3621.7 | 0.002382 | unavailable | The value "eu-west-1" is held in a setting called **`region`** in the file `config/settings.toml`. |
| key-holding-the-region | multi-step | claude-agent-sdk | yes | completed | 2 | 4425.0 | 0.006516 | 0.006515999999999999 | The value **eu-west-1** is held in the setting named **`region`** in the file `config/settings.toml`. |
| every-python-file | long | agentsdk | yes | completed | 2 | 3233.0 | 0.002237 | unavailable | src/ledger.py src/report.py tests/test_ledger.py |
| every-python-file | long | claude-agent-sdk | yes | completed | 2 | 4406.9 | 0.006395 | 0.006395 | src/ledger.py src/report.py tests/test_ledger.py |
| sum-of-amounts | long | agentsdk | yes | completed | 2 | 4029.8 | 0.002198 | unavailable | 372.50 |
| sum-of-amounts | long | claude-agent-sdk | yes | completed | 3 | 8336.7 | 0.010075 | 0.010075 | 120.00 + 45.50 + 89.25 + 17.75 + 60.00 + 40.00 = 372.50 372.50 |
| functions-in-src | long | agentsdk | yes | completed | 3 | 6295.3 | 0.004079 | unavailable | apply_fee total summarise |
| functions-in-src | long | claude-agent-sdk | yes | completed | 6 | 9140.1 | 0.014524 | 0.014523999999999999 | apply_fee total summarise |
| decline-to-delete | decline | agentsdk | yes | completed | 1 | 4665.3 | 0.001321 | unavailable | I don't have the ability to delete files. I can only read and search files using the tools available to me. To delete `config/settings.toml`, you would need ... |
| decline-to-delete | decline | claude-agent-sdk | yes | completed | 1 | 3976.7 | 0.004182 | 0.004182 | I don't have the ability to delete files. The tools available to me are: 1. **Glob** - for finding files by pattern 2. **Grep** - for searching file contents... |
| count-error-lines | sandbox | agentsdk | yes | completed | 6 | 21638.4 | 0.018145 | unavailable | Done! I've counted the lines in logs/app.log that contain the word ERROR (there are 7) and written the count "7" to answer.txt. The log file was left unchang... |
| count-error-lines | sandbox | claude-agent-sdk | yes | completed | 3 | 10043.0 | 0.01142485 | 0.011424850000000002 | Done! The logs/app.log file contains **7 lines with the word ERROR**. I've written the count "7" to answer.txt. |
| unique-names | sandbox | agentsdk | yes | completed | 4 | 13654.3 | 0.010867 | unavailable | Perfect! I've successfully created `unique.txt` with the distinct names from `names.txt`, sorted alphabetically. The program: 1. Read each line from names.tx... |
| unique-names | sandbox | claude-agent-sdk | yes | completed | 5 | 20354.8 | 0.00699165 | 0.00699165 | Perfect! I've created `unique.txt` with all distinct names from `names.txt`, sorted alphabetically, with blank lines skipped. The file contains: - anouk - be... |
| largest-file | sandbox | agentsdk | yes | completed | 4 | 21863.8 | 0.010122 | unavailable | Done! The largest file in the data folder is `gamma.json` with 3013 bytes. Its path relative to the workspace folder has been written to `answer.txt`. |
| largest-file | sandbox | claude-agent-sdk | yes | completed | 5 | 13865.6 | 0.00853395 | 0.00853395 | Done! I found that the largest file in the data folder is **data/gamma.json** with **3013 bytes**, and I've written its path to answer.txt. |
| rename-notes | sandbox | agentsdk | yes | completed | 7 | 32840.8 | 0.022900 | unavailable | Perfect! I've successfully renamed all .txt files in the notes folder to .md files: - `budget.txt` → `budget.md` - `design.txt` → `design.md` - `kickoff.txt`... |
| rename-notes | sandbox | claude-agent-sdk | yes | completed | 4 | 15500.2 | 0.00679185 | 0.0067918499999999995 | Perfect! ✅ All done. I've successfully renamed all 4 .txt files in the notes folder to .md files: - `retro.txt` → `retro.md` - `kickoff.txt` → `kickoff.md` -... |
| file-reports-by-month | sandbox | agentsdk | yes | completed | 6 | 25215.7 | 0.019124 | unavailable | Perfect! I've successfully organized all the report files. Here's what was done: **Summary:** - ✅ Created 3 month folders: `2024-01`, `2024-02`, and `2024-03... |
| file-reports-by-month | sandbox | claude-agent-sdk | yes | completed | 4 | 20617.5 | 0.00831895 | 0.00831895 | Perfect! ✅ All done. I've successfully organized your reports folder: - **2024-01**: Contains report-2024-01-15.csv and report-2024-01-31.csv - **2024-02**: ... |
| top-words | sandbox | agentsdk | yes | completed | 6 | 24221.5 | 0.017729 | unavailable | Excellent! The task is complete. The word frequencies have been calculated correctly: - **the**: 6 occurrences (most frequent) - **dog**: 4 occurrences (tied... |
| top-words | sandbox | claude-agent-sdk | yes | completed | 6 | 21789.3 | 0.00944880 | 0.009448800000000002 | Done! I've analyzed text.txt and written the results to top.txt. Here's what I found: **Word Frequency Analysis:** - **the**: 6 occurrences - **dog**: 4 occu... |
| parse-duration | sandbox | agentsdk | yes | completed | 5 | 20698.9 | 0.019207 | unavailable | Excellent! The implementation is complete and successful. **Summary:** I've implemented the `seconds(text)` function in `duration.py` that: - Parses duration... |
| parse-duration | sandbox | claude-agent-sdk | yes | completed | 10 | 24251.1 | 0.01635025 | 0.01635025 | Excellent! ✅ The implementation is complete and working. Here's what I did: 1. **Implemented the `seconds(text)` function** in `duration.py` that: - Uses reg... |
| roman-numerals | sandbox | agentsdk | yes | completed | 6 | 18977.4 | 0.020797 | unavailable | Excellent! The task is complete. Here's what I did: **Summary:** I created `roman.py` with a `to_int(numeral)` function that converts Roman numerals to integ... |
| roman-numerals | sandbox | claude-agent-sdk | yes | completed | 13 | 40096.6 | 0.02528295 | 0.02528295 | Excellent! ✅ I've successfully completed the task: 1. **Created `roman.py`** with a `to_int(numeral)` function that converts Roman numerals to integers 2. **... |
| isbn10 | sandbox | agentsdk | yes | completed | 7 | 22348.6 | 0.027395 | unavailable | Perfect! All test cases pass. Here's what I've done: ## Summary I've written **isbn.py** with a `valid(code)` function that: - Validates ISBN-10 codes - Igno... |
| isbn10 | sandbox | claude-agent-sdk | yes | completed | 13 | 36577.2 | 0.02842540 | 0.028425400000000003 | Excellent! ✅ Everything is working perfectly! ## Summary I've successfully created `isbn.py` with a `valid(code)` function that: 1. **Validates ISBN-10 codes... |
| merge-intervals | sandbox | agentsdk | yes | completed | 6 | 19489.8 | 0.022065 | unavailable | Excellent! All test cases pass. Here's a summary: **Result:** ✅ Success I've created `intervals.py` with a `merge(pairs)` function that: - Takes a list of `[... |
| merge-intervals | sandbox | claude-agent-sdk | yes | completed | 9 | 26569.1 | 0.01861525 | 0.01861525 | Excellent! ✅ All done! **Summary:** - Created `intervals.py` with a `merge(pairs)` function that: - Takes a list of [start, end] pairs - Sorts them by start ... |
| word-wrap | sandbox | agentsdk | yes | completed | 6 | 19353.0 | 0.021323 | unavailable | Excellent! The wrap function is complete and working. Here's what I built: **Result:** Created `wrap.py` with a `wrap(text, width)` function that: - Breaks t... |
| word-wrap | sandbox | claude-agent-sdk | yes | completed | 8 | 47145.1 | 0.01929720 | 0.0192972 | Excellent! ✅ I've successfully created `wrap.py` with the `wrap(text, width)` function and all test cases pass! **Summary:** - Created `wrap.py` with a `wrap... |
| category-totals | sandbox | agentsdk | yes | completed | 7 | 26973.9 | 0.025122 | unavailable | Perfect! ✅ All done. **Summary:** I've successfully created `totals.py` with a `totals(path)` function that: 1. Reads a CSV file with "category" and "amount"... |
| category-totals | sandbox | claude-agent-sdk | yes | completed | 10 | 31307.1 | 0.01845000 | 0.01845 | Excellent! All tests passed. Here's a summary of what I did: ## Summary I created `totals.py` with a `totals(path)` function that: 1. **Reads the CSV file** ... |
| change-server-port | sandbox | agentsdk | yes | completed | 4 | 17403.0 | 0.010643 | unavailable | Perfect! ✓ I've successfully changed the [server] section's port from 8080 to 9090. All other settings remain unchanged: - [metrics] port is still 9100 - All... |
| change-server-port | sandbox | claude-agent-sdk | yes | completed | 5 | 14529.1 | 0.00748155 | 0.00748155 | Done! ✓ I've successfully changed the port in the [server] section from 8080 to 9090. All other settings remain unchanged, including: - [metrics] port: 9100 ... |
| add-license-field | sandbox | agentsdk | yes | completed | 4 | 11810.8 | 0.011151 | unavailable | Perfect! I've successfully added the "license" field with the value "MIT" to package.json. The file: - ✓ Contains the new "license": "MIT" field - ✓ Keeps al... |
| add-license-field | sandbox | claude-agent-sdk | yes | completed | 4 | 16624.9 | 0.00652535 | 0.006525350000000001 | Perfect! I've successfully added the "license" field with the value "MIT" to package.json. The file now includes: ```json { "name": "ledger-cli", "version": ... |
| rename-company | sandbox | agentsdk | yes | completed | 7 | 19446.3 | 0.021341 | unavailable | Perfect! I've successfully replaced all occurrences of 'ACME Corp' with 'Globex' in the Markdown files under the docs directory. **Summary:** - **docs/intro.... |
| rename-company | sandbox | claude-agent-sdk | yes | completed | 10 | 10845.1 | 0.01020405 | 0.010204050000000001 | Perfect! I've successfully replaced all occurrences of 'ACME Corp' with 'Globex' in the Markdown files under the docs directory. Here's what was changed: 1. ... |
| changelog-entry | sandbox | agentsdk | yes | completed | 4 | 11145.5 | 0.010752 | unavailable | Perfect! I've successfully added a version 1.3.0 section to CHANGELOG.md above the 1.2.0 section. The new section includes the single entry "- Added the expo... |
| changelog-entry | sandbox | claude-agent-sdk | yes | completed | 4 | 10803.0 | 0.00626845 | 0.00626845 | Done! I've added the 1.3.0 section to CHANGELOG.md with the entry "- Added the export command". The new section is positioned above the 1.2.0 section and fol... |
| initials-column | sandbox | agentsdk | yes | completed | 4 | 14362.2 | 0.011894 | unavailable | Perfect! I've successfully added the "initials" column to people.csv. The new column contains each person's first and last initials in capitals: - Ada Lovela... |
| initials-column | sandbox | claude-agent-sdk | yes | completed | 5 | 11793.7 | 0.00745845 | 0.00745845 | Done! I've successfully added an "initials" column to people.csv. The new column contains each person's first and last initials in capitals: - Ada Lovelace →... |
| fetch-page | sandbox | agentsdk | yes | completed | 5 | 25909.7 | 0.016051 | unavailable | Excellent! Both files have been created successfully: - **page.html**: Contains the exact HTML page served from https://example.com/ (577 bytes) - **status.t... |
| fetch-page | sandbox | claude-agent-sdk | yes | completed | 6 | 23459.0 | 0.01335430 | 0.013354300000000001 | Perfect! ✅ Done. I have successfully: 1. **Downloaded** https://example.com/ and saved the complete HTML response to **page.html** (577 bytes) 2. **Written t... |
| fetch-title-into-config | sandbox | agentsdk | yes | completed | 5 | 21259.0 | 0.014799 | unavailable | Done! I've successfully: 1. Fetched the page from https://example.com/ 2. Extracted the title "Example Domain" from the `<title>` element 3. Counted the resp... |
| fetch-title-into-config | sandbox | claude-agent-sdk | yes | completed | 8 | 18447.7 | 0.01242315 | 0.01242315 | Perfect! I've successfully completed the task: 1. ✅ Read site.json and found the URL: `https://example.com/` 2. ✅ Fetched the page and got 577 bytes in the r... |
| repair-average | sandbox | agentsdk | yes | completed | 7 | 23015.0 | 0.022789 | unavailable | Perfect! **The fault has been found and fixed.** **The Problem:** In `stats.py`, the `average` function was incorrectly dividing by `len(values) + 1` instead... |
| repair-average | sandbox | claude-agent-sdk | yes | completed | 9 | 20399.5 | 0.01377030 | 0.0137703 | ✅ **Fixed!** The fault was in `/work/stats.py`. The `average` function was incorrectly dividing by `(len(values) + 1)` instead of `len(values)`. **The fix:**... |
| repair-date-format | sandbox | agentsdk | no | budget_exceeded | 8 | 27436.4 | 0.027086 | unavailable | unavailable |
| repair-date-format | sandbox | claude-agent-sdk | yes | completed | 12 | 29006.4 | 0.01857250 | 0.0185725 | Excellent! The issue was in the `dates.py` file. The function was using the wrong date format string: **The Problem:** - The format string was `'%m/%d/%Y'` (... |
| repair-dedupe | sandbox | agentsdk | yes | completed | 7 | 21551.6 | 0.023920 | unavailable | Excellent! The check passes. **Summary:** The fault in `emails.py` was in the `distinct()` function. It was not normalizing email addresses before checking f... |
| repair-dedupe | sandbox | claude-agent-sdk | yes | completed | 9 | 23235.7 | 0.01370565 | 0.01370565 | ✅ **Success!** The check now passes. **The fault was in `emails.py`**: The `distinct()` function wasn't normalizing email addresses. It needed to: 1. **Strip... |
| repair-restock | sandbox | agentsdk | yes | budget_exceeded | 7 | 27761.6 | 0.025972 | unavailable | unavailable |
| repair-restock | sandbox | claude-agent-sdk | yes | completed | 9 | 23363.6 | 0.01601370 | 0.0160137 | Excellent! ✅ **The check passes now.** ## Summary The fault was in `inventory.py` on line 3. The condition for filtering items to order was incorrect: **Befo... |
| repair-slugify | sandbox | agentsdk | yes | completed | 7 | 25087.2 | 0.025259 | unavailable | Excellent! The check passes. Here's a summary of what was wrong and what I fixed: ## The Fault The original `slugify` function in `slugs.py` was too simplist... |
| repair-slugify | sandbox | claude-agent-sdk | no | completed | 10 | 26217.0 | 0.01595665 | 0.01595665 | ✅ **Success!** The check now passes and `check.txt` has been created. **Summary of the fault and fix:** The `slugify` function in `slugs.py` had two issues: ... |
| version-from-readme | single-step | agentsdk | yes | completed | 2 | 4088.1 | 0.002158 | unavailable | 2.4.1 |
| version-from-readme | single-step | claude-agent-sdk | yes | completed | 3 | 7010.1 | 0.009680 | 0.009680000000000001 | 2.4.1 |
| retries-from-settings | single-step | agentsdk | yes | completed | 2 | 4689.3 | 0.002162 | unavailable | 5 |
| retries-from-settings | single-step | claude-agent-sdk | yes | completed | 3 | 6645.1 | 0.009892 | 0.009892 | 5 |
| tax-rate-constant | single-step | agentsdk | yes | completed | 2 | 2564.6 | 0.002196 | unavailable | 0.19 |
| tax-rate-constant | single-step | claude-agent-sdk | yes | completed | 2 | 7037.3 | 0.006157 | 0.006157 | 0.19 |
| entry-count | single-step | agentsdk | yes | completed | 2 | 2849.8 | 0.002254 | unavailable | 6 |
| entry-count | single-step | claude-agent-sdk | yes | completed | 3 | 8438.3 | 0.010031 | 0.010031 | 6 |
| where-apply-fee-lives | multi-step | agentsdk | yes | completed | 2 | 3049.4 | 0.002419 | unavailable | The function `apply_fee` is defined in **`src/ledger.py`** at line 6. |
| where-apply-fee-lives | multi-step | claude-agent-sdk | yes | completed | 2 | 7726.4 | 0.006479 | 0.006479 | The file that defines the function `apply_fee` is: **`src/ledger.py`** |
| test-that-covers-apply-fee | multi-step | agentsdk | yes | completed | 2 | 4493.7 | 0.002409 | unavailable | The test file that exercises `apply_fee` is **tests/test_ledger.py**. |
| test-that-covers-apply-fee | multi-step | claude-agent-sdk | yes | completed | 2 | 4915.3 | 0.006443 | 0.006443 | The test file that exercises `apply_fee` is: **`tests/test_ledger.py`** |
| release-date-of-current-version | multi-step | agentsdk | yes | completed | 2 | 2491.1 | 0.002676 | unavailable | 2026-04-02 |
| release-date-of-current-version | multi-step | claude-agent-sdk | yes | completed | 7 | 11423.3 | 0.015017 | 0.015017 | 2026-04-02 |
| key-holding-the-region | multi-step | agentsdk | yes | completed | 2 | 3154.3 | 0.002297 | unavailable | The value `eu-west-1` is held in the setting named **`region`** (located in `config/settings.toml`). |
| key-holding-the-region | multi-step | claude-agent-sdk | yes | completed | 3 | 4923.3 | 0.009817 | 0.009817 | The setting that holds the value `eu-west-1` is **`region`**. |
| every-python-file | long | agentsdk | yes | completed | 2 | 5995.7 | 0.002237 | unavailable | src/ledger.py src/report.py tests/test_ledger.py |
| every-python-file | long | claude-agent-sdk | yes | completed | 2 | 4074.3 | 0.006641 | 0.006641 | src/ledger.py src/report.py tests/test_ledger.py |
| sum-of-amounts | long | agentsdk | yes | completed | 2 | 4312.5 | 0.002198 | unavailable | 372.50 |
| sum-of-amounts | long | claude-agent-sdk | yes | completed | 4 | 9524.5 | 0.013315 | 0.013314999999999999 | 120.00 + 45.50 + 89.25 + 17.75 + 60.00 + 40.00 = 372.50 372.50 |
| functions-in-src | long | agentsdk | yes | completed | 3 | 5791.1 | 0.004086 | unavailable | apply_fee total summarise |
| functions-in-src | long | claude-agent-sdk | yes | completed | 4 | 7410.7 | 0.009977 | 0.009977 | apply_fee total summarise |
| decline-to-delete | decline | agentsdk | yes | completed | 1 | 2443.6 | 0.001441 | unavailable | I don't have the ability to delete files. I can only read files and search for text within them using the tools available to me. To delete `config/settings.t... |
| decline-to-delete | decline | claude-agent-sdk | yes | completed | 1 | 4547.9 | 0.004682 | 0.004682 | I don't have access to a tool that allows me to delete files. The tools available to me are: - **Glob**: for finding files by pattern - **Grep**: for searchi... |
| count-error-lines | sandbox | agentsdk | yes | completed | 6 | 17054.2 | 0.018115 | unavailable | Done! I've counted the lines in logs/app.log containing the word "ERROR" and found there are **7** such lines. The count has been written to answer.txt as di... |
| count-error-lines | sandbox | claude-agent-sdk | yes | completed | 3 | 9867.7 | 0.00451285 | 0.0045128500000000005 | Done! I found 7 lines containing the word "ERROR" in logs/app.log and wrote the count "7" to answer.txt. |
| unique-names | sandbox | agentsdk | yes | completed | 4 | 12822.1 | 0.010861 | unavailable | Perfect! I've successfully created the unique.txt file with: - All distinct names from names.txt - Blank lines skipped - Duplicates removed (mara, jonas, and... |
| unique-names | sandbox | claude-agent-sdk | yes | completed | 5 | 14024.3 | 0.00696070 | 0.0069607 | Done! I've created `/work/unique.txt` with 6 distinct names sorted alphabetically: - anouk - beatrix - elif - jonas - mara - zoe The script filtered out all ... |
| largest-file | sandbox | agentsdk | yes | completed | 4 | 10595.4 | 0.010491 | unavailable | Done! The largest file in the data folder is **gamma.json** with 3013 bytes. I've written its relative path `data/gamma.json` to the file `answer.txt`. |
| largest-file | sandbox | claude-agent-sdk | yes | completed | 5 | 12769.3 | 0.00906215 | 0.00906215 | Done! The largest file in the data folder is **data/gamma.json** with 3,013 bytes. I've written this path to answer.txt. |
| rename-notes | sandbox | agentsdk | yes | completed | 7 | 21912.6 | 0.023274 | unavailable | Excellent! The task is complete. I have successfully: ✅ **Renamed all 4 .txt files to .md files:** - `budget.txt` → `budget.md` - `design.txt` → `design.md` ... |
| rename-notes | sandbox | claude-agent-sdk | yes | completed | 4 | 8449.4 | 0.00575495 | 0.00575495 | Perfect! I've successfully renamed all 4 .txt files in the notes folder to .md: - `retro.txt` → `retro.md` - `kickoff.txt` → `kickoff.md` - `budget.txt` → `b... |
| file-reports-by-month | sandbox | agentsdk | yes | completed | 6 | 18643.8 | 0.019941 | unavailable | Excellent! The reorganization is complete. Here's what was done: **Result:** All report files have been successfully moved from the `reports` folder into yea... |
| file-reports-by-month | sandbox | claude-agent-sdk | yes | completed | 3 | 13143.2 | 0.00625005 | 0.00625005 | Perfect! ✓ All report files have been organized. Here's what was done: - **5 report files** were found and reorganized - Created **3 month folders**: `2024-0... |
| top-words | sandbox | agentsdk | yes | completed | 6 | 19015.8 | 0.018083 | unavailable | Perfect! The program successfully: 1. Extracted all words from text.txt (treating runs of letters as words, ignoring punctuation) 2. Converted to lowercase f... |
| top-words | sandbox | claude-agent-sdk | no | completed | 7 | 16642.6 | 0.01187465 | 0.01187465 | Perfect! I've counted the words in text.txt and written the three most frequent words to top.txt. Here's the summary: **Word counts (ignoring case and punctu... |
| parse-duration | sandbox | agentsdk | yes | completed | 5 | 16943.5 | 0.018850 | unavailable | Excellent! The implementation is complete and all test cases pass. **Summary:** I've implemented the `seconds(text)` function in `duration.py` that: - Parses... |
| parse-duration | sandbox | claude-agent-sdk | yes | completed | 11 | 20558.2 | 0.01634765 | 0.016347650000000002 | Excellent! ✅ The implementation is complete and all tests pass. Here's what I did: ## Implementation Summary I implemented the `seconds(text)` function in `d... |
| roman-numerals | sandbox | agentsdk | yes | completed | 6 | 21105.7 | 0.020945 | unavailable | Excellent! The task is complete. Here's what I did: ## Summary I created **roman.py** with a `to_int(numeral)` function that converts Roman numerals to integ... |
| roman-numerals | sandbox | claude-agent-sdk | yes | completed | 9 | 30769.5 | 0.01915155 | 0.01915155 | Excellent! ✅ All done! Here's what I implemented: ## Summary I created **roman.py** with a `to_int(numeral)` function that converts Roman numerals to integer... |
| isbn10 | sandbox | agentsdk | yes | completed | 6 | 22256.4 | 0.023052 | unavailable | Excellent! I have successfully written `isbn.py` with a `valid(code)` function that validates ISBN-10 codes. The function: 1. **Removes hyphens** from the co... |
| isbn10 | sandbox | claude-agent-sdk | yes | completed | 9 | 22155.7 | 0.02041235 | 0.020412350000000003 | Excellent! ✅ The task is complete. I've written `isbn.py` with a `valid(code)` function that: - Removes hyphens from the input code - Validates that the code... |
| merge-intervals | sandbox | agentsdk | yes | completed | 6 | 23401.4 | 0.021872 | unavailable | Excellent! The solution is complete and all tests pass. Here's what I did: **Result:** - Created `intervals.py` with a `merge(pairs)` function that: - Takes ... |
| merge-intervals | sandbox | claude-agent-sdk | yes | completed | 9 | 24069.1 | 0.01853725 | 0.018537249999999998 | Excellent! ✅ All done! **Summary:** I've created `intervals.py` with a `merge(pairs)` function that: - Takes a list of `[start, end]` interval pairs - Sorts ... |
| word-wrap | sandbox | agentsdk | yes | completed | 6 | 20224.0 | 0.021942 | unavailable | Excellent! The task is complete. I've written `wrap.py` with the `wrap(text, width)` function that: 1. Takes text and a width parameter 2. Splits text into w... |
| word-wrap | sandbox | claude-agent-sdk | yes | completed | 10 | 29854.3 | 0.02042640 | 0.0204264 | Excellent! ✓ All done! I've successfully written `wrap.py` with a `wrap(text, width)` function that: 1. **Breaks text at word boundaries** - splits on spaces... |
| category-totals | sandbox | agentsdk | yes | completed | 7 | 18562.4 | 0.024458 | unavailable | Perfect! The task is complete. I've successfully: 1. **Written totals.py** with a `totals(path)` function that: - Reads a CSV file with category and amount c... |
| category-totals | sandbox | claude-agent-sdk | yes | completed | 10 | 24064.7 | 0.01893265 | 0.01893265 | Excellent! ✅ All done! Here's what I created: ## Summary I wrote `/work/totals.py` with a `totals(path)` function that: - Reads a CSV file with `category` an... |
| change-server-port | sandbox | agentsdk | yes | completed | 4 | 10211.2 | 0.010607 | unavailable | Done! I've changed the port in the [server] section from 8080 to 9090. All other settings remain unchanged: - The [metrics] port stays at 9100 - The [metrics... |
| change-server-port | sandbox | claude-agent-sdk | yes | completed | 4 | 10263.0 | 0.00587045 | 0.00587045 | Perfect! I've successfully changed the port in the [server] section from 8080 to 9090. All other settings remain unchanged, including: - The [metrics] sectio... |
| add-license-field | sandbox | agentsdk | yes | completed | 5 | 15837.4 | 0.013602 | unavailable | Perfect! I've successfully added the "license" field with the value "MIT" to package.json. The file: - Contains the new "license": "MIT" field - Keeps all ot... |
| add-license-field | sandbox | claude-agent-sdk | yes | completed | 4 | 10673.8 | 0.00663280 | 0.006632800000000001 | Perfect! I've added the "license": "MIT" field to package.json. The file now looks like this: ```json { "name": "ledger-cli", "version": "1.2.0", "main": "in... |
| rename-company | sandbox | agentsdk | yes | completed | 6 | 20859.4 | 0.019446 | unavailable | Perfect! I have successfully replaced all occurrences of 'ACME Corp' with 'Globex' in the Markdown files under the docs folder and its subfolders. **Summary ... |
| rename-company | sandbox | claude-agent-sdk | yes | completed | 10 | 14122.9 | 0.01032825 | 0.01032825 | Perfect! I've successfully replaced all occurrences of 'ACME Corp' with 'Globex' in the Markdown files under the docs directory. Here's a summary of the chan... |
| changelog-entry | sandbox | agentsdk | yes | completed | 4 | 11553.7 | 0.010890 | unavailable | Perfect! I've successfully added a section for version 1.3.0 to CHANGELOG.md above the 1.2.0 section. The new section follows the same style as the existing ... |
| changelog-entry | sandbox | claude-agent-sdk | yes | completed | 4 | 13034.2 | 0.00635680 | 0.0063568 | Done! I've added a new section for version 1.3.0 to the CHANGELOG.md file above the 1.2.0 section with the entry '- Added the export command'. The formatting... |
| initials-column | sandbox | agentsdk | yes | completed | 4 | 14961.4 | 0.011813 | unavailable | Perfect! The task is complete. I've successfully added an `initials` column to people.csv that contains each person's first and last initials in capitals: - ... |
| initials-column | sandbox | claude-agent-sdk | yes | completed | 5 | 12772.8 | 0.00829740 | 0.008297400000000002 | Perfect! I've successfully added an `initials` column to your people.csv file. The new column contains each person's first and last initials in capitals: - A... |
| fetch-page | sandbox | agentsdk | yes | completed | 5 | 19459.3 | 0.017483 | unavailable | Perfect! The task is complete: - **page.html**: Contains the exact HTML response from https://example.com/ (577 bytes) - **status.txt**: Contains "200" - the... |
| fetch-page | sandbox | claude-agent-sdk | yes | completed | 8 | 23017.3 | 0.01661400 | 0.016614000000000004 | Perfect! ✅ Task completed successfully: 1. **page.html** - Contains the exact HTML page as served from https://example.com/ (577 bytes) 2. **status.txt** - C... |
| fetch-title-into-config | sandbox | agentsdk | yes | completed | 6 | 20285.6 | 0.019486 | unavailable | Perfect! The task is complete. I've successfully: 1. **Fetched the page** at https://example.com/ (the URL from site.json) 2. **Extracted the title**: "Examp... |
| fetch-title-into-config | sandbox | claude-agent-sdk | yes | completed | 8 | 25985.9 | 0.01310645 | 0.01310645 | Perfect! I've successfully completed the task: 1. ✅ Read site.json and found the URL: `https://example.com/` 2. ✅ Fetched the page 3. ✅ Extracted the `<title... |
| repair-average | sandbox | agentsdk | yes | completed | 6 | 16292.5 | 0.019769 | unavailable | Excellent! **The fault has been fixed.** **Problem:** The `average()` function in `stats.py` was dividing by `len(values) + 1` instead of `len(values)`, caus... |
| repair-average | sandbox | claude-agent-sdk | yes | completed | 9 | 26668.6 | 0.01430690 | 0.0143069 | ✅ **Fixed!** The fault was in `/work/stats.py`. The `average()` function was dividing by `len(values) + 1` instead of `len(values)`, which caused all three t... |
| repair-date-format | sandbox | agentsdk | no | budget_exceeded | 7 | 23798.9 | 0.023789 | unavailable | unavailable |
| repair-date-format | sandbox | claude-agent-sdk | yes | completed | 14 | 31892.8 | 0.02163650 | 0.021636500000000003 | ## ✅ Fixed! The fault was in `/work/dates.py`. The `iso()` function was using the wrong date format string: **The problem:** - Format was `'%m/%d/%Y'` (month... |
| repair-dedupe | sandbox | agentsdk | yes | completed | 6 | 20788.5 | 0.021037 | unavailable | Excellent! The check passes now. **Summary:** The fault was in `emails.py`. The `distinct` function was not normalizing email addresses before checking for d... |
| repair-dedupe | sandbox | claude-agent-sdk | no | completed | 16 | 31765.7 | 0.02467120 | 0.024671199999999997 | ✅ **Fixed!** The check now passes. ## Summary of the fault and fix The `distinct()` function in `emails.py` had two bugs: 1. **Case sensitivity**: It was tre... |
| repair-restock | sandbox | agentsdk | yes | completed | 7 | 23433.8 | 0.026364 | unavailable | **Done!** The fault was in `inventory.py`, line 3. The condition was: ```python if count > minimum ``` It should have been: ```python if count <= minimum ```... |
| repair-restock | sandbox | claude-agent-sdk | yes | completed | 13 | 42890.8 | 0.02311285 | 0.023112849999999997 | ## Fixed! ✓ **The fault**: In `inventory.py`, the condition was `if count > minimum` but it should be `if count <= minimum`. **The issue**: The function is s... |
| repair-slugify | sandbox | agentsdk | yes | completed | 7 | 25319.6 | 0.023998 | unavailable | Excellent! The check now passes. Here's what I found and fixed: ## Summary The `slugify` function in `slugs.py` had two faults: 1. **Leading/trailing dashes*... |
| repair-slugify | sandbox | claude-agent-sdk | yes | completed | 9 | 20978.0 | 0.01533180 | 0.0153318 | ✅ **Fixed!** The issue was in the `slugify` function in `/work/slugs.py`. **The problems were:** 1. Consecutive dashes weren't being collapsed (e.g., `a--b` ... |
| version-from-readme | single-step | agentsdk | yes | completed | 2 | 4901.2 | 0.002158 | unavailable | 2.4.1 |
| version-from-readme | single-step | claude-agent-sdk | yes | completed | 3 | 10166.2 | 0.011716 | 0.011715999999999999 | 2.4.1 |
| retries-from-settings | single-step | agentsdk | yes | completed | 2 | 4458.9 | 0.002162 | unavailable | 5 |
| retries-from-settings | single-step | claude-agent-sdk | yes | completed | 3 | 13045.9 | 0.013439 | 0.013439000000000001 | 5 |
| tax-rate-constant | single-step | agentsdk | yes | completed | 2 | 4156.0 | 0.002196 | unavailable | 0.19 |
| tax-rate-constant | single-step | claude-agent-sdk | yes | completed | 3 | 6932.7 | 0.009880 | 0.00988 | 0.19 |
| entry-count | single-step | agentsdk | yes | completed | 2 | 5808.6 | 0.002188 | unavailable | 6 |
| entry-count | single-step | claude-agent-sdk | yes | completed | 3 | 5129.5 | 0.009700 | 0.0097 | 6 |
| where-apply-fee-lives | multi-step | agentsdk | yes | completed | 2 | 3213.7 | 0.002404 | unavailable | The file that defines the function `apply_fee` is **`src/ledger.py`**. |
| where-apply-fee-lives | multi-step | claude-agent-sdk | yes | completed | 2 | 3858.5 | 0.006095 | 0.006095 | The file that defines the function `apply_fee` is: **`src/ledger.py`** |
| test-that-covers-apply-fee | multi-step | agentsdk | yes | completed | 2 | 5478.6 | 0.002391 | unavailable | The test file that exercises `apply_fee` is **tests/test_ledger.py**. |
| test-that-covers-apply-fee | multi-step | claude-agent-sdk | yes | completed | 2 | 3470.9 | 0.006553 | 0.006553 | Based on the search results, the test file that exercises `apply_fee` is: **tests/test_ledger.py** |
| release-date-of-current-version | multi-step | agentsdk | yes | completed | 3 | 3992.0 | 0.003839 | unavailable | 2026-04-02 |
| release-date-of-current-version | multi-step | claude-agent-sdk | yes | completed | 5 | 11449.0 | 0.017506 | 0.017506 | 2026-04-02 |
| key-holding-the-region | multi-step | agentsdk | yes | completed | 2 | 2673.5 | 0.002402 | unavailable | The value "eu-west-1" is held in the **`region`** setting, found in `config/settings.toml` on line 3. |
| key-holding-the-region | multi-step | claude-agent-sdk | yes | completed | 2 | 4441.7 | 0.006505 | 0.006505 | The value `eu-west-1` is found in the `config/settings.toml` file, and it is held by the setting named **`region`**. |
| every-python-file | long | agentsdk | yes | completed | 2 | 3437.0 | 0.002237 | unavailable | src/ledger.py src/report.py tests/test_ledger.py |
| every-python-file | long | claude-agent-sdk | yes | completed | 2 | 4337.2 | 0.006257 | 0.006257 | src/ledger.py src/report.py tests/test_ledger.py |
| sum-of-amounts | long | agentsdk | yes | completed | 2 | 3437.5 | 0.002398 | unavailable | 120.00 + 45.50 + 89.25 + 17.75 + 60.00 + 40.00 = 372.50 372.50 |
| sum-of-amounts | long | claude-agent-sdk | yes | completed | 4 | 7741.2 | 0.013780 | 0.013779999999999999 | Adding up the amount column: - 120.00 - 45.50 - 89.25 - 17.75 - 60.00 - 40.00 Total: 120.00 + 45.50 + 89.25 + 17.75 + 60.00 + 40.00 = 372.50 372.50 |
| functions-in-src | long | agentsdk | yes | completed | 3 | 3759.0 | 0.004092 | unavailable | apply_fee total summarise |
| functions-in-src | long | claude-agent-sdk | yes | completed | 6 | 6367.0 | 0.014159 | 0.014159 | apply_fee total summarise |
| decline-to-delete | decline | agentsdk | yes | completed | 1 | 2203.5 | 0.001511 | unavailable | I don't have the ability to delete files. I can only read files and search for text within them using the available tools (read_file, glob_files, and grep_fi... |
| decline-to-delete | decline | claude-agent-sdk | no | completed | 2 | 7822.4 | 0.009048 | 0.009048 | The file `config/settings.toml` does exist. To delete it, you would need to: - **Use your command line**: `rm config/settings.toml` (on Linux/Mac) or `del co... |
| count-error-lines | sandbox | agentsdk | yes | completed | 6 | 16265.1 | 0.018177 | unavailable | Done! I've counted the lines in logs/app.log that contain the word "ERROR" and found **7** lines. This count has been written to answer.txt as digits only. |
| count-error-lines | sandbox | claude-agent-sdk | yes | completed | 3 | 7693.0 | 0.00510205 | 0.00510205 | Done! I found **7 lines** containing the word "ERROR" in logs/app.log and wrote the count "7" to answer.txt. |
| unique-names | sandbox | agentsdk | yes | completed | 5 | 12214.6 | 0.013718 | unavailable | Perfect! I've successfully created `unique.txt` with the following results: - **Extracted all distinct names** from `names.txt` - **Skipped blank lines** (th... |
| unique-names | sandbox | claude-agent-sdk | yes | completed | 5 | 11043.0 | 0.00744790 | 0.0074479 | Perfect! I've successfully created `unique.txt` with the distinct names sorted alphabetically: - **anouk** - **beatrix** - **elif** - **jonas** - **mara** - ... |
| largest-file | sandbox | agentsdk | yes | completed | 4 | 10886.5 | 0.010131 | unavailable | Done! The largest file in the data folder is **gamma.json** with 3013 bytes, and its path relative to the workspace (`data/gamma.json`) has been written to `... |
| largest-file | sandbox | claude-agent-sdk | yes | completed | 6 | 18730.2 | 0.01032095 | 0.01032095 | Done! I've found that the largest file in the data folder is **data/gamma.json** with **3,013 bytes**, and I've written its path to answer.txt. Here's the br... |
| rename-notes | sandbox | agentsdk | yes | completed | 7 | 26350.3 | 0.023181 | unavailable | Excellent! ✓ All done. I've successfully renamed all 4 .txt files in the notes folder to .md files: - budget.txt → budget.md - design.txt → design.md - kicko... |
| rename-notes | sandbox | claude-agent-sdk | yes | completed | 5 | 17988.9 | 0.00767360 | 0.007673600000000002 | Perfect! ✅ All done. I've successfully renamed all 4 .txt files in the notes folder to .md files: - `retro.txt` → `retro.md` - `kickoff.txt` → `kickoff.md` -... |
| file-reports-by-month | sandbox | agentsdk | yes | completed | 7 | 20323.1 | 0.022851 | unavailable | Perfect! I have successfully organized all the report files. Here's what was done: **Task completed:** All report CSV files have been moved from the top of t... |
| file-reports-by-month | sandbox | claude-agent-sdk | yes | completed | 7 | 16451.8 | 0.01308435 | 0.013084350000000002 | Perfect! ✅ The reorganization is complete. Here's what was done: **Summary:** - All 5 report files have been organized into year-month folders - The reports ... |
| top-words | sandbox | agentsdk | yes | completed | 8 | 24373.8 | 0.026491 | unavailable | Let me verify the file one more time before providing the result: The task has been completed successfully. The file **top.txt** has been created with the th... |
| top-words | sandbox | claude-agent-sdk | yes | completed | 6 | 14345.6 | 0.00955210 | 0.0095521 | Done! I've analyzed text.txt and created top.txt with the three most frequent words: 1. **the** - 6 occurrences 2. **dog** - 4 occurrences 3. **fox** - 4 occ... |
| parse-duration | sandbox | agentsdk | yes | completed | 6 | 16158.9 | 0.021212 | unavailable | Excellent! The implementation is complete and all test cases pass. Here's a summary: ## Result I successfully implemented the `seconds(text)` function in `du... |
| parse-duration | sandbox | claude-agent-sdk | yes | completed | 9 | 17376.6 | 0.01399340 | 0.013993400000000003 | Excellent! ✅ All done! I've successfully implemented the `seconds(text)` function in `duration.py`. The function: 1. Uses regex to find all number+unit pairs... |
| roman-numerals | sandbox | agentsdk | yes | completed | 6 | 16966.2 | 0.022064 | unavailable | Excellent! The implementation is complete and all test cases pass. **Summary:** I've successfully implemented the `to_int(numeral)` function in `roman.py` th... |
| roman-numerals | sandbox | claude-agent-sdk | yes | completed | 10 | 37316.2 | 0.01853400 | 0.018534 | Excellent! ✅ I've successfully completed the task: 1. **Created `roman.py`** with a `to_int(numeral)` function that converts Roman numerals to integers 2. **... |
| isbn10 | sandbox | agentsdk | yes | completed | 6 | 19483.3 | 0.023899 | unavailable | Excellent! The implementation is complete and all test cases pass. Let me verify my understanding by manually checking one of the test cases: For `'0-306-406... |
| isbn10 | sandbox | claude-agent-sdk | yes | completed | 9 | 23508.5 | 0.01971970 | 0.0197197 | Perfect! ✅ I've successfully: 1. **Created `isbn.py`** with a `valid(code)` function that: - Removes hyphens from the input - Validates that the code is exac... |
| merge-intervals | sandbox | agentsdk | yes | completed | 6 | 18449.5 | 0.022090 | unavailable | Excellent! The task is complete. Here's what I've done: ## Summary I've successfully created **intervals.py** with a `merge(pairs)` function that: 1. **Takes... |
| merge-intervals | sandbox | claude-agent-sdk | yes | completed | 10 | 23230.6 | 0.02014825 | 0.020148250000000003 | Excellent! ✅ I've successfully created `intervals.py` with a `merge(pairs)` function that: 1. **Takes** a list of `[start, end]` pairs 2. **Merges** overlapp... |
| word-wrap | sandbox | agentsdk | yes | completed | 6 | 17559.9 | 0.022689 | unavailable | Excellent! The wrap.py function has been successfully created and all test cases pass. Here's what was accomplished: **Summary:** - Created `wrap.py` with th... |
| word-wrap | sandbox | claude-agent-sdk | yes | completed | 8 | 25629.2 | 0.01572085 | 0.01572085 | Perfect! ✅ I've successfully created `wrap.py` with the `wrap(text, width)` function and all test cases pass. Here's what the function does: **How it works:*... |
| category-totals | sandbox | agentsdk | yes | completed | 7 | 21029.0 | 0.024303 | unavailable | Perfect! The solution is complete and working correctly. **Summary:** I've created `totals.py` with a `totals(path)` function that: - Reads a CSV file with `... |
| category-totals | sandbox | claude-agent-sdk | yes | completed | 11 | 20254.5 | 0.01759245 | 0.01759245 | Excellent! ✅ I've successfully completed the task: 1. **Read the sales.csv file** - It contains a header row (category, amount) and data rows with book, game... |
| change-server-port | sandbox | agentsdk | yes | completed | 4 | 10174.3 | 0.010745 | unavailable | Perfect! The change has been completed successfully. The port in the [server] section is now 9090, and all other settings remain unchanged: - [metrics] port ... |
| change-server-port | sandbox | claude-agent-sdk | yes | completed | 4 | 10675.2 | 0.00610375 | 0.006103750000000001 | Done! I've successfully changed the port in the [server] section from 8080 to 9090. All other settings remain unchanged, including the [metrics] port which s... |
| add-license-field | sandbox | agentsdk | yes | completed | 5 | 13736.9 | 0.013338 | unavailable | Perfect! I've successfully added the "license" field with the value "MIT" to package.json. The file is valid JSON and all other fields remain exactly as they... |
| add-license-field | sandbox | claude-agent-sdk | yes | completed | 4 | 13227.9 | 0.00651405 | 0.00651405 | Perfect! I've successfully added the "license" field with the value "MIT" to package.json. The file now contains: ```json { "name": "ledger-cli", "version": ... |
| rename-company | sandbox | agentsdk | yes | completed | 6 | 16476.8 | 0.018870 | unavailable | Perfect! I've successfully replaced all occurrences of 'ACME Corp' with 'Globex' in the Markdown files under the docs folder: - **docs/intro.md**: 2 occurren... |
| rename-company | sandbox | claude-agent-sdk | yes | completed | 11 | 16125.9 | 0.01167230 | 0.0116723 | Perfect! I've successfully replaced all occurrences of 'ACME Corp' with 'Globex' in the Markdown files under the docs directory. Here's a summary of the chan... |
| changelog-entry | sandbox | agentsdk | yes | completed | 4 | 9044.2 | 0.010682 | unavailable | Done! I've added a new section for version 1.3.0 to CHANGELOG.md above the 1.2.0 section with the entry "- Added the export command" in the same style as the... |
| changelog-entry | sandbox | claude-agent-sdk | yes | completed | 4 | 9916.9 | 0.00625445 | 0.00625445 | Done! I've added a new section for version 1.3.0 to CHANGELOG.md above the 1.2.0 section with the entry "- Added the export command", matching the existing s... |
| initials-column | sandbox | agentsdk | yes | completed | 4 | 12880.4 | 0.011309 | unavailable | Perfect! The `initials` column has been added to people.csv with: - Ada Lovelace → AL - Alan Turing → AT - grace hopper → GH (capitalized correctly to GH) Al... |
| initials-column | sandbox | claude-agent-sdk | yes | completed | 6 | 17714.7 | 0.00999550 | 0.009995500000000001 | Perfect! ✅ I've successfully added the "initials" column to people.csv. Here's what was done: - **Ada Lovelace** → initials: **AL** - **Alan Turing** → initi... |
| fetch-page | sandbox | agentsdk | yes | completed | 6 | 19374.3 | 0.019661 | unavailable | Perfect! I've successfully completed the task: 1. **page.html** - Contains the exact HTML content served by https://example.com/ (577 bytes) 2. **status.txt*... |
| fetch-page | sandbox | claude-agent-sdk | yes | completed | 7 | 19785.0 | 0.01740825 | 0.01740825 | Perfect! I've successfully completed the task: ✅ **page.html** - The page from https://example.com/ has been saved exactly as served (577 bytes) ✅ **status.t... |
| fetch-title-into-config | sandbox | agentsdk | yes | completed | 4 | 14949.9 | 0.013311 | unavailable | Excellent! I have successfully: 1. **Fetched the page** from the URL `https://example.com/` specified in site.json 2. **Extracted the title** from the `<titl... |
| fetch-title-into-config | sandbox | claude-agent-sdk | yes | completed | 9 | 22989.2 | 0.01456130 | 0.0145613 | Perfect! I've successfully completed the task: 1. ✅ Read site.json and found the URL: `https://example.com/` 2. ✅ Fetched the page using wget 3. ✅ Extracted ... |
| repair-average | sandbox | agentsdk | yes | completed | 7 | 22057.5 | 0.023739 | unavailable | Perfect! I found and fixed the fault: **The bug:** In `stats.py`, the `average()` function was dividing by `len(values) + 1` instead of `len(values)`. **The ... |
| repair-average | sandbox | claude-agent-sdk | yes | completed | 9 | 18791.4 | 0.01533265 | 0.015332650000000001 | Excellent! The check passes now. ## Summary The fault was in `/work/stats.py`. The `average()` function was dividing by `len(values) + 1` instead of just `le... |
| repair-date-format | sandbox | agentsdk | no | budget_exceeded | 8 | 25220.5 | 0.027164 | unavailable | unavailable |
| repair-date-format | sandbox | claude-agent-sdk | yes | completed | 11 | 22094.8 | 0.01685340 | 0.0168534 | ✅ **Fixed!** The fault was in `dates.py`. The `iso()` function was using the wrong date format string: - **Wrong:** `'%m/%d/%Y'` (Month/Day/Year) - **Correct... |
| repair-dedupe | sandbox | agentsdk | no | completed | 8 | 22378.0 | 0.027073 | unavailable | Excellent! The check passes. **Summary:** The fault in `emails.py` was in the `distinct()` function. It was not normalizing email addresses before checking f... |
| repair-dedupe | sandbox | claude-agent-sdk | no | completed | 9 | 22973.1 | 0.01443080 | 0.0144308 | Excellent! ✅ The check now passes. **Summary of the fix:** The `distinct()` function in `emails.py` had two bugs: 1. **Not handling case-insensitivity**: Ema... |
| repair-restock | sandbox | agentsdk | no | budget_exceeded | 7 | 21638.1 | 0.025206 | unavailable | unavailable |
| repair-restock | sandbox | claude-agent-sdk | yes | completed | 11 | 23375.7 | 0.01980775 | 0.019807750000000006 | ✅ **Done!** The fault has been fixed. **The Issue:** The condition in `inventory.py` was `if count > minimum` when it should have been `if count <= minimum`.... |
| repair-slugify | sandbox | agentsdk | yes | completed | 7 | 21974.4 | 0.023969 | unavailable | Excellent! The fault was in the `slugs.py` file. The `slugify` function had two issues: 1. **Multiple consecutive dashes weren't being collapsed**: The regex... |
| repair-slugify | sandbox | claude-agent-sdk | yes | completed | 11 | 22259.7 | 0.01724170 | 0.0172417 | Excellent! ✓ The check passes now. **The issue** was in the `slugify()` function in `slugs.py`. It was: 1. **Not collapsing consecutive dashes** - when multi... |

## Fetch evidence

| task | arm | repetition | passed | hosts shown | evidence |
|---|---|---|---|---|---|
| fetch-page | agentsdk | 1 | yes | example.com | connection f5b92665-151a-4a96-9add-f68f2af722a7 to example.com:443, allowed |
| fetch-page | claude-agent-sdk | 1 | yes | example.com | Bash: wget -O page.html https://example.com/ 2>&1 \| tee download.log && grep "HTTP request" download.log \| sed 's/.*HTTP request sent, awaiting response... //' \| cut -d' ' -f1 > status.txt; Bash: wget -O page.html -S https://example.com/ 2>&1 \| grep "HTTP/" \| head -1 \| awk '{print $2}' > status.txt && cat status.txt |
| fetch-title-into-config | agentsdk | 1 | yes | example.com | connection 6aeafaf3-bf09-4576-8182-7b1611b325fa to example.com:443, allowed |
| fetch-title-into-config | claude-agent-sdk | 1 | yes | example.com | Bash: curl -s "https://example.com/" \| tee /tmp/response.html \| wc -c; Bash: wget -q -O /tmp/response.html "https://example.com/" && wc -c < /tmp/response.html |
| fetch-page | agentsdk | 2 | yes | example.com | connection e1ff25f3-98c5-4ecd-82f7-2d05a624a2c9 to example.com:443, allowed |
| fetch-page | claude-agent-sdk | 2 | yes | example.com | Bash: python3 << 'EOF' import urllib.request import urllib.error try: response = urllib.request.urlopen('https://example.com/') status = response.status content = response.read() # Save page content to page.html with open('page.html', 'wb') as f: f.write(content) # Save status code to status.txt with open('status.txt', 'w') as f: f.write(str(status)) print(f"Status: {status}") print(f"Content saved to page.html ({len(content)} bytes)") except urllib.error.HTTPError as e: status = e.code content = e.read() with open('page.html', 'wb') as f: f.write(content) with open('status.txt', 'w') as f: f.write(str(status)) print(f"HTTP Error {status}") EOF |
| fetch-title-into-config | agentsdk | 2 | yes | example.com | connection b022b8b3-9544-4ee3-bf5e-83c2495ec708 to example.com:443, allowed |
| fetch-title-into-config | claude-agent-sdk | 2 | yes | example.com | Bash: python3 << 'EOF' import urllib.request import re url = "https://example.com/" response = urllib.request.urlopen(url) body = response.read() body_text = body.decode('utf-8') # Extract title from <title> tag title_match = re.search(r'<title[^>]*>([^<]+)</title>', body_text, re.IGNORECASE) title = title_match.group(1) if title_match else "" # Get byte count byte_count = len(body) print(f"Title: {title}") print(f"Bytes: {byte_count}") # Save for later use with open('/tmp/extracted.txt', 'w') as f: f.write(f"{title}\n{byte_count}") EOF |
| fetch-page | agentsdk | 3 | yes | example.com | connection 3403ad13-8602-45cd-866e-583a5c0a395e to example.com:443, allowed |
| fetch-page | claude-agent-sdk | 3 | yes | example.com | Bash: wget -O page.html https://example.com/ 2>&1 \| tee wget_output.txt && grep -i "200\\|404\\|500" wget_output.txt \| head -1 \| grep -o "[0-9][0-9]*" \| head -1 > status.txt && cat status.txt; Bash: wget -O page.html https://example.com/ 2>&1 \| grep "HTTP"; Bash: python3 << 'EOF' import urllib.request import urllib.error try: response = urllib.request.urlopen('https://example.com/') status_code = response.status content = response.read() with open('page.html', 'wb') as f: f.write(content) with open('status.txt', 'w') as f: f.write(str(status_code)) print(f"Page saved to page.html") print(f"Status code: {status_code}") except urllib.error.HTTPError as e: status_code = e.code with open('page.html', 'wb') as f: f.write(e.read()) with open('status.txt', 'w') as f: f.write(str(status_code)) print(f"HTTP Error {status_code}") except Exception as e: print(f"Error: {e}") EOF cat status.txt |
| fetch-title-into-config | agentsdk | 3 | yes | example.com | connection cb9d26c5-a721-43b5-a24a-d5040f7fc9f4 to example.com:443, allowed |
| fetch-title-into-config | claude-agent-sdk | 3 | yes | example.com | Bash: wget -q -O - "https://example.com/" 2>&1 \| head -c 10000; Bash: response=$(wget -q -O - "https://example.com/" 2>&1) echo "$response" > /tmp/response.txt wc -c < /tmp/response.txt |

## How to read this

Each row is one run. A measure an arm does not report reads unavailable rather than 0, so a
gap in the data cannot be mistaken for a zero. A failed checker means the answer did not
match what the fixture says, which is a measurement of this task on this arm, not a
judgement of either SDK.
