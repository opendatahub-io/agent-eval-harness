"""OpenAI-compatible API runner implementation.

Evaluates models served via any OpenAI-compatible endpoint (vLLM, KServe,
Ollama, LiteLLM, etc.) by sending prompts to /v1/chat/completions.

Unlike the Claude Code runner which invokes skills via the CLI, this runner
sends the test case input directly to the model as a chat completion request
and captures the response as the output artifact.
"""

import json
import os
import time
from pathlib import Path
from typing import Optional

from .base import EvalRunner, RunResult


class OpenAICompatibleRunner(EvalRunner):
    """Runs evaluation cases against an OpenAI-compatible chat completions API."""

    def __init__(
        self,
        base_url: str = "",
        api_key: str = "",
        default_model: str = "",
        system_prompt: Optional[str] = None,
        max_tokens: int = 512,
        temperature: float = 0.3,
    ):
        self._base_url = base_url or os.environ.get("OPENAI_BASE_URL", "")
        if not self._base_url:
            raise ValueError(
                "OpenAI-compatible runner requires base_url argument or "
                "OPENAI_BASE_URL environment variable"
            )
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self._default_model = default_model
        self._system_prompt = system_prompt
        self._max_tokens = max_tokens
        self._temperature = temperature

    @property
    def name(self) -> str:
        return "openai-compatible"

    def run_skill(
        self,
        skill_name: str,
        args: str,
        workspace: Path,
        model: str,
        settings_path: Optional[Path] = None,
        system_prompt: Optional[str] = None,
        max_budget_usd: float = 5.0,
        timeout_s: int = 600,
    ) -> RunResult:
        """Send the prompt to the model and capture the response.

        For this runner, `args` is the resolved prompt (the diff to review).
        The response is written to workspace/artifacts/response.md only on
        success; on failure the artifacts directory is left empty so judges
        can distinguish infra errors from bad model output.
        """
        import urllib.request
        import urllib.error

        effective_model = model or self._default_model
        effective_system = system_prompt or self._system_prompt or ""
        api_url = self._base_url.rstrip("/")
        if api_url.endswith("/v1"):
            api_url = api_url[:-3]
        if not api_url.endswith("/v1/chat/completions"):
            api_url = api_url + "/v1/chat/completions"

        messages = []
        if effective_system:
            messages.append({"role": "system", "content": effective_system})
        messages.append({"role": "user", "content": args})

        payload = json.dumps({
            "model": effective_model,
            "messages": messages,
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
        }).encode()

        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        start = time.monotonic()
        stdout_lines = []
        stderr_text = ""
        exit_code = 0
        response_text = ""
        token_usage = None
        cost_usd = None
        body = None

        try:
            req = urllib.request.Request(api_url, data=payload, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                body = json.loads(resp.read().decode())

            choices = body.get("choices", [])
            if not choices:
                exit_code = 1
                stderr_text = "API returned empty choices array"
            else:
                response_text = choices[0]["message"]["content"]
                stdout_lines.append(response_text)

            usage = body.get("usage", {})
            if usage:
                token_usage = {
                    "input": usage.get("prompt_tokens", 0),
                    "output": usage.get("completion_tokens", 0),
                }

        except urllib.error.HTTPError as e:
            exit_code = 1
            try:
                err_body = e.read().decode()[:500]
            except Exception:
                err_body = "(could not read error body)"
            stderr_text = f"HTTP {e.code}: {err_body}"
        except urllib.error.URLError as e:
            exit_code = 1
            stderr_text = f"Connection error: {e.reason}"
        except Exception as e:
            exit_code = 1
            stderr_text = str(e)

        duration = time.monotonic() - start

        artifacts_dir = workspace / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        if exit_code == 0 and response_text:
            (artifacts_dir / "response.md").write_text(response_text)
        else:
            (artifacts_dir / "error.txt").write_text(
                f"Runner failed (exit_code={exit_code}): {stderr_text}")

        run_result = {
            "exit_code": exit_code,
            "duration_s": round(duration, 2),
            "token_usage": token_usage,
            "cost_usd": cost_usd,
            "num_turns": 1,
            "model": effective_model,
        }
        (workspace / "run_result.json").write_text(json.dumps(run_result, indent=2))
        (workspace / "stdout.log").write_text("\n".join(stdout_lines))

        return RunResult(
            exit_code=exit_code,
            stdout="\n".join(stdout_lines),
            stderr=stderr_text,
            duration_s=duration,
            token_usage=token_usage,
            cost_usd=cost_usd,
            num_turns=1,
            resolved_model=effective_model,
            raw_output=body if exit_code == 0 else None,
        )
