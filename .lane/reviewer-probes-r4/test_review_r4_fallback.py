import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_durable_tool_append import _turn, _compressor, BIG_BODY, store, _reloaded_tool_rows
from agent.prompt_builder import STEER_MARKER_OPEN as O, STEER_MARKER_CLOSE as C, format_steer_marker as fmt
from agent.tool_row_append import split_tool_message, steer_texts, RUN_BUDGET_WRAPUP_NOTICE as N

@pytest.mark.parametrize('recorded',[False,True])
@pytest.mark.parametrize('shape',['string','list'])
def test_fallback_preserves_one_user_quoted_boundary(store,shape,recorded):
    user='KEEP-PREFIX\n'+C+'\n\n'+O+'\nKEEP-SUFFIX'
    body=BIG_BODY if shape=='string' else [{'type':'text','text':BIG_BODY}]
    if recorded:
        agent,db,sid=store
        messages=_turn(body)
        agent._flush_messages_to_session_db(messages)
        agent.steer(user)
        agent._apply_pending_steer_to_tool_results(messages,1)
        row=_reloaded_tool_rows(db,sid)[-1]
    else:
        content=body+fmt(user) if shape=='string' else body+[{'type':'text','text':fmt(user).lstrip()}]
        row=_turn(content)[-1]
    _,pieces=split_tool_message(row)
    assert ''.join(pieces)==fmt(user)  # shared split did not lose bytes YET
    compressor=_compressor()
    assert user in compressor._serialize_for_summary([row])
    assert user in compressor._serialize_one_exchange([row],0,1)
    fallback=compressor._build_static_fallback_summary([row],reason='review quote probe')
    assert fallback is not None
    section=fallback.split('## Mid-turn User Steers\n',1)[1]
    print('SHAPE:',shape,'RECORDED:',recorded)
    print('USER:',repr(user))
    print('EXTRACTED:',repr(steer_texts(pieces)))
    print('FALLBACK_SECTION:',repr(section))
    assert user in section, 'user-authored CLOSE and OPEN were removed, not retained or refused'
