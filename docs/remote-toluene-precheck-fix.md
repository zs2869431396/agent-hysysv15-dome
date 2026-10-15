# First workstation Agent pre-check failure

The workstation's tool self-check and COM health check passed. The Agent run then
failed before simulation: `2 toluene -> benzene + o-xylene + m-xylene + p-xylene`
does not conserve elements. All three isomers were assigned coefficient 1, although
the written equation gives their group C8H10 a total coefficient of 1. The existing
equal-split assumption reported thirds but did not enforce the group total for
isomers the model had already listed separately.

The normalizer now restores the group total only when a unique written equation
agrees with all other species and coefficients, the model's isomer coefficients are
equal and have the correct sign, and the proposed correction conserves elements.
The transformation is logged and the equal split remains an explicit assumption.
It does not repair arbitrary unbalanced chemistry or change an unequal split.
Six offline regression tests reproduce the reported coefficient shape and cover
the conditions in which correction is not justified. The reproduction is based on
the terminal error, not a claimed verbatim model response.

No paid model request or HYSYS simulation was repeated during this repair.

## Workstation update

For a Git checkout, activate the existing environment, enter the project directory
and run `git pull --ff-only`. For a ZIP download, download the updated repository ZIP
and extract it into a new folder, retaining the previous run artifacts.

Run `python -m unittest reactor_agent.test_remote_regressions` first. Then inspect
the tool acceptance without calling a model or HYSYS:

```bat
python -c "from reactor_agent.capabilities import acceptance_state; print(acceptance_state())"
```

The local tool files still match the accepted hashes. The workstation's experimental
label needs the above diagnostic: it cannot be proven from the terminal error alone.
Windows Git may convert LF to CRLF during checkout, invalidating raw-byte hashes
without changing Python semantics. The acceptance uses LF for twelve runtime files
and CRLF for `hysys_tools/main.py`. `.gitattributes` preserves both conventions on
new checkouts. Existing checkouts can run
`python scripts/restore_accepted_line_endings.py --apply`: it checks all candidate
bytes against the accepted hashes before writing anything, preserves files already
matching the acceptance, and stops if any difference is beyond line endings. No
capability is forcibly marked verified.

After the offline reproduction passes, the toluene command can be retried on the
workstation. Only a real successful execution establishes end-to-end acceptance.
