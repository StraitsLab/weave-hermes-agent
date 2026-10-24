"""WEV-3513: image/no-prose turns must deliver an attachment or an honest failure."""
import pytest

from agent.turn_explainers import empty_response_explanation
from gateway import weave_chat_sync as sync
from tests.gateway.test_weave_chat_sync import quiet_writer as _quiet_writer

quiet_writer = _quiet_writer


@pytest.mark.parametrize("has_image", [False, True])
@pytest.mark.parametrize("content", ["(empty)", ""])
def test_no_prose_turn_keeps_shared_image_and_shows_failure(quiet_writer, has_image, content):
    writer, live, calls = quiet_writer
    ref = live["ref"]
    live["messages"] = [
        {"role": "user", "content": "Generate an image of a peaceful beach", "platform_message_id": ref},
        {"role": "assistant", "content": content, "message_uid": "empty-image-answer"},
    ]
    writer._take(ref, {"type": "tool.started", "tool_name": "image_generate"})
    writer._flush(live)
    image = {"item_id": "shared-image", "role": "assistant", "kind": "text", "state": "final",
             "body": {"text": "Peaceful beach", "attachments": [
                 {"path": "conversation/image/beach.png", "media_type": "image/png"}]}}
    if has_image:
        writer._take(ref, {"type": "out.rows", "rows": [image]})
    writer._take(ref, {"type": "turn.completed"})
    writer._write(ref, live)

    sent = [row for batch in calls for row in batch]
    final = calls[-1][-1]
    assert final["state"] == "final"
    assert final["body"]["text"] == empty_response_explanation()
    assert final["body"]["status"] is None
    assert "(empty)" not in str(sent)
    assert any(row.get("state") == "removed" and row.get("item_id") == writer._status_item_id(live)
               for row in calls[-1])
    if has_image:
        shared = next(row for row in sent if row.get("item_id") == "shared-image")
        assert shared["state"] == "final" and shared["body"] == image["body"]
        assert not any(row.get("item_id") == "shared-image" and row.get("state") == "removed" for row in sent)
    assert ref not in writer.live


def test_reported_beach_turn_replays_as_clear_failure_without_tool_output(quiet_writer):
    """Read-only cell rows 1377/1378: no tool calls, only the terminal sentinel."""
    writer, live, calls = quiet_writer
    live["messages"] = [
        {"id": 1377, "role": "user", "content": "Generate an image of a peaceful beach",
         "timestamp": 1792408473.967171, "platform_message_id": live["ref"]},
        {"id": 1378, "role": "assistant", "content": "(empty)",
         "timestamp": 1792408627.115427, "message_uid": "11f38070920b4677aa15717878cf76a9"},
    ]
    writer._take(live["ref"], {"type": "turn.completed"})
    writer._write(live["ref"], live)
    answer = calls[-1][-1]
    assert answer["state"] == "final"
    assert answer["cell_key"] == "msg:11f38070920b4677aa15717878cf76a9"
    assert answer["body"]["text"] == empty_response_explanation()
    assert "attachments" not in answer["body"]  # no image was actually made


@pytest.mark.parametrize("absorbed_key", ["merged_into", "steered_into"])
def test_absorbed_turn_without_saved_answer_retires_silently(quiet_writer, absorbed_key):
    writer, live, calls = quiet_writer
    ref = live["ref"]
    live["messages"] = [
        {"role": "user", "content": "Also make it a beach", "platform_message_id": ref},
    ]
    writer._take(ref, {"type": "tool.started", "tool_name": "image_generate"})
    writer._flush(live)
    writer._take(ref, {"type": "turn.completed", absorbed_key: "survivor-still-running"})
    writer._write(ref, live)

    final = calls[-1][-1]
    assert final["state"] == "removed"
    assert final["body"] == {"outcome": "completed", "status": None, "status_detail": None}
    assert not any(row.get("body", {}).get("text") for batch in calls for row in batch
                   if row.get("role") == "assistant")
    assert any(row.get("state") == "removed" and row.get("item_id") == writer._status_item_id(live)
               for row in calls[-1])
    assert ref not in writer.live


def test_non_sentinel_reply_is_unchanged():
    rows = sync.visible_rows([{"role": "assistant", "content": "The beach is empty."}])
    assert rows[0]["body"]["text"] == "The beach is empty."
