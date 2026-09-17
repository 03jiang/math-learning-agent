"""一题一份 JSON；明确保存、版本检查与操作去重。旧四题存档保持独立。"""
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile

from study.images import image_bytes

REASONS = ('尚不确定', '读题理解', '概念不清', '计算失误', '方法选择', '步骤表达')
LEVELS = ('小学', '初中', '高中')
OUTCOMES = ('仍需练习', '独立做对')
ID = re.compile(r'^[a-f0-9]{32}$')


def now():
    return datetime.now(timezone.utc).isoformat()


def text(value, name, maximum=6000, required=False):
    if type(value) is not str or len(value) > maximum or (required and not value.strip()):
        raise ValueError(f'{name}不能为空或超过 {maximum} 字。' if required else f'{name}格式或长度不正确。')
    return value.strip()


def validate_entry(entry):
    expected = {'schema_version','id','version','created_at','updated_at','question','level','my_work',
                'topic','reason','correction','analysis','analysis_origin','image','reviews','operation_ids'}
    if type(entry) is not dict or type(entry.get('schema_version')) is not int or entry['schema_version'] not in (1,2,3,4):
        raise ValueError('错题存档格式不正确，未覆盖原文件。')
    if entry['schema_version']>=2: expected.add('corrections')
    if entry['schema_version']>=3: expected.update(('work_image','archived_at','archive_events'))
    if entry['schema_version']==4: expected.add('transcription')
    if set(entry)!=expected: raise ValueError('错题存档字段不正确，未覆盖原文件。')
    if not isinstance(entry['id'], str) or not ID.fullmatch(entry['id']):
        raise ValueError('错题编号无效。')
    if type(entry['version']) is not int or entry['version'] < 1:
        raise ValueError('错题版本无效。')
    for field, maximum, required in [('question',6000,True),('my_work',3000,False),('topic',80,True),
                                     ('correction',3000,False),('analysis_origin',100,True)]:
        text(entry[field], field, maximum, required)
    if entry['level'] not in LEVELS or entry['reason'] not in REASONS:
        raise ValueError('学段或错因分类无效。')
    if entry['image'] is not None:
        image_bytes(entry['image'])
    if entry['analysis'] is not None:
        from study.diagnosis import validate_analysis
        validate_analysis(entry['analysis'],student_work=entry['my_work'],allow_legacy=True)
    if type(entry['operation_ids']) is not list or any(type(v) is not str or not ID.fullmatch(v) for v in entry['operation_ids']):
        raise ValueError('操作记录无效。')
    if len(set(entry['operation_ids'])) != len(entry['operation_ids']):
        raise ValueError('操作记录重复。')
    if type(entry['reviews']) is not list:
        raise ValueError('复习记录无效。')
    for review in entry['reviews']:
        if type(review) is not dict or set(review) != {'id','at','outcome','answer','note'} or review['outcome'] not in OUTCOMES:
            raise ValueError('复习记录无效。')
        if review['id'] not in entry['operation_ids']:
            raise ValueError('复习记录缺少操作编号。')
        text(review['answer'],'复习答案',3000)
        text(review['note'],'复习笔记',3000)
        datetime.fromisoformat(review['at'])
    for key in ('created_at','updated_at'):
        datetime.fromisoformat(entry[key])
    if entry['schema_version']>=2:
        from study.corrections import validate_result
        from study.diagnosis import object_fields, rows
        prior_id,prior_work,prior_analysis='original',entry['my_work'],entry['analysis']
        used={row['id'] for row in entry['reviews']}
        for row in rows(entry['corrections'],50,'订正记录'):
            object_fields(row,{'id','at','based_on','answer','work_kind','result','analysis_origin'})
            if (type(row['id']) is not str or not ID.fullmatch(row['id']) or row['id'] in used
                    or row['id'] not in entry['operation_ids'] or row['based_on']!=prior_id):
                raise ValueError('订正记录编号或对照版本无效。')
            used.add(row['id'])
            text(row['analysis_origin'],'订正分析来源',100,True)
            datetime.fromisoformat(row['at'])
            validate_result(row['result'],previous_work=prior_work,previous_analysis=prior_analysis,
                            answer=row['answer'],work_kind=row['work_kind'],allow_legacy=True)
            prior_id,prior_work,prior_analysis=row['id'],row['answer'],row['result']['analysis']
    if entry['schema_version']>=3:
        if entry['work_image'] is not None: image_bytes(entry['work_image'])
        if entry['archived_at'] is not None:
            text(entry['archived_at'],'回收站时间',100,True)
            datetime.fromisoformat(entry['archived_at'])
        expected_archived=False
        archived_at=None
        for event in rows(entry['archive_events'],1000,'回收站操作'):
            object_fields(event,{'id','at','archived'})
            if (type(event['id']) is not str or not ID.fullmatch(event['id']) or event['id'] in used
                    or event['id'] not in entry['operation_ids'] or type(event['archived']) is not bool
                    or event['archived']==expected_archived):
                raise ValueError('回收站操作记录无效。')
            text(event['at'],'回收站操作时间',100,True)
            datetime.fromisoformat(event['at'])
            used.add(event['id'])
            expected_archived=event['archived']
            archived_at=event['at'] if expected_archived else None
        if entry['archived_at']!=archived_at:
            raise ValueError('回收站状态与操作记录不符。')
    if entry['schema_version']==4:
        from study.transcription import validate_trace
        validate_trace(entry['transcription'],question=entry['question'],my_work=entry['my_work'],
                       image=entry['image'],work_image=entry['work_image'])
        if entry['analysis'] and entry['analysis'].get('schema_version')==2:
            if entry['analysis']['student_review']['work_kind']!=entry['transcription']['confirmed']['work_kind']:
                raise ValueError('分析作答类型与核对记录不同。')
    return entry


def upgrade_v3(entry):
    entry.setdefault('corrections',[])
    entry.setdefault('work_image',None)
    entry.setdefault('archived_at',None)
    entry.setdefault('archive_events',[])
    entry['schema_version']=max(entry['schema_version'],3)
    return entry


def ensure_active(entry):
    if entry.get('archived_at') is not None:
        raise ValueError('这道题在回收站中，请先恢复后再操作。')


def make_entry(entry_id, *, question, level, my_work='', topic='待整理', reason='尚不确定',
               correction='', analysis=None, analysis_origin='手动整理', image=None,work_image=None,transcription=None):
    stamp=now()
    entry={'schema_version':1,'id':entry_id,'version':1,'created_at':stamp,'updated_at':stamp,
        'question':question.strip(),'level':level,'my_work':my_work.strip(),'topic':topic.strip(),
        'reason':reason,'correction':correction.strip(),'analysis':deepcopy(analysis),
        'analysis_origin':analysis_origin,'image':deepcopy(image),'reviews':[],'operation_ids':[]}
    if work_image is not None:
        upgrade_v3(entry)['work_image']=deepcopy(work_image)
    if transcription is not None:
        upgrade_v3(entry)['transcription']=deepcopy(transcription)
        entry['schema_version']=4
    return validate_entry(entry)


class Notebook:
    def __init__(self, directory):
        self.directory=Path(directory)

    def path(self, entry_id):
        if type(entry_id) is not str or not ID.fullmatch(entry_id):
            raise ValueError('错题编号无效。')
        return self.directory / (entry_id+'.json')

    def get(self, entry_id):
        path=self.path(entry_id)
        if path.is_symlink() or path.stat().st_size > 16*1024*1024:
            raise ValueError('错题文件异常，未覆盖。')
        entry=validate_entry(json.loads(path.read_text()))
        if entry['id'] != entry_id:
            raise ValueError('错题编号与文件不符。')
        return entry

    def list(self,*,archived=False):
        entries=[]
        errors=[]
        if self.directory.exists():
            for path in sorted(self.directory.glob('*.json')):
                try:
                    entry=self.get(path.stem)
                    if bool(entry.get('archived_at'))==archived: entries.append(entry)
                except (ValueError,OSError,TypeError,KeyError) as exc:
                    errors.append(f'{path.name}：无法读取，原文件已保留。')
        return sorted(entries,key=lambda entry:entry['created_at'],reverse=True),errors

    @contextmanager
    def locked(self):
        self.directory.mkdir(parents=True,exist_ok=True)
        with (self.directory / '.write.lock').open('a') as stream:
            fcntl.flock(stream,fcntl.LOCK_EX)
            yield

    def write(self, entry):
        validate_entry(entry)
        content=json.dumps(entry,ensure_ascii=False,indent=2)
        if len(content.encode('utf-8'))>16*1024*1024:
            raise ValueError('本题记录超过文件上限，请先导出；本次没有修改。')
        temporary=None
        try:
            with tempfile.NamedTemporaryFile('w',encoding='utf-8',dir=self.directory,prefix='.entry-',delete=False) as stream:
                temporary=stream.name
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary,self.path(entry['id']))
        finally:
            if temporary and os.path.exists(temporary): os.unlink(temporary)

    def save_new(self, entry):
        validate_entry(entry)
        # 兼容读取旧回复不等于允许新收藏绕过当前证据规则。
        def check_new_evidence():
            from study.diagnosis import validate_analysis
            from study.corrections import validate_result
            previous_work, previous_analysis = entry['my_work'], entry['analysis']
            if previous_analysis is not None and previous_analysis.get('schema_version') == 2:
                validate_analysis(previous_analysis, student_work=previous_work)
            for row in entry.get('corrections', []):
                validate_result(row['result'], previous_work=previous_work, previous_analysis=previous_analysis,
                                answer=row['answer'], work_kind=row['work_kind'])
                previous_work, previous_analysis = row['answer'], row['result']['analysis']
        if not self.path(entry['id']).exists():
            check_new_evidence()  # 无效新记录不创建存档目录。
        with self.locked():
            if self.path(entry['id']).exists():
                previous=self.get(entry['id'])
                if previous == entry: return 'already_saved'
                raise ValueError('该编号已有不同内容，未覆盖。请重新开始一道题。')
            check_new_evidence()  # 加锁期间重新检查，避免旧文件消失后绕过新存档规则。
            self.write(entry)
        return 'saved'

    def update(self, entry_id, version, operation_id, *, edit=None, review=None):
        if type(operation_id) is not str or not ID.fullmatch(operation_id) or (edit is None)==(review is None):
            raise ValueError('更新操作无效。')
        with self.locked():
            entry=self.get(entry_id)
            ensure_active(entry)
            if operation_id in entry['operation_ids']: return entry
            if entry['version'] != version: raise ValueError('这道题已在其他操作中更新，请重新打开后再保存。')
            if edit is not None:
                if type(edit) is not dict or set(edit) != {'topic','reason','correction'}:
                    raise ValueError('只能修改知识点、错因和订正笔记。')
                entry.update(edit)
            else:
                if type(review) is not dict or set(review) != {'outcome','answer','note'}:
                    raise ValueError('复习内容不完整。')
                if review['outcome']=='独立做对' and not review['answer'].strip():
                    raise ValueError('请先写下本次答案或解题思路，再记录独立做对。')
                entry['reviews'].append({'id':operation_id,'at':now(),**review})
            entry['operation_ids'].append(operation_id)
            entry['version']+=1
            entry['updated_at']=now()
            self.write(entry)
            return entry

    def add_correction(self,entry_id,version,operation_id,*,based_on,answer,work_kind,result,analysis_origin,
                       initial_entry=None):
        if type(operation_id) is not str or not ID.fullmatch(operation_id):
            raise ValueError('订正操作编号无效。')
        text(answer,'本次订正',3000,True)
        from study.corrections import baseline
        if initial_entry is not None:
            validate_entry(initial_entry)
            if initial_entry['id'] != entry_id or initial_entry['version'] != 1:
                raise ValueError('复用原记录编号或版本无效。')
        with self.locked():
            if (initial_entry is not None and not self.path(entry_id).exists()
                    and not self.path(entry_id).is_symlink()):
                # 只在用户确认后复制；原记录和订正合成后一次原子写入。
                entry=deepcopy(initial_entry)
            else:
                entry=self.get(entry_id)
            ensure_active(entry)
            if operation_id in entry['operation_ids']:
                existing=next((r for r in entry.get('corrections',[]) if r['id']==operation_id),None)
                expected={'based_on':based_on,'answer':answer.strip(),'work_kind':work_kind,
                          'result':result,'analysis_origin':analysis_origin}
                if not existing or any(existing[k]!=v for k,v in expected.items()):
                    raise ValueError('该操作编号已用于不同内容，未修改。')
                return entry
            if initial_entry is not None and entry != initial_entry:
                raise ValueError('目的存档与复用原记录不同，未覆盖。')
            if type(version) is not int or entry['version']!=version or baseline(entry)['id']!=based_on:
                raise ValueError('本题或对照作答已更新，请重新核对并分析。')
            text(answer,'本次订正',3000,True)
            from study.corrections import validate_result
            previous = baseline(entry)
            validate_result(result, previous_work=previous['work'], previous_analysis=previous['analysis'],
                            answer=answer, work_kind=work_kind)
            entry.setdefault('corrections',[]).append({'id':operation_id,'at':now(),'based_on':based_on,
                'answer':answer.strip(),'work_kind':work_kind,'result':deepcopy(result),'analysis_origin':analysis_origin})
            entry['schema_version']=max(entry['schema_version'],2)
            entry['operation_ids'].append(operation_id)
            entry['version']+=1
            entry['updated_at']=now()
            self.write(entry)
            return entry

    def set_archived(self,entry_id,version,operation_id,*,archived):
        if (type(operation_id) is not str or not ID.fullmatch(operation_id)
                or type(archived) is not bool or type(version) is not int):
            raise ValueError('回收站操作无效。')
        with self.locked():
            entry=self.get(entry_id)
            if operation_id in entry['operation_ids']:
                event=next((e for e in entry.get('archive_events',[]) if e['id']==operation_id),None)
                if not event or event['archived']!=archived: raise ValueError('操作编号已用于其他内容。')
                return entry
            if entry['version']!=version: raise ValueError('这道题已更新，请重新打开后再操作。')
            if bool(entry.get('archived_at'))==archived: return entry
            upgrade_v3(entry)
            stamp=now()
            entry['archived_at']=stamp if archived else None
            entry['archive_events'].append({'id':operation_id,'at':stamp,'archived':archived})
            entry['operation_ids'].append(operation_id)
            entry['version']+=1
            entry['updated_at']=stamp
            self.write(entry)
            return entry


def summarize(entries):
    return {'total':len(entries),'unreviewed':sum(not item['reviews'] for item in entries),
        'needs_practice':sum(not item['reviews'] or item['reviews'][-1]['outcome']=='仍需练习' for item in entries),
        'self_reported_correct':sum(bool(item['reviews']) and item['reviews'][-1]['outcome']=='独立做对' for item in entries),
        'topics':dict(Counter(item['topic'] for item in entries)),
        'reasons':dict(Counter(item['reason'] for item in entries)),
        'review_count':sum(len(item['reviews']) for item in entries)}


def learning_groups(entries):
    """按用户确认的分类归纳；模型错因不能自动进入已确认错因统计。"""
    groups={}
    for entry in entries:
        group=groups.setdefault(entry['topic'],{'topic':entry['topic'],'count':0,'needs_practice':0,
                                               'confirmed_reasons':{},'notes':[]})
        group['count']+=1
        group['needs_practice']+=int(not entry['reviews'] or entry['reviews'][-1]['outcome']=='仍需练习')
        reasons=group['confirmed_reasons']
        reasons[entry['reason']]=reasons.get(entry['reason'],0)+1
        latest=entry.get('corrections',[])
        analysis=latest[-1]['result']['analysis'] if latest else (entry['analysis'] or {})
        group['notes'].append({'question':entry['question'],'correction':entry['correction'],
            'takeaway':analysis.get('takeaway',''), 'next_practice':analysis.get('next_practice',''),
            'analysis_origin':latest[-1]['analysis_origin'] if latest else entry['analysis_origin'],
            'correction_count':len(latest),
            'latest_change':latest[-1]['result']['comparison']['summary'] if latest else ''})
    return sorted(groups.values(),key=lambda group:(-group['needs_practice'],-group['count'],group['topic']))
