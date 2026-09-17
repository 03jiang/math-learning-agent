"""模型只能查询指定笔记/当前用户错题本；工具进程没有写入入口。"""
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import hashlib
import json
import re
import stat
import subprocess

from retrieval import search_notes, _terms, NOTES_DIR
from study.agent_protocol import AgentError
from study.notebook import Notebook


def definitions(history_enabled=False):
    specs=[('search_notes','查自写课程笔记；只返回关键词相关片段，无结果也正常。',
            {'query':{'type':'string'}},['query'])]
    if history_enabled:
        specs.append(('get_review_history','查当前用户已确认收藏的相关记录；旧模型分析未经教师核对，不代表能力或掌握。',
                      {'topic':{'type':'string'},'limit':{'type':'integer'}},['topic','limit']))
    return [{'type':'function','function':{'name':name,'description':description,
             'parameters':{'type':'object','properties':properties,'required':required,'additionalProperties':False}}}
            for name,description,properties,required in specs]


def check_arguments(name, arguments, history_enabled):
    if type(arguments) is not dict: raise AgentError('invalid_tool_arguments')
    expected={'query'} if name=='search_notes' else {'topic','limit'} if name=='get_review_history' and history_enabled else None
    if expected is None: raise AgentError('tool_not_allowed')
    if set(arguments)!=expected: raise AgentError('invalid_tool_arguments')
    query=arguments['query' if name=='search_notes' else 'topic']
    if type(query) is not str or not query.strip() or len(query)>200:
        raise AgentError('invalid_tool_query')
    if name=='get_review_history' and (type(arguments['limit']) is not int or not 1<=arguments['limit']<=3):
        raise AgentError('invalid_history_limit')


def files_in(directory, maximum):
    directory=Path(directory)
    if directory.is_symlink(): raise AgentError('unsafe_tool_directory')
    if not directory.exists(): return []
    result=[]
    for path in directory.iterdir():
        if path.suffix!='.json': continue
        if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode): raise AgentError('unsafe_tool_file')
        result.append(path)
        if len(result)>maximum: raise AgentError('tool_collection_too_large')
    return sorted(result)


def revision(directory, maximum):
    # 只统计元信息；关闭历史时不调用此函数，也不预读历史正文。
    rows=[]
    for p in files_in(directory,maximum):
        s=p.stat();rows.append([p.name,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns])
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def history_sources(directory, topic, limit, exclude_id=None):
    book=Notebook(directory); query=_terms(topic); found=[]
    for path in files_in(directory,200):
        entry=book.get(path.stem)
        if entry.get('archived_at') or entry['id']==exclude_id: continue
        score=len(query & _terms(entry['topic']+' '+entry['question']))
        if not score: continue
        latest=entry.get('corrections',[])
        model=latest[-1]['result']['analysis'] if latest else entry['analysis']
        origin=latest[-1]['analysis_origin'] if latest else entry['analysis_origin']
        # 当前题目不截断；这里仅传相关历史摘录，明确标记节选与来源类型。
        lines=['历史题目节选：'+entry['question'][:240], '用户原作答节选：'+entry['my_work'][:400]]
        if latest: lines.append('用户最近订正节选：'+latest[-1]['answer'][:400])
        if model: lines.append('旧模型分析节选（未核对）：'+model['summary'][:240])
        reviews=entry.get('reviews',[])
        if reviews: lines.append('最近用户自评（不代表掌握）：'+reviews[-1]['outcome'])
        source={'source_id':'history:'+entry['id'],'kind':'review_history','title':entry['topic'],
                'record_id':entry['id'],'saved_at':entry['created_at'],'updated_at':entry['updated_at'],
                'model_origin':origin[:100],'snippet':'\n'.join(lines),'is_excerpt':True}
        found.append((score,entry['updated_at'],entry['id'],source))
    return [row[3] for row in sorted(found,key=lambda r:(r[0],r[1],r[2]),reverse=True)[:limit]]


def execute_read(name, arguments, notes_dir, history_dir, history_enabled, exclude_id=None):
    check_arguments(name,arguments,history_enabled)
    if name=='search_notes':
        files_in(notes_dir,32)  # 拒绝 FIFO、符号链接，防止阻塞读取。
        rows=search_notes(arguments['query'],notes_dir=notes_dir)
        result=[]
        for row in rows:
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',row['source_id']): raise AgentError('invalid_note_id')
            result.append({'source_id':'note:'+row['source_id'],'kind':'course_note',
                           'title':row['title'][:120],'snippet':row['snippet'],'is_excerpt':True})
    else:
        result=history_sources(history_dir,arguments['topic'],arguments['limit'],exclude_id)
    value={'status':'ok' if result else 'no_results','sources':result}
    if len(json.dumps(value,ensure_ascii=False))>7000: raise AgentError('tool_result_too_large')
    return value


class ToolScope:
    def __init__(self, notebook_dir, *, notes_dir=NOTES_DIR, history_enabled=False, exclude_id=None):
        if type(history_enabled) is not bool: raise AgentError('invalid_history_policy')
        self.notes_dir=Path(notes_dir);self.history_dir=Path(notebook_dir)
        self.history_enabled=history_enabled;self.exclude_id=exclude_id

    def signature(self):
        return {'notes':revision(self.notes_dir,32),'history_enabled':self.history_enabled,
                'history':revision(self.history_dir,200) if self.history_enabled else None,
                'exclude_id':self.exclude_id}

    def call(self, name, arguments, timeout):
        check_arguments(name,arguments,self.history_enabled)
        request={'name':name,'arguments':arguments,'notes_dir':str(self.notes_dir.resolve()),
                 'history_dir':str(self.history_dir.resolve()),'history_enabled':self.history_enabled,
                 'exclude_id':self.exclude_id}
        # 模型不提供目录；执行固定工作进程，不继承 API 密钥等环境变量。
        if self.notes_dir.is_symlink() or (self.history_enabled and self.history_dir.is_symlink()):
            raise AgentError('unsafe_tool_directory')
        try:
            process=subprocess.run([sys.executable,'-B',str(Path(__file__).resolve())],
                input=json.dumps(request),capture_output=True,text=True,timeout=timeout,
                env={'PYTHONIOENCODING':'utf-8'})
        except subprocess.TimeoutExpired:
            raise AgentError('tool_timeout') from None
        try: value=json.loads(process.stdout)
        except ValueError: raise AgentError('invalid_tool_result') from None
        if process.returncode: raise AgentError('tool_failed')
        if (type(value) is not dict or set(value)!={'status','sources'}
                or value['status'] not in ('ok','no_results') or type(value['sources']) is not list
                or len(json.dumps(value,ensure_ascii=False))>7000):
            raise AgentError('invalid_tool_result')
        return value


def check_result(value):
    if (type(value) is not dict or set(value)!={'status','sources'} or type(value['sources']) is not list
            or len(value['sources'])>3 or value['status']!=('ok' if value['sources'] else 'no_results')):
        raise AgentError('invalid_tool_result')
    seen=set()
    for source in value['sources']:
        if (type(source) is not dict or source.get('kind') not in ('course_note','review_history')
                or type(source.get('source_id')) is not str
                or not re.fullmatch(r'(note:[A-Za-z0-9_-]{1,64}|history:[a-f0-9]{32})',source['source_id'])
                or source['source_id'] in seen or type(source.get('snippet')) is not str
                or not source['snippet'].strip() or len(source['snippet'])>1800):
            raise AgentError('invalid_tool_result')
        seen.add(source['source_id'])
    if len(json.dumps(value,ensure_ascii=False))>7000: raise AgentError('tool_result_too_large')
    return value


if __name__=='__main__':
    try:
        request=json.loads(sys.stdin.read(4097))
        print(json.dumps(execute_read(**request),ensure_ascii=False))
    except (ValueError,OSError,TypeError,KeyError):
        print('{"error":"tool_failed"}')
        raise SystemExit(2)
