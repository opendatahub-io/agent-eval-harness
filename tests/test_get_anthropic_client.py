"""Tests for _get_anthropic_client() in score.py."""

import os
from unittest.mock import patch

import pytest

from score import _get_anthropic_client


class TestGetAnthropicClient:

    # Vertex AI path: GCP_SA_ACCESS_TOKEN should be forwarded to AnthropicVertex
    @patch.dict(os.environ, {
        "ANTHROPIC_VERTEX_PROJECT_ID": "my-project",
        "GCP_SA_ACCESS_TOKEN": "test-token",
    }, clear=True)
    def test_vertex_with_access_token(self):
        from anthropic import AnthropicVertex
        client = _get_anthropic_client()
        assert isinstance(client, AnthropicVertex)
        assert client.project_id == "my-project"
        assert client.region == "us-east5"
        assert client.access_token == "test-token"

    # Vertex AI path without token: access_token should be None (falls back to google-auth)
    @patch.dict(os.environ, {
        "ANTHROPIC_VERTEX_PROJECT_ID": "my-project",
    }, clear=True)
    def test_vertex_without_access_token(self):
        from anthropic import AnthropicVertex
        client = _get_anthropic_client()
        assert isinstance(client, AnthropicVertex)
        assert client.access_token is None

    # OpenShell >= 0.1.2: the placeholder the gateway injects is the access token
    # (the proxy swaps it on the Vertex hosts), and the host agentic-ci picked
    # for Claude Code is the judges' base URL too.
    @patch.dict(os.environ, {
        "ANTHROPIC_VERTEX_PROJECT_ID": "my-project",
        "CLOUD_ML_REGION": "global",
        "GOOGLE_VERTEX_AI_SERVICE_ACCOUNT_TOKEN": "openshell-placeholder",
        "ANTHROPIC_VERTEX_BASE_URL": "https://aiplatform.googleapis.com/v1",
    }, clear=True)
    def test_vertex_with_openshell_placeholder(self):
        from anthropic import AnthropicVertex
        client = _get_anthropic_client()
        assert isinstance(client, AnthropicVertex)
        assert client.access_token == "openshell-placeholder"
        assert str(client.base_url).rstrip("/") == "https://aiplatform.googleapis.com/v1"

    @patch.dict(os.environ, {
        "ANTHROPIC_VERTEX_PROJECT_ID": "my-project",
        "GOOGLE_VERTEX_AI_TOKEN": "adc-placeholder",
    }, clear=True)
    def test_vertex_with_adc_placeholder(self):
        client = _get_anthropic_client()
        assert client.access_token == "adc-placeholder"

    # The older provider's token still wins when both are present.
    @patch.dict(os.environ, {
        "ANTHROPIC_VERTEX_PROJECT_ID": "my-project",
        "GCP_SA_ACCESS_TOKEN": "legacy-token",
        "GOOGLE_VERTEX_AI_SERVICE_ACCOUNT_TOKEN": "openshell-placeholder",
    }, clear=True)
    def test_legacy_token_takes_precedence(self):
        client = _get_anthropic_client()
        assert client.access_token == "legacy-token"

    # Direct API path: ANTHROPIC_API_KEY should produce a standard Anthropic client
    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}, clear=True)
    def test_api_key(self):
        from anthropic import Anthropic
        client = _get_anthropic_client()
        assert isinstance(client, Anthropic)

    # Direct API path: ANTHROPIC_AUTH_TOKEN should work as a fallback for API key
    @patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": "sk-auth"}, clear=True)
    def test_auth_token_fallback(self):
        from anthropic import Anthropic
        client = _get_anthropic_client()
        assert isinstance(client, Anthropic)

    # No credentials set: should raise RuntimeError with guidance
    @patch.dict(os.environ, {}, clear=True)
    def test_no_credentials_raises(self):
        with pytest.raises(RuntimeError, match="Set ANTHROPIC_VERTEX_PROJECT_ID"):
            _get_anthropic_client()
