"""冻结五个 Agent 联调场景；每次 HTTP 发送先占累计名额，失败不续跑。"""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path

from legacy.model_api import ModelConfig
from study.agent import AgentStudyService, Limits, build_agent_payload
from study.agent_audit import AgentAudit, assert_candidate, record_decision
from study.agent_tools import ToolScope
from study.notebook import make_entry, Notebook
from study.run_audit import RunAudit, AuditError, digest, read_json, write_json, stamp, redact
from study.service import ANALYSIS_PROMPT, PhotoTransport, StudyService, analysis_context
from study.smoke import ROOT, default_config, source_snapshot

SUITE='study-agent-live-v1'
MAX_REQUESTS=10
MAX_REQUEST_BYTES=26_000
BUDGET_CNY=1
LIMITS=asdict(Limits(model_requests=2))
PRICE_URL='https://api-docs.deepseek.com/zh-cn/quick_start/pricing/'


def byte_count(payload):
    return len(json.dumps(payload,ensure_ascii=False).encode('utf-8'))


def cases(config):
    examples=read_json(ROOT/'evaluation/study_smoke_v1.json')['cases']
    specs=[
        ('b01','直接回答','direct',3,'',False,'b04'),
        ('b02','查课程笔记','notes',1,'请参考我的课程笔记，解释这题应怎样通分。',False,'b02'),
        ('b03','笔记查无结果','no_results',1,'请参考我的课程笔记讲解；如果没有相关资料，请明确说明。',False,'b02'),
        ('b04','查相关历史','history',1,'请参考我以前收藏的分数错题，对比这次作答；不要据此断定我已经掌握。',True,'b02'),
        ('b05','工具故障停止','failure',1,'请参考我的课程笔记，解释这题应怎样通分。',False,'b02')]
    notes={f'notes/{p.name}':p.read_text() for p in sorted((ROOT/'notes').glob('*.json'))}
    if any(p.is_symlink() for p in (ROOT/'notes').glob('*.json')):raise AuditError('笔记来源不能是符号链接。')
    rows=[]
    for row_id,title,scenario,index,request,history,fixture in specs:
        case=examples[index]
        question=case['question']+('\n'+request if request else '')
        context=analysis_context(question,case['level'],case['student_work'],work_kind=case['work_kind'])
        assets=deepcopy(notes) if scenario not in ('no_results','failure') else {}
        if scenario=='failure':assets['notes/injected-failure.json']='{'
        if history:
            entry=make_entry('a'*32,question='计算 1/2 + 1/3。',level='小学',my_work='1/2 + 1/3 = 2/5',
                topic='分数加法与通分',analysis_origin='人工联调历史样例，非真实学生记录')
            entry['created_at']=entry['updated_at']='2026-09-17T00:00:00+00:00'
            assets['history/'+entry['id']+'.json']=json.dumps(entry,ensure_ascii=False,indent=2)+'\n'
        payload=build_agent_payload(config,ANALYSIS_PROMPT,context,history,limits=Limits(**LIMITS))
        if byte_count(payload)>MAX_REQUEST_BYTES:raise AuditError('初始请求超出冻结字节预算。')
        rows.append({'row_id':row_id,'title':title,'scenario':scenario,'context':context,
            'history_enabled':history,'assets':assets,'first_payload':payload,'limits':LIMITS,
            'local_fixture':fixture,'review_reference':case['human_reference'],
            'fault_injection':'malformed local note JSON' if scenario=='failure' else None})
    return rows


class AgentPlan(RunAudit):
    def scope(self,row_id):
        row=self.rows[row_id];folder=self.directory/'inputs'/row_id
        return ToolScope(folder/'history',notes_dir=folder/'notes',history_enabled=row['history_enabled'])

    def trace_id(self,row_id):return digest([self.manifest['plan_id'],row_id])[:32]

    def trace(self,row_id):
        path=self.directory/'agent-runs'/(self.trace_id(row_id)+'.json')
        return AgentAudit(path.parent).read(path.stem) if path.exists() else None

    def status(self):
        counts={'pending':0,'completed':0,'failed':0,'running':0};requests=[];coverage={};saved=rejected=0;live=self.manifest['execution_mode']=='real_api'
        for row_id in self.rows:
            row=self.row(row_id)
            counts[row['status'] if row else 'pending']+=1
            if row:
                requests.extend(row['requests']);coverage[row_id]=row.get('observed_branch')
                trace=self.trace(row_id)
                if trace and trace['decision']:
                    saved+=trace['decision']['action']=='accept';rejected+=trace['decision']['action']=='reject'
        usage=[r['usage'] for r in requests if r.get('usage')]
        return {'mode':self.manifest['execution_mode'] if requests else 'preview',
            'target_mode':self.manifest['execution_mode'],'plan_id':self.manifest['plan_id'],
            'planned_scenarios':len(self.rows),'max_model_requests':self.manifest['max_model_requests'],
            'reserved_model_requests':len(requests),'known_real_api_attempts':sum(r.get('attempted_requests',0) for r in requests) if live else 0,
            'real_api_calls_unknown':live and any(r.get('completion_unknown') or r['status']=='reserved' for r in requests),
            'counts':counts,'observed_branches':coverage,'notebook_saves':saved,'rejected':rejected,
            'usage_recorded_requests':len(usage),'usage_source':'provider_if_available' if live else 'handwritten_fixture',
            'usage_totals':{k:sum(r[k] for r in usage) for k in ('input_tokens','output_tokens','total_tokens')},
            'actual_cost_cny':None,'human_scores':None,'quality_note':'行为覆盖待复核；格式通过不等于教学正确'}


def create(directory,mode='real_api'):
    if mode not in ('real_api','local_http_test'):raise AuditError('运行模式无效。')
    config=default_config();rows=cases(config)
    manifest={'suite':SUITE,'execution_mode':mode,'code':source_snapshot(),'config':asdict(config),'rows':rows,
        'max_model_requests':MAX_REQUESTS,'max_request_bytes':MAX_REQUEST_BYTES,'budget_cny':BUDGET_CNY,
        'pricing':{'checked_on':'2026-09-17','source':PRICE_URL,'currency':'CNY','peak_input_miss_per_million':2,
                   'peak_output_per_million':8,'rough_estimate_cny':0.87,
                   'assumption':'每请求以 26000 UTF-8 字节约算 26000 input tokens，另留 1000 token 协议余量；输出按 4096 上限，忽略缓存优惠。非账单封顶。'},
        'automatic_saving':False,'independent_math_cases':2,'human_scoring':None}
    plan=AgentPlan.create(directory,manifest)
    for row in rows:
        folder=plan.directory/'inputs'/row['row_id']
        for kind in ('notes','history'):(folder/kind).mkdir(parents=True,mode=0o700)
        for name,content in row['assets'].items():(folder/name).write_text(content)
    lines=['# Agent 真实联调冻结计划','',f"计划编号：`{plan.manifest['plan_id']}`",f"代码提交：`{plan.manifest['code']['commit']}`",'',
        '5 个行为场景、2 道题目母版；均为自写样例，不使用日常错题本或私人照片。',
        '每个场景至多 2 次模型请求、3 次只读工具请求；整份计划累计最多 10 次模型请求。',
        '预留预算 1 元，按高峰未命中输入 2 元/百万 token、输出 8 元/百万 token 粗估约 0.87 元；实际以账单为准，程序不保证金额封顶。',
        f'价格核对：2026-09-17；[官方价格]({PRICE_URL})。估算按每次 26000 UTF-8 字节近似 26000 输入 token，另留 1000 token 协议余量，输出按 4096 上限；不是实际 tokenizer 计数。',
        '任一失败或中断均停止，不能跳过、自动重试或扩大额度。模型未选择预期工具时如实记未覆盖，不凑请求。',
        '第五项在独立目录放入一份格式错误的笔记，检查真实模型选择查询后程序能否停止；这不是供应商服务异常。',
        '不会自动收藏或评分；下面的核对参考只供人工看，绝不进入模型请求。','']
    for row in rows:
        c=row['context'];lines += [f"## {row['row_id']} · {row['title']}",'',c['confirmed_question'],'',
            '自写原作答：','```text',c['student_work'],'```',f"跨题历史：{'允许，仅独立样例' if row['history_enabled'] else '关闭'}",'',
            '人工核对参考（不发送）：'+row['review_reference'],'']
    (plan.directory/'PLAN.md').write_text('\n'.join(lines)+'\n')
    report(plan)
    return plan


def verify(plan,*,live=False):
    if read_json(plan.directory/'manifest.json')!=plan.manifest:raise AuditError('计划文件发生变化，已停止。')
    config=ModelConfig(**plan.manifest['config']);current=source_snapshot()
    if (plan.manifest['suite']!=SUITE or plan.manifest['code']!=current or config!=default_config()
            or list(plan.rows.values())!=cases(config) or plan.manifest['max_model_requests']!=MAX_REQUESTS
            or plan.manifest['max_request_bytes']!=MAX_REQUEST_BYTES or plan.manifest['budget_cny']!=BUDGET_CNY):
        raise AuditError('冻结的代码、输入、配置或上限不一致；不能继续旧计划。')
    if live and (not current['commit'] or current['dirty']):raise AuditError('真实联调必须使用干净的冻结提交。')
    if plan.directory.joinpath('inputs').is_symlink():raise AuditError('输入目录不能是符号链接。')
    for row_id,row in plan.rows.items():
        folder=plan.directory/'inputs'/row_id
        if folder.is_symlink():raise AuditError('场景目录不能是符号链接。')
        found={}
        for kind in ('notes','history'):
            directory=folder/kind
            if directory.is_symlink() or not directory.is_dir():raise AuditError('资料目录发生变化。')
            for path in directory.iterdir():
                if path.is_symlink() or not path.is_file() or path.stat().st_size>64*1024:
                    raise AuditError('资料文件类型或大小异常。')
                found[f'{kind}/{path.name}']=path.read_text()
        if found!=row['assets']:raise AuditError('冻结资料已改变，不能继续发送。')


def authorize(plan,confirm_plan,max_requests,budget_cny):
    if (confirm_plan!=plan.manifest['plan_id'] or type(max_requests) is not int or max_requests!=MAX_REQUESTS
            or type(budget_cny) not in (int,float) or budget_cny!=BUDGET_CNY):
        raise AuditError('需确认这份计划的完整编号、最多 10 次请求和预留 1 元预算。')


class CountedTransport:
    def __init__(self,plan,row_id,transport):
        self.plan=plan;self.row_id=row_id;self.base=transport;self.endpoint=transport.endpoint;self.kind=transport.kind

    def send(self,payload,key,timeout,record):
        plan=self.plan;plan.require_lock();verify(plan,live=self.kind=='real_api')
        row=plan.row(self.row_id);expected=plan.rows[self.row_id]['first_payload']
        if (payload['messages'][:2]!=expected['messages'] or payload['tool_choice'] not in ('auto','none')
                or {k:v for k,v in payload.items() if k not in ('messages','tool_choice')}
                   !={k:v for k,v in expected.items() if k not in ('messages','tool_choice')}):
            raise AuditError('实际请求与冻结协议不一致。')
        if not row['requests'] and payload!=expected:raise AuditError('首轮请求与预览不同。')
        if byte_count(payload)>MAX_REQUEST_BYTES:raise AuditError('请求超过字节预算，未截断或发送。')
        if key in json.dumps(payload,ensure_ascii=False):raise AuditError('请求包含凭证，未记录或发送。')
        if plan.status()['reserved_model_requests']>=MAX_REQUESTS or len(row['requests'])>=2:
            raise AuditError('已用完累计模型请求名额。')
        index=len(row['requests'])
        item={'request_id':record['request_id'],'status':'reserved','reserved_at':stamp(),'payload':payload,
            'request_hash':digest(payload),'request_bytes':byte_count(payload),'attempted_requests':0,
            'http_status':None,'completion_unknown':self.kind=='real_api','usage':None,'diagnostic_content':None}
        row['requests'].append(deepcopy(item));plan.put(self.row_id,row)
        try:
            envelope=self.base.send(payload,key,timeout,record)
            # 只保留限长的对外正文诊断，排除 reasoning_content 和凭证回显。
            if key not in json.dumps(envelope,ensure_ascii=False):
                choices=envelope.get('choices',[]) if type(envelope) is dict else []
                if choices and type(choices[0]) is dict:
                    message=choices[0].get('message',{})
                    if type(message) is dict and type(message.get('content')) is str:
                        item['diagnostic_content']=redact(message['content'][:8000],key)
            return envelope
        finally:
            item.update(attempted_requests=record['attempted_requests'],http_status=record['http_status'],
                status='received' if record['http_status'] is not None else 'unknown',
                completion_unknown=self.kind=='real_api' and record['http_status'] is None)
            row=plan.row(self.row_id);row['requests'][index]=item;plan.put(self.row_id,redact(row,key))


def observed(planned,trace):
    if not trace:return 'not_exercised'
    tools=trace['tool_calls'];scenario=planned['scenario']
    if scenario=='failure':
        return 'injected_tool_failure_observed' if any(t['name']=='search_notes' and t['status']=='error' and t.get('error_code')=='tool_failed' for t in tools) else 'not_exercised'
    if trace['status'] not in ('success','needs_clarification'):return 'unexpected_failure'
    if scenario=='direct':return 'direct_observed' if not tools else 'model_chose_tools'
    if scenario=='no_results':return 'no_results_observed' if any(t['status']=='no_results' for t in tools) else 'not_exercised'
    name='get_review_history' if scenario=='history' else 'search_notes'
    return scenario+'_observed' if any(t['name']==name and t['status']=='ok' for t in tools) else 'not_exercised'


def execute(plan,*,key='',confirm_plan=None,max_requests=None,budget_cny=None,server=None):
    live=plan.manifest['execution_mode']=='real_api';verify(plan,live=live)
    if live:
        if server is not None:raise AuditError('真实计划不能注入本机响应。')
        authorize(plan,confirm_plan,max_requests,budget_cny)
        StudyService(ModelConfig(**plan.manifest['config']),key)  # 只验证密钥格式，预留名额前发现误输入。
    elif server is None:raise AuditError('本机演练必须指定本机 HTTP 服务。')
    with plan.locked():
        state=plan.status()
        if state['counts']['failed'] or state['counts']['running']:raise AuditError('已有失败或未完成场景；不重试、不跳过。')
        if live:
            approval={'plan_id':confirm_plan,'max_model_requests':max_requests,'budget_cny':budget_cny}
            path=plan.directory/'approval.json'
            if path.exists() and read_json(path)!=approval:raise AuditError('已确认的调用范围不能更改。')
            if not path.exists():write_json(path,approval)
        for row_id,planned in plan.rows.items():
            if plan.row(row_id):continue
            plan.put(row_id,{'row_id':row_id,'plan_id':plan.manifest['plan_id'],'status':'running',
                'started_at':stamp(),'finished_at':None,'requests':[],'error_code':None,'observed_branch':None,
                'trace_id':plan.trace_id(row_id),'human_scores':None})
            transport=PhotoTransport() if live else PhotoTransport(server.chat_url)
            service=AgentStudyService(ModelConfig(**plan.manifest['config']),key if live else 'local-test-key',
                scope=plan.scope(row_id),audit_dir=plan.directory/'agent-runs',limits=Limits(**planned['limits']),
                run_id=plan.trace_id(row_id),transport=CountedTransport(plan,row_id,transport))
            context=planned['context'];failed=False
            try:
                service.analyze(context['confirmed_question'],context['school_level'],context['student_work'],work_kind=context['student_work_kind'])
            except (ValueError,OSError):failed=True
            trace=service.last_run;row=plan.row(row_id)
            if trace:
                for item,call in zip(row['requests'],trace['model_calls']):
                    if item['request_id']!=call['request_id']:raise AuditError('调用记录编号不一致。')
                    for name in ('status','attempted_requests','http_status','usage','completion_unknown','elapsed_ms','error_code','tool_response_diagnostic'):
                        if name in call:item[name]=call[name]
            row.update(status='failed' if failed else 'completed',finished_at=stamp(),
                error_code=trace.get('error_code') if trace else 'before_trace_failure',observed_branch=observed(planned,trace))
            plan.put(row_id,row)
            if failed:break
    return report(plan)


def report(plan):
    state=plan.status();write_json(plan.directory/'status.json',state)
    lines=['# Agent 联调报告','',f"模式：{state['mode']}；模型名额：{state['reserved_model_requests']}/{MAX_REQUESTS}。",
           f"正文、工具结果和执行记录为私人验证数据；没有自动收藏；已确认保存 {state['notebook_saves']} 条、拒绝 {state['rejected']} 条，人工评分为空。",
           '分支被观察到不表示使用合理、引用支持结论或数学正确。失败场景使用故障注入，不是供应商故障。','',
           '| 场景 | 状态 | 模型名额 | 观察到的分支 |','|---|---|---|---|']
    for row_id,planned in plan.rows.items():
        row=plan.row(row_id);lines.append(f"| {row_id} {planned['title']} | {row['status'] if row else 'pending'} | {len(row['requests']) if row else 0} | {row['observed_branch'] if row else '未执行'} |")
    (plan.directory/'REPORT.md').write_text('\n'.join(lines)+'\n')
    return state


def decide(plan,row_id,action):
    if row_id not in plan.rows or action not in ('accept','reject'):raise AuditError('决定或场景编号无效。')
    with plan.locked():
        trace=plan.trace(row_id)
        if not trace or not trace['final']:raise AuditError('本行没有可确认的结果。')
        if trace['decision']:
            if trace['decision']['action']!=action:raise AuditError('已有相反决定，不能覆盖。')
            return trace['decision']
        log=AgentAudit(plan.directory/'agent-runs');saved=None
        if action=='accept':
            verify(plan);result=trace['final']['result']
            assert_candidate(log,trace['run_id'],result,plan.scope(row_id))
            context=trace['context']
            saved=make_entry(trace['run_id'],question=context['confirmed_question'],level=context['school_level'],
                my_work=context['student_work'],topic=result['topic'],analysis=result,
                analysis_origin=('DeepSeek 联调 / '+plan.manifest['config']['model']) if plan.manifest['execution_mode']=='real_api' else '本机手写 Agent 演练，非真实模型')
            saved['created_at']=saved['updated_at']=trace['finished_at']
            book=Notebook(plan.directory/'notebook');book.save_new(saved)
            if Notebook(book.directory).get(saved['id'])!=saved:raise AuditError('重新读取与保存结果不一致。')
        decision=record_decision(log,trace['run_id'],action,saved_entry=saved)
    report(plan)
    return decision
