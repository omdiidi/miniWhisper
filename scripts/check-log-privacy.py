#!/usr/bin/env python3
"""Log-privacy gate for the macOS client (Part 3 Task 5).

Scans every `Log.(info|debug|warning|error)(` call under client/WisprAlt and
client/WisprAltCore. Each `\\(expr)` interpolation is allowed when `expr` ends
in `.count`, `.statusCode`, `.code` or `.rawValue`, or is
`ErrorLog.describe(...)`. Otherwise it fails when it mentions a risky
identifier (transcript text, errors, URLs, bodies, server reasons, file names)
or `.path`, `.host`, `localizedDescription`, `absoluteString`,
`errorDescription`.

Usage: check-log-privacy.py [--self-test] [ROOT]
Exit 0 clean, 1 on any finding (or a wrong self-test result).
"""
import pathlib
import re
import sys

LOG_CALL = re.compile(r"\bLog\.(info|debug|warning|error)\(")
IDENT = re.compile(r"[A-Za-z_]\w*")
RISKY_IDENTS = {
    "text", "transcript", "error", "err", "e", "underlying", "url", "body",
    "reason", "message", "response", "stem", "name",
}
RISKY_SUBSTRINGS = (".path", ".host", "localizedDescription", "absoluteString", "errorDescription")
SAFE_SUFFIXES = (".count", ".statusCode", ".code", ".rawValue")


def _scan_string(src, i, exprs):
    """src[i] is the opening quote. Returns the index after the closing quote."""
    triple = src.startswith('"""', i)
    i += 3 if triple else 1
    while i < len(src):
        if src[i] == "\\" and i + 1 < len(src) and src[i + 1] == "(":
            start = i + 2
            end = _scan_code(src, start, exprs)
            exprs.append(src[start:end - 1])
            i = end
        elif src[i] == "\\":
            i += 2
        elif triple and src.startswith('"""', i):
            return i + 3
        elif not triple and src[i] == '"':
            return i + 1
        else:
            i += 1
    return i


def _scan_code(src, i, exprs):
    """Scans code until the `)` closing depth 0. Returns the index after it."""
    depth = 0
    while i < len(src):
        c = src[i]
        if c == '"':
            i = _scan_string(src, i, exprs)
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            if depth == 0:
                return i + 1
            depth -= 1
        i += 1
    return i


def _strip_strings(expr):
    return re.sub(r'"(?:\\.|[^"\\])*"', '""', expr)


def is_risky(expr):
    e = expr.strip()
    if e.endswith(SAFE_SUFFIXES):
        return False
    if e.startswith("ErrorLog.describe(") and e.endswith(")"):
        return False
    code = _strip_strings(e)
    if any(s in code for s in RISKY_SUBSTRINGS):
        return True
    for ident in IDENT.findall(code):
        if ident in RISKY_IDENTS or re.fullmatch(r"\w+Error", ident):
            return True
    return False


def check_source(src):
    """Returns [(line_number, expr)] for every risky interpolation in a Log call."""
    # Blank out comment lines so the scanner never sees them (keeps line numbers).
    lines = [
        "" if ln.lstrip().startswith("//") else ln
        for ln in src.split("\n")
    ]
    text = "\n".join(lines)
    findings = []
    for m in LOG_CALL.finditer(text):
        exprs = []
        _scan_code(text, m.end(), exprs)
        line = text.count("\n", 0, m.start()) + 1
        findings += [(line, x) for x in exprs if is_risky(x)]
    return findings


def scan(root):
    bad = 0
    for sub in ("client/WisprAlt", "client/WisprAltCore"):
        for path in sorted((root / sub).rglob("*.swift")):
            for line, expr in check_source(path.read_text()):
                print(f"{path.relative_to(root)}:{line}: \\({expr})")
                bad += 1
    return bad


SELF_TEST_HITS = [
    'Log.warning(\n    "a "\n    + "b \\(text)",\n    category: "x")',
    'let err = x.error?.localizedDescription\nLog.error("failed \\(err)")',
    'Log.error("open() failed for \\(folderURL.path)")',
    'Log.error("decode \\(decodeError)")',
    'Log.info("done \\(stem)")',
    'Log.info("\\(name): granted.")',
]
SELF_TEST_MISSES = [
    'Log.info("chars=\\(text.count)")',
    'Log.info("HTTP \\(response.statusCode)")',
    'Log.warning("URLError \\(urlError.code.rawValue)")',
    'Log.error("failed: \\(ErrorLog.describe(error))")',
    '/// Log.error("x \\(err)")',
    'Log.info(\n    "a=\\(String(format: "%.1f", ms)) "\n    + "b=\\(n)",\n    category: "x")',
]


def self_test():
    wrong = 0
    for case in SELF_TEST_HITS:
        if not check_source(case):
            print(f"self-test: expected a hit: {case!r}")
            wrong += 1
    for case in SELF_TEST_MISSES:
        if check_source(case):
            print(f"self-test: unexpected hit: {case!r}")
            wrong += 1
    return wrong


def main(argv):
    if "--self-test" in argv:
        wrong = self_test()
        print("log-privacy self-test: " + ("FAIL" if wrong else "ok"))
        return 1 if wrong else 0
    args = [a for a in argv if not a.startswith("--")]
    root = pathlib.Path(args[0]) if args else pathlib.Path(__file__).resolve().parent.parent
    bad = scan(root)
    print(f"log-privacy: {bad} finding(s)" if bad else "log-privacy: clean")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
