"""作答诊断 v2：引用可追溯到已核对作答；旧分析只供存档兼容读取。"""
from study.notebook import REASONS, text
from study.evidence import evidence_issue

WORK_KINDS = {'none':'未提供作答', 'answer_only':'只有最终答案', 'steps':'有解题过程', 'unclear':'暂不确定 / 字迹不清'}
LEGACY_FIELDS = {'status','topic','summary','steps','answer','error_analysis','next_practice','clarification'}
FIELDS = (LEGACY_FIELDS-{'error_analysis'}) | {'schema_version','student_review','knowledge_points','diagnosis','takeaway'}
VERDICTS = {'not_provided':'未提供作答', 'correct':'当前作答正确', 'incorrect':'发现需订正之处',
            'partial':'部分正确 / 尚未完成', 'uncertain':'还需核对'}

VALIDATION_ISSUES = {
    'analysis_fields': '分析回复字段不完整或包含未知字段。',
    'student_review_fields': 'student_review 字段不完整或包含未知字段；diagnosis 应在分析顶层。',
    'incorrect_without_evidence': '判定步骤有误却未提供可核对的错误步骤。',
    'partial_without_support': '未发现错误步骤时，部分完成必须说明遗漏，并且已给步骤全部正确。',
    'answer_only_feedback_not_bounded': '仅有答案时，反馈须按判断使用规定的结果对照语句，不能扩写计算方法。',
    'answer_only_method_claim': '仅有答案却在讲解中断言学生计算方法，证据不足。',
    'step_quote_incomplete': '步骤引用缺少等号一侧，需引用完整变形后再判断。',
    'step_mixed_equalities': '引用混合了成立与不成立的数值等式，请分开逐步判断。',
    'step_arithmetic_verdict_mismatch': '步骤判断与受限数值等式检查冲突；不能因最终答案错误而否定成立的局部运算。',
    'step_equation_verdict_mismatch': '步骤判断与已核对题目的一元一次方程等价变形检查冲突。',
    'analysis_summary_required': '解答需要简短思路。',
    'analysis_next_practice_required': '解答需要自检问题。',
    'clarification_required': '信息不足时应先询问，不能给出确定答案。',
    'clarification_contains_answer': '信息不足时应先询问，不能给出确定答案。',
    'clarification_contains_solution': '条件缺失时先澄清，不输出确定解法或学生错因。',
}


class AnalysisValidationError(ValueError):
    """只暴露固定错误码，不把模型字段名或回复正文写进异常。"""
    def __init__(self, code):
        self.code = code
        super().__init__(VALIDATION_ISSUES[code])


def object_fields(value, fields, *, issue='analysis_fields'):
    if type(value) is not dict or set(value) != set(fields):
        raise AnalysisValidationError(issue)


def rows(value, maximum, name):
    if type(value) is not list or len(value) > maximum:
        raise ValueError(f'{name}格式或数量不正确。')
    return value


def quoted(quote, student_work):
    text(quote,'学生原文引用',1500,True)
    # 只忽略排版空白；不改数字、运算符、顺序，更不靠近似匹配制造证据。
    if student_work is not None and ''.join(quote.split()) not in ''.join(student_work.split()):
        raise ValueError('分析引用了学生没有提供的步骤。')


def work_kind_for(student_work, work_kind=None):
    kind = work_kind if work_kind is not None else ('unclear' if student_work.strip() else 'none')
    if type(kind) is not str or kind not in WORK_KINDS or (kind == 'none') != (not student_work.strip()):
        raise ValueError('作答内容与作答类型不一致，请重新核对。')
    return kind


def validate_analysis(value, *, student_work=None, work_kind=None, question=None, allow_legacy=False):
    legacy = type(value) is dict and set(value) == LEGACY_FIELDS
    object_fields(value, LEGACY_FIELDS if legacy and allow_legacy else FIELDS)
    if value['status'] not in ('solved','needs_clarification'):
        raise ValueError('分析状态无效。')
    solved = value['status'] == 'solved'
    for field in LEGACY_FIELDS-{'status','steps','error_analysis'}:
        text(value[field],field,80 if field=='topic' else 4000,field == 'topic')
    if solved and not value['summary'].strip():
        raise AnalysisValidationError('analysis_summary_required')
    for step in rows(value['steps'],12,'参考步骤'): text(step,'参考步骤',1500,True)
    if solved and (not value['steps'] or not value['answer'].strip() or value['clarification'].strip()):
        raise ValueError('解答缺少步骤或答案，或包含未解决的澄清。')
    if not solved and not value['clarification'].strip():
        raise AnalysisValidationError('clarification_required')
    if not solved and value['answer'].strip():
        raise AnalysisValidationError('clarification_contains_answer')
    if legacy:
        text(value['error_analysis'],'旧作答分析',4000)
        return value
    if type(value['schema_version']) is not int or value['schema_version'] != 2:
        raise ValueError('分析版本无效。')
    points=rows(value['knowledge_points'],6,'知识点')
    for point in points: text(point,'知识点',80,True)
    if len(set(points)) != len(points) or (solved and not points):
        raise ValueError('知识点重复或缺失。')
    text(value['takeaway'],'归纳方法',1500,solved)
    text(value['next_practice'],'自检问题',4000)
    if solved and not value['next_practice'].strip():
        raise AnalysisValidationError('analysis_next_practice_required')
    review=value['student_review']
    object_fields(review, {'work_kind','verdict','observed_approach','answer_feedback','comparisons'},
                  issue='student_review_fields')
    kind=review['work_kind']
    if (type(kind) is not str or kind not in WORK_KINDS
            or type(review['verdict']) is not str or review['verdict'] not in VERDICTS):
        raise ValueError('作答诊断状态无效。')
    if work_kind is not None and kind != work_kind:
        raise ValueError('模型改变了已核对的作答类型。')
    if student_work is not None: work_kind_for(student_work,kind)
    text(review['observed_approach'],'已观察到的解题方法',1500)
    text(review['answer_feedback'],'答案对照',1500)
    comparisons=rows(review['comparisons'],12,'步骤对照')
    errors=[]
    for row in comparisons:
        object_fields(row, {'student_excerpt','reference_step','verdict','explanation'})
        quoted(row['student_excerpt'],student_work)
        text(row['reference_step'],'参考做法',1500,True)
        text(row['explanation'],'对照解释',1500,True)
        if row['verdict'] not in ('correct','incorrect','uncertain'):
            raise ValueError('步骤判断无效。')
        if row['verdict']=='incorrect': errors.append(row['student_excerpt'])
    if kind != 'steps' and (comparisons or review['observed_approach'].strip()):
        raise ValueError('没有清楚的解题过程时，不能还原或诊断具体步骤。')
    if kind == 'none' and (review['verdict'] != 'not_provided' or review['answer_feedback'].strip()):
        raise ValueError('未提供作答时不能判断学生答对或答错。')
    if kind != 'none' and review['verdict']=='not_provided':
        raise ValueError('已提供作答，不能标为未提供。')
    if kind == 'unclear' and review['verdict'] != 'uncertain':
        raise ValueError('作答不清楚时不能确定判断。')
    if solved and kind=='steps' and (not comparisons or not review['observed_approach'].strip()):
        raise ValueError('已提供步骤，分析必须逐步对照并概括可观察的方法。')
    if solved and kind=='answer_only' and not review['answer_feedback'].strip():
        raise ValueError('仅有答案时应说明答案对照结果。')
    if solved and kind=='steps' and not errors:
        if review['verdict']=='incorrect':
            raise AnalysisValidationError('incorrect_without_evidence')
        if review['verdict']=='partial' and (
                not review['answer_feedback'].strip()
                or any(row['verdict']!='correct' for row in comparisons)):
            raise AnalysisValidationError('partial_without_support')
    if review['verdict']=='correct' and any(row['verdict']!='correct' for row in comparisons):
        raise ValueError('整体判断与步骤对照矛盾。')
    diagnoses=rows(value['diagnosis'],4,'待核对错因')
    if diagnoses and (kind!='steps' or review['verdict'] not in ('incorrect','partial') or not solved):
        raise ValueError('没有足够步骤证据，不能生成具体错因归纳。')
    for item in diagnoses:
        object_fields(item, {'category','knowledge_point','evidence','explanation','check_question'})
        if item['category'] not in REASONS[1:] or item['knowledge_point'] not in points:
            raise ValueError('错因类别或知识点无效。')
        quoted(item['evidence'],student_work)
        if not any(''.join(item['evidence'].split()) in ''.join(e.split()) for e in errors):
            raise ValueError('错因未对应已指出的错误步骤。')
        text(item['explanation'],'错因假设',1500,True)
        text(item['check_question'],'核对问题',1000,True)
    if not solved and (value['steps'] or diagnoses or comparisons or review['observed_approach'].strip()
                       or (kind!='none' and review['verdict']!='uncertain')):
        raise AnalysisValidationError('clarification_contains_solution')
    # 旧存档只兼容读取；新请求和接受候选时检查新证据规则，不重写历史回复。
    if not allow_legacy:
        issue = evidence_issue(value, question=question, student_work=student_work)
        if issue:
            raise AnalysisValidationError(issue)
    return value
