"""`reward.score_range` as an explicit, deprecated fallback.

A judge's own `score_range` now normalizes it in every reward composition.
`reward.score_range` survives only for composed judges that declare none, and
warns where it used to win. The warning has to be narrow: it fires only where
the precedence change actually moves a number, or it becomes noise on configs
that are already correct.
"""

import warnings

import pytest
import yaml

from agent_eval.config import EvalConfig, RewardConfig

_T = {"name": "testability", "feedback_type": "int", "score_range": [0, 2],
      "prompt": "p"}
_C = {"name": "clarity", "feedback_type": "int", "score_range": [1, 5],
      "prompt": "p"}
_PLAIN = {"name": "plain", "feedback_type": "int", "prompt": "p"}


def _load(tmp_path, judges, reward):
    raw = {"execution": {"mode": "case", "prompt": "x"},
           "dataset": {"path": "cases", "schema": "s"},
           "mlflow": {"experiment": "t"},
           "judges": judges, "reward": reward}
    path = tmp_path / "eval.yaml"
    path.write_text(yaml.safe_dump(raw))
    return EvalConfig.from_yaml(path)


def _warnings(tmp_path, judges, reward):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _load(tmp_path, judges, reward)
    return [str(w.message) for w in caught
            if "reward.score_range" in str(w.message)]


class TestOptionalFallback:

    def test_an_omitted_score_range_parses_to_none(self, tmp_path):
        config = _load(tmp_path, [_C],
                       {"formula": "weighted", "weights": {"clarity": 1.0}})
        assert config.reward.score_range is None

    def test_the_resolved_fallback_is_one_to_five(self):
        assert RewardConfig().effective_score_range == [1.0, 5.0]

    def test_an_explicit_range_is_kept_verbatim(self, tmp_path):
        config = _load(tmp_path, [_C], {"formula": "weighted",
                                        "weights": {"clarity": 1.0},
                                        "score_range": [1, 10]})
        assert config.reward.score_range == [1.0, 10.0]
        assert config.reward.effective_score_range == [1.0, 10.0]

    @pytest.mark.parametrize("bad,message", [
        ("nope", "must be a \\[min, max\\] list"),
        ([1], "must be a \\[min, max\\] list"),
        (["a", "b"], "values must be numeric"),
        ([5, 1], "must be increasing"),
    ])
    def test_a_written_range_is_still_validated(self, tmp_path, bad, message):
        with pytest.raises(ValueError, match=message):
            _load(tmp_path, [_C], {"formula": "weighted",
                                   "weights": {"clarity": 1.0},
                                   "score_range": bad})


class TestDeprecationWarning:

    def test_it_names_only_the_shadowed_judges(self, tmp_path):
        msgs = _warnings(tmp_path, [_T, _C, _PLAIN],
                         {"formula": "weighted", "gate": False,
                          "weights": {"testability": 0.5, "plain": 0.5},
                          "score_range": [1, 5]})
        assert len(msgs) == 1
        assert "'testability' [0.0, 2.0]" in msgs[0]
        # 'plain' declares nothing, so the fallback still governs it.
        assert "still applies to 'plain'" in msgs[0]
        # 'clarity' is not composed, so it is not mentioned at all.
        assert "clarity" not in msgs[0]

    def test_it_says_so_when_nothing_needs_the_fallback(self, tmp_path):
        msgs = _warnings(tmp_path, [_T, _C],
                         {"formula": "weighted", "gate": False,
                          "weights": {"testability": 0.5, "clarity": 0.5},
                          "score_range": [1, 5]})
        assert "No composed judge relies on it any more" in msgs[0]

    def test_an_expression_only_warns_about_names_it_reads(self, tmp_path):
        unused = {"name": "unused", "feedback_type": "int",
                  "score_range": [0, 10], "prompt": "p"}
        msgs = _warnings(tmp_path, [_T, _C, unused],
                         {"formula": "0.6 * testability + 0.4 * clarity",
                          "gate": False, "score_range": [1, 5]})
        assert "testability" in msgs[0] and "unused" not in msgs[0]

    def test_single_judge_with_normalize_warns(self, tmp_path):
        msgs = _warnings(tmp_path, [_T],
                         {"judge": "testability", "normalize": True,
                          "score_range": [1, 5]})
        assert "'testability' [0.0, 2.0]" in msgs[0]

    @pytest.mark.parametrize("judges,reward", [
        # No range written — nothing was overridden, so nothing to deprecate.
        ([_T, _C], {"formula": "weighted", "gate": False,
                    "weights": {"testability": 0.5, "clarity": 0.5}}),
        # Ranges agree — the number does not move.
        ([_C], {"formula": "weighted", "weights": {"clarity": 1.0},
                "gate": False, "score_range": [1, 5]}),
        # `raw` short-circuits before any range is consulted.
        ([_T], {"formula": "weighted", "weights": {"testability": 1.0},
                "gate": False, "raw": ["testability"], "score_range": [1, 5]}),
        # The conflicting judge is not composed at all.
        ([_T, _C], {"formula": "weighted", "weights": {"clarity": 1.0},
                    "gate": False, "score_range": [1, 5]}),
        # A clamped single judge never normalizes.
        ([_T], {"judge": "testability", "score_range": [1, 5]}),
    ])
    def test_it_stays_silent_when_no_number_moves(self, tmp_path, judges, reward):
        assert _warnings(tmp_path, judges, reward) == []


class TestClampedSingleJudge:
    """`reward: {judge: x}` clamps rather than normalizes. On a scale wider
    than [0, 1] that silently saturates: every value >= 1 is a perfect reward."""

    def test_it_warns_on_a_wider_scale(self, tmp_path):
        with pytest.warns(UserWarning, match="clamped to \\[0, 1\\]"):
            _load(tmp_path, [_T], {"judge": "testability"})

    @pytest.mark.parametrize("judges,reward", [
        # Already [0, 1] — clamping is exactly right.
        ([{"name": "rm", "feedback_type": "float", "score_range": [0, 1],
           "prompt": "p"}], {"judge": "rm"}),
        # normalize: true maps the value instead of clamping it.
        ([_T], {"judge": "testability", "normalize": True}),
        # No declared range — nothing to contradict.
        ([_PLAIN], {"judge": "plain"}),
    ])
    def test_it_stays_silent_when_clamping_is_right(self, tmp_path, judges, reward):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _load(tmp_path, judges, reward)
        assert [str(w.message) for w in caught if "clamped" in str(w.message)] == []
