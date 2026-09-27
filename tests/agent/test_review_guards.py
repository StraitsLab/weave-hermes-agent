import sys, json
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'tests/agent'))
from test_durable_tool_append import store, _turn, _compressor, _prunable_history, BIG_BODY
from agent.prompt_builder import format_steer_marker, STEER_MARKER_OPEN
import pytest

@pytest.mark.parametrize('guard',['session','role','tool_call','row_id','expected_content','deleted'])
def test_db_append_identity_guards(store,guard):
    agent,db,sid=store
    msgs=_turn('original'); agent._flush_messages_to_session_db(msgs)
    row=msgs[-1]['_row_id']; call='call_1'; expected='original'
    if guard=='session': sid='other'
    if guard=='role': db._execute_write(lambda c:c.execute("UPDATE messages SET role='assistant' WHERE id=?",(row,)))
    if guard=='tool_call': call='other'
    if guard=='row_id': row+=1000
    if guard=='expected_content': expected='other'
    if guard=='deleted': db._execute_write(lambda c:c.execute('DELETE FROM messages WHERE id=?',(row,)))
    assert db.append_to_tool_message(sid,row,call,expected,'rewritten')==0
    assert all(m['content']!='rewritten' for m in db.get_messages_as_conversation('sess-unit',include_inactive=True))

def test_quoted_steer_marker_does_not_erase_user_prefix():
    steer='USER-PREFIX-MUST-SURVIVE\n\n'+STEER_MARKER_OPEN+'\nquoted marker, not a new steer'
    out,count=_compressor()._prune_old_tool_results(_prunable_history(steer),protect_tail_count=4)
    assert count>0
    assert steer in out[2]['content'], 'rfind chose the opening marker quoted inside the user steer'
