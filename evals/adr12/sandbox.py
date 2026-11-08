"""The sandbox tasks of the larger comparison (FR-179, P5A-D12, DECISION-d0269633).

Each task starts from its own files, and its checker reads the workspace after the run -- the
files and what the run's own programs wrote, not the answer text alone. The 24 tasks fall in five
groups: shell work, code that must run and pass the task's own check, files written and edited,
a fetch through the workspace's egress to its one allowed host, and a multi-step repair.

A task whose code is checked ships a `check.py` that the run is told to run, and the run's
`check.txt` is only its own claim. What the checker trusts is the harness's verification
(DECISION-b944d3ab): after the run, the files read back are copied with a pristine verifier into
a fresh container of the workspace image with no network, where each case -- the visible ones and
hidden ones the run never saw -- calls the named module file, loaded by its path, in a child
process of its own. The verifier prints a result carrying a nonce that no workspace code sees, and
the checker accepts only `pass`. A forged `check.txt`, a module that writes one when imported, a
package shadowing the module, or a table of the visible answers all fail there. `check.py` must
also come back unchanged.

A fetch task is judged against the harness's own witness: the page the harness fetched from the
same host for this comparison (DECISION-b944d3ab).

Every task carries a good, a bad and a tricky outcome, each a workspace as the checker would read
it, so the gate proves that every checker accepts the right result and refuses the wrong ones
(FR-180). Nothing here runs a file of a workspace on the host.
"""

from __future__ import annotations

import csv
import configparser
import hashlib
import io
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

SANDBOX = "sandbox"
GROUPS = ("shell", "code", "files", "fetch", "repair")
# DECISION-d0269633: the fetch tasks' one allowed host.
FETCH_HOST = "example.com:443"


@dataclass(frozen=True)
class Workspace:
    """A workspace as the checker reads it after a run: every file by its relative path, and the
    run's final answer."""

    files: Mapping[str, bytes]
    answer: str | None = None
    # A code task's verification by the harness: "pass", or what failed; None when none ran.
    verified: str | None = None
    # A fetch task's witness: the page body the harness fetched itself; None when it has none.
    witness: bytes | None = None

    def text(self, path: str) -> str | None:
        data = self.files.get(path)
        if data is None:
            return None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return None


Checker = Callable[[Workspace], bool]


@dataclass(frozen=True)
class SandboxTask:
    """One sandbox task (FR-179): its files, its turn limit, the hosts its workspace may reach, and
    a checker over the workspace, with the three outcomes that test that checker."""

    id: str
    group: str
    prompt: str
    files: Mapping[str, bytes]
    max_turns: int
    checker: Checker
    # Each outcome is the changes a run makes to `files`: a path to its new bytes, or to None
    # when the run removed it.
    good: Mapping[str, bytes | None]
    bad: Mapping[str, bytes | None]
    tricky: Mapping[str, bytes | None]
    egress: tuple[str, ...] = ()
    kind: str = SANDBOX
    answers: tuple[str | None, str | None, str | None] = (None, None, None)
    # A code task's verification: the module file, its function, and every case, hidden ones
    # included, as (arguments, expected) pairs. None for every other task.
    verification: tuple[str, str, tuple[tuple[tuple[Any, ...], Any], ...]] | None = None
    # Files the verification's hidden cases read, placed under /verify, never in the workspace.
    verification_files: Mapping[str, bytes] | None = None

    def __post_init__(self) -> None:
        # Anything but a Workspace -- no run, a crashed read-back -- is refused, never raised.
        check = self.checker
        object.__setattr__(self, "checker", lambda space: isinstance(space, Workspace) and bool(check(space)))

    def outcome(self, which: str, *, verified: str | None = None, witness: bytes | None = None) -> Workspace:
        """The workspace after a run that made the good, bad or tricky changes, with the
        harness's verification and witness as given."""
        changes = {"good": self.good, "bad": self.bad, "tricky": self.tricky}[which]
        answer = self.answers[("good", "bad", "tricky").index(which)]
        return Workspace(files=applied(self.files, changes), answer=answer, verified=verified, witness=witness)


def applied(files: Mapping[str, bytes], changes: Mapping[str, bytes | None]) -> dict[str, bytes]:
    result = dict(files)
    for path, data in changes.items():
        if data is None:
            result.pop(path, None)
        else:
            result[path] = data
    return result


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- checker parts ------------------------------------------------------------------------------


def all_of(*parts: Checker) -> Checker:
    def check(space: Workspace) -> bool:
        return isinstance(space, Workspace) and all(part(space) for part in parts)

    return check


def unchanged(original: Mapping[str, bytes], *paths: str) -> Checker:
    """Each named input comes back byte for byte."""
    return lambda space: all(space.files.get(path) == original[path] for path in paths)


def stripped(path: str, expected: str) -> Checker:
    """The file holds `expected` and nothing else but surrounding whitespace."""
    return lambda space: (space.text(path) or "").strip() == expected


def lines_of(path: str, expected: list[str]) -> Checker:
    """The file's non-empty lines, stripped, are exactly `expected`, in order."""
    def check(space: Workspace) -> bool:
        text = space.text(path)
        return text is not None and [line.strip() for line in text.splitlines() if line.strip()] == expected

    return check


def verified() -> Checker:
    """The harness's verification passed: every case, hidden ones included, on the module file
    as it came back (DECISION-b944d3ab). The run's own check.txt is not evidence."""
    return lambda space: space.verified == "pass"


# The harness's verifier, run in a fresh container with no network over the files read back. It
# reads a nonce on its standard input, then runs each case in a child process that loads the named
# file by its path and prints its result; only this parent, which no workspace code runs in, knows
# the nonce and the expected values. One line out: VERIFIED <nonce> <passed> <total>.
VERIFIER = '''import json, os, subprocess, sys
WORK = sys.argv[1] if len(sys.argv) > 1 else "/work"
NONCE = sys.stdin.readline().strip()
MODULE, FUNCTION, CASES = json.loads(sys.stdin.readline())
CHILD = """import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("solution", sys.argv[3] + "/" + sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
arguments = json.loads(sys.stdin.read())
result = getattr(module, sys.argv[2])(*arguments)
sys.stdout.write(chr(10) + "RESULT " + json.dumps(result) + chr(10))
"""
passed = 0
for arguments, expected in CASES:
    try:
        done = subprocess.run([sys.executable, "-I", "-c", CHILD, MODULE, FUNCTION, WORK],
                              input=json.dumps(arguments), capture_output=True, text=True, timeout=20, cwd=WORK)
        lines = [l for l in done.stdout.splitlines() if l.startswith("RESULT ")]
        if done.returncode == 0 and lines and json.loads(lines[-1][7:]) == expected:
            passed += 1
    except Exception:
        pass
sys.stdout.write("VERIFIED %s %d %d" % (NONCE, passed, len(CASES)) + chr(10))
'''


def verification_input(task: "SandboxTask", nonce: str) -> bytes:
    """What the verifier reads: the nonce, then the module, the function and every case."""
    module, function, cases = task.verification
    return (nonce + "\n" + json.dumps([module, function, [[list(a), e] for a, e in cases]]) + "\n").encode()


def verification_result(output: bytes, nonce: str) -> str:
    """`pass` when the verifier's line carries this nonce and every case passed; else what failed."""
    lines = [line.split() for line in output.decode("utf-8", "replace").splitlines()]
    found = [parts for parts in lines if len(parts) == 4 and parts[0] == "VERIFIED" and parts[1] == nonce]
    if not found:
        return "no verification"
    passed, total = found[-1][2], found[-1][3]
    return "pass" if passed == total and total != "0" else f"{passed} of {total} cases"


def under(prefix: str) -> Callable[[Workspace], dict[str, bytes]]:
    return lambda space: {p: d for p, d in space.files.items() if p.startswith(prefix)}


# --- the task's own check -----------------------------------------------------------------------

# One check program, its cases filled in per task. It imports the module from the folder it runs
# in, runs every case, and writes check.txt only when all of them pass.
_CHECK = '''"""The task's own check: run it with `python check.py` from this folder."""
import hashlib
import importlib
import sys

sys.path.insert(0, ".")
CASES = {cases}
SOURCES = {sources}


def main():
    module = importlib.import_module({module!r})
    function = getattr(module, {function!r})
    failed = 0
    for arguments, expected in CASES:
        try:
            got = function(*arguments)
        except Exception as exc:
            got = "raised " + type(exc).__name__
        if got != expected:
            failed += 1
            print("FAIL", arguments, "expected", repr(expected), "got", repr(got))
    if failed:
        print(failed, "of", len(CASES), "cases failed")
        return 1
    digests = [hashlib.sha256(open(path, "rb").read()).hexdigest() for path in SOURCES]
    with open("check.txt", "w") as out:
        out.write(" ".join(["PASS"] + digests) + "\\n")
    print("PASS: all", len(CASES), "cases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def check_program(module: str, function: str, cases: list[tuple[tuple[Any, ...], Any]],
                  sources: tuple[str, ...]) -> bytes:
    return _CHECK.format(cases=repr(cases), sources=repr(list(sources)), module=module,
                         function=function).encode()


def passing(sources: Mapping[str, bytes]) -> bytes:
    """The check.txt a passing check writes over these sources, in order."""
    return (" ".join(["PASS", *(sha256(data) for data in sources.values())]) + "\n").encode()


def code_task(*, id: str, group: str, prompt: str, module: str, function: str,
              cases: list[tuple[tuple[Any, ...], Any]], hidden: list[tuple[tuple[Any, ...], Any]], given: bytes,
              solution: bytes, wrong: bytes, extra: Mapping[str, bytes] | None = None,
              hidden_files: Mapping[str, bytes] | None = None, max_turns: int = 16) -> SandboxTask:
    """A task whose code is checked (FR-179): the run is given check.py with the visible cases;
    the harness verifies the module with those and the hidden ones (DECISION-b944d3ab).

    good: the working module and the check.txt its check wrote; bad: the wrong module; tricky:
    the wrong module beside a check.txt forged for it, digest and all."""
    path = f"{module}.py"
    files = {path: given, "check.py": check_program(module, function, cases, (path,)), **(extra or {})}
    return SandboxTask(
        id=id, group=group, prompt=prompt, files=files, max_turns=max_turns,
        checker=all_of(verified(), unchanged(files, "check.py", *(extra or {}))),
        good={path: solution, "check.txt": passing({path: solution})},
        bad={path: wrong},
        tricky={path: wrong, "check.txt": passing({path: wrong})},
        verification=(path, function, tuple((tuple(a), e) for a, e in [*cases, *hidden])),
        verification_files=dict(hidden_files or {}),
    )


# --- the files of the shell tasks ---------------------------------------------------------------

_LOG = "".join(
    f"2026-05-0{1 + i % 9} 10:{i:02d}:00 {'ERROR' if i in (3, 7, 11, 18, 22, 30, 37) else 'INFO'} "
    f"{'disk quota reached' if i in (3, 7, 11, 18, 22, 30, 37) else 'request served'} id={1000 + i}\n"
    for i in range(40)
).encode()
_NAMES = b"mara\njonas\n\nelif\nmara\nanouk\njonas\nbeatrix\n\nelif\nzoe\n"
_SIZES = {
    # The largest file by bytes has the fewest lines; the one with the most lines is small.
    "data/alpha.csv": b"id,value\n" + b"".join(f"{i},{'x' * 60}\n".encode() for i in range(40)),
    "data/beta.csv": b"".join(f"{i}\n".encode() for i in range(400)),
    "data/gamma.json": b'{"blob": "' + b"y" * 3000 + b'"}\n',
    "data/delta.txt": b"short\n",
    "data/epsilon.log": b"z" * 900 + b"\n",
}
_NOTES = {f"notes/{name}.txt": f"# {name}\n\nNotes on {name}.\n".encode()
          for name in ("kickoff", "design", "retro", "budget")}
_REPORTS = {f"reports/report-{day}.csv": f"day,total\n{day},{n * 11}\n".encode()
            for n, day in enumerate(("2024-01-15", "2024-01-31", "2024-02-01", "2024-02-29", "2024-03-10"))}
_TEXT = (b"The fox saw the dog. The dog saw the fox! A fox, a dog, and THE cat.\n"
         b"Cat and dog and fox: the end.\n")


def _top3(text: bytes) -> list[str]:
    import re
    from collections import Counter

    words = re.findall(r"[a-z]+", text.decode().lower())
    counts = Counter(words)
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:3]
    return [f"{word} {count}" for word, count in ordered]


def _renamed(space: Workspace) -> bool:
    found = under("notes/")(space)
    expected = {path[:-4] + ".md": data for path, data in _NOTES.items()}
    return found == expected


def _filed(space: Workspace) -> bool:
    found = under("reports/")(space)
    expected = {f"reports/{p[15:22]}/{p[8:]}": d for p, d in _REPORTS.items()}
    return found == expected


_SHELL = (
    SandboxTask(
        id="count-error-lines", group="shell", max_turns=10, files={"logs/app.log": _LOG},
        prompt="Count the lines of logs/app.log that contain the word ERROR, and write the count, digits only, "
               "to answer.txt. Leave the log as it is.",
        checker=all_of(stripped("answer.txt", "7"), unchanged({"logs/app.log": _LOG}, "logs/app.log")),
        good={"answer.txt": b"7\n"}, bad={"answer.txt": b"6\n"},
        # The right count, after keeping only the ERROR lines in the log itself.
        tricky={"answer.txt": b"7\n",
                "logs/app.log": b"".join(line + b"\n" for line in _LOG.splitlines() if b"ERROR" in line)},
    ),
    SandboxTask(
        id="unique-names", group="shell", max_turns=10, files={"names.txt": _NAMES},
        prompt="names.txt holds one name per line, with repeats and blank lines. Write every distinct name, sorted "
               "alphabetically, one per line, to unique.txt. Skip blank lines.",
        checker=all_of(lines_of("unique.txt", ["anouk", "beatrix", "elif", "jonas", "mara", "zoe"]),
                       lambda s: b"\n\n" not in (s.files.get("unique.txt") or b"").strip()),
        good={"unique.txt": b"anouk\nbeatrix\nelif\njonas\nmara\nzoe\n"},
        bad={"unique.txt": b"mara\njonas\nelif\nanouk\nbeatrix\nzoe\n"},
        # Distinct and sorted, but the blank line kept as a name of its own.
        tricky={"unique.txt": b"\n\nanouk\nbeatrix\nelif\n\njonas\nmara\nzoe\n"},
    ),
    SandboxTask(
        id="largest-file", group="shell", max_turns=10, files=dict(_SIZES),
        prompt="Find the largest file in the data folder, by size in bytes, and write its path relative to this "
               "folder (for example data/name.ext) to answer.txt.",
        checker=stripped("answer.txt", "data/gamma.json"),
        good={"answer.txt": b"data/gamma.json\n"}, bad={"answer.txt": b"data/alpha.csv\n"},
        # The file with the most lines, not the most bytes.
        tricky={"answer.txt": b"data/beta.csv\n"},
    ),
    SandboxTask(
        id="rename-notes", group="shell", max_turns=10, files=dict(_NOTES),
        prompt="Rename every .txt file in the notes folder to the same name ending in .md, leaving each file's "
               "contents exactly as they are. No .txt file should remain there.",
        checker=_renamed,
        good={**{p: None for p in _NOTES}, **{p[:-4] + ".md": d for p, d in _NOTES.items()}},
        # Copied, not renamed.
        bad={p[:-4] + ".md": d for p, d in _NOTES.items()},
        # Renamed, but one file emptied on the way.
        tricky={**{p: None for p in _NOTES}, **{p[:-4] + ".md": d for p, d in _NOTES.items()},
                "notes/retro.md": b""},
    ),
    SandboxTask(
        id="file-reports-by-month", group="shell", max_turns=12, files=dict(_REPORTS),
        prompt="The reports folder holds files named report-YYYY-MM-DD.csv. Move each into a folder under reports "
               "named for its year and month, YYYY-MM (for example reports/2024-01/report-2024-01-15.csv). "
               "Leave nothing at the top of the reports folder but those month folders.",
        checker=_filed,
        good={**{p: None for p in _REPORTS}, **{f"reports/{p[15:22]}/{p[8:]}": d for p, d in _REPORTS.items()}},
        bad={},
        # Filed, but one by its day of month rather than its month.
        tricky={**{p: None for p in _REPORTS},
                **{f"reports/{p[15:22]}/{p[8:]}": d for p, d in _REPORTS.items() if "02-29" not in p},
                "reports/2024-29/report-2024-02-29.csv": _REPORTS["reports/report-2024-02-29.csv"]},
    ),
    SandboxTask(
        id="top-words", group="shell", max_turns=10, files={"text.txt": _TEXT},
        prompt="Count the words in text.txt, ignoring case and punctuation (a word is a run of letters). Write the "
               "three most frequent words to top.txt, one per line as 'word count', most frequent first; break a "
               "tie alphabetically.",
        checker=lines_of("top.txt", _top3(_TEXT)),
        good={"top.txt": ("\n".join(_top3(_TEXT)) + "\n").encode()},
        bad={"top.txt": b"the 4\nfox 4\ndog 4\n"},
        # Counted with case: 'The' and 'THE' apart from 'the'.
        tricky={"top.txt": b"dog 4\nfox 4\nthe 3\n"},
    ),
)


# --- the code tasks: written, run, and passing the task's own check ---------------------------

_CODE = (
    code_task(
        id="parse-duration", group="code", module="duration", function="seconds",
        prompt="duration.py has an empty function seconds(text). Make it return the number of seconds in a "
               "duration written like '1h30m', '45s', '2h' or '1h2m3s' (hours h, minutes m, seconds s, in that "
               "order, each optional but at least one present). Then run `python check.py` here: it writes "
               "check.txt when every case passes. Do not change check.py.",
        hidden=[(("3m10s",), 190), (("5h",), 18000), (("1m",), 60)],
        cases=[(("1h30m",), 5400), (("45s",), 45), (("2h",), 7200), (("1h2m3s",), 3723), (("10m",), 600)],
        given=b"def seconds(text):\n    raise NotImplementedError\n",
        solution=(b"import re\n\n\ndef seconds(text):\n"
                  b"    match = re.fullmatch(r'(?:(\\d+)h)?(?:(\\d+)m)?(?:(\\d+)s)?', text)\n"
                  b"    h, m, s = (int(g) if g else 0 for g in match.groups())\n"
                  b"    return h * 3600 + m * 60 + s\n"),
        wrong=b"def seconds(text):\n    return int(text.rstrip('hms')) * 60\n",
    ),
    code_task(
        id="roman-numerals", group="code", module="roman", function="to_int",
        prompt="Write roman.py with a function to_int(numeral) that converts a Roman numeral such as 'XIV' or "
               "'MCMXCIV' to an integer. Then run `python check.py` here: it writes check.txt when every case "
               "passes. Do not change check.py.",
        hidden=[(("IX",), 9), (("MMXXVI",), 2026), (("XC",), 90)],
        cases=[(("III",), 3), (("XIV",), 14), (("XL",), 40), (("MCMXCIV",), 1994), (("CDXLIV",), 444)],
        given=b"",
        solution=(b"VALUES = {'I': 1, 'V': 5, 'X': 10, 'L': 50, 'C': 100, 'D': 500, 'M': 1000}\n\n\n"
                  b"def to_int(numeral):\n    total = 0\n"
                  b"    for here, after in zip(numeral, numeral[1:] + ' '):\n"
                  b"        value = VALUES[here]\n"
                  b"        total += -value if after != ' ' and VALUES[after] > value else value\n"
                  b"    return total\n"),
        wrong=(b"VALUES = {'I': 1, 'V': 5, 'X': 10, 'L': 50, 'C': 100, 'D': 500, 'M': 1000}\n\n\n"
               b"def to_int(numeral):\n    return sum(VALUES[c] for c in numeral)\n"),
    ),
    code_task(
        id="isbn10", group="code", module="isbn", function="valid",
        prompt="Write isbn.py with a function valid(code) that returns True for a valid ISBN-10 and False "
               "otherwise. Hyphens are ignored; the last character may be X, meaning 10. Then run "
               "`python check.py` here: it writes check.txt when every case passes. Do not change check.py.",
        hidden=[(("0-19-852663-6",), True), (("0-19-852663-7",), False), (("12345678901",), False)],
        cases=[(("0-306-40615-2",), True), (("0306406153",), False), (("080442957X",), True),
               (("123",), False), (("X123456789",), False)],
        given=b"",
        solution=(b"def valid(code):\n    digits = code.replace('-', '')\n"
                  b"    if len(digits) != 10 or not digits[:9].isdigit() or not (digits[9].isdigit() or digits[9] == 'X'):\n"
                  b"        return False\n"
                  b"    values = [int(c) for c in digits[:9]] + [10 if digits[9] == 'X' else int(digits[9])]\n"
                  b"    return sum((10 - i) * v for i, v in enumerate(values)) % 11 == 0\n"),
        wrong=(b"def valid(code):\n    digits = code.replace('-', '')\n"
               b"    return len(digits) == 10\n"),
    ),
    code_task(
        id="merge-intervals", group="code", module="intervals", function="merge",
        prompt="Write intervals.py with a function merge(pairs) that takes a list of [start, end] pairs and "
               "returns the overlapping or touching ones merged, as a list of [start, end] lists sorted by start. "
               "Then run `python check.py` here: it writes check.txt when every case passes. Do not change "
               "check.py.",
        hidden=[(([[6, 8], [1, 9], [2, 4], [4, 7]],), [[1, 9]]), (([[1, 2], [3, 4]],), [[1, 2], [3, 4]]), (([[2, 3], [2, 2], [3, 3], [1, 3], [5, 7], [2, 2], [4, 6]],), [[1, 3], [4, 7]])],
        cases=[(([[1, 3], [2, 6], [8, 10]],), [[1, 6], [8, 10]]), (([[5, 7], [1, 2]],), [[1, 2], [5, 7]]),
               (([[1, 4], [4, 5]],), [[1, 5]]), (([],), []), (([[1, 10], [2, 3]],), [[1, 10]])],
        given=b"",
        solution=(b"def merge(pairs):\n    merged = []\n"
                  b"    for start, end in sorted(pairs):\n"
                  b"        if merged and start <= merged[-1][1]:\n"
                  b"            merged[-1][1] = max(merged[-1][1], end)\n"
                  b"        else:\n            merged.append([start, end])\n"
                  b"    return merged\n"),
        wrong=(b"def merge(pairs):\n    merged = []\n"
               b"    for start, end in sorted(pairs):\n"
               b"        if merged and start < merged[-1][1]:\n"
               b"            merged[-1][1] = end\n"
               b"        else:\n            merged.append([start, end])\n"
               b"    return merged\n"),
    ),
    code_task(
        id="word-wrap", group="code", module="wrap", function="wrap",
        prompt="Write wrap.py with a function wrap(text, width) that breaks text into lines of at most width "
               "characters at spaces, returning the lines joined with newlines. A word longer than width stands "
               "on a line of its own. Then run `python check.py` here: it writes check.txt when every case "
               "passes. Do not change check.py.",
        hidden=[(("aaa bbb ccc", 7), "aaa bbb" + chr(10) + "ccc"), (("hello", 10), "hello"), (("a bb ccc dddd", 4), "a bb" + chr(10) + "ccc" + chr(10) + "dddd")],
        cases=[(("the quick brown fox", 10), "the quick\nbrown fox"), (("a b c", 1), "a\nb\nc"),
               (("extraordinary words", 5), "extraordinary\nwords"), (("", 4), ""),
               (("one two three", 13), "one two three")],
        given=b"",
        solution=(b"def wrap(text, width):\n    lines, line = [], ''\n"
                  b"    for word in text.split():\n"
                  b"        if line and len(line) + 1 + len(word) > width:\n"
                  b"            lines.append(line)\n            line = word\n"
                  b"        else:\n            line = word if not line else line + ' ' + word\n"
                  b"    if line:\n        lines.append(line)\n"
                  b"    return '\\n'.join(lines)\n"),
        wrong=(b"def wrap(text, width):\n"
               b"    return '\\n'.join(text[i:i + width] for i in range(0, len(text), width))\n"),
    ),
    code_task(
        id="category-totals", group="code", module="totals", function="totals",
        prompt="sales.csv lists sales with a category and an amount. Write totals.py with a function "
               "totals(path) that reads such a file and returns a dict of each category's total amount, rounded "
               "to 2 decimal places. Then run `python check.py` here: it writes check.txt when every case passes. "
               "Do not change check.py.",
        hidden=[(("../verify/more_sales.csv",), {"toys": 10.0, "books": 1.1})], hidden_files={"more_sales.csv": b"category,amount" + bytes([10]) + b"toys,5.25" + bytes([10]) + b"toys,4.75" + bytes([10]) + b"books,1.10" + bytes([10])},
        cases=[(("sales.csv",), {"books": 47.5, "games": 120.0, "music": 9.99})],
        given=b"",
        extra={"sales.csv": b"category,amount\nbooks,12.50\ngames,60.00\nmusic,9.99\nbooks,35.00\ngames,60.00\n"},
        solution=(b"import csv\n\n\ndef totals(path):\n    found = {}\n"
                  b"    with open(path, newline='') as handle:\n"
                  b"        for row in csv.DictReader(handle):\n"
                  b"            found[row['category']] = found.get(row['category'], 0) + float(row['amount'])\n"
                  b"    return {k: round(v, 2) for k, v in found.items()}\n"),
        wrong=(b"import csv\n\n\ndef totals(path):\n    found = {}\n"
               b"    with open(path, newline='') as handle:\n"
               b"        for row in csv.DictReader(handle):\n"
               b"            found[row['category']] = float(row['amount'])\n"
               b"    return found\n"),
    ),
)


# --- files written and edited -------------------------------------------------------------------

_INI = (b"[server]\nhost = 0.0.0.0\nport = 8080\n\n[metrics]\nport = 9100\nenabled = true\n\n"
        b"[database]\nname = orders\n")
_PACKAGE = b'{\n  "name": "ledger-cli",\n  "version": "1.2.0",\n  "main": "index.js",\n  "dependencies": {\n    "yargs": "17.7.2"\n  }\n}\n'
_DOCS = {
    "docs/intro.md": b"# Intro\n\nACME Corp builds ledgers. Ask ACME Corp support.\n",
    "docs/guide/setup.md": b"# Setup\n\nInstall the ACME Corp CLI.\n",
    "docs/faq.md": b"# FAQ\n\nNo mention here.\n",
    "LICENSE.txt": b"Copyright ACME Corp 2026. All rights reserved.\n",
}
_CHANGELOG = (b"# Changelog\n\n## 1.2.0\n\n- Added the report command\n\n## 1.1.0\n\n"
              b"- First public release\n")
_PEOPLE = b"first,last,team\nAda,Lovelace,core\nAlan,Turing,research\ngrace,hopper,core\n"


def _ini(space: Workspace) -> bool:
    text = space.text("settings.ini")
    if text is None:
        return False
    parser = configparser.ConfigParser()
    try:
        parser.read_string(text)
    except configparser.Error:
        return False
    try:
        return (parser["server"]["port"] == "9090" and parser["metrics"]["port"] == "9100"
                and parser["server"]["host"] == "0.0.0.0" and parser["metrics"]["enabled"] == "true"
                and parser["database"]["name"] == "orders" and set(parser.sections()) == {"server", "metrics", "database"})
    except KeyError:
        return False


def _package(space: Workspace) -> bool:
    try:
        found = json.loads(space.text("package.json") or "")
    except ValueError:
        return False
    return found == {**json.loads(_PACKAGE), "license": "MIT"}


def _changelog(space: Workspace) -> bool:
    text = space.text("CHANGELOG.md")
    if text is None:
        return False
    sections = [line.strip() for line in text.splitlines() if line.startswith("## ")]
    return (sections == ["## 1.3.0", "## 1.2.0", "## 1.1.0"]
            and "- Added the export command" in text.split("## 1.2.0")[0]
            and text.split("## 1.2.0", 1)[1] == _CHANGELOG.decode().split("## 1.2.0", 1)[1]
            and text.startswith("# Changelog"))


def _initials(space: Workspace) -> bool:
    text = space.text("people.csv")
    if text is None:
        return False
    rows = list(csv.reader(io.StringIO(text)))
    expected = [["first", "last", "team", "initials"], ["Ada", "Lovelace", "core", "AL"],
                ["Alan", "Turing", "research", "AT"], ["grace", "hopper", "core", "GH"]]
    return [[cell.strip() for cell in row] for row in rows if row] == expected


def _replaced(space: Workspace) -> bool:
    expected = {path: (data.replace(b"ACME Corp", b"Globex") if path.endswith(".md") else data)
                for path, data in _DOCS.items()}
    return {p: d for p, d in space.files.items() if p in _DOCS or p.startswith("docs/")} == expected


_FILES = (
    SandboxTask(
        id="change-server-port", group="files", max_turns=10, files={"settings.ini": _INI},
        prompt="In settings.ini, change the port of the [server] section to 9090. Every other setting, including "
               "the [metrics] port, must stay as it is.",
        checker=_ini,
        good={"settings.ini": _INI.replace(b"port = 8080", b"port = 9090")},
        bad={},
        # Both ports changed.
        tricky={"settings.ini": _INI.replace(b"port = 8080", b"port = 9090").replace(b"port = 9100", b"port = 9090")},
    ),
    SandboxTask(
        id="add-license-field", group="files", max_turns=10, files={"package.json": _PACKAGE},
        prompt='Add a "license" field with the value "MIT" to package.json, keeping it valid JSON and every other '
               "field exactly as it is.",
        checker=_package,
        good={"package.json": _PACKAGE.replace(b'"main"', b'"license": "MIT",\n  "main"')},
        bad={"package.json": _PACKAGE.replace(b'"main"', b'"license": "MIT"\n  "main"')},
        # The licence added, and the version bumped on the way.
        tricky={"package.json": _PACKAGE.replace(b'"main"', b'"license": "MIT",\n  "main"').replace(b"1.2.0", b"1.3.0")},
    ),
    SandboxTask(
        id="rename-company", group="files", max_turns=12, files=dict(_DOCS),
        prompt="Replace every occurrence of 'ACME Corp' with 'Globex' in the Markdown files under docs, its "
               "subfolders included. Do not change any other file.",
        checker=_replaced,
        good={p: d.replace(b"ACME Corp", b"Globex") for p, d in _DOCS.items() if p.endswith(".md")},
        # The subfolder missed.
        bad={p: d.replace(b"ACME Corp", b"Globex") for p, d in _DOCS.items() if p.endswith(".md") and "guide" not in p},
        # The licence changed too.
        tricky={p: d.replace(b"ACME Corp", b"Globex") for p, d in _DOCS.items()},
    ),
    SandboxTask(
        id="changelog-entry", group="files", max_turns=10, files={"CHANGELOG.md": _CHANGELOG},
        prompt="Add a section for version 1.3.0 to CHANGELOG.md, above the 1.2.0 section and in the same style, "
               "with the one entry '- Added the export command'. Leave the earlier sections as they are.",
        checker=_changelog,
        good={"CHANGELOG.md": _CHANGELOG.replace(b"## 1.2.0", b"## 1.3.0\n\n- Added the export command\n\n## 1.2.0", 1)},
        # Appended at the end.
        bad={"CHANGELOG.md": _CHANGELOG + b"\n## 1.3.0\n\n- Added the export command\n"},
        # Above 1.2.0, but an earlier entry reworded.
        tricky={"CHANGELOG.md": _CHANGELOG.replace(b"## 1.2.0", b"## 1.3.0\n\n- Added the export command\n\n## 1.2.0", 1)
                .replace(b"First public release", b"First release")},
    ),
    SandboxTask(
        id="initials-column", group="files", max_turns=10, files={"people.csv": _PEOPLE},
        prompt="Add a column named initials to people.csv, holding each person's first and last initials in capitals "
               "(Ada Lovelace is AL). Keep every other column and row as it is.",
        checker=_initials,
        good={"people.csv": b"first,last,team,initials\nAda,Lovelace,core,AL\nAlan,Turing,research,AT\n"
                            b"grace,hopper,core,GH\n"},
        bad={"people.csv": b"first,last,team,initials\nAda,Lovelace,core,AL\nAlan,Turing,research,AT\n"
                           b"grace,hopper,core,gh\n"},
        # Right initials, but the names capitalised along the way.
        tricky={"people.csv": b"first,last,team,initials\nAda,Lovelace,core,AL\nAlan,Turing,research,AT\n"
                              b"Grace,Hopper,core,GH\n"},
    ),
)


# --- the fetch tasks: the workspace's one allowed host ----------------------------------------

# What the tests take as the served page; a live comparison judges against the harness's own
# fetch of the same URL, its witness (DECISION-b944d3ab).
_PAGE = (b"<!doctype html><html><head><title>Example Domain</title></head><body><div><h1>Example Domain</h1>"
         b"<p>This domain is for use in documentation examples.</p></div></body></html>\n")
_SITE = b'{\n  "name": "docs-mirror",\n  "url": "https://example.com/",\n  "title": "",\n  "bytes": 0\n}\n'


def _squeezed(data: bytes) -> bytes:
    """A page with its whitespace runs collapsed: how it was saved may differ, not what it holds."""
    return b" ".join(data.split())


def _title(page: bytes) -> str | None:
    import re

    found = re.search(rb"<title>(.*?)</title>", page, re.IGNORECASE | re.DOTALL)
    return found.group(1).decode("utf-8", "replace").strip() if found else None


def _fetched(space: Workspace) -> bool:
    """page.html is the page the witness was served, and status.txt says 200."""
    page = space.files.get("page.html")
    return (space.witness is not None and page is not None and _squeezed(page) == _squeezed(space.witness)
            and (space.text("status.txt") or "").strip() == "200")


def _site(space: Workspace) -> bool:
    """site.json carries the served page's title and its byte length, its other fields kept."""
    if space.witness is None:
        return False
    try:
        found = json.loads(space.text("site.json") or "")
    except ValueError:
        return False
    return found == {**json.loads(_SITE), "title": _title(space.witness), "bytes": len(space.witness)}


_FETCH = (
    SandboxTask(
        id="fetch-page", group="fetch", max_turns=10, files={}, egress=(FETCH_HOST,),
        prompt="Download https://example.com/ and save the page exactly as served to page.html, then write the "
               "HTTP status code of the response, digits only, to status.txt.",
        checker=_fetched,
        good={"page.html": _PAGE, "status.txt": b"200\n"},
        bad={"status.txt": b"200\n"},
        # A page typed from memory: its title right, its body not the one served.
        tricky={"page.html": b"<html><head><title>Example Domain</title></head><body><h1>Example Domain</h1>"
                             b"</body></html>\n", "status.txt": b"200\n"},
    ),
    SandboxTask(
        id="fetch-title-into-config", group="fetch", max_turns=12, files={"site.json": _SITE}, egress=(FETCH_HOST,),
        prompt='site.json names a url. Fetch that page, set the "title" field of site.json to the text of the '
               "page's <title> element, and set its \"bytes\" field to the number of bytes in the response body. "
               "Keep site.json valid JSON with its other fields unchanged.",
        checker=_site,
        good={"site.json": _SITE.replace(b'"title": ""', b'"title": "Example Domain"')
              .replace(b'"bytes": 0', b'"bytes": ' + str(len(_PAGE)).encode())},
        bad={"site.json": _SITE.replace(b'"title": ""', b'"title": "example.com"')
             .replace(b'"bytes": 0', b'"bytes": ' + str(len(_PAGE)).encode())},
        # The title typed from memory, the length guessed.
        tricky={"site.json": _SITE.replace(b'"title": ""', b'"title": "Example Domain"')
                .replace(b'"bytes": 0', b'"bytes": 1256')},
    ),
)


# --- multi-step repair: find the fault from the failing check, fix it, check again ----------

_REPAIR = (
    code_task(
        id="repair-average", group="repair", module="stats", function="average", max_turns=20,
        prompt="`python check.py` fails here. Find the fault in the code it checks and fix it, without changing "
               "check.py, until the check passes; it writes check.txt when it does.",
        hidden=[(([10, 20, 30, 40],), 25.0), (([-1, 1],), 0.0)],
        cases=[(([2, 4, 6],), 4.0), (([5],), 5.0), (([1, 2],), 1.5), (([],), None)],
        given=(b"def average(values):\n    if not values:\n        return None\n"
               b"    return sum(values) / (len(values) + 1)\n"),
        solution=(b"def average(values):\n    if not values:\n        return None\n"
                  b"    return sum(values) / len(values)\n"),
        wrong=(b"def average(values):\n    return sum(values) / len(values)\n"),
    ),
    code_task(
        id="repair-date-format", group="repair", module="dates", function="iso", max_turns=20,
        prompt="`python check.py` fails here. Find the fault in the code it checks and fix it, without changing "
               "check.py, until the check passes; it writes check.txt when it does.",
        hidden=[(("15/08/1999",), "1999-08-15"), (("29/02/2024",), "2024-02-29")],
        cases=[(("03/04/2026",), "2026-04-03"), (("31/12/2025",), "2025-12-31"), (("01/01/2026",), "2026-01-01")],
        given=(b"from datetime import datetime\n\n\ndef iso(day_first):\n"
               b"    return datetime.strptime(day_first, '%m/%d/%Y').date().isoformat()\n"),
        solution=(b"from datetime import datetime\n\n\ndef iso(day_first):\n"
                  b"    return datetime.strptime(day_first, '%d/%m/%Y').date().isoformat()\n"),
        wrong=(b"from datetime import datetime\n\n\ndef iso(day_first):\n"
               b"    return datetime.strptime(day_first, '%m/%d/%Y').date().isoformat()[:10]\n"),
    ),
    code_task(
        id="repair-dedupe", group="repair", module="emails", function="distinct", max_turns=20,
        prompt="`python check.py` fails here. Find the fault in the code it checks and fix it, without changing "
               "check.py, until the check passes; it writes check.txt when it does.",
        hidden=[((["X@y.io ", "x@Y.io", "z@y.io"],), ["x@y.io", "z@y.io"])],
        cases=[((["a@x.io", "A@x.io", "b@x.io"],), ["a@x.io", "b@x.io"]),
               (([" c@x.io", "c@x.io"],), ["c@x.io"]), (([],), [])],
        given=(b"def distinct(addresses):\n    seen, kept = set(), []\n    for address in addresses:\n"
               b"        if address not in seen:\n            seen.add(address)\n            kept.append(address)\n"
               b"    return kept\n"),
        solution=(b"def distinct(addresses):\n    seen, kept = set(), []\n    for address in addresses:\n"
                  b"        key = address.strip().lower()\n"
                  b"        if key not in seen:\n            seen.add(key)\n            kept.append(key)\n"
                  b"    return kept\n"),
        wrong=(b"def distinct(addresses):\n    return sorted(set(a.lower() for a in addresses))\n"),
    ),
    code_task(
        id="repair-restock", group="repair", module="inventory", function="to_order", max_turns=20,
        prompt="`python check.py` fails here. Find the fault in the code it checks and fix it, without changing "
               "check.py, until the check passes; it writes check.txt when it does.",
        hidden=[(({"a": 5, "b": 6}, 9, 5), {"a": 4}), (({}, 3, 1), {})],
        cases=[(({"pens": 3, "ink": 12}, 10, 5), {"pens": 7}), (({"pens": 10}, 10, 5), {}),
               (({"pads": 0, "tape": 4}, 8, 4), {"pads": 8, "tape": 4})],
        given=(b"def to_order(stock, target, minimum):\n    \"\"\"Order up to target every item below minimum.\"\"\"\n"
               b"    return {item: target - count for item, count in stock.items() if count > minimum}\n"),
        solution=(b"def to_order(stock, target, minimum):\n    \"\"\"Order up to target every item below minimum.\"\"\"\n"
                  b"    return {item: target - count for item, count in stock.items() if count <= minimum}\n"),
        wrong=(b"def to_order(stock, target, minimum):\n    \"\"\"Order up to target every item below minimum.\"\"\"\n"
               b"    return {item: target for item, count in stock.items() if count < minimum}\n"),
    ),
    code_task(
        id="repair-slugify", group="repair", module="slugs", function="slugify", max_turns=20,
        prompt="`python check.py` fails here. Find the fault in the code it checks and fix it, without changing "
               "check.py, until the check passes; it writes check.txt when it does.",
        hidden=[(("C'est la vie!",), "c-est-la-vie"), (("__init__",), "init")],
        cases=[(("Hello World",), "hello-world"), (("  Ledger: v2.0 notes ",), "ledger-v2-0-notes"),
               (("a--b",), "a-b"), (("",), "")],
        given=(b"import re\n\n\ndef slugify(title):\n"
               b"    return re.sub(r'[^a-z0-9]', '-', title.lower())\n"),
        solution=(b"import re\n\n\ndef slugify(title):\n"
                  b"    return re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')\n"),
        wrong=(b"import re\n\n\ndef slugify(title):\n"
               b"    return re.sub(r'[^a-z0-9]+', '-', title.lower())\n"),
    ),
)

SANDBOX_TASKS: tuple[SandboxTask, ...] = (*_SHELL, *_CODE, *_FILES, *_FETCH, *_REPAIR)


# --- the budget rule (FR-179, DECISION-d0269633) ------------------------------------------------

# The medians of this SDK's arm in M15 (evals/adr12/reports/adr12-comparison-2026-09-16.json), by
# kind: cost in USD and tokens (prompt + completion). The gate recomputes them from that report.
M15_MEDIANS = {
    "single-step": ("0.002175", 1925),
    "multi-step": ("0.002394", 2017.5),
    "long": ("0.002237", 1942),
    "decline": ("0.001531", 999),
}
M15_REPORT = "adr12-comparison-2026-09-16.json"
BUDGET_MULTIPLE = 10


def ceilings(kind: str) -> tuple[str, int]:
    """One rule for every task of a kind: ten times that kind's M15 median, the sandbox kind ten
    times the multi-step median. The USD ceiling as text, the token ceiling as a whole number."""
    from decimal import Decimal

    usd, tokens = M15_MEDIANS["multi-step" if kind == SANDBOX else kind]
    return str(Decimal(usd) * BUDGET_MULTIPLE), int(tokens * BUDGET_MULTIPLE)
