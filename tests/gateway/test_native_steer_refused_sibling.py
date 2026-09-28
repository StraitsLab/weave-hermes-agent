"""WEV-2108 (PR #61 review R3-F1, reviewer reproduction): an accepted native steer whose turn ended with a queued
sibling that is refused during preparation must still reach the model, not be terminalized undelivered."""
import asyncio
import importlib.util
import sys
import threading
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from gateway.config import Platform, PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource
from hermes_state import SessionDB
from run_agent import AIAgent as RealAgent
import gateway.run as gr

spec=importlib.util.spec_from_file_location('runner_fixture',Path(__file__).with_name('test_queued_native_image_session_key.py'))
f=importlib.util.module_from_spec(spec);spec.loader.exec_module(f)

@pytest.mark.asyncio
@pytest.mark.parametrize('sibling_prepared',[False,True])
async def test_an_accepted_steer_survives_a_refused_queued_sibling(sibling_prepared,monkeypatch,tmp_path):
    queued_sibling=True
    monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    adapter=APIServerAdapter(PlatformConfig(enabled=True,extra={'key':'test-only'}))
    db=SessionDB(tmp_path/'state.db')
    db.create_session('s','api_server')
    adapter._session_db=db
    runner=f._make_runner(adapter)
    runner._running=True
    runner._draining=False
    key='native-key'
    source=SessionSource(platform=Platform.API_SERVER,chat_id='s',chat_type='dm',user_id='api_server')
    runner.session_store = None
    runner._async_session_store=SimpleNamespace(_store=None, bind_existing_session=AsyncMock(return_value=SimpleNamespace(session_key=key,session_id='s')))
    adapter.gateway_runner=runner
    runner._is_session_running=lambda k: True
    # The production preparer returns None when queued @context expansion is blocked.
    runner._prepare_profile_scoped_inbound_message_text=AsyncMock(return_value='QUEUED-TEXT' if sibling_prepared else None)
    loop=asyncio.get_running_loop()
    calls=[]; admissions=[]
    class Agent:
        steer=RealAgent.steer
        steer_if_open=RealAgent.steer_if_open
        _close_steer_window=RealAgent._close_steer_window
        _drain_pending_steer=RealAgent._drain_pending_steer
        def __init__(self,**kw):
            self.tools=[]
            self.tool_progress_callback=kw.get('tool_progress_callback')
            self._pending_steer_lock=threading.Lock();self._pending_steer=None
        def run_conversation(self,message,conversation_history=None,task_id=None):
            calls.append(message)
            if len(calls)==1:
                async def admit_once_tracked():
                    for _ in range(200):
                        state = runner._peek_session_state(key)
                        if state and state.turn.agent is self:
                            return await adapter._admit_native_session_submit('s','STEER-TEXT','steer-ref',busy_mode='steer')
                        await asyncio.sleep(0.01)
                    raise AssertionError('real track_agent never published agent')
                future=asyncio.run_coroutine_threadsafe(admit_once_tracked(),loop)
                admissions.append(future.result(timeout=5))
                leftover=self._close_steer_window()
                return {'final_response':'original answer','messages':[], 'api_calls':1,'pending_steer':leftover}
            return {'final_response':'next answer','messages':[],'api_calls':1}
    fake=types.ModuleType('run_agent');fake.AIAgent=Agent
    monkeypatch.setitem(sys.modules,'run_agent',fake)
    monkeypatch.setattr(gr,'_hermes_home',tmp_path)
    monkeypatch.setattr(gr,'_resolve_runtime_agent_kwargs',lambda:{'api_key':'test-only'})
    for ref in ['running-ref','queued-ref']:
        db.register_native_session_submit('s',external_request_id=ref,message_sha256='0'*64,native_request_ref=ref)
    outer=MessageEvent(text='ORIGINAL',message_type=MessageType.TEXT,source=source,metadata={'native_request_ref':'running-ref'})
    await adapter._on_native_submit_started(outer,key)
    if queued_sibling:
        runner._enqueue_fifo(key,MessageEvent(text='QUEUED-TEXT',message_type=MessageType.TEXT,source=source,metadata={'native_request_ref':'queued-ref'}),adapter)
    try:
        result=await runner._run_agent(message='ORIGINAL',context_prompt='',history=[],source=source,session_id='s',session_key=key)
        await adapter._on_native_submit_finished(outer,key)
        print('INTEGRATED',{'sibling':queued_sibling,'calls':calls,'admissions':admissions,'steer_terminal':'steer-ref' in adapter._native_submit_terminals,'result':result})
        assert admissions==['steered']
        assert calls[1:]==(['STEER-TEXT\n\nQUEUED-TEXT'] if sibling_prepared else ['STEER-TEXT'])
    finally:
        db.close()
