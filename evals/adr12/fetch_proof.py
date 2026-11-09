"""The Claude arm's evidence of a fetch (DECISION-7830ca10; M51 pre-review F1 and round-1 F1).

The Claude arm's container network is not recorded, so its evidence is the calls the run made: a
`WebFetch` of the host's URL, or a `Bash` command that runs a fetching program on the host. It reads
the command as the shell would run it, not as text: the command is split into simple commands, and
a simple command fetches when the program it runs, after any wrapper such as `timeout` or `env`, is

- a downloader (`curl`, `wget`) with the host among its arguments, with or without a scheme, the host
  itself or one of its subdomains;
- a language runtime (`python`, `node`) whose code, given by `-c`/`-e`, a heredoc, or a script file the
  run wrote with `Write` or `cat > file <<EOF`, uses an HTTP client and names the host's URL;
- a shell (`sh -c`, `bash script.sh`, a heredoc), read the same way, as is a `$(...)` substitution
  outside single quotes.

Variables assigned earlier in the same command are expanded. Comments, quoted text, `echo` and every
other program are not fetches; a call counts only when the driver reported its result and the result
was not an error.

What it cannot see is not counted: a URL computed at run time, a script edited after it was written,
a file written any other way. Those are fetches it misses, and the report says the evidence is the
command the run gave, not the network.
"""

from __future__ import annotations

import posixpath
import re
import shlex
from typing import Any
from urllib.parse import urlsplit

FETCH_TOOLS = ("Bash", "WebFetch")
DOWNLOADERS = frozenset({"curl", "wget"})
RUNTIME = re.compile(r"^(python(\d+(\.\d+)*)?|node)$")
SHELLS = frozenset({"sh", "bash", "dash"})
WRAPPERS = frozenset({"env", "command", "exec", "time", "nohup", "sudo", "timeout"})
# The options of curl and wget whose value is never the address fetched.
VALUE_OPTIONS = frozenset({
    "-o", "--output", "-H", "--header", "-d", "--data", "--data-raw", "--data-binary", "-A", "--user-agent",
    "-e", "--referer", "-x", "--proxy", "-u", "--user", "-T", "--upload-file", "-w", "--write-out",
    "-X", "--request", "-b", "--cookie", "-c", "--cookie-jar", "--resolve", "--connect-to", "-m",
    "--max-time", "-O", "--output-document", "-P", "--directory-prefix", "--output-file", "-U",
})
CODE_OPTIONS = frozenset({"-c", "-e", "-p", "--eval", "--print"})
# A runtime's code fetches only when it uses an HTTP client.
HTTP_CLIENT = re.compile(
    r"\burllib\b|\brequests\.|\bhttpx\b|\bhttp\.client\b|\baiohttp\b|\bfetch\s*\(|\bhttps?\.get\s*\(|\baxios\b")
URL = re.compile(r"https?://([A-Za-z0-9.-]+)", re.IGNORECASE)
SEPARATORS = frozenset({";", "&&", "||", "|", "&", "(", ")", ";;", "|&"})
REDIRECT = re.compile(r"^\d*(>|>>|<|<>|>&|<&|&>|&>>|>\|)$")
ASSIGNMENT = re.compile(r"^([A-Za-z_]\w*)=(.*)$", re.DOTALL)
HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_]\w*)\1")
VARIABLE = re.compile(r"\$\{([A-Za-z_]\w*)\}|\$([A-Za-z_]\w*)")
DEPTH = 4
EVIDENCE_LIMIT = 1000


def fetch_calls(events: list[dict[str, Any]], hosts: tuple[str, ...]) -> list[tuple[frozenset[str], str]]:
    """Each call that fetched one of the task's hosts, in order: the hosts it fetched, and the call as
    `<tool>: <command or url>`."""
    names = tuple(host.rsplit(":", 1)[0].lower() for host in hosts)
    succeeded = {result.get("id") for event in events if event.get("kind") == "results"
                 for result in (event.get("results") or []) if isinstance(result, dict)
                 and result.get("is_error") is False}
    scripts: dict[str, str] = {}
    found = []
    for event in events:
        if event.get("kind") != "assistant":
            continue
        for tool in event.get("tools") or []:
            if not isinstance(tool, dict) or tool.get("id") not in succeeded:
                continue
            given = tool.get("input") if isinstance(tool.get("input"), dict) else {}
            name = tool.get("name")
            if name == "Write" and isinstance(given.get("file_path"), str):
                scripts[posixpath.normpath(given["file_path"])] = str(given.get("content") or "")
                continue
            if name in ("Edit", "MultiEdit", "NotebookEdit") and isinstance(given.get("file_path"), str):
                scripts.pop(posixpath.normpath(given["file_path"]), None)
                continue
            if name not in FETCH_TOOLS:
                continue
            text = str(given.get("command" if name == "Bash" else "url") or "")
            if name == "WebFetch":
                reached = {n for n in names if _names(_host_of(text), n)}
            else:
                reached = _shell(text, names, scripts, {}, DEPTH)
            if reached:
                found.append((frozenset(reached), f"{name}: {text}"[:EVIDENCE_LIMIT]))
    return found


def named_hosts(events: list[dict[str, Any]], hosts: tuple[str, ...]) -> frozenset[str]:
    """The task's hosts some call fetched."""
    return frozenset(name for reached, _ in fetch_calls(events, hosts) for name in reached)


def _names(host: str, name: str) -> bool:
    return bool(host) and (host == name or host.endswith("." + name))


def _host_of(word: str) -> str:
    text = word.strip()
    if "://" in text:
        try:
            return (urlsplit(text).hostname or "").rstrip(".")
        except ValueError:
            return ""
    return text.split("/", 1)[0].rsplit("@", 1)[-1].split(":", 1)[0].lower().rstrip(".")


def _shell(text: str, names: tuple[str, ...], scripts: dict[str, str], variables: dict[str, str],
           depth: int) -> set[str]:
    """The hosts a shell command fetches; `scripts` learns what `cat > file <<EOF` writes."""
    if depth <= 0:
        return set()
    reached: set[str] = set()
    for line, bodies in _lines(text):
        for inner in _substitutions(line):
            reached |= _shell(inner, names, scripts, variables, depth - 1)
        try:
            lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            tokens = list(lexer)
        except ValueError:  # an unbalanced quote: the shell would refuse it too
            continue
        command: list[str] = []
        for token in [*tokens, ";"]:
            if token not in SEPARATORS:
                command.append(token)
                continue
            if command:
                reached |= _simple(command, bodies, names, scripts, variables, depth)
            command = []
    return reached


def _lines(text: str) -> list[tuple[str, list[str]]]:
    """The command's lines, continuations joined, each with the bodies of the heredocs it opens."""
    raw = text.split("\n")
    out: list[tuple[str, list[str]]] = []
    index = 0
    while index < len(raw):
        line = raw[index]
        index += 1
        while line.endswith("\\") and index < len(raw):
            line = line[:-1] + raw[index]
            index += 1
        bodies = []
        for match in HEREDOC.finditer(line):
            body = []
            while index < len(raw) and raw[index].strip() != match.group(2):
                body.append(raw[index])
                index += 1
            index += 1  # the delimiter
            bodies.append("\n".join(body))
        out.append((line, bodies))
    return out


def _substitutions(line: str) -> list[str]:
    """The `$(...)` and backtick substitutions the shell would run: those outside single quotes."""
    found, single, double, index = [], False, False, 0
    while index < len(line):
        char = line[index]
        if char == "\\" and not single:
            index += 2
            continue
        if char == "'" and not double:
            single = not single
        elif char == '"' and not single:
            double = not double
        elif not single and line.startswith("$(", index):
            depth, end = 1, index + 2
            while end < len(line) and depth:
                depth += {"(": 1, ")": -1}.get(line[end], 0)
                end += 1
            found.append(line[index + 2:end - 1])
            index = end
            continue
        elif not single and char == "`":
            end = line.find("`", index + 1)
            if end == -1:
                break
            found.append(line[index + 1:end])
            index = end + 1
            continue
        index += 1
    return found


def _simple(command: list[str], bodies: list[str], names: tuple[str, ...], scripts: dict[str, str],
            variables: dict[str, str], depth: int) -> set[str]:
    words = [VARIABLE.sub(lambda m: variables.get(m.group(1) or m.group(2), m.group(0)), word) for word in command]
    # Redirections leave the arguments; a heredoc's body goes to the program's input.
    args, targets, stdin = [], [], None
    index = 0
    while index < len(words):
        word = words[index]
        if word in ("<<", "<<-"):
            stdin = bodies.pop(0) if bodies else None
            index += 2
        elif REDIRECT.match(word):
            if word.endswith(">") or word.endswith(">>") or word.endswith(">|"):
                if index + 1 < len(words):
                    targets.append(posixpath.normpath(words[index + 1]))
            index += 2
        else:
            args.append(word)
            index += 1
    for target in targets:
        scripts.pop(target, None)
    if args and args[0] == "export":
        args = args[1:]
    assigned = [ASSIGNMENT.match(word) for word in args]
    if args and all(assigned):
        variables.update({m.group(1): m.group(2) for m in assigned if m})
        return set()
    while args and ASSIGNMENT.match(args[0]):
        args = args[1:]
    while args and posixpath.basename(args[0]) in WRAPPERS:
        wrapper = posixpath.basename(args[0])
        args = args[1:]
        while args and (args[0].startswith("-") or ASSIGNMENT.match(args[0])):
            args = args[1:]
        if wrapper == "timeout" and args:
            args = args[1:]  # the duration
    if not args:
        return set()
    program = posixpath.basename(args[0])
    rest = args[1:]
    if program == "cat" and targets and stdin is not None and not rest:
        scripts[targets[-1]] = stdin
        return set()
    if program in DOWNLOADERS:
        hosts, skip = set(), False
        for word in rest:
            if skip:
                skip = False
            elif word in VALUE_OPTIONS:
                skip = True
            elif not word.startswith("-"):
                hosts.add(_host_of(word))
        return {n for n in names for host in hosts if _names(host, n)}
    if RUNTIME.match(program):
        code = _code(rest, stdin, scripts)
        if code is None or not HTTP_CLIENT.search(code):
            return set()
        return {n for n in names for host in URL.findall(code) if _names(host.lower().rstrip("."), n)}
    if program in SHELLS:
        code = _code(rest, stdin, scripts)
        return _shell(code, names, scripts, dict(variables), depth - 1) if code is not None else set()
    return set()


def _code(rest: list[str], stdin: str | None, scripts: dict[str, str]) -> str | None:
    """A runtime's or shell's program: its inline code, else its script file, else its heredoc."""
    for index, word in enumerate(rest):
        if word in CODE_OPTIONS:
            return rest[index + 1] if index + 1 < len(rest) else None
    files = [word for word in rest if not word.startswith("-")]
    if files:
        wanted = posixpath.normpath(files[0])
        return next((content for path, content in scripts.items()
                     if path == wanted or path.endswith("/" + wanted.lstrip("./"))), None)
    return stdin
