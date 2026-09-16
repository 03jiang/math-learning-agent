"""自写题的合成图片测试集：只生成文件，不联网，不伪装成实拍或已评分。"""
import argparse
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter

CASES = [
    {'id':'p01_fraction_steps','level':'小学','tags':['分数','错误过程'],
     'question':'计算 1/2 + 1/4，并写出过程。','student_work':'1/2 + 1/4 = (1 + 1)/(2 + 4) = 2/6',
     'work_kind':'steps','answer':'3/4','status':'solved',
     'checks':['保留学生将分子、分母分别相加的原文。','说明先通分到相同分母。','错因假设必须引用原步骤，不贴能力标签。']},
    {'id':'p02_answer_only','level':'小学','tags':['分数','只有错答案'],
     'question':'计算 1/2 + 1/4。','student_work':'2/6','work_kind':'answer_only','answer':'3/4','status':'solved',
     'checks':['区分题目和最终答案。','不得由 2/6 推断学生把分母相加。','要求补充列式，步骤对照和具体错因为空。']},
    {'id':'p03_word_reference','level':'小学','tags':['应用题','参照整体'],
     'question':'一根绳子长 12 米，第一次用去全长的 1/3，\n第二次用去剩下部分的 1/4。还剩多少米？',
     'student_work':'第一次用去 12 × 1/3 = 4 米\n第二次用去 12 × 1/4 = 3 米\n还剩 12 - 4 - 3 = 5 米',
     'work_kind':'steps','answer':'6 米','status':'solved',
     'checks':['识别“剩下部分”而非“全长”。','第二次用去 (12 - 4) × 1/4 = 2 米。','指出第二次列式的参照整体问题，认可第一次计算。']},
    {'id':'m04_equation_wrong','level':'初中','tags':['方程','符号错误','订正基线'],
     'question':'解方程：2x + 3 = 11，并检验。','student_work':'2x = 11 + 3\n2x = 14\nx = 7',
     'work_kind':'steps','answer':'x = 4','status':'solved',
     'checks':['第一处问题在两边运算不一致。','后续加法、除法本身无新计算错误。','不能仅凭一题排除符号笔误的可能。']},
    {'id':'m05_equivalent_method','level':'初中','tags':['方程','等价正确解法'],
     'question':'解方程：2x + 3 = 11。','student_work':'x = (11 - 3)/2 = 4',
     'work_kind':'steps','answer':'x = 4','status':'solved',
     'checks':['认可合并列式的正确解法。','不得因学生步骤比参考少而判错。','可以建议检验，不应归为概念错误。']},
    {'id':'m06_geometry_missing','level':'初中','tags':['几何图形','缺少条件'],
     'question':'如图，三角形 ABC 的底边 BC 长 6 cm，求面积。\n示意图不按比例绘制，不能从图片量取长度。',
     'student_work':'','work_kind':'none','answer':None,'status':'needs_clarification','diagram':'missing_height',
     'checks':['需要询问对应高或足以确定面积的其他条件。','不得从像素、图形外观或猜测补出高。','不得给出确定面积。']},
    {'id':'m07_geometry_right','level':'初中','tags':['几何图形','明确直角'],
     'question':'如图，角 B 为直角，AB = 3 cm，BC = 4 cm。\n求三角形 ABC 的面积。',
     'student_work':'3 × 4 = 12 cm^2','work_kind':'steps','answer':'6 cm^2','status':'solved','diagram':'right_triangle',
     'checks':['正确对应直角顶点 B 与两条直角边。','面积需要除以 2。','指出原式遗漏 1/2，并引用学生原式。']},
    {'id':'h08_missing_root','level':'高中','tags':['方程','漏根'],
     'question':'在实数范围内，求方程 x^2 = 9 的所有解。',
     'student_work':'x = √9 = 3','work_kind':'steps','answer':'x = 3 或 x = -3','status':'solved',
     'checks':['识别平方与根号。','指出“所有解”还包括 -3。','区分算术平方根与方程的两个根。']},
    {'id':'h09_inequality_sign','level':'高中','tags':['不等式','负数除法'],
     'question':'解不等式：-2x > 6。','student_work':'两边除以 -2，得到 x > -3',
     'work_kind':'steps','answer':'x < -3','status':'solved',
     'checks':['保留负号和不等号方向。','除以负数时应改变不等号方向。','原文引用必须来自学生作答。']},
    {'id':'q10_teacher_annotation','level':'小学','tags':['教师批注','作答归属'],
     'question':'计算 1/2 + 1/4。','student_work':'2/6','work_kind':'answer_only',
     'answer':'3/4','status':'solved','teacher_note':'老师批注：应先通分，答案是 3/4。',
     'checks':['红色教师批注不能混入学生作答。','学生作答仍为 2/6，不应被识图自动订正。','仅有学生答案，不推断具体错误过程。']},
    {'id':'q11_obscured_digit','level':'初中','tags':['遮挡','无法确定数字'],
     'question':'解方程：2x + [待核对] = 11。','student_work':'','work_kind':'none',
     'answer':None,'status':'needs_clarification','obscured':True,
     'checks':['被遮挡数字必须标记待核对。','不得根据常见例题猜为 3。','先询问缺失数字，不输出确定解。']},
    {'id':'q12_rotated_image','level':'初中','tags':['方向','EXIF'],
     'question':'解方程：2x + 3 = 11。','student_work':'2x = 11 - 3\n2x = 8\nx = 4',
     'work_kind':'steps','answer':'x = 4','status':'solved','rotate':True,
     'checks':['本地图片方向校正后文字应正向显示。','保留学生正确过程。','方向处理不能改变符号或数字。']},
    {'id':'q13_blurred_work','level':'初中','tags':['模糊作答','证据不足'],
     'question':'解方程：2x + 3 = 11。','student_work':'[待核对]','work_kind':'unclear',
     'answer':'x = 4','status':'solved','blurred_work':True,
     'checks':['题目保持清晰，学生答案区域做了模糊处理。','无法读清的作答应标记待核对，不用参考答案补齐。',
               '可讲解清晰题目，但不能确定学生对错或推断具体错因。']},
]

MANUAL_CASES = [
    {'id':'real_handwriting_fraction','status':'awaiting_image','image':None,
     'description':'纸上手写分数与通分过程，用手机拍摄；题目与作答同框。'},
    {'id':'real_pencil_geometry','status':'awaiting_image','image':None,
     'description':'铅笔作答、几何图形与橡皮擦痕，用手机在自然光下拍摄。'},
]


def font_file(specified=None):
    options=[Path(specified)] if specified else [Path('/System/Library/Fonts/STHeiti Light.ttc'),
        Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc')]
    for path in options:
        if path.is_file(): return path
    raise ValueError('缺少中文字体，请用 --font 指定支持中文的字体文件。')


def draw_case(case,font):
    image=Image.new('RGB',(1200,1000),'#fffdf7')
    draw=ImageDraw.Draw(image)
    normal=ImageFont.truetype(str(font),34)
    large=ImageFont.truetype(str(font),40)
    small=ImageFont.truetype(str(font),25)
    draw.text((60,36),f"合成测试图片 · {case['id']} · {case['level']}",font=small,fill='#606366')
    draw.line((60,90,1140,90),fill='#ddd8ce',width=2)
    draw.text((60,125),'题目',font=normal,fill='#252c35')
    if case.get('obscured'):
        draw.text((60,194),'解方程：2x + 3 = 11。',font=large,fill='#222222')
        start=60+draw.textlength('解方程：2x + ',font=large)
        draw.rectangle((start-2,194,start+draw.textlength('3',font=large)+3,243),fill='#6f7478')
        draw.text((60,270),'（原图此处被遮挡，请核对）',font=small,fill='#696969')
    else:
        draw.multiline_text((60,194),case['question'],font=large,fill='#222222',spacing=22)
    if case.get('diagram'):
        a,b,c=(245,355),(245,590),(640,590)
        if case['diagram']=='missing_height': a=(385,355)
        draw.line([a,b,c,a],fill='#31343a',width=4)
        for point,label in [(a,'A'),(b,'B'),(c,'C')]:
            draw.text((point[0]-20,point[1]-40 if label=='A' else point[1]+8),label,font=normal,fill='#222222')
        draw.text((410,620),'6 cm' if case['diagram']=='missing_height' else '4 cm',font=normal,fill='#222222')
        if case['diagram']=='right_triangle':
            draw.line([(245,560),(275,560),(275,590)],fill='#31343a',width=3)
            draw.text((110,445),'3 cm',font=normal,fill='#222222')
        work_y=725
    else: work_y=390
    if case['student_work']:
        draw.text((60,work_y),'学生原作答',font=normal,fill='#235e91')
        written='x = 4' if case.get('blurred_work') else case['student_work']
        draw.multiline_text((60,work_y+65),written,font=large,fill='#235e91',spacing=26)
        if case.get('blurred_work'):
            region=(40,work_y+55,370,work_y+145)
            image.paste(image.crop(region).filter(ImageFilter.GaussianBlur(14)),region)
    if case.get('teacher_note'):
        draw.text((60,650),case['teacher_note'],font=normal,fill='#b7313c')
    draw.text((60,940),'自写题的程序排版样本；不是实拍、真实手写或模型实测结果。',font=small,fill='#737373')
    return image


def build(output,font=None):
    target=Path(output)
    if target.exists(): raise ValueError('目标目录已存在；请使用新目录，避免覆盖测试图片或评分。')
    selected_font=font_file(font)
    target.mkdir(parents=True)
    (target/'images').mkdir()
    manifest={'schema_version':1,'evidence_kind':'synthetic_images_not_real_photos','real_api_calls':0,
              'font_name':selected_font.name,'cases':[],'manual_cases':deepcopy(MANUAL_CASES)}
    sheet=Image.new('RGB',(1200,((len(CASES)+2)//3)*340),'white')
    for index,case in enumerate(CASES):
        image=draw_case(case,selected_font)
        name=case['id']+('.jpg' if case.get('rotate') else '.png')
        path=target/'images'/name
        if case.get('rotate'):
            exif=Image.Exif();exif[274]=6
            image.transpose(Image.Transpose.ROTATE_90).save(path,'JPEG',quality=92,exif=exif)
        else: image.save(path,'PNG')
        sheet.paste(image.resize((400,333)),((index%3)*400,(index//3)*340))
        record={key:deepcopy(value) for key,value in case.items() if key not in ('diagram','teacher_note','obscured','rotate','blurred_work')}
        record.update(image='images/'+name,sha256=sha256(path.read_bytes()).hexdigest(),evaluation_status='not_run')
        manifest['cases'].append(record)
    sheet.save(target/'contact-sheet.png')
    (target/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    scores={'status':'unscored','evidence_kind':manifest['evidence_kind'],'rubric':{
        '0':'不符合','1':'部分符合','2':'符合','null':'未检查，不作为 0 分'},
        'rows':[{'case_id':case['id'],'reply_file':None,'ocr':None,'separation':None,'math':None,
                 'evidence_boundary':None,'next_step':None,'notes':''} for case in CASES]}
    (target/'score-template.json').write_text(json.dumps(scores,ensure_ascii=False,indent=2)+'\n')
    lines=['# 照片流程测试集 v1',
        f'{len(CASES)} 张可上传的合成测试图片，覆盖小学、初中、高中。全部为自写题的程序排版，没有真实学生资料；不是实际照片或真实手写。',
        '**尚未运行真实模型，也没有人工评分。** `score-template.json` 中空值表示未检查。两个真实拍摄场景仍为 `awaiting_image`，不能算作已覆盖。',
        '![合成图片总览](contact-sheet.png)',
        '## 使用',
        '在拍照解题页面逐张上传 images/ 内的文件。连接真实服务后先看 OCR 是否分开题干和学生作答，再由人核对和修正，最后分析。',
        'manifest.json 是核对用参考资料，不随图片发送给识图模型。q11 被遮挡的数字不可猜测，q12 带 EXIF 方向；m06 缺少高，必须澄清。',
        '评分仅在恢复人工检查后进行。保留每次原始识图、人工改动和最终分析，分别记录，不能用修正后的题干冒充原始识图准确率。',
        '当前没有自动批量付费运行入口。生成、查看和校验这套文件都不会调用 API。',
        '## 场景']
    table=['| 编号 | 学段 / 覆盖点 | 图片 |', '|---|---|---|']
    table += [f"| {case['id']} | {case['level']} · {' / '.join(case['tags'])} | [打开]({case['image']}) |" for case in manifest['cases']]
    lines.append('\n'.join(table))
    lines += ['## 待补真实拍摄',*[f"- {case['id']}：{case['description']}（尚未提供）" for case in MANUAL_CASES],
        '## 重新生成',
        '在项目目录执行 `python -m study.photo_cases --output 新目录`；已存在目录会拒绝覆盖。macOS 默认用 STHeiti；其他系统可用 --font 指定中文字体。']
    (target/'README.md').write_text('\n\n'.join(lines)+'\n')
    return manifest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True)
    parser.add_argument('--font')
    args=parser.parse_args()
    try:
        manifest=build(args.output,args.font)
    except (ValueError,OSError) as exc:
        parser.exit(2,str(exc)+'\n')
    print(json.dumps({'images':len(manifest['cases']),'real_photo_slots_pending':len(MANUAL_CASES),
        'real_api_calls':0,'human_scores':'not_run','output':args.output},ensure_ascii=False))


if __name__=='__main__': main()
