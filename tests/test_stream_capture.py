"""Tests for agent_eval.agent.stream_capture — turn counting and deduplication."""

import json

from conftest import make_assistant, make_result, to_jsonl

from agent_eval.agent.stream_capture import (
    _is_real_model, count_subagent_turns, count_subagent_turns_by_model,
    extract_usage,
)


class TestExtractUsage:
    def test_returns_seen_ids(self, post_2_1_108_stream):
        """extract_usage returns a 7-tuple including the seen_msg_ids set."""
        result = extract_usage(post_2_1_108_stream["lines"])
        assert len(result) == 7
        _, _, num_turns, seen_msg_ids, _, _, _ = result
        assert isinstance(seen_msg_ids, set)
        assert len(seen_msg_ids) == num_turns

    def test_includes_subagent_ids_post_2_1_108(self, post_2_1_108_stream):
        """Post-2.1.108: seen_msg_ids includes foreground subagent message IDs."""
        _, _, num_turns, seen_ids, _, _, _ = extract_usage(post_2_1_108_stream["lines"])
        assert num_turns == 7  # 5 root + 2 foreground subagent
        assert "msg_001" in seen_ids
        assert "msg_sub_001" in seen_ids
        assert "msg_sub_002" in seen_ids

    def test_prefers_model_usage(self):
        """When modelUsage is present in result, token_usage comes from it."""
        events = [
            make_assistant("msg_001", input_tokens=100, output_tokens=50),
            make_result(model_usage={
                "claude-sonnet-4-5": {
                    "inputTokens": 9000,
                    "outputTokens": 3000,
                    "cacheReadInputTokens": 500,
                    "cacheCreationInputTokens": 200,
                    "costUSD": 0.42,
                },
            }),
        ]
        token_usage, _, _, _, _, per_model, _ = extract_usage(to_jsonl(events))
        # Should use modelUsage totals, not assistant event usage
        assert token_usage["input"] == 9000
        assert token_usage["output"] == 3000
        assert per_model["claude-sonnet-4-5"]["cost_usd"] == 0.42

    def test_returns_per_model_msg_ids(self):
        """seen_msg_ids_by_model groups assistant message IDs by their model."""
        events = [
            make_assistant("msg_001", model="claude-opus-4-7"),
            make_assistant("msg_002", model="claude-opus-4-7"),
            make_assistant("msg_003", model="claude-sonnet-4-6"),
        ]
        _, _, num_turns, _, _, _, by_model = extract_usage(to_jsonl(events))
        assert by_model == {
            "claude-opus-4-7": {"msg_001", "msg_002"},
            "claude-sonnet-4-6": {"msg_003"},
        }
        assert num_turns == 3


class TestCountSubagentTurns:
    def test_deduplicates_with_already_seen(self, tmp_path, post_2_1_108_stream):
        """With already_seen, only genuinely new IDs are counted."""
        already_seen = {"msg_sub_001", "msg_sub_002"}
        new = count_subagent_turns(
            post_2_1_108_stream["subagent_dir"], already_seen=already_seen)
        assert new == 1  # only msg_sub_003

    def test_counts_all_without_already_seen(self, post_2_1_108_stream):
        """Without already_seen, counts all unique assistant IDs."""
        total = count_subagent_turns(post_2_1_108_stream["subagent_dir"])
        assert total == 3

    def test_returns_zero_for_missing_dir(self, tmp_path):
        """Non-existent directory returns 0."""
        assert count_subagent_turns(tmp_path / "nonexistent") == 0


class TestCountSubagentTurnsByModel:
    def test_returns_empty_for_missing_dir(self, tmp_path):
        """Non-existent directory returns empty dict."""
        assert count_subagent_turns_by_model(tmp_path / "nonexistent") == {}

    def test_groups_new_turns_by_model(self, post_2_1_108_stream):
        """Without already_seen, groups all subagent turns by their model."""
        per_model = count_subagent_turns_by_model(
            post_2_1_108_stream["subagent_dir"])
        # All subagent turns should be attributed to some model. Sum equals
        # the int returned by count_subagent_turns for the same dir.
        assert sum(per_model.values()) == 3

    def test_dedupes_against_already_seen_per_model(self, post_2_1_108_stream):
        """With already_seen_by_model, only NEW per-model IDs are counted."""
        # Pre-seed with all 3 subagent IDs under whatever model they belong to;
        # we don't know that model from the fixture, so use the subagent dir's
        # actual content to construct already_seen_by_model from a first pass.
        all_seen = count_subagent_turns_by_model(
            post_2_1_108_stream["subagent_dir"])
        # Re-pass with the already-counted IDs as "seen" per model — should be 0.
        # Use the by-model totals from a no-op call as a proxy.
        # (Simpler: pass an exhaustive set per model.)
        seeded = {m: {"msg_sub_001", "msg_sub_002", "msg_sub_003"} for m in all_seen}
        new = count_subagent_turns_by_model(
            post_2_1_108_stream["subagent_dir"], already_seen_by_model=seeded)
        assert new == {}


class TestCombinedTurnCount:
    def test_no_double_count_post_2_1_108(self, post_2_1_108_stream):
        """Post-2.1.108: combined count deduplicates overlapping IDs."""
        _, _, stream_turns, stream_ids, _, _, _ = extract_usage(
            post_2_1_108_stream["lines"])
        new_turns = count_subagent_turns(
            post_2_1_108_stream["subagent_dir"], already_seen=stream_ids)
        total = stream_turns + new_turns
        assert total == post_2_1_108_stream["expected_total"]  # 8, not 10

    def test_no_overlap_pre_2_1_108(self, pre_2_1_108_stream):
        """Pre-2.1.108: no overlap, sum is correct without dedup changing anything."""
        _, _, stream_turns, stream_ids, _, _, _ = extract_usage(
            pre_2_1_108_stream["lines"])
        new_turns = count_subagent_turns(
            pre_2_1_108_stream["subagent_dir"], already_seen=stream_ids)
        total = stream_turns + new_turns
        assert total == pre_2_1_108_stream["expected_total"]  # 8
        assert stream_turns == 5
        assert new_turns == 3


class TestPlaceholderModel:
    """`<synthetic>` (and other <bracketed> pseudo-models) are counted for
    dedup/totals but excluded from per-model accounting."""

    def test_is_real_model(self):
        assert _is_real_model("claude-opus-4-7") is True
        assert _is_real_model("<synthetic>") is False
        assert _is_real_model("") is False
        assert _is_real_model(None) is False

    def test_extract_usage_excludes_placeholder(self):
        events = [
            make_assistant("msg_real", model="claude-opus-4-7"),
            make_assistant("msg_synth", model="<synthetic>"),
            make_result(cost_usd=0.10),
        ]
        (_tok, _cost, num_turns, seen_ids, models_seen,
         _per_model, by_model) = extract_usage(to_jsonl(events))
        # Placeholder turn still counts toward totals and dedup ...
        assert num_turns == 2
        assert "msg_synth" in seen_ids
        # ... but never pollutes per-model accounting: both models_seen and the
        # per-model msg-id map exclude the pseudo-model.
        assert models_seen == {"claude-opus-4-7"}
        assert set(by_model) == {"claude-opus-4-7"}

    def test_subagent_turns_exclude_placeholder(self, tmp_path):
        subdir = tmp_path / "subagents"
        subdir.mkdir()
        lines = [
            {"message": {"id": "s1", "role": "assistant",
                         "model": "claude-opus-4-7"}},
            {"message": {"id": "s2", "role": "assistant",
                         "model": "<synthetic>"}},
        ]
        (subdir / "agent-x.jsonl").write_text(
            "\n".join(json.dumps(x) for x in lines) + "\n")
        per_model = count_subagent_turns_by_model(subdir)
        assert per_model == {"claude-opus-4-7": 1}
        assert "<synthetic>" not in per_model
