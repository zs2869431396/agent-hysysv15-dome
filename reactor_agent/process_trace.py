"""Read actual graph events for presentation without changing graph decisions."""
from __future__ import annotations

import copy
import json
import time
from datetime import datetime, timezone

from .report import results_view

NODE_TITLES = {
    'intake': '模型抽取输入信息',
    'review': '预检缺口与模型定向补漏',
    'plan': '校验、反应器选型与编译预检',
    'ask': '追问与用户确认',
    'execute': '执行 HYSYS 模拟',
    'explain': '生成结果报告',
}
DETAIL_FIELDS = {
    'intake': ('facts', 'extraction_error', 'extraction_attempts', 'ungrounded'),
    'review': ('review', 'extraction_error', 'ungrounded'),
    'plan': ('decision', 'components', 'thermal_mode', 'assumptions', 'applied',
             'notes', 'cases', 'blocking', 'open_questions', 'problems', 'status'),
    'ask': ('answers', 'resolved_fields', 'clarification_rounds'),
    'execute': ('executions', 'status', 'problems'),
    'explain': ('explanation',),
}


def stream_graph(graph, inputs, config, observer):
    """Invoke once via streaming; read final checkpoint rather than invoking again."""
    interrupts = None
    for part in graph.stream(inputs, config, stream_mode=['updates', 'tasks', 'custom'],
                             version='v2'):
        data = part['data']
        if part['type'] == 'tasks' and 'input' in data:
            node = data.get('name')
            if node in NODE_TITLES:
                observer({'stage': node, 'status': 'RUNNING', 'title': NODE_TITLES[node]})
        elif part['type'] == 'tasks' and data.get('error'):
            node = data.get('name')
            if node in NODE_TITLES:
                observer({'stage': node, 'status': 'FAILED', 'title': NODE_TITLES[node],
                          'details': {'error': str(data['error'])}})
        elif part['type'] == 'updates':
            for node, update in data.items():
                if node == '__interrupt__':
                    interrupts = update
                    questions = update[0].value.get('questions', []) if update else []
                    observer({'stage': 'ask', 'status': 'WAITING_INPUT',
                              'title': '等待补充信息，HYSYS 尚未执行',
                              'details': {'questions': questions}})
                elif node in NODE_TITLES and isinstance(update, dict):
                    status = 'DONE'
                    if node == 'intake' and update.get('extraction_error'):
                        status = 'FAILED'
                    if node == 'review':
                        status = update.get('review', {}).get('status', 'DONE')
                    if node == 'plan':
                        status = update.get('status', 'DONE')
                    if node == 'execute':
                        status = update.get('status', 'DONE') if update.get('executions') else 'SKIPPED'
                    observer({'stage': node, 'status': status, 'title': NODE_TITLES[node],
                              'details': {k: update[k] for k in DETAIL_FIELDS[node] if k in update}})
        elif part['type'] == 'custom' and data.get('ui_process'):
            observer({k: v for k, v in data.items() if k != 'ui_process'})
    state = dict(graph.get_state(config).values or {})
    if interrupts:
        state['__interrupt__'] = interrupts
    return state


class ProgressAdapter:
    """Add per-case events around the existing adapter, calling it exactly once."""
    def __init__(self, adapter):
        self.adapter = adapter

    def run_case(self, spec, case_id, run_root, attempt=1):
        from langgraph.config import get_stream_writer
        writer = get_stream_writer()
        writer({'ui_process': True, 'stage': 'case', 'status': 'RUNNING',
                'title': 'HYSYS 正在计算工况 ' + case_id,
                'details': {'case_id': case_id, 'attempt': attempt}})
        try:
            outcome = self.adapter.run_case(spec, case_id, run_root, attempt)
        except Exception as exc:
            writer({'ui_process': True, 'stage': 'case', 'status': 'FAILED',
                    'title': 'HYSYS 工况执行异常 ' + case_id,
                    'details': {'case_id': case_id, 'attempt': attempt, 'error': str(exc)}})
            raise
        writer({'ui_process': True, 'stage': 'case', 'status': outcome.status,
                'title': 'HYSYS 返回工况 ' + case_id,
                'details': results_view(outcome)})
        return outcome


class ProcessTrace:
    def __init__(self, run, secret='', on_progress=None):
        self.run = run
        self.secret = secret
        self.on_progress = on_progress
        self.started = time.monotonic()
        self.events = []
        self.path = run.folder / 'process.json'
        try:
            self.events = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            pass

    def record(self, event):
        event = dict(event, run_id=self.run.run_id,
                     time=datetime.now(timezone.utc).isoformat(timespec='seconds'),
                     round_elapsed_s=round(time.monotonic() - self.started, 2))
        encoded = json.dumps(event, ensure_ascii=False)
        if self.secret:
            encoded = encoded.replace(self.secret, '[已隐藏]')
        self.events.append(json.loads(encoded))
        try:
            self.path.write_text(json.dumps(self.events, ensure_ascii=False, indent=2), encoding='utf-8')
        except OSError:
            pass  # A presentation artifact cannot turn a successful simulation into a failure.
        if self.on_progress:
            try:
                self.on_progress(copy.deepcopy(self.events))
            except Exception:
                pass  # A disconnected page must not stop the graph or trigger a retry.
