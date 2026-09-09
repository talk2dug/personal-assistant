"""Pure-logic coverage of assistant/core/identity.py's matching -- no GPU/InsightFace
needed to exercise cosine_similarity/match_known_person, which is the part that's
actually testable without real camera hardware."""
import json

from assistant.core.identity import MATCH_THRESHOLD, cosine_similarity, match_known_person


def test_identical_vectors_score_one():
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == 1.0


def test_orthogonal_vectors_score_zero():
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0


def test_opposite_vectors_score_negative_one():
    assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == -1.0


def _person(key, *samples):
    return {"key": key, "embeddings": json.dumps(list(samples))}


def test_match_returns_best_scoring_person_above_threshold():
    target = [1.0, 0.0, 0.0]
    known = [
        _person("gf", [0.0, 1.0, 0.0]),
        _person("dug", [0.9, 0.1, 0.0], [1.0, 0.0, 0.0]),
    ]
    match = match_known_person(target, known)
    assert match is not None
    key, score = match
    assert key == "dug"
    assert score >= MATCH_THRESHOLD


def test_no_match_below_threshold_returns_none():
    target = [1.0, 0.0]
    known = [_person("gf", [0.0, 1.0])]
    assert match_known_person(target, known) is None


def test_person_with_no_samples_is_never_matched():
    known = [_person("nobody")]
    assert match_known_person([1.0, 0.0], known) is None


def test_matches_any_one_of_multiple_stored_samples():
    """vision.py stores more than one embedding per person because the same face at the
    door in daylight and indoors at night sit far apart in embedding space -- a match
    against any one stored sample should be enough."""
    target = [0.0, 0.0, 1.0]
    known = [_person("dug", [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0])]
    match = match_known_person(target, known)
    assert match is not None and match[0] == "dug"


def test_malformed_embeddings_field_is_skipped_not_fatal():
    known = [{"key": "broken", "embeddings": "not json"}]
    assert match_known_person([1.0, 0.0], known) is None
