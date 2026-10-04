"""Run with python -m streamlit run reactor_agent/streamlit_app.py."""
from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import streamlit as st

# Streamlit executes this file as a script, including when launched by double-click.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from reactor_agent.chat_service import ChatService, chat_answers
from reactor_agent.web import PROJECT_ROOT, SCENARIO_LABELS, WebApp, scenarios

STATUS = {'READY': '方案已就绪', 'PASS': '模拟通过', 'FAILED': '运行失败',
          'WAITING_INPUT': '等待补充信息', 'RUNNING': '正在处理', 'DONE': '已完成',
          'SKIPPED': '未产生新计算', 'PARTIAL': '部分工况通过', 'UNSUPPORTED': '当前不支持',
          'TIMEOUT': '执行超时', 'CANNOT_CONNECT_TO_HYSYS': '无法连接 HYSYS',
          'NO_RESULT': '未返回结果', 'REFUSED_BEFORE_EXECUTION': '执行前被拒绝'}


def render_process(events):
    st.markdown('**输入到结果的运行过程**')
    # Pair each start with its completion; keep repeated planning rounds and every case.
    rows = []
    active = {}
    for event in events:
        details = event.get('details') or {}
        identity = (event['stage'], details.get('case_id'), details.get('attempt'))
        if event['status'] != 'RUNNING' and identity in active:
            rows[active.pop(identity)] = (identity, event)
        else:
            if event['status'] == 'RUNNING':
                active[identity] = len(rows)
            rows.append((identity, event))
    for _, event in rows:
        status = event['status']
        icon = ('⏳' if status == 'RUNNING' else '❓' if status == 'WAITING_INPUT'
                else '⏭️' if status == 'SKIPPED' else '⚠️' if status == 'PARTIAL'
                else '✅' if status in ('DONE', 'PASS', 'READY') else '❌')
        stamp = datetime.fromisoformat(event['time']).astimezone(
            timezone(timedelta(hours=8))).strftime('%H:%M:%S')
        st.markdown(f'{icon} **{event["title"]}** · {STATUS.get(status, status)} · {stamp}')
        details = event.get('details') or {}
        if event['stage'] == 'plan':
            decision = details.get('decision') or {}
            if decision:
                st.caption('反应器：%s · 能力：%s · 热边界：%s · 工况数：%d' % (
                    decision.get('executed') or decision.get('preferred'),
                    decision.get('capability'), details.get('thermal_mode'),
                    len(details.get('cases') or [])))
        if event['stage'] == 'execute' and status == 'SKIPPED':
            st.caption('本节点没有发起新的 HYSYS 计算；方案模式只生成规格。')
        if details and event['stage'] not in ('finished', 'explain'):
            with st.expander('查看：' + event['title']):
                st.json(details)
    st.caption('时间为北京时间；各阶段耗时不同。详情和 process.json 来自实际运行事件。')


def default_text(value):
    return (json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list))
            else str(value) if value is not None else '')


def main():
    st.set_page_config(page_title='HYSYS 模拟助手', page_icon='⚗️', layout='centered')
    if 'service' not in st.session_state:
        root = PROJECT_ROOT / 'agent-runs' / ('chat-' + uuid.uuid4().hex[:12])
        st.session_state.service = ChatService(WebApp(root=root))
    if 'conversations' not in st.session_state:
        st.session_state.conversations = [[]]
        st.session_state.conversation = 0
    service = st.session_state.service
    app = service.app
    with st.sidebar:
        st.title('⚗️ 模拟助手')
        if st.button('＋ 新建对话', use_container_width=True):
            st.session_state.conversations.append([])
            st.session_state.conversation = len(st.session_state.conversations) - 1
        for i, history in enumerate(st.session_state.conversations):
            title = next((m['text'][:18] for m in history if m['role'] == 'user'), '新对话')
            if st.button(title, key=f'history-{i}', use_container_width=True):
                st.session_state.conversation = i
        st.divider()
        mode = st.radio('运行模式', ['生成模拟方案', '执行 HYSYS 模拟'])
        execute = mode == '执行 HYSYS 模拟'
        ready = st.checkbox('已安装 HYSYS，并确认允许创建和计算案例', disabled=not execute)
        basis = st.selectbox('未说明时的组成基准', ['质量分数', '摩尔分数'])
        with st.expander('模型连接', expanded=not app.settings.public()['key_set']):
            resolved = app.settings.resolve()
            base = st.text_input('Base URL', value=resolved.base, key='model-base')
            model = st.text_input('模型名称', value=resolved.model, key='model-name')
            key = st.text_input('API Key', value=resolved.key, type='password', key='model-key',
                                help='仅保存在当前会话内存中；留空使用环境变量或 .env。')
            app.settings.base, app.settings.model, app.settings.key = base.strip(), model.strip(), key.strip()
            st.caption('接口：/chat/completions')
        st.caption('对话列表保存在当前浏览器会话；模拟文件保存到 agent-runs。')

    history = st.session_state.conversations[st.session_state.conversation]
    st.title('HYSYS 模拟助手')
    st.caption('描述你的反应体系和工况，我会整理方案、追问缺失信息并生成结果报告。')
    if not history:
        with st.chat_message('assistant'):
            st.write('你好！请告诉我进料组成、流量、温度、压力以及模拟目标。也可以从下方示例开始。')
        cols = st.columns(3)
        for col, (name, label) in zip(cols, SCENARIO_LABELS.items()):
            if col.button(label, use_container_width=True):
                st.session_state['prompt'] = scenarios()[name]['text']
                st.session_state['example'] = name

    pending = None
    for index, message in enumerate(history):
        with st.chat_message(message['role']):
            if message.get('text'):
                st.write(message['text'])
            payload = message.get('payload')
            if not payload:
                continue
            st.markdown('**' + STATUS.get(payload['status'], payload['status']) + '** · '
                        + ('HYSYS 执行' if payload['execute'] else '方案模式'))
            if payload.get('process'):
                with st.expander('完整运行过程', expanded=True):
                    render_process(payload['process'])
            if payload.get('error'):
                st.error(payload['error'])
            if payload.get('report'):
                st.text(payload['report'])
            for problem in payload.get('problems', []):
                st.warning(str(problem))
            run = app.get_run(payload['run_id'])
            if payload.get('files') and run:
                with st.expander('报告与方案文件'):
                    st.caption(str(run.folder))
                    for name in payload['files']:
                        path = run.folder / name
                        if path.is_file():
                            st.download_button(name, path.read_bytes(), file_name=name,
                                               key=f'download-{run.run_id}-{index}-{name}')
            if index == len(history) - 1 and payload.get('questions'):
                pending = payload
                st.write('请补充以下信息，回答后会继续同一个任务。')
                with st.form('answers-' + payload['run_id'] + '-' + str(index)):
                    answers = {}
                    for q in payload['questions']:
                        if q.get('reason'):
                            st.caption(q['reason'])
                        answers[q['id']] = st.text_area(q['question'],
                            value=default_text(q.get('default')),
                            key=f'answer-{payload["run_id"]}-{index}-{q["id"]}')
                    submitted = st.form_submit_button('提交回答并继续')
                if submitted:
                    if any(not value.strip() for value in answers.values()):
                        st.warning('请填写所有问题的答案。')
                    else:
                        send(service, history, '补充信息：\n' + '\n'.join(
                            q['question'] + '：' + answers[q['id']] for q in payload['questions']),
                            pending=pending, answers=answers)

    prompt = st.chat_input('输入模拟题目；追问时可输入“默认”', key='prompt',
                           submit_mode='disable')
    if prompt:
        if not pending and execute and not ready:
            st.error('请先在侧栏确认工作站已准备好执行 HYSYS。')
        else:
            example = st.session_state.get('example', '')
            scenario = example if example and prompt == scenarios()[example]['text'] else ''
            send(service, history, prompt, pending=pending, scenario=scenario,
                 execute=execute, basis='molar_fraction' if basis == '摩尔分数' else 'mass_fraction')


def send(service, history, text, *, pending=None, answers=None, **options):
    latest = {}
    try:
        if pending:
            answers = answers or chat_answers(text, pending['questions'])
        with st.chat_message('user'):
            st.write(text)
        with st.chat_message('assistant'):
            with st.status('正在运行，请查看各阶段的实际状态', expanded=True) as running:
                process_box = st.empty()
                def on_progress(events):
                    latest['events'] = events
                    with process_box.container():
                        render_process(events)
                payload = (service.answer(pending['run_id'], answers, on_progress=on_progress)
                           if pending else service.start(text, on_progress=on_progress, **options))
                running.update(label=STATUS.get(payload['status'], payload['status']),
                               state='error' if payload['status'] in ('FAILED', 'PARTIAL') else 'complete')
        history.extend([{'role': 'user', 'text': text},
                        {'role': 'assistant', 'payload': payload}])
        st.rerun()
    except Exception as exc:
        # A provider exception may contain its credential; redact before displaying.
        message = str(exc)
        secret = service.app.settings.resolve().key
        if secret:
            message = message.replace(secret, '[已隐藏]')
        if latest.get('events'):
            run = service.app.get_run(latest['events'][-1]['run_id'])
            payload = dict(service.app.run_payload(run), process=latest['events'], error=message)
            history.extend([{'role': 'user', 'text': text},
                            {'role': 'assistant', 'payload': payload}])
            st.rerun()
        st.error('处理失败：' + message)


if __name__ == '__main__':
    main()
