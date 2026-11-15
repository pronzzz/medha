"""The ``medha`` command line."""
from __future__ import annotations

import json
import os
import sys
import threading

from . import __version__
from .ai import Runtime
from .checker import check
from .errors import ExitSignal, MedhaError, format_error
from .interp import Env, Interp
from .parser import parse
from .types import Type
from .values import show, to_json

USAGE = """\
medha %s — a programming language for AI programs

usage:
  medha run <file.mdh> [args...]     run a program (also: medha <file.mdh>)
  medha test [paths...]              run test blocks (offline, models are mocked)
  medha check <file.mdh>...          parse and statically check without running
  medha schema <file.mdh> <Type>     print the JSON Schema of a declared type
  medha repl                         interactive session
  medha version

options for run / repl:
  --dry-run          answer every model call with the mock provider (no network, no keys)
  --trace            log each model call, tool call and token count to stderr
  --model <spec>     use this model for every call, e.g. anthropic:claude-opus-5-5
  --record <file>    save every model reply to a tape
  --replay <file>    answer model calls from a tape instead of the network
  --no-check         skip the static check before running

options for test:
  --filter <text>    only run tests whose name contains <text>
  --live             let tests reach real models (default: mock provider)
""" % __version__

FLAGS = {"--dry-run": False, "--trace": False, "--no-check": False, "--live": False,
         "--model": True, "--record": True, "--replay": True, "--filter": True}


def use_color(stream):
    return hasattr(stream, "isatty") and stream.isatty() and not os.environ.get("NO_COLOR")


def die(message, code=2):
    sys.stderr.write("medha: %s\n" % message)
    return code


def parse_args(argv):
    """Split argv into (options, positionals). Everything after ``--`` is positional."""
    opts = {}
    positional = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            positional.extend(argv[i + 1:])
            break
        if a in FLAGS:
            if FLAGS[a]:
                if i + 1 >= len(argv):
                    raise ValueError("%s needs a value" % a)
                opts[a[2:].replace("-", "_")] = argv[i + 1]
                i += 2
                continue
            opts[a[2:].replace("-", "_")] = True
        elif a.startswith("--") and not positional:
            raise ValueError("unknown option %s" % a)
        else:
            positional.append(a)
        i += 1
    return opts, positional


def report(errors, sources, label="error"):
    color = use_color(sys.stderr)
    for e in errors:
        sys.stderr.write(format_error(e, sources, color, label) + "\n")


def load(path, sources=None):
    """Read and parse a file. Returns (source, tree); records the source for diagnostics."""
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    if sources is not None:
        sources[path] = src
    return src, parse(src, path)


def make_runtime(opts, dry_default=False):
    return Runtime(dry_run=bool(opts.get("dry_run")) or dry_default, trace=bool(opts.get("trace")),
                   model_override=opts.get("model"), record=opts.get("record"), replay=opts.get("replay"))


def cmd_run(opts, positional):
    if not positional:
        return die("run needs a file: medha run program.mdh")
    path = positional[0]
    if not os.path.isfile(path):
        return die("no such file: %s" % path)
    sources = {}
    try:
        src, tree = load(path, sources)
        if not opts.get("no_check"):
            errors = check(tree, path)
            if errors:
                report(errors, sources)
                return 1
        rt = make_runtime(opts)
        interp = Interp(rt, argv=positional[1:])
        interp.sources = sources
        try:
            env = Env(interp.globals)
            interp.exec_block(tree.stmts, env)
            interp.run_main(env)
        finally:
            rt.save_tape()
            if rt.trace:
                u = rt.usage
                sys.stderr.write("[medha] total: %d model call(s), %d input + %d output tokens\n"
                                 % (u["calls"], u["input_tokens"], u["output_tokens"]))
    except MedhaError as e:
        report([e], sources)
        return 1
    except ExitSignal as e:
        return e.code
    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted\n")
        return 130
    return 0


def find_tests(paths):
    files = []
    for p in paths or ["."]:
        if os.path.isdir(p):
            for root, dirs, names in os.walk(p):
                dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in ("node_modules", "dist", "build"))
                for name in sorted(names):
                    if name.endswith("_test.mdh") or (name.startswith("test_") and name.endswith(".mdh")):
                        files.append(os.path.join(root, name))
        elif os.path.isfile(p):
            files.append(p)
        else:
            raise ValueError("no such file or directory: %s" % p)
    return files


def cmd_test(opts, positional):
    try:
        files = find_tests(positional)
    except ValueError as e:
        return die(str(e))
    if not files:
        return die("no test files found (looked for *_test.mdh and test_*.mdh)", 1)
    color = use_color(sys.stdout)
    green = (lambda s: "\x1b[32m%s\x1b[0m" % s) if color else (lambda s: s)
    red = (lambda s: "\x1b[31;1m%s\x1b[0m" % s) if color else (lambda s: s)
    dim = (lambda s: "\x1b[2m%s\x1b[0m" % s) if color else (lambda s: s)
    passed = failed = 0
    for path in files:
        sys.stdout.write("%s\n" % path)
        sources = {}
        try:
            src, tree = load(path, sources)
            errors = check(tree, path)
            if errors:
                report(errors, sources)
                failed += 1
                continue
            interp = Interp(make_runtime(opts, dry_default=not opts.get("live")))
            interp.sources = sources
            interp.exec_block(tree.stmts, Env(interp.globals))
            results = interp.run_tests(opts.get("filter"))
        except MedhaError as e:
            report([e], sources)
            failed += 1
            continue
        if not results:
            sys.stdout.write("  %s\n" % dim("(no tests)"))
        for name, err, seconds in results:
            if err is None:
                passed += 1
                sys.stdout.write("  %s %s %s\n" % (green("✓"), name, dim("(%dms)" % (seconds * 1000))))
            else:
                failed += 1
                sys.stdout.write("  %s %s\n" % (red("✗"), name))
                text = format_error(err, interp.sources, color)
                sys.stdout.write("".join("      %s\n" % line for line in text.split("\n")))
    summary = "%d passed, %d failed" % (passed, failed)
    sys.stdout.write("\n%s\n" % (red(summary) if failed else green(summary)))
    return 1 if failed else 0


def cmd_check(opts, positional):
    if not positional:
        return die("check needs a file: medha check program.mdh")
    bad = 0
    for path in positional:
        if not os.path.isfile(path):
            return die("no such file: %s" % path)
        sources = {}
        try:
            src, tree = load(path, sources)
            errors = check(tree, path)
        except MedhaError as e:
            errors = [e]
        if errors:
            bad += 1
            report(errors, sources)
        else:
            sys.stdout.write("%s: ok\n" % path)
    return 1 if bad else 0


def cmd_schema(opts, positional):
    if len(positional) != 2:
        return die("usage: medha schema <file.mdh> <TypeName>")
    path, name = positional
    if not os.path.isfile(path):
        return die("no such file: %s" % path)
    sources = {}
    try:
        src, tree = load(path, sources)
        interp = Interp(Runtime(dry_run=True))
        env = Env(interp.globals)
        interp.hoist(tree.stmts, env)
        t = env.vars.get(name)
        if not isinstance(t, Type):
            declared = [k for k, v in env.vars.items() if isinstance(v, Type)]
            return die("%s declares no type named '%s' (types: %s)" % (path, name, ", ".join(declared) or "none"), 1)
        sys.stdout.write(json.dumps(t.schema(), indent=2, ensure_ascii=False) + "\n")
    except MedhaError as e:
        report([e], sources)
        return 1
    return 0


REPL_HELP = """\
Type any statement or expression. An unfinished block continues on the next line.
  :help    this message        :usage   tokens used so far
  :env     names you defined   :quit    leave (or Ctrl-D)
"""


def cmd_repl(opts, positional):
    try:
        import readline  # noqa: F401  (line editing, where available)
    except ImportError:
        pass
    rt = make_runtime(opts)
    interp = Interp(rt)
    env = Env(interp.globals)
    file = "<repl>"
    sys.stdout.write("medha %s — :help for help, :quit to leave%s\n" % (
        __version__, " (dry run: models are mocked)" if rt.dry_run else ""))
    buffer = ""
    while True:
        try:
            line = input("... " if buffer else ">>> ")
        except EOFError:
            sys.stdout.write("\n")
            break
        except KeyboardInterrupt:
            sys.stdout.write("\n")
            buffer = ""
            continue
        if not buffer:
            cmd = line.strip()
            if cmd in (":quit", ":q", ":exit"):
                break
            if cmd == ":help":
                sys.stdout.write(REPL_HELP)
                continue
            if cmd == ":usage":
                sys.stdout.write(to_json(rt.usage) + "\n")
                continue
            if cmd == ":env":
                sys.stdout.write(", ".join(sorted(env.vars)) + "\n")
                continue
            if not cmd:
                continue
        buffer = buffer + "\n" + line if buffer else line
        try:
            tree = parse(buffer, file)
        except MedhaError as e:
            unfinished = "end of file" in e.message or "unterminated" in e.message or "unclosed" in e.message
            if unfinished and line.strip():
                continue
            report([e], {file: buffer})
            buffer = ""
            continue
        source, buffer = buffer, ""
        interp.sources[file] = source
        try:
            errors = check(tree, file, known=env.names())
            if errors:
                report(errors, interp.sources)
                continue
            value = interp.exec_block(tree.stmts, env)
            if value is not None and tree.stmts and tree.stmts[-1].kind == "ExprStmt":
                sys.stdout.write((to_json(value) if isinstance(value, str) else show(value)) + "\n")
        except MedhaError as e:
            report([e], interp.sources)
        except ExitSignal as e:
            return e.code
        except KeyboardInterrupt:
            sys.stdout.write("\ninterrupted\n")
    rt.save_tape()
    return 0


COMMANDS = {"run": cmd_run, "test": cmd_test, "check": cmd_check, "schema": cmd_schema, "repl": cmd_repl}


def dispatch(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        sys.stdout.write(USAGE)
        return 0
    if argv[0] in ("version", "--version", "-V"):
        sys.stdout.write("medha %s\n" % __version__)
        return 0
    command = argv[0]
    rest = argv[1:]
    if command not in COMMANDS:
        if command.endswith(".mdh") or os.path.isfile(command):
            command, rest = "run", argv
        else:
            sys.stderr.write("medha: unknown command '%s'\n\n%s" % (command, USAGE))
            return 2
    try:
        opts, positional = parse_args(rest)
    except ValueError as e:
        return die(str(e))
    try:
        return COMMANDS[command](opts, positional)
    except OSError as e:
        return die(str(e), 1)


def main(argv=None):
    """Entry point. Runs the interpreter on a thread with a deep stack so recursion has room."""
    argv = sys.argv[1:] if argv is None else argv
    result = {}
    for stream in (sys.stdout, sys.stderr):
        # Diagnostics use a few non-ASCII characters; never let a legacy console encoding crash on them.
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower().replace("-", "") != "utf8":
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

    def target():
        try:
            result["code"] = dispatch(argv)
        except SystemExit as e:
            result["code"] = e.code
        except BaseException as e:  # surface crashes instead of dying silently in the thread
            result["error"] = e

    sys.setrecursionlimit(40000)
    try:
        threading.stack_size(256 * 1024 * 1024)
    except (ValueError, RuntimeError):
        pass
    if argv and argv[0] == "repl":
        code = dispatch(argv)
    else:
        t = threading.Thread(target=target, name="medha-main")
        t.start()
        try:
            t.join()
        except KeyboardInterrupt:
            sys.stderr.write("\ninterrupted\n")
            os._exit(130)
        if "error" in result:
            raise result["error"]
        code = result.get("code", 0)
    try:
        sys.stdout.flush()
    except (BrokenPipeError, OSError):
        pass
    return code or 0


if __name__ == "__main__":
    sys.exit(main())
