"""人工编写的离线展示，不是真实模型回复或真实学生记录。"""
from copy import deepcopy
QUESTION = '解方程：2x + 3 = 11，并检验答案。'
STUDENT_WORK = '2x = 11 + 3\n2x = 14\nx = 7'
ANALYSIS = {
    'schema_version':2, 'status':'solved', 'topic':'一元一次方程',
    'summary':'利用等式两边做相同运算，把含 x 的项单独留下，再代入原方程检验。',
    'steps':['两边同时减去 3：2x = 11 - 3 = 8。', '两边同时除以 2：x = 4。',
             '代回原式：2 × 4 + 3 = 11，等式成立。'],
    'answer':'x = 4',
    'student_review':{
        'work_kind':'steps', 'verdict':'incorrect',
        'observed_approach':'你先试着把常数移到等号右边，算出 2x 的值，再除以系数 2。',
        'answer_feedback':'你写的 x = 7 代回原式得到 17，不等于 11；参考答案是 x = 4。',
        'comparisons':[
            {'student_excerpt':'2x = 11 + 3', 'reference_step':'两边同时减去 3，得到 2x = 11 - 3。',
             'verdict':'incorrect','explanation':'左边去掉 +3 对应减去 3，右边也应减去 3。这里的符号改变了等式。'},
            {'student_excerpt':'2x = 14', 'reference_step':'应从 11 - 3 得到 2x = 8。',
             'verdict':'incorrect','explanation':'11 + 3 的加法本身算得对，但上一行的变形已经出错，所以这里沿用了错误结果。'},
            {'student_excerpt':'x = 7', 'reference_step':'由 2x = 8，两边除以 2，得到 x = 4。',
             'verdict':'incorrect','explanation':'从 14 除以 2 得到 7 的计算没错，最终答案错误来自前面的等式变形。'}]},
    'knowledge_points':['一元一次方程','等式的基本性质','代入检验'],
    'diagnosis':[{'category':'概念不清','knowledge_point':'等式的基本性质','evidence':'2x = 11 + 3',
                  'explanation':'可能还没把“移项”与“两边同时做相同运算”联系起来，也可能只是符号笔误；单题不足以确定。',
                  'check_question':'如果左边减去 3，右边也应做什么？请写出两边同时运算的完整一行。'}],
    'takeaway':'解这类方程时，先用相同运算消去常数，再把未知数的系数化为 1；最后代回原式检查。不要只记移项变号，要能说明等式两边做了什么。',
    'next_practice':'请把错误的第一行订正，再解 3x + 2 = 14，并用代入法自检。',
    'clarification':''
}


def corrected_example():
    answer='2x = 11 - 3\n2x = 8\nx = 4\n检验：2 × 4 + 3 = 11'
    analysis=deepcopy(ANALYSIS)
    analysis['student_review']={'work_kind':'steps','verdict':'correct',
        'observed_approach':'两边同时减去 3，再除以 2，并代回原式检验。',
        'answer_feedback':'本次 x = 4 代回成立。',
        'comparisons':[{'student_excerpt':line,'reference_step':reference,'verdict':'correct','explanation':explanation}
            for line,reference,explanation in [
                ('2x = 11 - 3','两边同时减去 3。','两边做相同运算，等式仍成立。'),
                ('2x = 8','计算 11 - 3。','减法计算正确。'),
                ('x = 4','两边同时除以 2。','系数化为 1，得到 x = 4。'),
                ('检验：2 × 4 + 3 = 11','代入原方程检验。','左边计算得到 11，与右边相等。')]]}
    analysis['diagnosis']=[]
    analysis['next_practice']='试着不看参考过程，解 3x + 2 = 14，再代回检验。'
    return answer,{'schema_version':1,'analysis':analysis,'comparison':{
        'summary':'本次把第一行的 +3 改为 -3，并补上了代入检验；这次作答正确，还可以用同类题自检。',
        'changes':[{'previous_excerpt':'2x = 11 + 3','current_excerpt':'2x = 11 - 3','status':'corrected',
                    'explanation':'之前右边加 3，这次两边同时减 3，修正了等式变形。'}]}}
