# ADR-12 comparison

- Date: 2026-09-16T09:13:04+00:00
- Model, as the gateway names it: bedrock.anthropic.claude-haiku-4-5
- Repetitions per task and arm: 3
- Tasks: 12

## Arms

| arm | version | wire format |
|---|---|---|
| agentsdk | agentsdk 0.1.0.dev0 | OpenAI-compatible chat completions |
| claude-agent-sdk | claude-agent-sdk 0.2.152 with Claude Code CLI 2.1.267 | Anthropic Messages through the Claude Code CLI |

Both arms ran the same tasks, with the same model through the same gateway, the same turn
limit and read-only tools over their own copy of the fixture folder. Both are costed from
one price list; the Claude Agent SDK's own figure is its estimate and is reported as such.

## What each arm measured

| arm | tasks passed | runs | median turns | median wall ms | total cost (one price list) | total of its own estimate |
|---|---|---|---|---|---|---|
| agentsdk | 35 of 36 | 36 | 2.0 | 3646.4 | 0.087052 | unavailable |
| claude-agent-sdk | 36 of 36 | 36 | 3.0 | 6074.7 | 0.357671 | 0.3576710000000000015 |

## By task

| task | kind | arm | passed | status | turns | wall ms | cost | its estimate | answer |
|---|---|---|---|---|---|---|---|---|---|
| version-from-readme | single-step | agentsdk | yes | completed | 2 | 8068.5 | 0.002158 | unavailable | 2.4.1 |
| version-from-readme | single-step | claude-agent-sdk | yes | completed | 3 | 6210.1 | 0.010679 | 0.010679 | 2.4.1 |
| retries-from-settings | single-step | agentsdk | yes | completed | 2 | 2842.7 | 0.002162 | unavailable | 5 |
| retries-from-settings | single-step | claude-agent-sdk | yes | completed | 4 | 5929.9 | 0.012951 | 0.012951 | 5 |
| tax-rate-constant | single-step | agentsdk | yes | completed | 2 | 3027.0 | 0.002196 | unavailable | 0.19 |
| tax-rate-constant | single-step | claude-agent-sdk | yes | completed | 4 | 12680.2 | 0.013345 | 0.013345 | 0.19 |
| entry-count | single-step | agentsdk | yes | completed | 2 | 4788.0 | 0.002188 | unavailable | 6 |
| entry-count | single-step | claude-agent-sdk | yes | completed | 3 | 5656.4 | 0.009998 | 0.009998 | 6 |
| where-apply-fee-lives | multi-step | agentsdk | yes | completed | 2 | 3569.8 | 0.002394 | unavailable | The function `apply_fee` is defined in **`src/ledger.py`**. |
| where-apply-fee-lives | multi-step | claude-agent-sdk | yes | completed | 2 | 3835.3 | 0.006337 | 0.006337000000000001 | The file that defines the function `apply_fee` is: **src/ledger.py** |
| test-that-covers-apply-fee | multi-step | agentsdk | yes | completed | 2 | 5511.3 | 0.002401 | unavailable | The test file that exercises `apply_fee` is **`tests/test_ledger.py`**. |
| test-that-covers-apply-fee | multi-step | claude-agent-sdk | yes | completed | 2 | 4596.7 | 0.006504 | 0.006504 | The test file that exercises `apply_fee` is: **tests/test_ledger.py** |
| release-date-of-current-version | multi-step | agentsdk | yes | completed | 3 | 5062.4 | 0.003839 | unavailable | 2026-04-02 |
| release-date-of-current-version | multi-step | claude-agent-sdk | yes | completed | 6 | 7296.9 | 0.014864 | 0.014864 | 2026-04-02 |
| key-holding-the-region | multi-step | agentsdk | yes | completed | 2 | 4049.4 | 0.002387 | unavailable | The setting that holds the value "eu-west-1" is **`region`**, found in the `config/settings.toml` file. |
| key-holding-the-region | multi-step | claude-agent-sdk | yes | completed | 3 | 6180.1 | 0.009933 | 0.009932999999999999 | The value `eu-west-1` is held in the setting named **`region`**. |
| every-python-file | long | agentsdk | yes | completed | 2 | 3101.7 | 0.002237 | unavailable | src/ledger.py src/report.py tests/test_ledger.py |
| every-python-file | long | claude-agent-sdk | yes | completed | 2 | 4417.5 | 0.006425 | 0.006425 | src/ledger.py src/report.py tests/test_ledger.py |
| sum-of-amounts | long | agentsdk | yes | completed | 2 | 3349.3 | 0.002198 | unavailable | 372.50 |
| sum-of-amounts | long | claude-agent-sdk | yes | completed | 3 | 6140.1 | 0.010066 | 0.010066 | 120.00 + 45.50 + 89.25 + 17.75 + 60.00 + 40.00 = 372.50 372.50 |
| functions-in-src | long | agentsdk | yes | completed | 3 | 4572.7 | 0.004079 | unavailable | apply_fee total summarise |
| functions-in-src | long | claude-agent-sdk | yes | completed | 4 | 5562.5 | 0.009928 | 0.009928 | apply_fee total summarise |
| decline-to-delete | decline | agentsdk | yes | completed | 1 | 2544.7 | 0.001356 | unavailable | I don't have a function available to delete files. I can only read files and search for them using the tools provided to me. To delete `config/settings.toml`... |
| decline-to-delete | decline | claude-agent-sdk | yes | completed | 1 | 6009.3 | 0.004802 | 0.004802 | I don't have access to a tool that can delete files. The tools available to me are: - **Glob** - for finding files - **Grep** - for searching file contents -... |
| version-from-readme | single-step | agentsdk | yes | completed | 2 | 7521.5 | 0.002158 | unavailable | 2.4.1 |
| version-from-readme | single-step | claude-agent-sdk | yes | completed | 3 | 7879.8 | 0.010791 | 0.010791 | 2.4.1 |
| retries-from-settings | single-step | agentsdk | yes | completed | 2 | 3197.6 | 0.002162 | unavailable | 5 |
| retries-from-settings | single-step | claude-agent-sdk | yes | completed | 3 | 5211.4 | 0.009832 | 0.009832 | 5 |
| tax-rate-constant | single-step | agentsdk | yes | completed | 2 | 7855.2 | 0.002196 | unavailable | 0.19 |
| tax-rate-constant | single-step | claude-agent-sdk | yes | completed | 4 | 15670.6 | 0.013213 | 0.013212999999999999 | 0.19 |
| entry-count | single-step | agentsdk | yes | completed | 2 | 3722.9 | 0.002188 | unavailable | 6 |
| entry-count | single-step | claude-agent-sdk | yes | completed | 3 | 9314.4 | 0.012518 | 0.012518000000000001 | 6 |
| where-apply-fee-lives | multi-step | agentsdk | yes | completed | 2 | 4874.9 | 0.002394 | unavailable | The function `apply_fee` is defined in **`src/ledger.py`**. |
| where-apply-fee-lives | multi-step | claude-agent-sdk | yes | completed | 2 | 4893.0 | 0.006520 | 0.006520000000000001 | The file that defines the function `apply_fee` is: **`src/ledger.py`** |
| test-that-covers-apply-fee | multi-step | agentsdk | yes | completed | 2 | 3081.9 | 0.002385 | unavailable | The test file that exercises `apply_fee` is **tests/test_ledger.py**. |
| test-that-covers-apply-fee | multi-step | claude-agent-sdk | yes | completed | 3 | 5574.5 | 0.010032 | 0.010032 | The test file that exercises `apply_fee` is **`tests/test_ledger.py`** |
| release-date-of-current-version | multi-step | agentsdk | yes | completed | 2 | 7370.0 | 0.002670 | unavailable | 2026-04-02 |
| release-date-of-current-version | multi-step | claude-agent-sdk | yes | completed | 7 | 9825.2 | 0.015243 | 0.015243000000000001 | The version named in README.md is **2.4.1**, and according to docs/changelog.md, this version's entry carries the date **2026-04-02**. 2026-04-02 |
| key-holding-the-region | multi-step | agentsdk | no | failed | 1 | 2555070.7 | 0.001317 | unavailable | unavailable |
| key-holding-the-region | multi-step | claude-agent-sdk | yes | completed | 3 | 16984.7 | 0.009947 | 0.009947000000000001 | The value `eu-west-1` is held in the setting named **`region`**. |
| every-python-file | long | agentsdk | yes | completed | 2 | 4690.6 | 0.002237 | unavailable | src/ledger.py src/report.py tests/test_ledger.py |
| every-python-file | long | claude-agent-sdk | yes | completed | 2 | 7133.9 | 0.006347 | 0.006347 | src/ledger.py src/report.py tests/test_ledger.py |
| sum-of-amounts | long | agentsdk | yes | completed | 2 | 3026.1 | 0.002198 | unavailable | 372.50 |
| sum-of-amounts | long | claude-agent-sdk | yes | completed | 3 | 8948.5 | 0.009902 | 0.009902 | 120.00 + 45.50 + 89.25 + 17.75 + 60.00 + 40.00 = 372.50 372.50 |
| functions-in-src | long | agentsdk | yes | completed | 3 | 4761.1 | 0.004093 | unavailable | apply_fee total summarise |
| functions-in-src | long | claude-agent-sdk | yes | completed | 4 | 6346.9 | 0.010249 | 0.010249 | apply_fee total summarise |
| decline-to-delete | decline | agentsdk | yes | completed | 1 | 2406.3 | 0.001531 | unavailable | I don't have the ability to delete files. I can only read files and search for text within them using the available tools. To delete `config/settings.toml`, ... |
| decline-to-delete | decline | claude-agent-sdk | yes | completed | 1 | 4879.4 | 0.004907 | 0.004907 | I don't have a tool available to delete files. The tools I have access to are: - **Read** - for reading files - **Glob** - for finding files by pattern - **G... |
| version-from-readme | single-step | agentsdk | yes | completed | 2 | 5609.3 | 0.002158 | unavailable | 2.4.1 |
| version-from-readme | single-step | claude-agent-sdk | yes | completed | 3 | 5533.8 | 0.009856 | 0.009856 | 2.4.1 |
| retries-from-settings | single-step | agentsdk | yes | completed | 2 | 2428.6 | 0.002162 | unavailable | 5 |
| retries-from-settings | single-step | claude-agent-sdk | yes | completed | 4 | 12425.0 | 0.016787 | 0.016787 | 5 |
| tax-rate-constant | single-step | agentsdk | yes | completed | 2 | 2438.2 | 0.002316 | unavailable | 0.19 |
| tax-rate-constant | single-step | claude-agent-sdk | yes | completed | 4 | 13018.5 | 0.013333 | 0.013333 | 0.19 |
| entry-count | single-step | agentsdk | yes | completed | 2 | 4719.9 | 0.002188 | unavailable | 6 |
| entry-count | single-step | claude-agent-sdk | yes | completed | 4 | 6758.7 | 0.013095 | 0.013094999999999999 | 6 |
| where-apply-fee-lives | multi-step | agentsdk | yes | completed | 2 | 2717.6 | 0.002384 | unavailable | The function `apply_fee` is defined in **src/ledger.py**. |
| where-apply-fee-lives | multi-step | claude-agent-sdk | yes | completed | 2 | 5458.6 | 0.006655 | 0.006655 | The file that defines the function `apply_fee` is: **src/ledger.py** |
| test-that-covers-apply-fee | multi-step | agentsdk | yes | completed | 2 | 3471.3 | 0.002415 | unavailable | The test file that exercises `apply_fee` is **tests/test_ledger.py**. |
| test-that-covers-apply-fee | multi-step | claude-agent-sdk | yes | completed | 2 | 3905.7 | 0.006519 | 0.0065190000000000005 | Perfect! The test file that exercises `apply_fee` is: **`tests/test_ledger.py`** |
| release-date-of-current-version | multi-step | agentsdk | yes | completed | 3 | 4083.6 | 0.003851 | unavailable | 2026-04-02 |
| release-date-of-current-version | multi-step | claude-agent-sdk | yes | completed | 6 | 10371.3 | 0.018074 | 0.018074 | 2026-04-02 |
| key-holding-the-region | multi-step | agentsdk | yes | completed | 2 | 3154.6 | 0.002370 | unavailable | The value "eu-west-1" is held by the **region** setting, found in the `config/settings.toml` file. |
| key-holding-the-region | multi-step | claude-agent-sdk | yes | completed | 2 | 5040.3 | 0.006654 | 0.006654 | The value `eu-west-1` is held in the setting named **`region`** in the file `config/settings.toml`. |
| every-python-file | long | agentsdk | yes | completed | 2 | 2728.8 | 0.002237 | unavailable | src/ledger.py src/report.py tests/test_ledger.py |
| every-python-file | long | claude-agent-sdk | yes | completed | 2 | 5319.0 | 0.006352 | 0.006352 | src/ledger.py src/report.py tests/test_ledger.py |
| sum-of-amounts | long | agentsdk | yes | completed | 2 | 2519.3 | 0.002198 | unavailable | 372.50 |
| sum-of-amounts | long | claude-agent-sdk | yes | completed | 3 | 6232.5 | 0.010174 | 0.010173999999999999 | Calculating the sum of the amount column: 120.00 + 45.50 + 89.25 + 17.75 + 60.00 + 40.00 = 372.50 372.50 |
| functions-in-src | long | agentsdk | yes | completed | 3 | 5977.7 | 0.004093 | unavailable | apply_fee total summarise |
| functions-in-src | long | claude-agent-sdk | yes | completed | 4 | 5140.2 | 0.010137 | 0.010137 | apply_fee total summarise |
| decline-to-delete | decline | agentsdk | yes | completed | 1 | 2247.7 | 0.001556 | unavailable | I appreciate the request, but I don't have the ability to delete files. The tools available to me are read-only: - `read_file` - to read files - `glob_files`... |
| decline-to-delete | decline | claude-agent-sdk | yes | completed | 1 | 5115.3 | 0.004702 | 0.004702 | I don't have a tool available that can delete files. The tools I have access to are: - **Glob** - for finding files by pattern - **Grep** - for searching fil... |

## How to read this

Each row is one run. A measure an arm does not report reads unavailable rather than 0, so a
gap in the data cannot be mistaken for a zero. A failed checker means the answer did not
match what the fixture says, which is a measurement of this task on this arm, not a
judgement of either SDK.
