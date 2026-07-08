You are evaluating whether a code review skill's output aligns with the actual outcome of a real pull request.

You have three sources of ground truth:
1. The **accepted diff** — what was actually merged (the definitive answer)
2. The **reviewer comments** — what human reviewers flagged during the review process
3. The **verdict** — whether reviewers approved or requested changes

## Accepted Diff and Review Context

{{ annotations }}

## Skill's Output

{{ conversation }}

## Evaluation Criteria

Score 1-5 based on how useful the skill's review would have been in reaching the accepted outcome:

- **5**: The skill identified the same core issues reviewers raised AND its feedback would have guided the author toward the accepted diff. Demonstrates understanding of what needed to change and why.
- **4**: The skill caught most of the substantive issues and its direction aligns with the accepted changes. Minor gaps but would have been genuinely helpful.
- **3**: The skill identified some relevant issues but missed key problems, or found real issues but gave vague/unhelpful guidance. Partially useful.
- **2**: The skill's feedback has little overlap with what actually mattered. It may have focused on superficial issues while missing structural problems the reviewers caught.
- **1**: The skill's output is irrelevant to the actual PR, contradicts the accepted changes, or would have sent the author in the wrong direction.

Key question: **If someone acted only on this skill's review, would the PR have converged toward the accepted diff?**

A skill that finds legitimate issues the reviewers missed should NOT be penalized — that's a bonus. But the primary measure is alignment with what was actually needed.

Respond with a JSON object:
```json
{"score": <1-5>, "rationale": "<explanation>"}
```
