from __future__ import annotations

from hotelapp_ai.domain.chunking import ContentBlock, chunk_blocks


def test_chunk_blocks_are_deterministic_and_keep_heading_paths() -> None:
    blocks = [
        ContentBlock(
            text=(
                "Reservations on our Flexible rate may be cancelled up to 48 hours before "
                "check-in."
            ),
            heading_path="The standard cancellation window",
        ),
        ContentBlock(
            text=(
                "The deadline is a fixed 48-hour duration measured back from the start of your "
                "check-in date in the property's own local time."
            ),
            heading_path="The standard cancellation window",
        ),
        ContentBlock(
            text="A single reservation may cover a maximum of 30 nights.",
            heading_path="Length of stay",
        ),
    ]

    first = chunk_blocks(blocks, max_chars=140)
    second = chunk_blocks(blocks, max_chars=140)

    assert first == second
    assert [chunk.heading_path for chunk in first] == [
        "The standard cancellation window",
        "The standard cancellation window",
        "Length of stay",
    ]
    assert first[0].content == blocks[0].text
    assert first[2].content == blocks[2].text


def test_chunk_blocks_split_long_content_on_sentence_boundaries() -> None:
    blocks = [
        ContentBlock(
            text=(
                "Sentence one is deliberately long enough to matter. "
                "Sentence two should land in the same chunk when it still fits. "
                "Sentence three forces a deterministic split."
            ),
            heading_path="Accessibility",
        )
    ]

    chunks = chunk_blocks(blocks, max_chars=120)

    assert len(chunks) == 2
    assert chunks[0].heading_path == "Accessibility"
    assert chunks[0].content.endswith("fits.")
    assert chunks[1].content == "Sentence three forces a deterministic split."
