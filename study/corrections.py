"""订正是可确认的新记录；引用分别校验前后作答，不覆盖原作答或自评。"""
import hashlib
import json

from study.diagnosis import object_fields, rows, quoted, validate_analysis, work_kind_for
from study.notebook import text

CHANGE_LABELS = {'corrected':'这处已订正（模型判断）', 'still_incorrect':'这处仍需订正',
                 'changed':'表达有变化', 'uncertain':'变化还需核对'}
EVIDENCE_VERSION = 'correction-evidence-v2'

CORRECTION_ISSUES = {
    'correction_steps_unavailable': '前后缺少可核对的步骤分析，不能断言具体步骤已订正。',
    'correction_previous_not_incorrect': '订正声明引用的旧步骤未被旧分析判为错误；不能把原本正确或不确定的步骤标为已订正。',
    'correction_current_verdict_mismatch': '本次订正引用未被对应判断的步骤完整覆盖，或包含冲突判断。',
}


class CorrectionValidationError(ValueError):
    """固定错误码；不把学生原文或服务端正文拼进异常。"""
    def __init__(self, code):
        self.code = code
        super().__init__(CORRECTION_ISSUES[code])


def baseline(entry):
    previous=entry.get('corrections',[])
    if previous:
        row=previous[-1]
        return {'id':row['id'],'work':row['answer'],'analysis':row['result']['analysis']}
    return {'id':'original','work':entry['my_work'],'analysis':entry['analysis']}


def fingerprint(entry,answer,work_kind):
    work_kind_for(answer,work_kind)
    payload={'entry_id':entry['id'],'version':entry['version'],'baseline':baseline(entry),
             'answer':answer.strip(),'work_kind':work_kind}
    return hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def excerpt_has_verdict(analysis,excerpt,verdict,student_work):
    """引用可横跨多步，但每个非空白字符都须有同一判断，且不能夹带冲突/未评内容。"""
    work=''.join(student_work.split())
    quote=''.join(excerpt.split())
    if not quote or quote not in work:
        return False

    def positions(fragment):
        start=work.find(fragment)
        while start>=0:
            yield start
            start=work.find(fragment,start+1)

    # 标记原文位置，不拼接散落的引用，不改变运算符或引用顺序。
    # 同一片段若同时有 correct / incorrect / uncertain，保守拒绝，不挑有利判断。
    expected=[0]*(len(work)+1)
    conflicting=[0]*(len(work)+1)
    for row in analysis['student_review']['comparisons']:
        fragment=''.join(row['student_excerpt'].split())
        if not fragment:
            continue
        coverage=expected if row['verdict']==verdict else conflicting
        for start in positions(fragment):
            coverage[start] += 1
            coverage[start+len(fragment)] -= 1
    good=bad=0
    unsupported=[0]
    for index in range(len(work)):
        good += expected[index]
        bad += conflicting[index]
        unsupported.append(unsupported[-1]+int(good==0 or bad>0))
    return all(unsupported[start+len(quote)]==unsupported[start] for start in positions(quote))


def validate_result(value,*,previous_work,previous_analysis,answer,work_kind,allow_legacy=False):
    object_fields(value,{'schema_version','analysis','comparison'})
    if type(value['schema_version']) is not int or value['schema_version']!=1:
        raise ValueError('订正分析版本无效。')
    text(answer,'订正作答',3000,True)
    work_kind_for(answer,work_kind)
    current=validate_analysis(value['analysis'],student_work=answer,work_kind=work_kind,allow_legacy=allow_legacy)
    comparison=value['comparison']
    object_fields(comparison,{'summary','changes'})
    text(comparison['summary'],'前后变化总结',2000,True)
    changes=rows(comparison['changes'],8,'前后步骤对照')
    before=previous_analysis or {}
    if before: validate_analysis(before,student_work=previous_work,allow_legacy=True)
    comparable=(before.get('schema_version')==2 and before['status']=='solved'
                and before['student_review']['work_kind']=='steps' and current['status']=='solved'
                and work_kind=='steps')
    if changes and not comparable:
        raise CorrectionValidationError('correction_steps_unavailable')
    for row in changes:
        object_fields(row,{'previous_excerpt','current_excerpt','status','explanation'})
        quoted(row['previous_excerpt'],previous_work)
        quoted(row['current_excerpt'],answer)
        if type(row['status']) is not str or row['status'] not in CHANGE_LABELS:
            raise ValueError('订正变化状态无效。')
        text(row['explanation'],'订正变化依据',1500,True)
        if row['status'] in ('corrected','still_incorrect'):
            expected='correct' if row['status']=='corrected' else 'incorrect'
            if not excerpt_has_verdict(before,row['previous_excerpt'],'incorrect',previous_work):
                raise CorrectionValidationError('correction_previous_not_incorrect')
            if not excerpt_has_verdict(current,row['current_excerpt'],expected,answer):
                raise CorrectionValidationError('correction_current_verdict_mismatch')
    return value
