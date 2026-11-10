"""The delegation tasks of the comparison (FR-188, P5A-D21, DECISION-0a617b3f).

Each task splits into independent pieces over its files: three or four files, each worked on its
own, with one result the run writes into its own workspace. A run may hand each piece to a helper
-- this SDK's arm through `delegate_task`, the Claude arm through its CLI's `Agent` tool -- or do
them itself: whether it delegated, and how many children it ran, is measured and reported, never a
condition of passing. The checker reads the workspace the parent left, as a sandbox task's does.

The two code tasks are verified by the harness's verifier as the sandbox code tasks are
(DECISION-b944d3ab): it loads `verify/dispatch.py`, a harness file the run never sees, which loads
each module of the run by its path and calls it, on visible and hidden cases. The run's own
check_*.txt are only its claims.
"""

from __future__ import annotations

import json
from typing import Any

from .sandbox import DELEGATION, SandboxTask, Workspace, all_of, check_program, passing, unchanged, verified

GROUP = "delegation"
TURNS = 30
INDEPENDENT = " The files are independent of one another."

# The harness's own entry for the code tasks' verification: the verifier calls call(module,
# function, arguments), which loads module.py from the folder it runs in -- the run's files. That
# folder is appended to the import path, after the standard library, so a module may import a
# sibling of the run's as it would when run there (M53 pre-review F5); a sibling cannot shadow the
# standard library.
DISPATCH = b'''import importlib.util, os, sys


def call(module, function, arguments):
    if os.getcwd() not in sys.path:
        sys.path.append(os.getcwd())
    spec = importlib.util.spec_from_file_location(module, os.path.join(os.getcwd(), module + ".py"))
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return getattr(loaded, function)(*arguments)
'''
DISPATCHER = ("../verify/dispatch.py", "call")


def _json(path: str, expected: Any):
    """The file holds JSON equal to `expected`, type for type: Python's == takes false for 0 and 7.0
    for 7 (M53 pre-review F4), so both sides are compared as canonical JSON."""
    def canonical(value: Any) -> str:
        return json.dumps(value, sort_keys=True)

    def check(space: Workspace) -> bool:
        try:
            return canonical(json.loads(space.text(path) or "")) == canonical(expected)
        except (ValueError, RecursionError):  # M53 round 1 NB-4: too deep to read is a failed row
            return False

    return check


def _dumped(value: Any) -> bytes:
    return (json.dumps(value, indent=2) + "\n").encode()


# --- word-counts ------------------------------------------------------------------------------------

_CHAPTERS = {
    "chapters/one.txt": b"The well-known fox ran.\nIt ran far.\n",
    "chapters/two.txt": b"",
    "chapters/three.txt": b"A  B\tC\n\nD-E F\n",
    "chapters/four.txt": b"one two three four five six seven eight nine ten\n",
}
_COUNTS = {"chapters/one.txt": 7, "chapters/two.txt": 0, "chapters/three.txt": 5, "chapters/four.txt": 10}

# --- csv-totals -------------------------------------------------------------------------------------

_REGIONS = {
    "regions/north.csv": b'item,amount\nbolts,"1,250.50"\nnuts,49.50\n',
    "regions/south.csv": b"item,amount\nwasher,10.25\n\nscrew,4.75\n",
    "regions/east.csv": b'item,amount\nnail,"2,000.00"\nglue,0.99\nrope,9.01\n',
}
_TOTALS = b"region,total\neast,2010.00\nnorth,1300.00\nsouth,15.00\n"


def _totals(space: Workspace) -> bool:
    text = space.text("totals.csv")
    if text is None:
        return False
    import csv
    import io

    rows = [[cell.strip() for cell in row] for row in csv.reader(io.StringIO(text)) if row]
    return rows == [line.split(",") for line in _TOTALS.decode().split()]


# --- log-errors -------------------------------------------------------------------------------------

_LOGS = {
    "logs/api.log": (b"2026-05-01 10:00:00 INFO request served\r\n2026-05-01 10:00:01 ERROR upstream timeout\r\n"
                     b"2026-05-01 10:00:02 INFO retry after ERROR\r\n2026-05-01 10:00:03 ERROR connection refused\r\n"),
    "logs/db.log": (b"2026-05-01 10:00:00 ERROR deadlock found\n2026-05-01 10:00:05 ERROR deadlock found\n"
                    b"2026-05-01 10:00:09 WARN slow query\n"),
    "logs/web.log": b"2026-05-01 10:00:00 INFO user typed ERROR in a form\n2026-05-01 10:00:01 INFO page served\n",
    "logs/worker.log": (b"2026-05-01 10:00:00 ERROR job crashed\n2026-05-01 10:00:01 INFO restarted\n"
                        b"2026-05-01 10:00:02 ERROR job crashed\n2026-05-01 10:00:03 ERROR job crashed\n"),
}
_ERRORS = {"api": 2, "db": 2, "web": 0, "worker": 3}

# --- ini-to-json ------------------------------------------------------------------------------------

_INIS = {
    "configs/app.ini": b"[server]\nhost = 0.0.0.0 ; all interfaces\nport = 8080\n\n[log]\nlevel = info\nlevel = debug\n",
    "configs/db.ini": b"[db]\nname = orders\nuser = app ; read-write\n",
    "configs/cache.ini": b"[cache]\nttl = 300\nsize = 64\n\n[cache.redis]\nhost = redis.local\n",
}
_CONVERTED = {
    "configs/app.json": {"server": {"host": "0.0.0.0", "port": "8080"}, "log": {"level": "debug"}},
    "configs/db.json": {"db": {"name": "orders", "user": "app"}},
    "configs/cache.json": {"cache": {"ttl": "300", "size": "64"}, "cache.redis": {"host": "redis.local"}},
}

# --- three-functions --------------------------------------------------------------------------------

_SPECS = {
    "specs/roman.md": (b"# roman.py\n\nto_int(numeral) converts a Roman numeral to an integer.\n\n"
                       b"- to_int('III') == 3\n- to_int('XIV') == 14\n- to_int('MCMXCIV') == 1994\n"),
    "specs/slugify.md": (b"# slugify.py\n\nslugify(title) lowercases the title, turns every run of characters other "
                         b"than a-z and 0-9 into one hyphen, and strips hyphens from both ends.\n\n"
                         b"- slugify('Hello World') == 'hello-world'\n- slugify('  Ledger: v2.0 notes ') == "
                         b"'ledger-v2-0-notes'\n- slugify('a--b') == 'a-b'\n"),
    "specs/isbn.md": (b"# isbn.py\n\nvalid(code) is True for a valid ISBN-10 and False otherwise. Hyphens are ignored; "
                      b"the last character may be X, meaning 10.\n\n- valid('0-306-40615-2') is True\n"
                      b"- valid('0306406153') is False\n- valid('080442957X') is True\n"),
}
_ROMAN = (b"VALUES = {'I': 1, 'V': 5, 'X': 10, 'L': 50, 'C': 100, 'D': 500, 'M': 1000}\n\n\n"
          b"def to_int(numeral):\n    total = 0\n"
          b"    for here, after in zip(numeral, numeral[1:] + ' '):\n"
          b"        value = VALUES[here]\n"
          b"        total += -value if after != ' ' and VALUES[after] > value else value\n"
          b"    return total\n")
_SLUGIFY = (b"import re\n\n\ndef slugify(title):\n"
            b"    return re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')\n")
_ISBN = (b"def valid(code):\n    digits = code.replace('-', '')\n"
         b"    if len(digits) != 10 or not digits[:9].isdigit() or not (digits[9].isdigit() or digits[9] == 'X'):\n"
         b"        return False\n"
         b"    values = [int(c) for c in digits[:9]] + [10 if digits[9] == 'X' else int(digits[9])]\n"
         b"    return sum((10 - i) * v for i, v in enumerate(values)) % 11 == 0\n")
_ISBN_WRONG = b"def valid(code):\n    return len(code.replace('-', '')) == 10\n"
_FUNCTION_CASES = (
    *((("roman", "to_int", [n]), v) for n, v in
      [("III", 3), ("XIV", 14), ("MCMXCIV", 1994), ("IX", 9), ("MMXXVI", 2026), ("XC", 90)]),
    *((("slugify", "slugify", [t]), s) for t, s in
      [("Hello World", "hello-world"), ("  Ledger: v2.0 notes ", "ledger-v2-0-notes"), ("a--b", "a-b"),
       ("C'est la vie!", "c-est-la-vie"), ("", "")]),
    *((("isbn", "valid", [c]), v) for c, v in
      [("0-306-40615-2", True), ("0306406153", False), ("080442957X", True), ("0-19-852663-6", True),
       ("123", False), ("X123456789", False)]),
)

# --- fix-three-bugs ---------------------------------------------------------------------------------

_BUGGY = {
    "stats": ("average",
              b"def average(values):\n    if not values:\n        return None\n"
              b"    return sum(values) / (len(values) + 1)\n",
              b"def average(values):\n    if not values:\n        return None\n"
              b"    return sum(values) / len(values)\n",
              [(([2, 4, 6],), 4.0), (([5],), 5.0), (([1, 2],), 1.5), (([],), None)],
              [(([10, 20, 30, 40],), 25.0), (([-1, 1],), 0.0)]),
    "dates": ("iso",
              b"from datetime import datetime\n\n\ndef iso(day_first):\n"
              b"    return datetime.strptime(day_first, '%m/%d/%Y').date().isoformat()\n",
              b"from datetime import datetime\n\n\ndef iso(day_first):\n"
              b"    return datetime.strptime(day_first, '%d/%m/%Y').date().isoformat()\n",
              [(("03/04/2026",), "2026-04-03"), (("31/12/2025",), "2025-12-31"), (("01/01/2026",), "2026-01-01")],
              [(("15/08/1999",), "1999-08-15"), (("29/02/2024",), "2024-02-29")]),
    "inventory": ("to_order",
                  b"def to_order(stock, target, minimum):\n    \"\"\"Order up to target every item at or below minimum.\"\"\"\n"
                  b"    return {item: target - count for item, count in stock.items() if count > minimum}\n",
                  b"def to_order(stock, target, minimum):\n    \"\"\"Order up to target every item at or below minimum.\"\"\"\n"
                  b"    return {item: target - count for item, count in stock.items() if count <= minimum}\n",
                  [(({"pens": 3, "ink": 12}, 10, 5), {"pens": 7}), (({"pens": 10}, 10, 5), {}),
                   (({"pads": 0, "tape": 4}, 8, 4), {"pads": 8, "tape": 4})],
                  [(({"a": 5, "b": 6}, 9, 5), {"a": 4}), (({}, 3, 1), {})]),
}
_INVENTORY_WRONG = (b"def to_order(stock, target, minimum):\n    \"\"\"Order up to target every item at or below minimum.\"\"\"\n"
                    b"    return {item: target for item, count in stock.items() if count < minimum}\n")


def _check_for(module: str) -> bytes:
    """The sandbox tasks' check program, for one module, writing check_<module>.txt."""
    function, _, _, cases, _ = _BUGGY[module]
    text = check_program(module, function, cases, (f"{module}.py",))
    return (text.replace(b"python check.py", f"python check_{module}.py".encode())
            .replace(b'open("check.txt"', f'open("check_{module}.txt"'.encode()))


_FIX_FILES = {**{f"{m}.py": given for m, (_, given, _, _, _) in _BUGGY.items()},
              **{f"check_{m}.py": _check_for(m) for m in _BUGGY}}
_FIX_CASES = tuple(((module, function, list(arguments)), expected)
                   for module, (function, _, _, cases, hidden) in _BUGGY.items()
                   for arguments, expected in [*cases, *hidden])


def _fixed(module: str, source: bytes) -> dict[str, bytes]:
    return {f"{module}.py": source, f"check_{module}.txt": passing({f"{module}.py": source})}


DELEGATION_TASKS: tuple[SandboxTask, ...] = (
    SandboxTask(
        id="word-counts", group=GROUP, kind=DELEGATION, max_turns=TURNS, files=dict(_CHAPTERS),
        prompt="Count the words in each file under chapters/, a word being a run of characters other than "
               "whitespace (so 'well-known' is one word). Write counts.json, a JSON object mapping each file's "
               "path, such as \"chapters/one.txt\", to its count. Leave the chapters as they are." + INDEPENDENT,
        checker=all_of(_json("counts.json", _COUNTS), unchanged(_CHAPTERS, *_CHAPTERS)),
        good={"counts.json": _dumped(_COUNTS)},
        bad={"counts.json": _dumped({path: count + 1 for path, count in _COUNTS.items()})},
        # Hyphenated words split, and the empty file left out.
        tricky={"counts.json": _dumped({"chapters/one.txt": 8, "chapters/three.txt": 6, "chapters/four.txt": 10})},
    ),
    SandboxTask(
        id="csv-totals", group=GROUP, kind=DELEGATION, max_turns=TURNS, files=dict(_REGIONS),
        prompt="Each CSV under regions/ lists items with an amount; an amount may be quoted and carry a thousands "
               "separator, as \"1,250.50\". Write totals.csv with the header region,total and one row per region -- "
               "the file's name without .csv -- holding the sum of its amounts with two decimal places, the rows "
               "sorted by region. Leave the region files as they are." + INDEPENDENT,
        checker=all_of(_totals, unchanged(_REGIONS, *_REGIONS)),
        good={"totals.csv": _TOTALS},
        # The right sums, in the files' order rather than sorted.
        bad={"totals.csv": b"region,total\nnorth,1300.00\nsouth,15.00\neast,2010.00\n"},
        # The quoted thousands read as their first digits.
        tricky={"totals.csv": b"region,total\neast,12.00\nnorth,50.50\nsouth,15.00\n"},
    ),
    SandboxTask(
        id="log-errors", group=GROUP, kind=DELEGATION, max_turns=TURNS, files=dict(_LOGS),
        prompt="Each file under logs/ is one service's log: each line is a date, a time, a level and a message. "
               "Count the lines whose level is ERROR in each log, and write errors.json, a JSON object mapping each "
               "service -- the file's name without .log -- to its count. Leave the logs as they are." + INDEPENDENT,
        checker=all_of(_json("errors.json", _ERRORS), unchanged(_LOGS, *_LOGS)),
        good={"errors.json": _dumped(_ERRORS)},
        # The service with no errors left out.
        bad={"errors.json": _dumped({k: v for k, v in _ERRORS.items() if v})},
        # Every line that mentions ERROR counted.
        tricky={"errors.json": _dumped({"api": 3, "db": 2, "web": 1, "worker": 3})},
    ),
    SandboxTask(
        id="ini-to-json", group=GROUP, kind=DELEGATION, max_turns=TURNS, files=dict(_INIS),
        prompt="For each .ini file under configs/, write a .json file beside it with the same name (configs/app.ini "
               "becomes configs/app.json): a JSON object mapping each section to an object of its keys and values, "
               "every value a string. A ';' after a value starts a comment, which is not part of the value; when a "
               "key appears twice in a section, the last value counts. Leave the .ini files as they are."
               + INDEPENDENT,
        checker=all_of(*(_json(path, expected) for path, expected in _CONVERTED.items()), unchanged(_INIS, *_INIS)),
        good={path: _dumped(expected) for path, expected in _CONVERTED.items()},
        # The port a number and the first level kept.
        bad={**{path: _dumped(expected) for path, expected in _CONVERTED.items()},
             "configs/app.json": _dumped({"server": {"host": "0.0.0.0", "port": 8080}, "log": {"level": "info"}})},
        # The comments kept in the values.
        tricky={**{path: _dumped(expected) for path, expected in _CONVERTED.items()},
                "configs/app.json": _dumped({"server": {"host": "0.0.0.0 ; all interfaces", "port": "8080"},
                                             "log": {"level": "debug"}}),
                "configs/db.json": _dumped({"db": {"name": "orders", "user": "app ; read-write"}})},
    ),
    SandboxTask(
        id="three-functions", group=GROUP, kind=DELEGATION, max_turns=TURNS, files=dict(_SPECS),
        prompt="specs/ describes three functions, one per file. Write each in a module of its own in this folder, as "
               "its spec says: roman.py with to_int(numeral), slugify.py with slugify(title) and isbn.py with "
               "valid(code). Leave the specs as they are." + INDEPENDENT,
        checker=all_of(verified(), unchanged(_SPECS, *_SPECS)),
        good={"roman.py": _ROMAN, "slugify.py": _SLUGIFY, "isbn.py": _ISBN},
        bad={"roman.py": _ROMAN},
        # Two right, and an ISBN check of the length alone.
        tricky={"roman.py": _ROMAN, "slugify.py": _SLUGIFY, "isbn.py": _ISBN_WRONG},
        verification=(*DISPATCHER, _FUNCTION_CASES), verification_files={"dispatch.py": DISPATCH},
    ),
    SandboxTask(
        id="fix-three-bugs", group=GROUP, kind=DELEGATION, max_turns=TURNS, files=dict(_FIX_FILES),
        prompt="check_stats.py, check_dates.py and check_inventory.py each fail here, each checking one module: "
               "stats.py, dates.py and inventory.py. Find the fault in each module and fix it, without changing the "
               "checks, until each check passes; a passing check writes its own check_<module>.txt." + INDEPENDENT,
        checker=all_of(verified(), unchanged(_FIX_FILES, *(p for p in _FIX_FILES if p.startswith("check_")))),
        good={k: v for m, (_, _, fixed, _, _) in _BUGGY.items() for k, v in _fixed(m, fixed).items()},
        bad={},
        # Two fixed, the third wrong beside a check_inventory.txt forged for it.
        tricky={**_fixed("stats", _BUGGY["stats"][2]), **_fixed("dates", _BUGGY["dates"][2]),
                **_fixed("inventory", _INVENTORY_WRONG)},
        verification=(*DISPATCHER, _FIX_CASES), verification_files={"dispatch.py": DISPATCH},
    ),
)
