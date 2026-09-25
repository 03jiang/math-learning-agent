"""小学分数与应用题：自写示例、精确答案校验、固定规则教学反馈。"""
from dataclasses import dataclass
from fractions import Fraction
import re

from legacy.core import Metadata, SimulatedReply, Snapshot, TaskState, ValidationError
from legacy.step_check import check_steps
from legacy.reading_check import check_reading

TASKS = {'fraction-add': '分数相加：每一份要一样大', 'fraction-word': '应用题：彩带还剩多少'}
QUESTIONS = {
    'fraction-add': '把同样大小的整体作为单位“1”。计算 1/2 + 1/4，并说明为什么不能直接把分母相加。',
    'fraction-word': '一条彩带，第一次用去全长的 1/2，第二次用去全长的 1/4。还剩全长的几分之几？',
}
STEPS = {
    'fraction-add': ['确认两个分数对应同样大小的整体', '把 1/2 改写为以 1/4 为单位的分数',
                     '合并大小相同的份数', '解释为什么分母保持不变'],
    'fraction-word': ['找出整体、已知量和所求量', '求两次一共用去全长的几分之几',
                      '用整体 1 减去已用部分', '检查已用部分与剩余部分的和是否为 1'],
}
ANSWERS = {'fraction-add': Fraction(3, 4), 'fraction-word': Fraction(1, 4)}
OPERANDS = {'fraction-add': (Fraction(1, 2), Fraction(1, 4)),
            'fraction-word': (Fraction(1, 2), Fraction(1, 4)),
            'fraction-add-thirds': (Fraction(1, 3), Fraction(1, 6)),
            'fraction-word-eighths': (Fraction(3, 8), Fraction(1, 4))}
KINDS = {task_id: 'addition' if task_id.startswith('fraction-add') else 'remaining' for task_id in OPERANDS}
TASKS.update({'fraction-add-thirds': '变式：三分之一加六分之一',
              'fraction-word-eighths': '变式：蛋糕还剩多少'})
QUESTIONS.update({'fraction-add-thirds': '以同样大小的整体为单位“1”，计算 1/3 + 1/6，并解释通分的依据。',
                  'fraction-word-eighths': '一个蛋糕，上午吃了整个蛋糕的 3/8，下午吃了整个蛋糕的 1/4。还剩整个蛋糕的几分之几？'})
STEPS.update({'fraction-add-thirds': ['确认两个分数对应同样大小的整体', '把 1/3 改写为以 1/6 为单位的分数',
                                     '合并大小相同的份数', '解释通分和约分为什么不改变分数大小'],
              'fraction-word-eighths': ['找出整体、已知量和所求量', '求上午和下午一共吃了整个蛋糕的几分之几',
                                        '用整体 1 减去已吃部分', '检查已吃部分与剩余部分的和是否为 1']})
for _task_id, (_left, _right) in OPERANDS.items():
    ANSWERS[_task_id] = _left + _right if KINDS[_task_id] == 'addition' else 1 - _left - _right


def check_solution(task_id, submitted):
    if task_id not in OPERANDS:
        raise ValidationError('未知数学任务')
    return check_steps(KINDS[task_id], *OPERANDS[task_id], submitted)


def variant_explanations(task_id):
    left, right = OPERANDS[task_id]
    if KINDS[task_id] == 'addition':
        return {
            'hint': ['先说明单位“1”。比较三分之一与六分之一的每一份大小。',
                     '把三分之一再平均分成两份，每小份是整个图形的几分之一？',
                     '现在每小份一样大，只需把份数相加。', '约分会改变写法，但不会改变它表示的大小。'],
            'example': ['同样长的纸带分别平均分成 3 份和 6 份，比较其中一份。',
                        '1/3 = 2/6，所以可以把两个加数都用六分之一来计数。',
                        '2/6 + 1/6 = 3/6，3/6 还可以约分成 1/2。', '3/6 与 1/2 都占同一个整体的一半。'],
            'direct': f'先通分：1/3 = 2/6。再计算：2/6 + 1/6 = 3/6 = {ANSWERS[task_id]}。',
        }
    return {
        'hint': ['把整个蛋糕看成单位“1”。两次吃掉的比例都以整个蛋糕为整体。',
                 '先求上午与下午合计吃了整个蛋糕的几分之几。',
                 '用整个蛋糕的 1 减去已经吃掉的部分。', '检查已吃和剩余两部分能不能合成 1。'],
        'example': ['把蛋糕画成 8 格：上午吃 3 格，下午吃另外 2 格。',
                    '1/4 = 2/8，所以上午与下午可以按同样大小的份数相加。',
                    '整块蛋糕可以写成 8/8，再减去已吃的 5/8。', '5/8 + 3/8 = 1，正好合成整个蛋糕。'],
        'direct': f'共吃了 {left} + {right} = {left + right}；剩下 1 - {left + right} = {ANSWERS[task_id]}。',
    }


def initial_snapshot(task_id):
    if task_id not in TASKS:
        raise ValidationError('未知数学任务')
    return Snapshot(task=TaskState(objective=QUESTIONS[task_id],
                                  requirements=['说明单位“1”是什么', '写出计算过程并解释结果',
                                                '数值正确不自动标记已理解或已完成'],
                                  current_step='读题，选择需要的帮助', completed_steps=[], deferred_ideas=[]),
                    metadata=Metadata(task_id=task_id))


@dataclass(frozen=True)
class AnswerCheck:
    status: str
    submitted: str
    numeric_match: bool | None
    feedback: str
    verified_scope: str = '只校验本题的数值及是否误加单位；不判断概念掌握程度'


def check_answer(task_id, submitted):
    """只接受整数、有限小数、a/b；不用 eval，不执行输入。"""
    if task_id not in ANSWERS:
        raise ValidationError('未知数学任务')
    if type(submitted) is not str or len(submitted) > 80:
        return AnswerCheck('invalid', '', None, '请填一个整数、小数或分数，例如 3/4。')
    normalized = submitted.strip().replace('／', '/').replace(' ', '')
    match = re.fullmatch(r'([+-]?\d{1,9}(?:/\d{1,9}|\.\d{1,9})?)([\u4e00-\u9fff]*)', normalized)
    if not match:
        return AnswerCheck('invalid', submitted, None, '暂时只能校验数值。请写成 3/4 或 0.75；想法可以写在上面的输入框。')
    token, unit = match.groups()
    try:
        value = Fraction(token)
    except (ValueError, ZeroDivisionError):
        return AnswerCheck('invalid', submitted, None, '分母不能是 0，请检查分数的写法。')
    numeric_match = value == ANSWERS[task_id]
    if unit:
        return AnswerCheck('invalid_unit', submitted, numeric_match,
                           '这道题问的是占整体的几分之几，不是实际长度或数量，请去掉“米”等单位再检查。')
    if numeric_match:
        used_part = '已吃部分' if task_id == 'fraction-word-eighths' else '已用部分'
        prompt = ('数值正确。再说说为什么要先把每一份变得一样大。' if KINDS[task_id] == 'addition'
                  else f'数值正确。请说明 1 代表什么，以及为什么用它减去{used_part}。')
        return AnswerCheck('correct', submitted, True, prompt)
    left, right = OPERANDS[task_id]
    if KINDS[task_id] == 'addition' and value == Fraction(left.numerator + right.numerator, left.denominator + right.denominator):
        feedback = f'结果还不相符。检查是否把分母也相加了：{left} 和 {right} 的每一份大小不同，要先统一分数单位。'
    elif KINDS[task_id] == 'remaining' and value == left + right:
        verb = '吃掉' if task_id == 'fraction-word-eighths' else '用去'
        feedback = f'{left + right} 表示两次一共{verb}的部分。本题问还剩多少，请再看所求量。'
    else:
        feedback = '结果还不相符。先确认整体，再检查通分和加减过程；可以查看小提示。'
    return AnswerCheck('incorrect', submitted, False, feedback)


def assess_submission(task_id, message, answer_submission=None, solution_steps=''):
    """本地数学校验独立于回复来源；显式答案框优先于旧的“答案：”写法。"""
    inline = re.search(r'^答案[:：][ \t]*(.*)$', message, re.MULTILINE)
    submitted = answer_submission if answer_submission is not None else inline.group(1) if inline else None
    answer = check_answer(task_id, submitted) if submitted is not None else None
    steps = check_solution(task_id, solution_steps) if solution_steps else None
    return answer, steps


def apply_learning_support(explanation, task_id, index, settings):
    """三个维度分别提供表达支架、通用方法、邻近知识；不决定答案或进度。"""
    addition = KINDS[task_id] == 'addition'
    detailed = settings.presentation_density == 'detailed'
    verb, used_part = ('吃掉', '已吃部分') if task_id == 'fraction-word-eighths' else ('用去', '已用部分')
    if settings.structure_level == 'guided':
        frames = ([
            '我把____看成单位“1”；两个分数的每一份____（一样大/不一样大）。',
            '我把每一份改成____分之一；分数表示的大小____（变/不变）。',
            '每一份都是____，一共有____份。',
            '分母表示____；通分或约分时，分数表示的大小____。',
        ] if addition else [
            '整体是____；已知两部分是____；要求的是____。',
            f'先求____，因为两次{verb}的部分____（重叠/不重叠）。',
            '整体用____表示；从整体中减去____，得到剩余部分。',
            '我用____加上____，检查能不能得到整体 1。',
        ])
        explanation = (f'**本轮目标**\n\n{STEPS[task_id][index]}\n\n'
                       f'**帮助说明**\n\n{explanation}\n\n**试着补全**\n\n{frames[index]}')
    if settings.pattern_guidance == 'on':
        pattern = ('同单位的份数才能直接相加：先统一分数单位，再相加份数，单位保持不变。'
                   if addition else f'求剩余的一般关系是：整体 − {used_part} = 剩余部分。先确认各部分以同一个整体为单位。')
        if detailed:
            pattern += (' 分母 d 表示把整体平均分成 d 份，每份是整体的 1/d；先统一每份大小，再合并份数。'
                        if addition else f' 如果题目改问“一共{verb}多少”，应停在{used_part}；读懂所求量才能选择最后一步。')
        explanation += '\n\n**方法提示**\n\n' + pattern
    if settings.scope_support == 'connected':
        if addition:
            connection = '联系数轴：把 0 到 1 看成一个整体，等值分数会落在同一个点上。'
            if detailed:
                connection += ' 试着用两种不同的等分方法表示同一个点，观察写法变了，位置是否改变。'
        else:
            quantity = '全长' if task_id == 'fraction-word' else '整个蛋糕的质量'
            unit = '米' if task_id == 'fraction-word' else '千克'
            connection = f'联系实际数量：占整体的比例和实际数量不同。只有知道{quantity}，才能把剩余比例换成带“{unit}”的数量。'
            if detailed:
                connection += ' 想一想：两个大小不同的整体，各剩一半，剩下的实际数量会一样吗？'
        explanation += '\n\n**知识联系（可选）**\n\n' + connection
    return explanation


class MockTutor:
    """无真实模型；按题目、支持设置和少量明确短语提供模拟反馈。"""
    name = 'primary-math-rule-mock-v1'

    def plan(self, message):
        query = re.search(r'^查笔记[:：]\s*(.*)$', message, re.MULTILINE)
        if query:
            return [('search_notes', {'query': query.group(1)})]
        if '查笔记' in message or '查资料' in message:
            return [('search_notes', {'query': message[:200]})]
        return []

    def answer(self, message, snapshot, settings, sources, searched, *, answer_submission=None, solution_steps='',
               reading_selections=None, help_action=None):
        task_id = snapshot.metadata.task_id
        check, step_report = assess_submission(task_id, message, answer_submission, solution_steps)
        if searched and not sources and check is None and step_report is None and reading_selections is None:
            return SimulatedReply('未找到匹配笔记。可以换一个数学关键词。',
                                  '试试“单位1”“通分”或“剩余部分”。', None, None, None,
                                  '数学规则模拟 · 无真实模型')
        steps = STEPS[task_id]
        # 接受建议只选定当前步骤；只有学生明确要“继续/下一步”时再提出后续步骤。
        index = steps.index(snapshot.task.current_step) if snapshot.task.current_step in steps else 0
        if KINDS[task_id] == 'addition' and ('通分' in message or '分母相加' in message):
            index = 1
        if KINDS[task_id] == 'remaining' and ('题意' in message or '求什么' in message):
            index = 0
        if ('继续' in message or '下一步' in message or help_action == 'next') and snapshot.task.current_step in steps:
            index = min(index + 1, len(steps) - 1)
        if check and check.status == 'correct':
            index = len(steps) - 1
        elif check and check.status == 'incorrect':
            index = 1 if KINDS[task_id] == 'addition' else 0
        if step_report:
            if step_report.status == 'incorrect':
                index = 1 if KINDS[task_id] == 'addition' else 0
            elif step_report.status == 'verified' and not (check and check.status != 'correct'):
                index = len(steps) - 1
            elif step_report.status == 'partial':
                index = 2 if KINDS[task_id] == 'remaining' and 'sum' in step_report.coverage else 1
        reading = check_reading(task_id, reading_selections) if reading_selections is not None else None
        if reading and reading.status != 'matched':
            index = 0
        selected = steps[index]
        mode = settings.explanation_mode
        explanations = {
            'fraction-add': {
                'hint': [
                    '先想一想：这两个分数说的是同样大小的整体吗？一半和四分之一的每一份一样大吗？',
                    '把一半再平均分成两份，每小份会是整个图形的几分之一？',
                    '现在每小份都是 1/4，只需要数一共有几小份。',
                    '分母说明整体被平均分成几份，分子说明取这样的几份。相加后，每一份的大小改变了吗？',
                ],
                'example': [
                    '把两张同样大的纸分别平均分成 2 份和 4 份。比较一份的大小，再在同一个整体中表示它们。',
                    '同一条纸带平均分成 4 份，其中 2 份正好是一半，所以 1/2 = 2/4。',
                    '同一个整体中的 2 个四分之一加 1 个四分之一，就是 3 个四分之一。',
                    '就像 2 个苹果加 1 个苹果是 3 个苹果，单位仍是苹果；分数加法也要先统一单位。',
                ],
                'direct': '先通分：1/2 = 2/4。再相加：2/4 + 1/4 = 3/4。分母 4 不变，因为每一份仍是整体的四分之一。',
            },
            'fraction-word': {
                'hint': [
                    '把整条彩带看成单位“1”。两次用去的分数都以全长为整体；题目问“还剩”，不是“一共用去”。',
                    '先求两次用去的部分合起来占全长的几分之几。相加前，要让每一份一样大。',
                    '整条彩带表示 1。已经知道用去的部分，剩下的部分可以怎样列式？',
                    '把用去的部分与剩余部分相加，看看能不能拼回整条彩带。',
                ],
                'example': [
                    '把整条彩带画成 4 个同样大的格子：第一次占其中 2 格，第二次占另外 1 格，问还没用的是几格。',
                    '1/2 与 1/4 都表示原来全长的一部分，所以用相加的方法求一共用去的部分。',
                    '如果一张纸用去 3/4，就把整张纸写成 4/4，再减去已经用掉的 3/4。',
                    '检查时，把已用部分和剩余部分拼起来，应当既没有重叠也没有空缺。',
                ],
                'direct': '两次共用去 1/2 + 1/4 = 3/4；剩下 1 - 3/4 = 1/4。答案是全长的 1/4，这里不能直接说“1/4 米”。',
            },
        }
        explanation = (explanations[task_id] if task_id in explanations else variant_explanations(task_id))[mode]
        if type(explanation) is list:
            explanation = explanation[index]
        if settings.presentation_density == 'detailed':
            explanation += (' 统一分母是在更换计数单位，分数表示的大小不变。分子、分母必须同时乘相同的非零数。'
                            if KINDS[task_id] == 'addition' else ' 可以按“整体是什么 → 知道哪两部分 → 求什么 → 选运算”的顺序读题。')
        if settings.step_size != 'small' and mode != 'direct':
            extent = 2 if settings.step_size == 'medium' else len(steps)
            explanation += ' 本轮可按这个顺序尝试：' + ' → '.join(steps[index:index+extent]) + '。'
        explanation = apply_learning_support(explanation, task_id, index, settings)
        if reading and reading.status != 'matched':
            explanation = '\n'.join(row['feedback'] for row in reading.rows if row['status'] != 'correct') + '\n\n' + explanation
        if check:
            feedback = check.feedback
            if step_report and step_report.status == 'incorrect' and check.status == 'correct':
                feedback = '最终答案的数值与本题相符，请先修正逐步检查中指出的等式问题。'
            explanation = feedback + '\n\n' + explanation
        if step_report:
            explanation = step_report.feedback + '\n\n' + explanation
        if searched and not sources:
            explanation += '\n\n笔记检索没有匹配结果；上面的数值和步骤检查仍独立执行。'
        if sources:
            explanation += '\n\n相关笔记已列在下方，可以核对概念。'
        # 不从一次正确数值推断“掌握”，不自动填写 completed_steps。
        state_patch = {'current_step': selected} if selected != snapshot.task.current_step else None
        if check and check.status in {'invalid', 'invalid_unit'}:
            state_patch = None
        if step_report and step_report.status == 'needs_review':
            state_patch = None
        denominator = max(value.denominator for value in OPERANDS[task_id])
        return SimulatedReply(explanation, selected, f'可以先画一个同样大小的整体，平均分成 {denominator} 份。' if mode == 'hint' else None,
                              state_patch, None, '数学规则模拟 · 无真实模型',
                              answer_check=check.__dict__ if check else None,
                              step_check=step_report.to_dict() if step_report else None)
