import sys
from pathlib import Path
from itertools import product
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_durable_tool_append import _turn, _compressor, BIG_BODY, _image_blocks
from agent.prompt_builder import STEER_MARKER_OPEN as O, STEER_MARKER_CLOSE as C, format_steer_marker as fmt
from agent.tool_row_append import RUN_BUDGET_WRAPUP_NOTICE as N, split_tool_message


def history(row):
    h=_turn('unused'); h[-1]=row
    for i in range(6): h += [{'role':'user','content':f'u{i}'},{'role':'assistant','content':f'a{i}'}]
    return h


def row_for(user,shape):
    body=BIG_BODY if shape=='string' else [{'type':'text','text':BIG_BODY},*_image_blocks()]
    content=body+fmt(user) if shape=='string' else body+[{'type':'text','text':fmt(user).lstrip()}]
    return _turn(content)[-1]

# Partial/nested marker text is authored as USER INPUT, then wrapped by the real
# producer. No invented corrupt or unwrapped row is used as loss evidence.
TOKENS=['plain','\n\n'+N,'\n'+C,'\n\n'+O+'\n','\n'+C+'\n\n'+O+'\n',O[:20],C[:-1],'🙂e\u0301\r\n']
CASES=['KEEP-START'+''.join(parts)+'KEEP-END' for size in (1,2,3) for parts in product(TOKENS,repeat=size)]
CASES += [N, N+'\nend','start\n'+N,'start\n'+N+'\nend',C+'\n\n'+N,O+'\n'+N]

@pytest.mark.parametrize('shape',['string','list'])
def test_complete_legacy_steers_fidelity_grid(shape):
    c=_compressor()
    for user in CASES:
        row=row_for(user,shape)
        body,pieces=split_tool_message(row)
        assert ''.join(pieces)==fmt(user),repr(user)
        for method in ('summary','single'):
            out=c._serialize_for_summary([row]) if method=='summary' else c._serialize_one_exchange([row],0,1)
            assert user in out,(method,repr(user))
        pruned,n=c._prune_old_tool_results(history(row),protect_tail_count=4)
        assert n>=1
        kept=pruned[2]['content']
        kept=kept if isinstance(kept,str) else '\n'.join(p.get('text','') for p in kept)
        assert user in kept,repr(user)
        again,_=c._prune_old_tool_results(history(pruned[2]),protect_tail_count=4)
        assert again[2]['content']==pruned[2]['content'],repr(user)

@pytest.mark.parametrize('shape',['string','list'])
def test_trailing_notice_after_valid_steer_grid(shape):
    c=_compressor()
    for user in CASES:
        row=row_for(user,shape)
        if shape=='string': row['content']+='\n\n'+N
        else: row['content'].append({'type':'text','text':N})
        _,pieces=split_tool_message(row)
        assert ''.join(pieces)==fmt(user),repr(user)
        assert user in c._serialize_for_summary([row]),repr(user)

@pytest.mark.parametrize('shape',['string','list'])
@pytest.mark.parametrize('notice_position',['before','after','none'])
def test_static_fallback_quoted_adjacent_boundary(shape,notice_position):
    quoted='PREFIX\n'+C+'\n\n'+O+'\nSUFFIX'
    user= ('HEAD\n\n'+N+quoted if notice_position=='before' else quoted+'\n\n'+N if notice_position=='after' else quoted)
    row=row_for(user,shape)
    out=_compressor()._build_static_fallback_summary(history(row),reason='byte preservation probe')
    assert 'PREFIX' in out and 'SUFFIX' in out
    assert user in out, 'Quoted user boundary bytes deleted by fallback'
