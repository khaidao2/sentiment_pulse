# sentiment_pulse_clean

## Commits — `ctx`
`ctx for <path>[:<a>-<b>]` before editing: a `rejected` entry is an alternative
already discarded, so don't re-propose it without addressing why; `open_incidents`
outranks everything else in that output. Every commit ends with trailers in the last
paragraph, one per line — `Decision:`, `Rejected:` (repeatable),
`Reversible: true|restore-only|never`, plus `Oracle: drift|test|contract` whenever
`Reversible: never`. Run `ctx gate` before pushing; it exits 1 on a violation.
Reverse an earlier decision with `ctx supersede <sha> -reason "<what changed>"`,
never a hand-edit of `refs/notes/decisions`.

## Project metadata — `codeops`
`codeops orient -C .` instead of reading the README and guessing: declared intent,
derived facts and the current drift list in one call. `codeops check -C .` exits
non-zero on drift. `.codeops.yaml` is tracked here — keep it honest rather than letting the
description quietly stop being true.

## Development — `/pstack:poteto-mode`
Open non-trivial work with `/pstack:poteto-mode` — it picks a playbook, opens the
todo list from its steps and pulls in the principle skills. Name the principle behind
each decision. Edits and tests proceed on their own; pushes, PRs and deploys wait for
explicit approval.

## Subagents — `agentctl`
Bulk, parallel or verbose work goes to a local agent, `omp` by default:
`A=$(agentctl spawn omp "<self-contained task>" -C . --label <name>)`, one
`agentctl wait`, then `agentctl result "$A"`. A subagent gets no conversation history,
so state the goal, the directory and the answer's exact shape. Full protocol in
`~/.claude/CLAUDE.md`; pstack's own fan-out (`swarm`, `poteto-agent`) routes here too.
