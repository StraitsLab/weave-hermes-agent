"""Independent lane-scoped counterexamples; no production modifications."""
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest
sys.path.insert(0,str(Path.cwd()/'tests/agent'))
from test_durable_tool_append import store, _turn, _image_blocks, _compressor, _prunable_history, BIG_BODY, LONG_STEER
from agent.prompt_builder import format_steer_marker
from agent.tool_row_append import append_to_tool_row

@pytest.mark.parametrize('drain',['post-batch','pre-api'])
def test_list_steer_reaches_llm_serializer_whole(store,drain):
    agent,db,sid=store
    messages=_turn([{'type':'text','text':BIG_BODY}]+_image_blocks())
    agent._flush_messages_to_session_db(messages)
    agent.steer(LONG_STEER)
    if drain=='post-batch': agent._apply_pending_steer_to_tool_results(messages,1)
    else:
        from agent.conversation_loop import drain_pending_steer_before_api_call
        assert drain_pending_steer_before_api_call(agent,messages)
    replay=db.get_messages_as_conversation(sid)
    assert replay[-1]['content']==messages[-1]['content']
    text=_compressor()._serialize_for_summary(replay)
    assert '...[truncated]...' in text
    assert LONG_STEER in text, 'list-carried steer was clipped after durable replay'

def test_static_fallback_keeps_long_steer():
    summary=_compressor()._build_static_fallback_summary(_prunable_history(LONG_STEER),reason='review')
    assert 'Mid-turn User Steers' in summary
    assert LONG_STEER in summary, 'static fallback clips the steer itself at 700 chars'

def test_static_fallback_keeps_newest_of_nine_steers():
    c=_compressor()
    messages=[{'role':'user','content':'do the task'}]
    for i in range(9):
        messages+=_turn(BIG_BODY+format_steer_marker(f'UNIQUE-STEER-{i}-END'),call_id=f'c{i}')
    summary=c._build_static_fallback_summary(messages,reason='review')
    assert 'UNIQUE-STEER-0-END' in summary
    assert 'UNIQUE-STEER-8-END' in summary, 'ninth/newest correction silently dropped'

def test_salvage_keeps_steer_on_old_tool():
    from agent.context_compressor import salvage_grown_transcript
    messages=_prunable_history('SALVAGE-STEER-MUST-SURVIVE')
    messages+=_turn('new short output',call_id='c2')+_turn('new short output',call_id='c3')
    candidate=[dict(m) for m in messages]+[{'role':'assistant','content':'summary growth '+('y'*1000)}]
    from agent.context_compressor import estimate_messages_tokens_rough
    assert estimate_messages_tokens_rough(candidate)>estimate_messages_tokens_rough(messages)
    out=salvage_grown_transcript(messages,candidate)
    assert out is not None
    assert len(out[2]['content'])<len(messages[2]['content'])
    assert 'SALVAGE-STEER-MUST-SURVIVE' in out[2]['content'], 'salvage rewrite erased steer'

@pytest.mark.parametrize('method',['_bound_summary_input','_sample_summary_input'])
def test_final_summary_input_budget_keeps_steer(method):
    c=_compressor()
    messages=[]
    for i in range(60):
        messages+=_turn(BIG_BODY+format_steer_marker(f'BUDGET-STEER-{i}-END'),call_id=f'c{i}')
    text=c._serialize_for_summary(messages)
    assert len(text)>c._SUMMARY_INPUT_MAX_CHARS
    out=getattr(c,method)(text)
    missing=[i for i in range(60) if f'BUDGET-STEER-{i}-END' not in out]
    assert not missing, f'final summary-input clipping erased steers at {missing}'

def test_inactive_tool_row_cannot_be_rewritten(store):
    agent,db,sid=store
    messages=_turn('original output')
    agent._flush_messages_to_session_db(messages)
    row=messages[-1]
    db._execute_write(lambda conn: conn.execute('UPDATE messages SET active=0 WHERE id=?',(row['_row_id'],)))
    agent.steer('next turn only')
    agent._apply_pending_steer_to_tool_results(messages,1)
    assert agent._pending_steer=='next turn only'
    assert row['content']=='original output'
    assert db.get_messages_as_conversation(sid,include_inactive=True)[-1]['content']=='original output'
