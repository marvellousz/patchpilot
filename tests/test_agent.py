import json

from patchpilot.agent import compact, parse_verdict
from patchpilot.llm import _for_provider, _gemini_compatible


def _conversation(n_steps: int) -> list[dict]:
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "issue"}]
    for i in range(n_steps):
        msgs.append({"role": "assistant", "tool_calls": [{
            "id": f"c{i}", "type": "function",
            "function": {"name": "write_file", "arguments": json.dumps({"path": "a.py", "content": "x" * 3000})},
        }]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "y" * 3000})
    return msgs


def test_compact_fits_budget_and_keeps_recent():
    msgs = _conversation(6)
    out = compact(msgs, budget=14000, keep_recent=2)
    assert sum(len(json.dumps(m)) for m in out) <= 14000
    assert out[:2] == msgs[:2]
    assert out[-2:] == msgs[-2:]
    assert len(out) == len(msgs)
    # Trimmed arguments stay valid JSON so providers accept the history.
    json.loads(out[2]["tool_calls"][0]["function"]["arguments"])


def test_compact_leaves_small_conversations_alone():
    msgs = _conversation(1)
    assert compact(msgs) == msgs


def test_parse_verdict_handles_fences_and_garbage():
    assert parse_verdict('```json\n{"matches": true, "reason": "same TypeError"}\n```') == (True, "same TypeError")
    assert parse_verdict('{"matches": false, "reason": "different bug"}') == (False, "different bug")
    ok, reason = parse_verdict("I think it matches")
    assert not ok and "Could not read" in reason


def test_provider_specific_fields_are_stripped():
    msgs = [{"role": "assistant", "reasoning": "r", "tool_calls": [{"id": "1", "extra_content": {"google": {}}}]}]
    assert "reasoning" not in _for_provider(msgs, "gemini")[0]
    assert "extra_content" in _for_provider(msgs, "gemini")[0]["tool_calls"][0]
    assert "extra_content" not in _for_provider(msgs, "groq")[0]["tool_calls"][0]
    assert not _gemini_compatible(_for_provider(msgs, "groq"))
