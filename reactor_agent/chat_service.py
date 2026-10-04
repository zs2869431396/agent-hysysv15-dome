"""Chat UI orchestration; extraction, checkpointing and execution stay in WebApp."""
from __future__ import annotations

import json
import threading
from typing import Any

from .web import WebApp, Run, persist_run, questions_of, scenarios
from .process_trace import ProcessTrace

EXECUTION_LOCK = threading.Lock()


class ChatService:
    def __init__(self, app: WebApp):
        self.app = app

    def start(self, text: str, *, scenario: str = '', execute: bool = False,
              basis: str = 'mass_fraction', on_progress=None) -> dict:
        if not text.strip():
            raise ValueError('请先输入模拟需求。')
        if not self.app.settings.public()['key_set']:
            raise ValueError('请先在侧栏填写模型 API Key。')
        source = scenarios().get(scenario, {})
        run = self.app.new_run(scenario if source else 'custom', execute)
        return self._invoke(run, lambda observer: self.app.start(
            run, text, scenario, source.get('kind', ''),
            source.get('phase', 'mixed'), source.get('feed_basis', basis), on_event=observer),
            on_progress=on_progress, input_details={'request': text, 'execute': execute,
                                                   'model': self.app.settings.public()})

    def answer(self, run_id: str, answers: dict[str, Any], on_progress=None) -> dict:
        run = self.app.get_run(run_id)
        if run is None or run.status != 'WAITING_INPUT':
            raise ValueError('这个任务当前没有等待回答的问题。')
        pending = self.app._pending_questions(run)
        valid = {q['id'] for q in pending}
        if not answers or not set(answers).issubset(valid):
            raise ValueError('请回答当前列出的问题。')
        return self._invoke(run, lambda observer: self.app.answer(run, answers, on_event=observer),
                            on_progress=on_progress, input_details={'answers': answers})

    def _invoke(self, run: Run, action, *, on_progress=None, input_details=None) -> dict:
        acquired = False
        trace = ProcessTrace(run, self.app.settings.resolve().key, on_progress)
        trace.record({'stage': 'answer' if run.status == 'WAITING_INPUT' else 'input',
                      'status': 'DONE', 'title': '收到用户回答' if run.status == 'WAITING_INPUT'
                      else '收到模拟题目', 'details': input_details or {}})
        try:
            if run.execute:
                acquired = EXECUTION_LOCK.acquire(blocking=False)
                if not acquired:
                    raise RuntimeError('HYSYS 正在执行其他任务，请稍后再试。')
            state = action(trace.record)
            questions = questions_of(state)
            if questions:
                run.status = 'WAITING_INPUT'
                (run.folder / 'paused.json').write_text(json.dumps({
                    'label': run.label, 'thread_id': run.thread_id,
                    'questions': questions,
                }, ensure_ascii=False, indent=2), encoding='utf-8')
            else:
                run.status = state.get('status') or 'FAILED'
                persist_run(run, state)
            trace.record({'stage': 'finished', 'status': run.status,
                          'title': '本轮流程结束', 'details': {'status': run.status}})
            return dict(self.app.run_payload(run, state), process=trace.events)
        except Exception as exc:
            trace.record({'stage': 'error', 'status': 'FAILED',
                          'title': '流程发生错误', 'details': {'error': str(exc)}})
            # Preserve a paused run so an answer can be retried after a transient error.
            if run.status != 'WAITING_INPUT':
                run.status = 'FAILED'
            raise
        finally:
            if acquired:
                EXECUTION_LOCK.release()
            with self.app._lock:
                self.app._save_runs()


def chat_answers(text: str, questions: list[dict]) -> dict:
    """Deterministic replies only; no extra model request to interpret an answer."""
    if text.strip() in ('默认', '接受默认', '全部采用默认值'):
        if any(q.get('default') is None for q in questions):
            raise ValueError('有问题没有默认答案，请在回答表单中填写。')
        return {q['id']: q['default'] for q in questions}
    if len(questions) == 1:
        return {questions[0]['id']: text}
    raise ValueError('当前有多个问题，请填写上方回答表单，或输入“默认”采用全部默认答案。')
