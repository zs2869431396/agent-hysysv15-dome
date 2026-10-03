"""Adapters between the agent and the outside world.

  `hysys_cli`  runs the tool layer as a subprocess, one case per call.
  `run_store`  keeps the execution ledger, so a resumed run does not redo work.

Everything here is deliberately narrow. The agent decides *what* to model; these
modules decide *how a process is started, where its files go, and what is recorded*,
and they do it with no model input at all.
"""
from .hysys_cli import ExecutionResult, HysysCliAdapter, WorkstationLock
from .run_store import RunStore

__all__ = ['ExecutionResult', 'HysysCliAdapter', 'WorkstationLock', 'RunStore']
