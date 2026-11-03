"""pytest 共享 fixtures。"""
from __future__ import annotations

import importlib.util
import sys
from io import BytesIO
from pathlib import Path

import boto3
import pytest
from moto import mock_aws
from PIL import Image

# 让 tests/ 下能 import 项目根目录里的东西
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# cloudfront-lambda-s3-picture.py 带连字符，只能动态加载
_MODULE_PATH = ROOT / "cloudfront-lambda-s3-picture.py"
spec = importlib.util.spec_from_file_location("picture", _MODULE_PATH)
picture = importlib.util.module_from_spec(spec)
sys.modules["picture"] = picture
spec.loader.exec_module(picture)

@pytest.fixture
def mod():
    """返回加载好的业务模块，每个测试直接用 mod.xxx。"""
    return picture

@pytest.fixture(autouse=True)
def _patch_s3_resource():
    """把模块顶层的 s3 resource 替换成 moto 假实现。

    必须在 mock 激活期间重新拿一次 resource，
    因为模块 import 时 s3 = boto3.resource(...) 已经指向真实 AWS 了。
    """
    with mock_aws():
        picture.s3 = boto3.resource("s3", region_name="us-east-1")
        yield

@pytest.fixture
def bucket_name() -> str:
    return "test-bucket"

@pytest.fixture
def setup_bucket(bucket_name: str):
    """预置 moto S3 bucket，塞 4 种覆盖场景的图片。"""
    bucket = picture.s3.create_bucket(Bucket=bucket_name)

    # 1) 普通 JPG 200x100 — 等比缩宽 / 缩高都会用到
    jpg_buf = BytesIO()
    Image.new("RGB", (200, 100), "red").save(jpg_buf, format="JPEG")
    bucket.Object("photo.jpg").put(Body=jpg_buf.getvalue(), ContentType="image/jpeg")

    # 2) PNG RGBA 100x100 — 测试 mode 保留
    png_buf = BytesIO()
    Image.new("RGBA", (100, 100), (0, 128, 255, 255)).save(png_buf, format="PNG")
    bucket.Object("pic.png").put(Body=png_buf.getvalue(), ContentType="image/png")

    # 3) GIF 3 帧 100x100 — 动图抽帧 / 全保留场景
    frames = []
    for color in ["red", "green", "blue"]:
        b = BytesIO()
        Image.new("P", (100, 100), color).save(b, format="GIF")
        frames.append(Image.open(BytesIO(b.getvalue())))
    gif_out = BytesIO()
    frames[0].save(
        gif_out,
        format="GIF",
        append_images=frames[1:],
        save_all=True,
        duration=50,
        loop=0,
    )
    bucket.Object("anim.gif").put(Body=gif_out.getvalue(), ContentType="image/gif")

    # 4) 超大 JPG — 触发 >MAX_RESPONSE_BODY 走 redirect 路径
    big_img = Image.new("RGB", (4000, 4000), "white")
    big_buf = BytesIO()
    big_img.save(big_buf, format="JPEG", quality=100)
    bucket.Object("big.jpg").put(Body=big_buf.getvalue(), ContentType="image/jpeg")

    return bucket

@pytest.fixture
def cf_event():
    """工厂 fixture，构造 CloudFront Origin Response 事件。"""
    def _make(uri: str, querystring: str = "", status: str = "200") -> dict:
        return {
            "Records": [
                {
                    "cf": {
                        "request": {
                            "uri": uri,
                            "querystring": querystring,
                            "origin": {
                                "s3": {"domainName": "test-bucket.s3.amazonaws.com"}
                            },
                        },
                        "response": {
                            "status": status,
                            "headers": {
                                "content-type": [
                                    {"key": "Content-Type", "value": "image/jpeg"}
                                ],
                            },
                        },
                    }
                }
            ]
        }
    return _make