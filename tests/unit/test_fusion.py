from __future__ import annotations

from hotelapp_ai.domain.fusion import reciprocal_rank_fusion


def test_empty_input_produces_no_results() -> None:
    assert reciprocal_rank_fusion([]) == []


def test_single_empty_list_produces_no_results() -> None:
    assert reciprocal_rank_fusion([[]]) == []


def test_two_empty_lists_produce_no_results() -> None:
    assert reciprocal_rank_fusion([[], []]) == []


def test_chunk_present_in_only_one_list_still_scores() -> None:
    fused = reciprocal_rank_fusion([["a", "b"], []], k=60)

    assert [result.chunk_id for result in fused] == ["a", "b"]
    assert fused[0].score == 1.0 / 61
    assert fused[1].score == 1.0 / 62


def test_chunk_present_in_both_lists_outranks_one_sided_hits() -> None:
    dense = ["a", "b", "c"]
    sparse = ["b", "d", "a"]

    fused = reciprocal_rank_fusion([dense, sparse], k=60)
    fused_ids = [result.chunk_id for result in fused]

    # "b" is rank 2 in both lists: 1/62 + 1/62. "a" is rank 1 dense, rank 3 sparse: 1/61 + 1/63.
    # Both appear in two lists and outrank "c" and "d", which each appear in only one.
    assert fused_ids[:2] == ["a", "b"] or fused_ids[:2] == ["b", "a"]
    assert set(fused_ids[:2]) == {"a", "b"}
    assert set(fused_ids[2:]) == {"c", "d"}


def test_ties_broken_by_first_seen_order_deterministically() -> None:
    # "x" and "y" are both rank 1 in their own list, and neither appears in the other list, so
    # their fused scores are exactly equal: 1/(60+1) each. The tie must resolve the same way every
    # time, not depend on dict iteration order.
    first_run = reciprocal_rank_fusion([["x"], ["y"]])
    second_run = reciprocal_rank_fusion([["x"], ["y"]])

    assert first_run == second_run
    assert [result.chunk_id for result in first_run] == ["x", "y"]


def test_ties_broken_by_first_seen_order_when_second_list_goes_first() -> None:
    # Same tied pair, but "y"'s list is passed first this time -- the earlier-seen id should win
    # the tie, proving the order is about input position, not an identity of the values "x"/"y".
    fused = reciprocal_rank_fusion([["y"], ["x"]])

    assert [result.chunk_id for result in fused] == ["y", "x"]


def test_results_are_sorted_descending_by_score() -> None:
    fused = reciprocal_rank_fusion([["a", "b", "c"]])

    scores = [result.score for result in fused]
    assert scores == sorted(scores, reverse=True)


def test_custom_k_changes_relative_weighting() -> None:
    fused_default = reciprocal_rank_fusion([["a"], ["b"]], k=60)
    fused_small_k = reciprocal_rank_fusion([["a"], ["b"]], k=1)

    assert fused_default[0].score == 1.0 / 61
    assert fused_small_k[0].score == 1.0 / 2
