import threading
from types import SimpleNamespace
import pytest
from tools import interrupt
from agent import tool_executor as ex

@pytest.fixture
def agent(monkeypatch):
 monkeypatch.setattr(ex,'_ra',lambda:SimpleNamespace(_set_interrupt=interrupt.set_interrupt))
 interrupt.set_interrupt(False)
 yield SimpleNamespace(_tool_worker_threads_lock=threading.Lock(),_tool_worker_threads=set(),_interrupt_requested=False,_tool_interrupt_reason='explicit stop requested')
 interrupt.set_interrupt(False)

def test_stale_flag_does_not_cancel_new_owner(agent):
 interrupt.set_interrupt(True,reason='previous owner')
 with ex._registered_tool_worker(agent):
  assert not interrupt.is_interrupted()

def test_pending_stop_survives_registration(agent):
 agent._interrupt_requested=True
 with ex._registered_tool_worker(agent):
  assert interrupt.is_interrupted()
 assert not interrupt.is_interrupted()

def test_stop_after_registration_is_not_lost(agent):
 entered=threading.Event();stopped=threading.Event();seen=[]
 def worker():
  with ex._registered_tool_worker(agent):
   entered.set();assert stopped.wait(2);seen.append(interrupt.is_interrupted())
 t=threading.Thread(target=worker);t.start();assert entered.wait(2)
 agent._interrupt_requested=True
 with agent._tool_worker_threads_lock:
  for tid in agent._tool_worker_threads:interrupt.set_interrupt(True,tid)
 stopped.set();t.join(2)
 assert not t.is_alive() and seen==[True]
 assert not agent._tool_worker_threads

def test_exception_exit_cleans_flag(agent):
 with pytest.raises(RuntimeError):
  with ex._registered_tool_worker(agent):
   interrupt.set_interrupt(True);raise RuntimeError('test')
 assert not interrupt.is_interrupted()
 assert not agent._tool_worker_threads
