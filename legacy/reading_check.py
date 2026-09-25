"""两道自写应用题的三问读题卡；只核对选项，不评价自由文字或掌握程度。"""
from dataclasses import dataclass

from legacy.core import ValidationError

READING_VERSION = 'word-reading-v1'
READING_TASKS = ('fraction-word', 'fraction-word-eighths')


@dataclass(frozen=True)
class ReadingQuestion:
    key: str
    prompt: str
    choices: tuple[tuple[str, str], ...]
    expected: str
    correct_feedback: str
    retry_feedback: str


@dataclass(frozen=True)
class ReadingCheck:
    task_id: str
    status: str
    selections: dict
    rows: list[dict]
    next_prompt: str
    verified_scope: str = '只核对这三项选择与本题题干是否相符，不判断概念掌握或完整推理'


def reading_questions(task_id):
    if task_id not in READING_TASKS:
        raise ValidationError('这道题还没有三问读题卡')
    ribbon = task_id == 'fraction-word'
    original = '原来整条彩带的长度' if ribbon else '原来整个蛋糕'
    remaining = '第一次用后剩下的彩带长度' if ribbon else '上午吃后剩下的蛋糕'
    used = '第一次用去的彩带长度' if ribbon else '上午吃掉的蛋糕'
    quote = '第二次用去全长的 1/4' if ribbon else '下午吃了整个蛋糕的 1/4'
    activity = '用去' if ribbon else '吃掉'
    quantity = '还剩多少米' if ribbon else '还剩多少千克'
    whole_word = '全长' if ribbon else '整个蛋糕'
    return (
        ReadingQuestion('whole', '1. 这道题把什么看成单位“1”？',
                        (('remaining', remaining), ('original', original), ('used', used)), 'original',
                        f'与题干相符：单位“1”指{original}。',
                        f'再看题目中的“{whole_word}”：这里的单位“1”是{original}，不是变化后的部分。'),
        ReadingQuestion('second_reference', '2. 第二次的 1/4 是谁的四分之一？' if ribbon else '2. 下午的 1/4 是谁的四分之一？',
                        (('remaining', remaining), ('unknown', '题目没有说明'), ('original', original)), 'original',
                        f'与题干相符：“{quote}”仍以{original}为整体。',
                        f'圈出“{quote}”。它指{original}的四分之一；若改成“剩下部分的四分之一”，就是另一道题。'),
        ReadingQuestion('target', '3. 题目最后要求什么？',
                        (('remaining_fraction', '剩余部分占原来整体的几分之几'),
                         ('used_fraction', f'两次一共{activity}整体的几分之几'),
                         ('remaining_quantity', quantity)), 'remaining_fraction',
                        '与题干相符：要求剩余部分占原来整体的比例。',
                        f'再读最后一句：问的是“还剩……几分之几”。一共{activity}多少是中间量；题目也没有给实际数量，不能直接计算{quantity}。'),
    )


def check_reading(task_id, selections):
    """缺选项标为未选择；未知字段、类型或选项拒绝。没有文件或模型调用。"""
    questions = reading_questions(task_id)
    if type(selections) is not dict or not selections.keys() <= {question.key for question in questions}:
        raise ValidationError('读题选择只能包含这三问的字段')
    rows, chosen = [], {}
    for question in questions:
        value = selections.get(question.key)
        options = dict(question.choices)
        if value is not None and (type(value) is not str or value not in options):
            raise ValidationError('读题选项不合法')
        chosen[question.key] = value
        status = 'unanswered' if value is None else 'correct' if value == question.expected else 'incorrect'
        feedback = ('这一问还没有选择，先找题干里的对应文字。' if status == 'unanswered'
                    else question.correct_feedback if status == 'correct' else question.retry_feedback)
        rows.append({'key': question.key, 'question': question.prompt, 'selected_choice': value,
                     'selected_text': options.get(value), 'status': status, 'feedback': feedback})
    statuses = {row['status'] for row in rows}
    status = 'incomplete' if 'unanswered' in statuses else 'retry' if 'incorrect' in statuses else 'matched'
    next_prompt = ('这三项选择与题干相符。接下来可以在下方写出解题步骤，并说明式子中的量分别表示什么。'
                   if status == 'matched' else '先补齐还没选择的问题，再检查。' if status == 'incomplete'
                   else '按提示重新圈出题干里的整体和所求量，再修改选择。')
    return ReadingCheck(task_id, status, chosen, rows, next_prompt)
