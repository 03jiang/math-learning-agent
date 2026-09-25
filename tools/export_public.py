"""导出明确列出的公开文件；不携带本地 Git 历史、存档、密钥或原始运行报告。"""
import argparse
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re

ROOT=Path(__file__).resolve().parents[1]
BLOCKED={'.git','data','evaluation_runs','verification','.env','.venv','__pycache__'}
CREDENTIAL=re.compile(r'\b(?:sk-[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})')
LOCAL_PATH=re.compile(r'/Users[/][^/\s]+/|/home[/][^/\s]+/|/private[/]var[/]folders/|[A-Za-z]:[\\/]Users[\\/][^\\/\s]+[\\/]')


def export(root, output):
    root,output=Path(root).resolve(),Path(output)
    if output.exists() or output.is_symlink(): raise ValueError('输出目录已存在；不会覆盖。')
    files=json.loads((root/'tools/public_files.json').read_text())['files']
    if not isinstance(files,list) or not files or len(files)!=len(set(files)):
        raise ValueError('公开文件清单为空或重复。')
    contents={}
    for name in files:
        path=PurePosixPath(name)
        if (path.is_absolute() or '..' in path.parts or '\\' in name or str(path)!=name
                or set(path.parts)&BLOCKED or path.name in ('secrets.toml','model_config.json')
                or any(part.startswith('.env') for part in path.parts)):
            raise ValueError('清单包含禁止公开的路径：'+name)
        source=root.joinpath(*path.parts)
        if not source.is_file() or any(p.is_symlink() for p in [source,*source.parents] if p!=root):
            raise ValueError('文件缺失或是符号链接：'+name)
        content=source.read_bytes()
        if source.suffix.lower() not in ('.png','.jpg','.jpeg','.webp'):
            decoded=content.decode('utf-8')
            if CREDENTIAL.search(decoded) or LOCAL_PATH.search(decoded):
                raise ValueError('疑似凭证或本机私人路径，需要检查文件：'+name)
        contents[name]=content
    # 全部检查通过后才创建目的地；失败不产生一个貌似可发布的部分包。
    output.mkdir(parents=True)
    for name,content in contents.items():
        target=output/name
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(content)
    manifest={'format':1,'kind':'public_source_export','file_count':len(contents),
              'files':{name:sha256(content).hexdigest() for name,content in sorted(contents.items())},
              'excludes':['local Git history','user notebook and photos','credentials','raw run reports'],
              'real_photo_model_quality':'not_verified','human_scoring':'paused'}
    (output/'PUBLIC_MANIFEST.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    return manifest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    try:
        result=export(ROOT,args.output)
        print(json.dumps({'output':str(args.output),'files':result['file_count']},ensure_ascii=False))
    except (ValueError,OSError) as exc:
        print(str(exc))
        return 2
    return 0


if __name__=='__main__': raise SystemExit(main())
