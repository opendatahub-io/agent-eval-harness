<!-- Evaluates whether the agent output is complete and addresses all aspects of the input. -->
<!-- Required fields: conversation, files -->
<!-- Failure means: The agent output is incomplete, missing key elements, or only partially addresses the task. -->

You are evaluating an AI agent's output for completeness.

Strictness level: {{ arguments.strictness | default('medium') }}

{% if arguments.criteria %}
**Specific criteria to check**:
{% for criterion in arguments.criteria %}
- {{ criterion }}
{% endfor %}
{% endif %}

## Agent Output

{{ outputs | tojson }}

## Instructions

Evaluate whether the output is complete. Consider:

{% if arguments.strictness | default('medium') == 'high' %}
- Every aspect of the task must be fully addressed
- All edge cases should be handled
- Documentation and error handling must be present
{% elif arguments.strictness | default('medium') == 'low' %}
- The core task should be addressed
- Minor omissions are acceptable
{% else %}
- The main requirements should be met
- Important edge cases should be considered
- Reasonable completeness is expected
{% endif %}

Respond with a JSON object:
{"passed": true, "rationale": "Output is complete: <explanation>"}
or
{"passed": false, "rationale": "Output is incomplete: <what is missing>"}
