"""Reactor-selection agent for the HYSYS tool layer.

This package turns a natural-language request into an executable HYSYS
specification. It never talks to COM itself: all simulation work goes through
`hysys_tools`, which already owns the case lifecycle, pre-flight checks and
independent validation.

The pipeline, top to bottom:

    llm.py          one endpoint, with a fallback chain and local rate control
    extraction.py   natural language -> facts, then check them (② and ③ below)
    normalize.py    facts -> a strict ProcessRequest (deterministic, no model)
    selection.py    ProcessRequest -> which reactor, and why (deterministic rules)
    compiler.py     ModelingPlan -> hysys-agent/spec/1, then pre-check it

Layering, deliberately kept separate:

    ProcessRequest   what the user actually asked for, with the source of every
                     field. Fields may be missing - that is information too.
    ModelingPlan     what we decided to model: reactor choice, species, thermal
                     boundary, assumptions, and one entry per operating case.
    ToolSpec         the strict hysys-agent/spec/1 document the tool layer accepts.

The point of the three layers is that the model's job ends at ProcessRequest.
Nothing model-generated becomes a COM call, a file path or a subprocess argument
without passing through deterministic Python first.

Fact handling follows the four-layer pattern borrowed from GWOA's parser:

    ① rules -> ② LLM -> ③ validation -> ④ confirmation

`normalize.py` is the rules layer, `extraction.py` is ② and ③, and the blocking
questions raised along the way are ④. The validation layer asks a question that
format checking cannot - did the user actually say this number? - which is what
catches a well-formed invention.

Dependencies: `pydantic` for the contracts; everything else is standard library,
so the pipeline imports and tests without HYSYS, pywin32 or a network.
"""

from .schemas import (
    Assumption,
    ModelingPlan,
    OperatingCase,
    ProcessRequest,
    Question,
    SelectionDecision,
)
from .capabilities import capability_report, combination_status, is_executable
from .selection import candidate_products, select_reactor, selection_questions
from .compiler import CompileError, compile_case, compile_plan, safe_case_name
from .compiler import coal_questions, feed_questions
from .llm import ChatClient, LlmConfig, LlmError, SlidingWindow, describe_config
from .extraction import (
    EXTRACTION_SCHEMA,
    REQUIRED_BY_KIND,
    Extraction,
    extract,
    extract_verified,
    grounding_failures,
)
from .normalize import (
    SPECIES_ALIASES,
    NormalizationReport,
    expand_ambiguous_species,
    flow_is_ours_to_choose,
    normalize,
    resolve_species,
)

__all__ = [
    # contracts
    'Assumption', 'ModelingPlan', 'OperatingCase', 'ProcessRequest', 'Question',
    'SelectionDecision',
    # capabilities and selection
    'capability_report', 'combination_status', 'is_executable',
    'select_reactor', 'selection_questions', 'candidate_products',
    # compilation
    'CompileError', 'compile_case', 'compile_plan', 'safe_case_name',
    'feed_questions', 'coal_questions',
    # model client
    'ChatClient', 'LlmConfig', 'LlmError', 'SlidingWindow', 'describe_config',
    # extraction and grounding
    'EXTRACTION_SCHEMA', 'REQUIRED_BY_KIND', 'Extraction', 'extract',
    'extract_verified', 'grounding_failures',
    # normalisation
    'SPECIES_ALIASES', 'NormalizationReport', 'normalize', 'resolve_species',
    'expand_ambiguous_species', 'flow_is_ours_to_choose',
]
__version__ = '0.2'
