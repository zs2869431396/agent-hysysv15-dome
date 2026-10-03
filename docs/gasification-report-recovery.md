# Gasification report recovery

The workstation failed in the `explain` node while rendering
`normal_volume_conversion`. `hysys_tools.native_flow` returns its conversion as a
human-readable string; the adapter preserves that string. The report had assumed a
dictionary and called `.items()`. It now prints the tool's string unchanged and
continues to support the older dictionary form.

This traceback is a report failure, not enough by itself to establish the simulation
verdict. Do not repeat the model request or HYSYS execution to obtain the report.
After updating the checkout, run:

```bat
python scripts/recover_saved_report.py
```

The script chooses the latest `agent-runs/gasification-*/checkpoints.sqlite` by file
modification time and prints the selected original run. To select a specific run:

```bat
python scripts/recover_saved_report.py --run agent-runs/gasification-YYYYMMDD-HHMMSS
```

It opens the source SQLite database in read-only mode, copies it to an in-memory
snapshot, and reads the most recent top-level checkpoint. It requires completed
execution evidence and an existing PASS/PARTIAL/FAILED execution state; a dry-run
checkpoint cannot become an executed PASS. It does not resume the graph, load a
model credential, invoke an adapter or touch HYSYS.

The recovered report, state summary and compiled specs go to a separate
`recovered-report/` directory inside the original run. Original execution evidence
and checkpoint contents are preserved. If the latest checkpoint does not contain
completed execution results, the script stops; inspect the existing tool result
files before deciding whether a fresh simulation is needed.

Regression tests cover string and dictionary conversion forms, an absent conversion,
an actual SQLite checkpoint saved before an injected report failure, unchanged
model/adapter call counts during recovery, preserved original checkpoint bytes, and
refusal to promote a dry run or missing checkpoint to a successful execution.
