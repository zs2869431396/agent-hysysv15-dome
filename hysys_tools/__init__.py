"""HYSYS tool layer driven by structured specifications.

Public surface for an agent or a host application:

    from hysys_tools import main, core, validate

    main.list_capabilities()        # what specs this tool accepts
    main.health_check(pythoncom, win32com)
    main.build_case(spec, folder, pythoncom, win32com, app)   # one case per call

Nothing in this package imports COM at module import time, so every part except
the thin COM calls can be exercised on a machine without HYSYS.
"""

from .core import (  # noqa: F401
    REACTOR_FACTORY,
    REACTOR_TYPE_NAMES,
    RESULT_SCHEMA,
    SPEC_SCHEMA,
    StepLog,
    SpecError,
    feed_molar_flows,
    feed_molar_mass,
    load_spec,
    write_json,
)
from .validate import ResultCheckError  # noqa: F401
from . import core, examples, precheck, validate  # noqa: F401
from .main import (  # noqa: F401
    build_case,
    health_check,
    list_capabilities,
    main,
)

__all__ = [
    'REACTOR_FACTORY', 'REACTOR_TYPE_NAMES', 'RESULT_SCHEMA', 'SPEC_SCHEMA',
    'StepLog', 'SpecError', 'ResultCheckError', 'load_spec', 'write_json',
    'feed_molar_flows', 'feed_molar_mass',
    'core', 'examples', 'precheck', 'validate',
    'build_case', 'health_check', 'list_capabilities', 'main',
]
__version__ = '1.0'
