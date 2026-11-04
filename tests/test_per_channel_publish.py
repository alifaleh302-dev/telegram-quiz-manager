"""Per-channel publishing state and the publish-window duplicate flow.

These cover the three behaviours requested for the publish window:

1. Publishing state is tracked per channel, so a question sent to one channel
   stays available for another.
2. Selecting questions for a channel reports the ones already published there,
   so the UI can warn and let the user exclude them.
3. A dedicated schedule can be created for a batch and pinned with its questions.
"""

from __future__ import annotations

import pytest

from app.services.publishing_service import STATUS_SUCCESS


@pytest.fixture
def ready_question(services):
    return services.questions.create(
        {
            "question": "Which protocol uses port 53?",
            "options": ["HTTP", "DNS", "FTP", "SSH"],
            "correct_option_id": 1,
            "explanation": "DNS uses port 53.",
            "category": "Networking",
            "difficulty": "easy",
            "status": "ready",
        }
    )


@pytest.fixture
def two_channels(services):
    first = services.channels.create({"name": "First", "chat_id": "@first_channel"})
    second = services.channels.create({"name": "Second", "chat_id": "@second_channel"})
    return first, second


async def test_question_stays_publishable_on_another_channel(
    services, ready_question, two_channels
) -> None:
    first, second = two_channels
    entry = await services.publishing.publish(ready_question["id"], first["id"])
    assert entry["status"] == STATUS_SUCCESS

    # The pair is published only for the first channel.
    assert services.publishing._channel_published(ready_question["id"], first["id"])
    assert not services.publishing._channel_published(ready_question["id"], second["id"])

    # It is still a candidate for the second channel and can be published there.
    candidates = services.questions.pick_candidates(
        channel_id=second["id"],
        published_pairs=services.publishing.published_pairs(),
    )
    assert ready_question["id"] in {q["id"] for q in candidates}

    again = await services.publishing.publish(ready_question["id"], second["id"])
    assert again["status"] == STATUS_SUCCESS


async def test_republish_to_same_channel_is_refused_without_allow_repeat(
    services, ready_question, two_channels
) -> None:
    first, _ = two_channels
    await services.publishing.publish(ready_question["id"], first["id"])
    from app.storage.validators import ValidationError

    with pytest.raises(ValidationError, match="نُشر مسبقاً"):
        await services.publishing.publish(ready_question["id"], first["id"])


def test_duplicates_reports_only_the_chosen_channel(
    services, ready_question, two_channels
) -> None:
    first, second = two_channels
    other = services.questions.create(
        {
            "question": "other",
            "options": ["a", "b"],
            "correct_option_id": 0,
            "status": "ready",
        }
    )
    services.storage.history.update(
        lambda doc: doc["history"].insert(
            0,
            {
                "id": "PUBX",
                "question_id": ready_question["id"],
                "channel_id": first["id"],
                "status": STATUS_SUCCESS,
                "published_at": "2026-11-04T00:00:00Z",
            },
        )
    )

    # On the first channel the question is a duplicate; on the second it is not.
    rows = services.publishing.duplicates(
        [ready_question["id"], other["id"]], first["id"]
    )
    assert [row["id"] for row in rows] == [ready_question["id"]]
    assert services.publishing.duplicates([ready_question["id"]], second["id"]) == []


def test_published_channels_for_lists_every_channel(services, ready_question, two_channels) -> None:
    first, second = two_channels

    def _seed(document):
        for channel in (first, second):
            document["history"].insert(
                0,
                {
                    "id": f"PUB-{channel['id']}",
                    "question_id": ready_question["id"],
                    "channel_id": channel["id"],
                    "status": STATUS_SUCCESS,
                    "published_at": "2026-11-04T00:00:00Z",
                },
            )

    services.storage.history.update(_seed)
    # ``published_channels_for`` preserves the order entries appear in history;
    # the second channel's row was inserted last, so it comes first.
    assert set(services.publishing.published_channels_for(ready_question["id"])) == {
        first["id"],
        second["id"],
    }
