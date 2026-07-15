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
