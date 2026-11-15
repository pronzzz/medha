"""Medha — a programming language for AI programs.

Embedding the interpreter::

    from medha import run
    env = run('print("hello")')
"""
__version__ = "0.1.0"

from .ai import Runtime  # noqa: E402
from .errors import MedhaError  # noqa: E402
from .interp import Interp  # noqa: E402
from .parser import parse  # noqa: E402


def run(source, file="<input>", dry_run=False, **runtime_options):
    """Run Medha source text and return the interpreter (its top-level names are in ``.last_env``)."""
    interp = Interp(Runtime(dry_run=dry_run, **runtime_options))
    interp.last_env = interp.run_source(source, file)
    interp.run_main(interp.last_env)
    return interp


__all__ = ["run", "parse", "Interp", "Runtime", "MedhaError", "__version__"]
