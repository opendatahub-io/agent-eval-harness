You are evaluating whether a bug-fix skill's output aligns with the actual accepted fix for a real pull request.

You have the ground truth:
1. The **accepted diff** — what was actually merged to fix the issue
2. The **reviewer comments** — feedback on the approach
3. The **PR description** — what the issue was

## Accepted Diff and Review Context

{{ annotations }}

## Skill's Output

{{ conversation }}

## Evaluation Criteria

Score 1-5 based on how closely the skill's fix aligns with the accepted solution:

- **5**: The skill produced changes that address the same root cause in a structurally similar way. The fix would be functionally equivalent to what was merged.
- **4**: The skill identified the right problem and touched the right code areas. The approach differs but would likely work. May need minor adjustments.
- **3**: The skill partially addressed the issue — right area but incomplete fix, or correct diagnosis but wrong approach.
- **2**: The skill attempted a fix but in the wrong location or with a fundamentally different (likely incorrect) approach.
- **1**: The skill's output doesn't address the actual problem, introduces new issues, or would not resolve the bug.

Key question: **Would this skill's fix have resolved the same issue the accepted PR resolved?**

A skill that produces a cleaner or more thorough fix than the accepted one should score 5 — the accepted diff is the baseline, not the ceiling.

Respond with a JSON object:
```json
{"score": <1-5>, "rationale": "<explanation>"}
```
