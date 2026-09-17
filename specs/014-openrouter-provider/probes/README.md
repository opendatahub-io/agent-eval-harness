# Probe evidence for spec 014 (OpenRouter provider)

Machine-readable outcomes of the verification probes listed in `../spec.md`
("Verification checklist before implementation"). None of these files contains key
material: both producer scripts read `OPENROUTER_API_KEY` from the environment and
redact it from every string before writing.

From report **schema 2** onwards every report carries a `producer` object
(`script`, `schema`, `git_sha` of the script's last commit, `dirty`). Files without it
were produced by an earlier revision of the same script and are kept as **historical
evidence** — do not expect their per-probe evidence keys to match the current script.

| File | Producer | Revision | Status | Notes |
| --- | --- | --- | --- | --- |
| `probe_report_2026-09-16_run1.json` | `scripts/probe_openrouter.py` | pre-isolation revision (before commit `c1f49c1`; schema 1, no `producer` stamp) | **historical** | First full run (probes 3, 4, 5, 6, 7, 8, 12, 13, 16, 17, 18, 19, 20). Probe 5 has no `isolation`/`config_used_for_tally`/`first_error` (all 20 requests errored and the cause was not yet captured); probe 12 has no `generation_lookup_latency_s` (the `/generation` lookup ran before the ~10 s lag elapsed). Its `endpoints_selected: null` in probes 3/4 reflects the pre-fix field name, not the API. Superseded for probes 3, 5 and 12 by `_run2.json`; still the evidence for 4, 6, 7, 8, 13, 16, 19, 20. |
| `probe_report_2026-09-16_run2.json` | `scripts/probe_openrouter.py` | isolation revision (as committed in `c1f49c1`; schema 1) | current for probes 3, 5, 12 | Re-run of `--only 3 5 12`: raw `openrouter_metadata` shape (`endpoints.available[].selected`), probe-5 configuration isolation (forced `tool_choice` + pins → 404), probe-12 `/generation` lookup after the lag. |
| `probe_cli_report_2026-09-16.json` | `scripts/probe_claude_cli.py` | schema 2 (see the file's `producer` stamp) | current | No-key Claude Code CLI 2.1.273 scenarios against a local fake Anthropic endpoint (rows 1, 9, 10, 22, 23, 27). |

To refresh a report with the current schema, re-run the producer (the KEY probes need
`OPENROUTER_API_KEY` exported by the operator; the CLI probes need no key):

```
python3 scripts/probe_openrouter.py --out specs/014-openrouter-provider/probes/probe_report_<date>.json
python3 scripts/probe_claude_cli.py --out specs/014-openrouter-provider/probes/probe_cli_report_<date>.json
```

Add a row here for every new file, and never overwrite a file that the spec cites.
