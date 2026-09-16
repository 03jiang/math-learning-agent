"""验证并规范化照片；原文件名与 EXIF 不进入存档或模型请求。"""
import base64
import hashlib
from io import BytesIO
import warnings
from PIL import Image, ImageOps, UnidentifiedImageError

MAX_UPLOAD = 8 * 1024 * 1024
MAX_PIXELS = 20_000_000


def _pack_image(image):
    clean=Image.new('RGB',image.size,'white')
    clean.paste(image)
    output=BytesIO()
    clean.save(output,'JPEG',quality=92)
    content=output.getvalue()
    return {'mime':'image/jpeg','base64':base64.b64encode(content).decode('ascii'),
            'sha256':hashlib.sha256(content).hexdigest(),'width':clean.width,'height':clean.height}


def prepare_image(raw):
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_UPLOAD:
        raise ValueError('请上传不超过 8 MB 的 JPG、PNG 或 WebP 图片。')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw)) as image:
                if image.format not in ('JPEG', 'PNG', 'WEBP') or getattr(image, 'n_frames', 1) != 1:
                    raise ValueError('仅支持单张 JPG、PNG 或 WebP 图片。')
                if image.width * image.height > MAX_PIXELS:
                    raise ValueError('图片像素过大，请裁剪为一道题再上传。')
                image.load()
                oriented = ImageOps.exif_transpose(image).convert('RGBA')
                clean = Image.new('RGB', oriented.size, 'white')
                clean.paste(oriented, mask=oriented.getchannel('A'))
                clean.thumbnail((2000, 2000))
                return _pack_image(clean)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise ValueError('图片无法读取或尺寸过大，请换一张清晰的图片。') from None


def image_bytes(image):
    if not isinstance(image, dict) or set(image) != {'mime', 'base64', 'sha256', 'width', 'height'}:
        raise ValueError('图片记录不完整。')
    if image['mime'] != 'image/jpeg' or not isinstance(image['base64'], str) or len(image['base64']) > MAX_UPLOAD * 2:
        raise ValueError('图片记录格式不正确。')
    try:
        raw = base64.b64decode(image['base64'], validate=True)
    except (ValueError, TypeError):
        raise ValueError('图片记录损坏。') from None
    if hashlib.sha256(raw).hexdigest() != image['sha256']:
        raise ValueError('图片校验失败。')
    with Image.open(BytesIO(raw)) as check:
        if check.size != (image['width'], image['height']) or check.width * check.height > MAX_PIXELS:
            raise ValueError('图片尺寸记录不符。')
    return raw


def transform_image(image,*,rotation=0,horizontal=(0,100),vertical=(0,100)):
    """始终从本轮原图生成裁剪预览；顺时针旋转后按百分比裁剪，不改原文件。"""
    if type(rotation) is not int or rotation not in (0,90,180,270):
        raise ValueError('旋转角度只能为 0、90、180 或 270 度。')
    for limits in (horizontal,vertical):
        if (type(limits) not in (tuple,list) or len(limits)!=2 or any(type(v) is not int for v in limits)
                or not 0<=limits[0]<limits[1]<=100):
            raise ValueError('裁剪范围需在 0–100% 内，并保留非空区域。')
    raw=image_bytes(image)
    if rotation==0 and tuple(horizontal)==(0,100) and tuple(vertical)==(0,100):
        return dict(image)
    with Image.open(BytesIO(raw)) as source:
        clean=source.convert('RGB')
        rotations={90:Image.Transpose.ROTATE_270,180:Image.Transpose.ROTATE_180,270:Image.Transpose.ROTATE_90}
        if rotation: clean=clean.transpose(rotations[rotation])
        left,right=[round(clean.width*v/100) for v in horizontal]
        top,bottom=[round(clean.height*v/100) for v in vertical]
        if left>=right or top>=bottom: raise ValueError('裁剪区域太小，请扩大范围。')
        return _pack_image(clean.crop((left,top,right,bottom)))
