"""cloudfront-lambda-s3-picture 单元测试。

覆盖:
    - 纯函数: to_number / parse_query_string / build_thumbnail_key /
              extract_bucket_name / should_skip_processing / _calc_target_size
    - 图片处理: resize_image / _resize_animated / build_response / redirect_response
    - S3 集成: resize_s3_image（含面积比较跳过逻辑）
    - handler 全路径: lambda_handler（skip / resize / redirect / exception）
"""
from __future__ import annotations

import base64
from io import BytesIO

import pytest
from PIL import Image

# ================================================================
# to_number
# ================================================================

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("100", 100),
        ("0", 0),
        ("  42 ", 42),
        ("", 0),
        (None, 0),
        ("abc", 0),
        ("-1", 0),        # ^\d+$ 只匹配正整数，负号视为非法值
        ("1a2", 0),
        ("007", 7),
    ],
)
def test_to_number(mod, raw, expected):
    assert mod.to_number(raw) == expected

# ================================================================
# parse_query_string — w / h / get_frame 格式
# ================================================================

class TestParseQueryString:
    def test_empty(self, mod):
        assert mod.parse_query_string("") == {"size": (0, 0), "get_frame": 0}

    def test_w_only(self, mod):
        assert mod.parse_query_string("w=100")["size"] == (100, 0)

    def test_h_only(self, mod):
        assert mod.parse_query_string("h=200")["size"] == (0, 200)

    def test_w_and_h(self, mod):
        assert mod.parse_query_string("w=200&h=150")["size"] == (200, 150)

    def test_zero_zero(self, mod):
        assert mod.parse_query_string("w=0&h=0")["size"] == (0, 0)

    def test_get_frame(self, mod):
        assert mod.parse_query_string("get_frame=24")["get_frame"] == 24

    def test_invalid_numbers_zero_out(self, mod):
        assert mod.parse_query_string("w=abc&h=&get_frame=xx") == {"size": (0, 0), "get_frame": 0}

# ================================================================
# should_skip_processing
# ================================================================

class TestShouldSkipProcessing:
    # ---- 静态图 ----
    def test_static_no_params(self, mod):
        assert mod.should_skip_processing("a.jpg", {"size": (0, 0), "get_frame": 0})

    def test_static_w_set(self, mod):
        assert not mod.should_skip_processing("a.jpg", {"size": (100, 0), "get_frame": 0})

    def test_static_h_set(self, mod):
        assert not mod.should_skip_processing("a.jpg", {"size": (0, 100), "get_frame": 0})

    def test_static_wh_set(self, mod):
        assert not mod.should_skip_processing("a.jpg", {"size": (100, 100), "get_frame": 0})

    # ---- 动图 ----
    def test_animated_no_params(self, mod):
        assert mod.should_skip_processing("a.gif", {"size": (0, 0), "get_frame": 0})

    def test_animated_get_frame_set(self, mod):
        """size=(0,0) 但 get_frame>0 → 不跳过（要抽帧）。"""
        assert not mod.should_skip_processing("a.gif", {"size": (0, 0), "get_frame": 5})

    def test_animated_w_set(self, mod):
        assert not mod.should_skip_processing("a.webp", {"size": (100, 0), "get_frame": 0})

# ================================================================
# _calc_target_size
# ================================================================

class TestCalcTargetSize:
    def test_zero_zero_returns_raw(self, mod):
        assert mod._calc_target_size((200, 100), (0, 0)) == (200, 100)

    def test_width_only_keep_ratio(self, mod):
        # 200x100 → 指定宽 100 → 高 50
        assert mod._calc_target_size((200, 100), (100, 0)) == (100, 50)

    def test_height_only_keep_ratio(self, mod):
        # 200x100 → 指定高 200 → 宽 400
        assert mod._calc_target_size((200, 100), (0, 200)) == (400, 200)

    def test_both_specified(self, mod):
        assert mod._calc_target_size((200, 100), (80, 60)) == (80, 60)

# ================================================================
# extract_bucket_name / build_thumbnail_key
# ================================================================

def test_extract_bucket_name(mod):
    assert mod.extract_bucket_name("my-bucket.s3.amazonaws.com") == "my-bucket"

class TestBuildThumbnailKey:
    def test_default(self, mod):
        assert mod.build_thumbnail_key({"size": (0, 0), "get_frame": 0}, "/a.jpg") == "0x0/a.jpg"

    def test_w_only(self, mod):
        assert mod.build_thumbnail_key({"size": (100, 0), "get_frame": 0}, "/a.jpg") == "100x0/a.jpg"

    def test_full(self, mod):
        assert mod.build_thumbnail_key({"size": (200, 200), "get_frame": 5}, "/a.gif") == "200x200_5/a.gif"

    def test_get_frame_zero_not_appended(self, mod):
        assert mod.build_thumbnail_key({"size": (100, 100), "get_frame": 0}, "/a.jpg") == "100x100/a.jpg"

    def test_get_frame_neg1_not_appended(self, mod):
        assert mod.build_thumbnail_key({"size": (0, 0), "get_frame": -1}, "/a.jpg") == "0x0/a.jpg"

# ================================================================
# resize_image — 静态图
# ================================================================

class TestResizeImage:
    def test_jpg_width_only(self, mod):
        # Image.new() 创建的 img.format=None，必须手动赋值以符合生产路径
        # （生产中 Image.open(S3 bytes) 会自动从 magic bytes 检测 format）
        img = Image.new("RGB", (200, 100), "red")
        img.format = "JPEG"
        buf = mod.resize_image(img, {"size": (100, 0), "get_frame": 0})
        result = Image.open(buf)
        assert result.size == (100, 50)
        assert result.format == "JPEG"

    def test_jpg_height_only(self, mod):
        img = Image.new("RGB", (200, 100), "red")
        img.format = "JPEG"
        buf = mod.resize_image(img, {"size": (0, 200), "get_frame": 0})
        assert Image.open(buf).size == (400, 200)

    def test_jpg_both_specified(self, mod):
        img = Image.new("RGB", (200, 100), "red")
        img.format = "JPEG"
        buf = mod.resize_image(img, {"size": (80, 60), "get_frame": 0})
        assert Image.open(buf).size == (80, 60)

    def test_no_resize_when_zero_zero(self, mod):
        img = Image.new("RGB", (200, 100), "red")
        img.format = "JPEG"
        buf = mod.resize_image(img, {"size": (0, 0), "get_frame": 0})
        assert Image.open(buf).size == (200, 100)

    def test_png_rgba_preserved(self, mod):
        img = Image.new("RGBA", (200, 200), (255, 0, 0, 128))
        img.format = "PNG"
        buf = mod.resize_image(img, {"size": (50, 50), "get_frame": 0})
        result = Image.open(buf)
        assert result.size == (50, 50)
        assert result.mode == "RGBA"
        assert result.format == "PNG"

# ================================================================
# _resize_animated — 动图抽帧
# ================================================================

class TestResizeAnimated:
    def _make_gif(self, n_frames: int, size=(50, 50)) -> Image.Image:
        palette = ["red", "green", "blue", "yellow", "purple", "orange", "cyan", "magenta", "white", "black"]
        frames = [Image.new("P", size, c) for c in palette[:n_frames]]
        out = BytesIO()
        frames[0].save(
            out,
            format="GIF",
            append_images=frames[1:],
            save_all=True,
            duration=40,
            loop=0,
        )
        img = Image.open(BytesIO(out.getvalue()))
        assert img.n_frames == n_frames
        return img

    def test_get_frame_zero_keeps_all(self, mod):
        img = self._make_gif(10)
        buf = mod._resize_animated(img, (25, 25), get_frame=0)
        result = Image.open(buf)
        assert result.n_frames == 10
        assert result.size == (25, 25)

    def test_get_frame_gt_n_frames_keeps_all(self, mod):
        img = self._make_gif(5)
        buf = mod._resize_animated(img, (25, 25), get_frame=20)
        assert Image.open(buf).n_frames == 5

    def test_get_frame_one_keeps_first(self, mod):
        img = self._make_gif(10)
        buf = mod._resize_animated(img, (25, 25), get_frame=1)
        assert Image.open(buf).n_frames == 1

    def test_even_spaced_sampling(self, mod):
        """10 帧抽 5 → 等间隔，首尾都有。"""
        img = self._make_gif(10)
        buf = mod._resize_animated(img, (25, 25), get_frame=5)
        assert Image.open(buf).n_frames == 5

    def test_preserves_duration_and_loop(self, mod):
        img = self._make_gif(5)
        buf = mod._resize_animated(img, (25, 25), get_frame=0)
        result = Image.open(buf)
        assert result.info["loop"] == 0
        result.seek(0)
        assert result.info["duration"] == 40

# ================================================================
# build_response / redirect_response
# ================================================================

def test_build_response(mod):
    resp = {"status": "200", "headers": {}}
    buf = BytesIO(b"\xff\xd8\xff\xe0fake-jpeg-bytes")
    out = mod.build_response(resp, buf, "jpeg")
    assert out["status"] == "200"
    assert out["bodyEncoding"] == "base64"
    assert out["body"] == base64.b64encode(buf.getvalue()).decode()
    assert out["headers"]["content-type"][0]["value"] == "image/jpeg"

def test_build_response_normalizes_ext_case(mod):
    """img.format 可能是 'GIF'，写入时应统一成小写。"""
    resp = {"status": "200", "headers": {}}
    buf = BytesIO(b"x")
    out = mod.build_response(resp, buf, "GIF")
    assert out["headers"]["content-type"][0]["value"] == "image/gif"

def test_redirect_response(mod):
    resp = {"status": "200", "headers": {}}
    out = mod.redirect_response(resp, "100x0/a.jpg")
    assert out["status"] == "301"
    assert out["statusDescription"] == "Moved Permanently"
    assert out["headers"]["location"][0]["value"] == "/100x0/a.jpg"

# ================================================================
# resize_s3_image — 含面积比较跳过逻辑
# ================================================================

class TestResizeS3Image:
    def test_resize_by_width(self, mod, setup_bucket):
        buf, ext = mod.resize_s3_image(
            "test-bucket", "photo.jpg", {"size": (100, 0), "get_frame": 0}
        )
        assert buf is not None
        assert ext == "jpeg"
        assert Image.open(buf).size == (100, 50)

    def test_resize_by_height(self, mod, setup_bucket):
        buf, ext = mod.resize_s3_image(
            "test-bucket", "photo.jpg", {"size": (0, 50), "get_frame": 0}
        )
        assert buf is not None
        assert Image.open(buf).size == (100, 50)

    def test_resize_by_wh(self, mod, setup_bucket):
        buf, ext = mod.resize_s3_image(
            "test-bucket", "photo.jpg", {"size": (80, 40), "get_frame": 0}
        )
        assert buf is not None
        assert Image.open(buf).size == (80, 40)

    def test_skip_when_target_bigger_area(self, mod, setup_bucket):
        """原图 200x100(面积 20000)，请求 (500,0) → 等比 500x250(125000) ≥ 20000 → 跳过。"""
        buf, ext = mod.resize_s3_image(
            "test-bucket", "photo.jpg", {"size": (500, 0), "get_frame": 0}
        )
        assert buf is None
        assert ext == ""

    def test_skip_when_wh_both_specified_but_bigger(self, mod, setup_bucket):
        """原图 200x100，请求 (150,200) → 面积 30000 ≥ 20000 → 跳过。"""
        buf, ext = mod.resize_s3_image(
            "test-bucket", "photo.jpg", {"size": (150, 200), "get_frame": 0}
        )
        assert buf is None

    def test_skip_when_zero_zero(self, mod, setup_bucket):
        buf, ext = mod.resize_s3_image(
            "test-bucket", "photo.jpg", {"size": (0, 0), "get_frame": 0}
        )
        assert buf is not None   # (0,0) 时面积不比较，resize_image 会返回原图大小

    def test_s3_exception_propagates(self, mod):
        with pytest.raises(Exception):
            mod.resize_s3_image(
                "no-such-bucket", "no.jpg", {"size": (100, 0), "get_frame": 0}
            )

# ================================================================
# lambda_handler — 全路径集成
# ================================================================

def test_handler_skip_static_no_params(mod, setup_bucket, cf_event):
    event = cf_event("/photo.jpg")
    resp = mod.lambda_handler(event, None)
    assert resp["status"] == "200"

def test_handler_skip_animated_no_params(mod, setup_bucket, cf_event):
    event = cf_event("/anim.gif")
    resp = mod.lambda_handler(event, None)
    assert resp["status"] == "200"

def test_handler_resize_static_by_w(mod, setup_bucket, cf_event):
    event = cf_event("/photo.jpg", "w=100")
    resp = mod.lambda_handler(event, None)
    assert resp["status"] == "200"
    assert resp["bodyEncoding"] == "base64"
    assert Image.open(BytesIO(base64.b64decode(resp["body"]))).size == (100, 50)

def test_handler_resize_static_by_h(mod, setup_bucket, cf_event):
    event = cf_event("/photo.jpg", "h=50")
    resp = mod.lambda_handler(event, None)
    assert Image.open(BytesIO(base64.b64decode(resp["body"]))).size == (100, 50)

def test_handler_resize_by_wh(mod, setup_bucket, cf_event):
    event = cf_event("/photo.jpg", "w=80&h=40")
    resp = mod.lambda_handler(event, None)
    assert Image.open(BytesIO(base64.b64decode(resp["body"]))).size == (80, 40)

def test_handler_resize_animated_keep_all_frames(mod, setup_bucket, cf_event):
    event = cf_event("/anim.gif", "w=50")
    resp = mod.lambda_handler(event, None)
    assert resp["status"] == "200"
    result = Image.open(BytesIO(base64.b64decode(resp["body"])))
    assert result.n_frames == 3
    assert result.size == (50, 50)

def test_handler_animated_get_frame_1(mod, setup_bucket, cf_event):
    """动图只取第一帧。"""
    event = cf_event("/anim.gif", "get_frame=1")
    resp = mod.lambda_handler(event, None)
    result = Image.open(BytesIO(base64.b64decode(resp["body"])))
    assert result.n_frames == 1

def test_handler_redirects_when_too_large(mod, setup_bucket, cf_event, monkeypatch):
    """resize 后结果超过 MAX_RESPONSE_BODY → 写回 S3 + 301 重定向。

    无法真实构造缩完仍 >1MB 的图片（JPEG 压缩率很高），
    把 MAX_RESPONSE_BODY patch 成 100 bytes 逻辑验证即可。
    """
    monkeypatch.setattr(mod, "MAX_RESPONSE_BODY", 100)
    event = cf_event("/photo.jpg", "w=100")
    resp = mod.lambda_handler(event, None)
    assert resp["status"] == "301"
    location = resp["headers"]["location"][0]["value"]
    assert location.startswith("/100x0")
    # moto S3 里应该有回写的对象（注意用 mod.s3，不是 picture.s3）
    obj = mod.s3.Object("test-bucket", location.lstrip("/"))
    obj.load()
    assert obj.content_type == "image/jpeg"

def test_handler_swallows_exception(mod, setup_bucket, cf_event):
    """异常时必须返回原始 response，不能让 CloudFront 502。"""
    event = cf_event("/no-such-file.jpg", "w=100", status="404")
    resp = mod.lambda_handler(event, None)
    assert resp["status"] == "404"