"""显式定义的 48 条合成用例，不从模型生成，不读取学生存档。"""
from dataclasses import asdict
import json
from pathlib import Path

from legacy.core import LearningSettings, ValidationError
from legacy.curriculum import ANSWERS, OPERANDS, TASKS
from legacy.reading_check import READING_TASKS

DATASET = Path(__file__).resolve().parents[1] / 'evaluation' / 'teaching_cases.jsonl'
PROFILES = {
    'brief_hint': asdict(LearningSettings()),
    'guided_example': {**asdict(LearningSettings()), 'explanation_mode': 'example', 'step_size': 'medium',
                       'presentation_density': 'detailed', 'structure_level': 'guided', 'pattern_guidance': 'on'},
    'connected_direct': {**asdict(LearningSettings()), 'explanation_mode': 'direct', 'step_size': 'large',
                         'scope_support': 'connected'},
}
MATCHING = {'whole': 'original', 'second_reference': 'original', 'target': 'remaining_fraction'}


def authored_cases():
    rows = []
    for task_id in TASKS:
        left, right = OPERANDS[task_id]
        word = task_id in READING_TASKS
        wrong = str(left + right) if word else f'{left.numerator + right.numerator}/{left.denominator + right.denominator}'
        scenarios = [
            ('starting', '我不知道从哪里开始，请帮助我理解这道题。', None, '', None,
             '围绕本题整体和分数单位，帮助学生开始；遵守指定解释方式。'),
            ('wrong_answer', '我这样算对吗？为什么？', wrong, '',
             {**MATCHING, 'whole': 'remaining'} if word else None,
             '指出把已用当剩余或分母相加的误区；应用题同时处理整体错选。'),
            ('right_answer_wrong_steps', '答案和过程都写好了，帮我检查理由。', str(ANSWERS[task_id]),
             f'{left} = 1/9', MATCHING if word else None,
             '即使最终数值正确，也要指出等值变形错误，不能称过程正确或已掌握。'),
            ('reading_misunderstanding' if word else 'unverifiable_reasoning',
             '我认为第二次是剩下部分的四分之一，题目问的是一共用了多少。' if word else '我凭感觉觉得这样算就行。',
             None, '' if word else '因为我觉得它们应该加在一起。',
             {**MATCHING, 'second_reference': 'remaining', 'target': 'used_fraction'} if word else None,
             '定位第二问和第三问错选，引用题干解释。' if word else '保留文字想法并请求补充，不能声称已自动验证。'),
        ]
        for index, (scenario, message, answer, steps, reading, focus) in enumerate(scenarios):
            for profile, settings in PROFILES.items():
                rows.append({'case_id': f'{task_id}--{scenario}--{profile}', 'task_id': task_id,
                             'split': 'reserved' if index == 3 else 'development', 'scenario': scenario,
                             'profile': profile, 'settings': settings, 'message': message,
                             'answer_submission': answer, 'solution_steps': steps,
                             'reading_selections': reading, 'search_query': '整体 剩余' if word else '通分',
                             'expected_focus': focus})
    return rows


def load_cases(suite='all'):
    if suite not in ('all', 'development', 'reserved', 'smoke', 'revision'):
        raise ValidationError('未知评估用例集')
    if suite == 'revision':
        return revision_cases()
    rows = [json.loads(line) for line in DATASET.read_text(encoding='utf-8').splitlines() if line.strip()]
    # 这个版本的用例是人工定义的固定集合；增改需同时更改定义并重新冻结。
    if rows != authored_cases():
        raise ValidationError('用例文件与已定义版本不一致，请显式更新定义并重新冻结')
    if suite == 'smoke':
        return [row for row in rows if row['scenario'] == 'starting' and row['profile'] == 'brief_hint']
    return rows if suite == 'all' else [row for row in rows if row['split'] == suite]


def revision_cases():
    """8 条修订回归用例：已知问题 + 显式按钮；不是新的独立保留集。"""
    rows = []
    # 先验证此次真实失败的 next 路径；失败即停，避免先消耗 hint 请求。
    for action in ('next', 'hint'):
        for task_id in TASKS:
            word = task_id in READING_TASKS
            left, right = OPERANDS[task_id]
            wrong = str(left + right) if word else f'{left.numerator + right.numerator}/{left.denominator + right.denominator}'
            rows.append({
                'case_id': f'revision-v3--{task_id}--{action}', 'task_id': task_id,
                'split': 'revision', 'scenario': f'explicit_{action}', 'profile': 'brief_hint',
                'settings': dict(PROFILES['brief_hint']),
                'message': '请只提示我哪里需要重新想，不要直接给最终答案。' if action == 'hint'
                           else '请建议一个下一步，等我确认后再保存。',
                'answer_submission': wrong if action == 'hint' else None, 'solution_steps': '',
                'reading_selections': {'whole': 'original', 'second_reference': 'remaining'}
                                      if word and action == 'hint' else None,
                'search_query': '整体 剩余' if word else '通分', 'help_action': action,
                'expected_focus': '只提示；应用题纠正第二问错选，不把未选第三问当作答错。' if action == 'hint'
                                  else '返回允许的下一步和一致的 current_step 建议；确认前不写状态，不标记完成。',
            })
    return rows
