"""作答诊断 v2：引用可追溯到已核对作答；旧分析只供存档兼容读取。"""
from study.notebook import REASONS, text

WORK_KINDS = {'none':'未提供作答', 'answer_only':'只有最终答案', 'steps':'有解题过程', 'unclear':'暂不确定 / 字迹不清'}
LEGACY_FIELDS = {'status','topic','summary','steps','answer','error_analysis','next_practice','clarification'}
FIELDS = (LEGACY_FIELDS-{'error_analysis'}) | {'schema_version','student_review','knowledge_points','diagnosis','takeaway'}
VERDICTS = {'not_provided':'未提供作答', 'correct':'当前作答正确', 'incorrect':'发现需订正之处',
            'partial':'部分正确', 'uncertain':'还需核对'}


def object_fields(value, fields):
    if type(value) is not dict or set(value) != set(fields):
        raise ValueError('分析回复字段不完整或包含未知字段。')


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


def validate_analysis(value, *, student_work=None, work_kind=None, allow_legacy=False):
    legacy = type(value) is dict and set(value) == LEGACY_FIELDS
    object_fields(value, LEGACY_FIELDS if legacy and allow_legacy else FIELDS)
    if value['status'] not in ('solved','needs_clarification'):
        raise ValueError('分析状态无效。')
    for field in LEGACY_FIELDS-{'status','steps','error_analysis'}:
        text(value[field],field,80 if field=='topic' else 4000,field in ('topic','summary'))
    for step in rows(value['steps'],12,'参考步骤'): text(step,'参考步骤',1500,True)
    solved = value['status'] == 'solved'
    if solved and (not value['steps'] or not value['answer'].strip() or value['clarification'].strip()):
        raise ValueError('解答缺少步骤或答案，或包含未解决的澄清。')
    if not solved and (not value['clarification'].strip() or value['answer'].strip()):
        raise ValueError('信息不足时应先询问，不能给出确定答案。')
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
    text(value['next_practice'],'自检问题',4000,True)
    review=value['student_review']
    object_fields(review, {'work_kind','verdict','observed_approach','answer_feedback','comparisons'})
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
    if solved and kind=='steps' and review['verdict'] in ('incorrect','partial') and not errors:
        raise ValueError('判定步骤有误却未提供可核对的错误步骤。')
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
        raise ValueError('条件缺失时先澄清，不输出确定解法或学生错因。')
    return value
