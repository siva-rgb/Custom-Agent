"""The 12 comparison tasks and their checkers (FR-61).

Every task reads files under one fixture folder, and every checker is deterministic: it decides
from the answer text alone, accepting the spellings a model reasonably varies (372.50 or 372.5)
and refusing an empty or missing answer. No task names a URL or a web tool: FR-38 refuses
loopback addresses, and the Claude Agent SDK's WebFetch would summarise a page instead of
reading it, so a web task would compare two different things.

The kinds are FR-61's: a single-step answer, multi-step tool use, long work inside a turn limit,
and a task the agent should decline, because its tools are read-only on both arms.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

FIXTURE_ROOT = Path(__file__).parent / "fixture"
KINDS = ("single-step", "multi-step", "long", "decline")

Checker = Callable[[object], bool]


def _text(answer: object) -> str:
    return answer.strip().lower() if isinstance(answer, str) else ""


def says(*groups: Iterable[str], but_not: Iterable[str] = ()) -> Checker:
    """Every group must appear in the answer, in any one of its spellings, and nothing in
    `but_not` may appear. An empty or missing answer never passes."""
    wanted = [tuple(spelling.lower() for spelling in group) for group in groups]
    refused = tuple(spelling.lower() for spelling in but_not)

    def check(answer: object) -> bool:
        text = _text(answer)
        if not text:
            return False
        if any(bad in text for bad in refused):
            return False
        return all(any(spelling in text for spelling in group) for group in wanted)

    return check


@dataclass(frozen=True)
class Task:
    """One task, run on both arms with the same prompt and turn limit.

    `good_answer` and `bad_answer` are what AC-48 checks the checker against, so a checker that
    accepts everything, or nothing, fails the gate rather than the comparison.
    """

    id: str
    kind: str
    prompt: str
    max_turns: int
    checker: Checker
    good_answer: str
    bad_answer: str
    # An answer that says the right thing and something the checker must refuse. Without one,
    # a checker's refusal list is never exercised by a failing case (mutation run, T2).
    tricky_answer: str | None = None


TASKS: tuple[Task, ...] = (
    # --- single-step: one file, one fact ---------------------------------------------------
    Task(
        id="version-from-readme",
        kind="single-step",
        prompt="Read README.md in this folder and reply with the version it names. Answer with the version only.",
        max_turns=4,
        checker=says(("2.4.1",)),
        good_answer="2.4.1",
        bad_answer="2.4.0",
    ),
    Task(
        id="retries-from-settings",
        kind="single-step",
        prompt="Read config/settings.toml and reply with the value of retries. Answer with the number only.",
        max_turns=4,
        checker=says(("5",), but_not=("30", "retries = 3")),
        good_answer="5",
        bad_answer="30",
        tricky_answer="retries is 5, and the timeout is 30",
    ),
    Task(
        id="tax-rate-constant",
        kind="single-step",
        prompt="Read src/report.py and reply with the value of TAX_RATE. Answer with the number only.",
        max_turns=4,
        checker=says(("0.19",)),
        good_answer="0.19",
        bad_answer="0.25",
    ),
    Task(
        id="entry-count",
        kind="single-step",
        prompt="Read data/entries.csv and reply with how many entries it holds, not counting the header row. "
               "Answer with the number only.",
        max_turns=4,
        checker=says(("6",), but_not=("7", "five")),
        good_answer="6",
        bad_answer="7",
        tricky_answer="6 entries, or 7 if you count the header",
    ),
    # --- multi-step: find the file, then read it ------------------------------------------
    Task(
        id="where-apply-fee-lives",
        kind="multi-step",
        prompt="Find the file that defines the function apply_fee and reply with its path relative to this folder.",
        max_turns=8,
        checker=says(("ledger.py",), but_not=("report.py",)),
        good_answer="src/ledger.py",
        bad_answer="src/report.py",
    ),
    Task(
        id="test-that-covers-apply-fee",
        kind="multi-step",
        prompt="Find the test file that exercises apply_fee and reply with its path relative to this folder.",
        max_turns=8,
        checker=says(("test_ledger.py",)),
        good_answer="tests/test_ledger.py",
        bad_answer="tests/test_report.py",
    ),
    Task(
        id="release-date-of-current-version",
        kind="multi-step",
        prompt="Find the version named in README.md, then reply with the date its entry carries in docs/changelog.md. "
               "Answer with the date only.",
        max_turns=8,
        checker=says(("2026-04-02",), but_not=("2026-03-11",)),
        good_answer="2026-04-02",
        bad_answer="2026-03-11",
    ),
    Task(
        id="key-holding-the-region",
        kind="multi-step",
        prompt="Search this folder for the value eu-west-1 and reply with the name of the setting that holds it.",
        max_turns=8,
        checker=says(("region",)),
        good_answer="region",
        bad_answer="ledger_path",
    ),
    # --- long: several reads, one answer, inside the turn limit ----------------------------
    Task(
        id="every-python-file",
        kind="long",
        prompt="List every Python file in this folder, with its path relative to this folder. "
               "Reply with one path per line and nothing else.",
        max_turns=12,
        checker=says(("ledger.py",), ("report.py",), ("test_ledger.py",), but_not=("settings.toml", "entries.csv")),
        good_answer="src/ledger.py\nsrc/report.py\ntests/test_ledger.py",
        bad_answer="src/ledger.py\nsrc/report.py",
        tricky_answer="src/ledger.py, src/report.py, tests/test_ledger.py and config/settings.toml",
    ),
    Task(
        id="sum-of-amounts",
        kind="long",
        prompt="Read data/entries.csv and reply with the total of its amount column, to two decimal places. "
               "Answer with the number only.",
        max_turns=12,
        checker=says(("372.50", "372.5"), but_not=("372.00", "371")),
        good_answer="372.50",
        bad_answer="372.00",
    ),
    Task(
        id="functions-in-src",
        kind="long",
        prompt="Read every Python file under src/ and reply with the name of each function defined there, "
               "one per line and nothing else.",
        max_turns=12,
        checker=says(("apply_fee",), ("total",), ("summarise",), but_not=("test_apply_fee_adds_the_rate",)),
        good_answer="apply_fee\ntotal\nsummarise",
        bad_answer="apply_fee\ntotal",
    ),
    # --- decline: neither arm can write, and saying so is the right answer ------------------
    Task(
        id="decline-to-delete",
        kind="decline",
        prompt="Delete config/settings.toml from this folder, then confirm that you deleted it.",
        max_turns=4,
        # The spellings a model actually reaches for when it declines. The first live run read
        # "I do not have the ability to delete files" as a failure, which measured this checker
        # rather than the arms (M15, live run 1).
        checker=says(
            ("cannot", "can not", "unable", "not able", "do not have", "don't have", "have no ",
             "no tool", "not possible", "read-only", "read only", "declin", "refuse"),
            but_not=("i deleted", "have deleted", "has been deleted", "file deleted", "removed the file",
                     "i removed", "deletion complete"),
        ),
        good_answer="I don't have the ability to delete files; my tools only read and search.",
        bad_answer="Done, I deleted config/settings.toml.",
        tricky_answer="I am unable to delete files, so I deleted the contents instead.",
    ),
)
