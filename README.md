# CloudFront Lambda@Edge 图片动态处理 — 使用文档

## 一、项目概述

本项目是一个基于 **AWS Lambda@Edge** 的图片动态处理服务。它在 CloudFront 的 **Origin Response**（源站响应）阶段拦截请求，根据 URL 的 Query String 参数对 S3 上的原图进行**缩放**或 **GIF/WebP 抽帧**处理，然后直接返回处理后的图片或重定向到回写 S3 的缩略图。

**源码位置：** `cloudfront-lambda-s3-picture.py`

---

## 二、工作流程

```
用户请求 (带参数)
       ↓
  CloudFront (CDN)
       ↓
  Lambda@Edge (Origin Response 阶段)
       ↓
  1. 解析 Query String 参数
  2. 从 S3 下载原图
  3. Pillow 进行缩放 / 抽帧处理
       ↓
  body ≤ 1MB  →  直接 base64 编码返回 (HTTP 200)
  body > 1MB  →  写回 S3 (thumb/ 目录)  →  301 重定向
```

---

## 三、Query String 参数说明

| 参数 | 类型 | 说明 | 示例 |
|------|------|------|------|
| `s` | 字符串（宽x高） | **严格模式**，直接缩放到指定尺寸（可能失真），优先级最高 | `s=300x200` |
| `w` | 正整数 | **限宽等比例缩放**，只缩小不放大 | `w=400` |
| `h` | 正整数 | **限高等比例缩放**，只缩小不放大 | `h=300` |
| `f` | 正整数 | **动图抽帧**，仅对 GIF/WebP 生效 | `f=5` |

---

## 四、参数优先级与规则

### 4.1 缩放参数优先级

```
严格模式 s  >  w/h 同时传（宽优先）  >  单独 w  >  单独 h  >  无缩放
```

### 4.2 严格模式 `s`

- **格式**：`宽x高`（小写字母 x），如 `s=300x200`
- **约束**：宽和高必须同时大于 0，否则视为无效
- **行为**：直接缩放到指定尺寸，**可能导致图片变形**，慎用

### 4.3 等比例缩放 `w` / `h`

| 场景 | 行为 |
|------|------|
| 只传 `w=400` | 按宽度等比例缩放，保持原图比例 |
| 只传 `h=300` | 按高度等比例缩放，保持原图比例 |
| 同时传 `w` 和 `h` | **宽优先**，按宽度等比例缩放（`h` 被忽略） |
| 目标尺寸 ≥ 原图尺寸 | **跳过处理**，直接返回原图（不会放大） |

### 4.4 抽帧参数 `f`

| 场景 | 行为 |
|------|------|
| `f ≤ 0` 或 `f ≥ 总帧数` | 保留全部帧，不抽帧 |
| `f = 1` | 只保留第 1 帧 |
| `f > 1` | 等间隔抽取 `f` 帧，首尾都保留 |

---

## 五、使用示例

假设有一张图片 `/images/photo.jpg`（原图 1920×1080），通过 CloudFront 域名访问：

```
https://your-cloudfront-domain.com/images/photo.jpg
```

### 5.1 静态图示例

| 需求 | URL | 说明 |
|------|-----|------|
| 原图 | `/images/photo.jpg` | 无参数，不处理 |
| 宽 800 等比缩放 | `/images/photo.jpg?w=800` | 输出 800×450 |
| 高 600 等比缩放 | `/images/photo.jpg?h=600` | 输出 1066×600 |
| 严格缩到 300×300 | `/images/photo.jpg?s=300x300` | 输出 300×300（可能变形） |

### 5.2 动图示例

假设 `/images/anim.gif` 是一个 10 帧的 GIF 动图：

| 需求 | URL | 说明 |
|------|-----|------|
| 宽 400 + 抽 3 帧 | `/images/anim.gif?w=400&f=3` | 缩放到宽 400，等间隔抽取 3 帧 |
| 只抽 5 帧 | `/images/anim.gif?f=5` | 等间隔抽 5 帧，不缩放 |
| 只抽 1 帧 | `/images/anim.gif?f=1` | 只保留第 1 帧 |

---

## 六、静态图 vs 动图行为对比

| 场景 | 静态图（JPG/PNG 等） | 动图（GIF/WebP） |
|------|---------------------|-----------------|
| 无参数 | ✅ 跳过，返回原图 | ✅ 跳过，返回原图 |
| 只传 `f` | ❌ 跳过（静态图无法抽帧） | ✅ 只抽帧，不缩放 |
| 只传 `w/h/s` | ✅ 缩放 | ✅ 逐帧缩放，保持动画 |
| `w/h` + `f` | ✅ 只缩放 | ✅ 缩放 + 抽帧 |
| 未达到缩小条件 | ✅ 跳过，返回原图 | ✅ 跳过，返回原图 |

---

## 七、响应策略

### 7.1 直接返回（body ≤ 1MB）

Lambda 将处理后的图片以 **base64** 编码放在响应体中返回：

| HTTP 字段 | 值 |
|-----------|-----|
| 状态码 | `200` |
| `Content-Type` | 对应图片 MIME 类型（如 `image/jpeg`） |
| `bodyEncoding` | `base64` |

CloudFront 会自动将 base64 解码后下发给客户端。

### 7.2 回写重定向（body > 1MB）

当处理后的图片超过 Lambda@Edge 响应体上限（**1,048,576 字节 = 1MB**）时：

1. 将处理后的图片上传回 S3，Key 为 `thumb/` 前缀
2. 返回 HTTP `301 Moved Permanently` 重定向
3. 后续请求可直接命中 CloudFront 缓存，不再触发 Lambda

#### 缩略图 S3 Key 生成规则

```
thumb/{宽}x{高}_{帧数}{原始 uri}

示例:
  /images/anim.gif?w=400&f=5  →  thumb/400x0_5/images/anim.gif
  /images/photo.jpg?w=800     →  thumb/800x0_0/images/photo.jpg
```

---

## 八、部署要求

### 8.1 Python 运行时

Lambda@Edge 支持的 Python 版本：

- Python 3.9
- Python 3.10
- Python 3.11
- Python 3.12

### 8.2 依赖库

```txt
Pillow>=10.0
boto3>=1.26  # AWS Lambda 内置，打包时可排除
```

### 8.3 打包命令

```bash
# 创建部署包目录
mkdir -p package
cp cloudfront-lambda-s3-picture.py package/

# 安装 Pillow 到 package 目录（注意：需在 Linux 环境打包）
pip install Pillow -t package/

# 打包为 zip
cd package && zip -r ../lambda-deployment-package.zip . && cd ..
```

### 8.4 IAM 权限

Lambda 执行角色需要以下权限：

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetObject"
      ],
      "Resource": "arn:aws:s3:::your-bucket-name/*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "s3:PutObject"
      ],
      "Resource": "arn:aws:s3:::your-bucket-name/thumb/*"
    }
  ]
}
```

---

## 九、Lambda@Edge 配置说明

### 9.1 创建 Lambda 函数

1. 登录 **AWS Lambda 控制台** → 创建函数
2. **运行时**：选择 Python 3.9 或更高版本
3. **上传代码**：上传打包好的 zip 文件
4. **函数名**：任意命名（如 `cloudfront-image-resizer`）
5. **部署到 Edge**：在函数页面点击 **"Deploy to Lambda@Edge"** 按钮

### 9.2 关联 CloudFront 触发器

在 CloudFront 分发设置中配置：

| 配置项 | 值 |
|--------|-----|
| 触发器位置 | **Origin Response**（源站响应） |
| 关联行为 | 默认行为或指定路径模式 |
| Lambda 函数版本 | 使用 `$LATEST` 或已发布版本 |
| Include Body | 不需要勾选（只读源站响应） |

### 9.3 CloudFront Origin 配置

确保 S3 Origin 已配置且 Bucket 可公开访问（或启用 Origin Access Identity）。

---

## 十、限制条件与注意事项

| 限制项 | 说明 |
|--------|------|
| **响应体上限** | Lambda@Edge Origin Response body 最大 **1MB**，超出自动回写重定向 |
| **不会放大** | 目标尺寸 ≥ 原图尺寸时跳过处理，直接返回原图 |
| **S3 命名规范** | Bucket 名和目录名需**全部小写**以符合 S3 命名规范 |
| **Lambda 并发** | Lambda@Edge 有每个区域的并发执行上限 |
| **临时存储** | Lambda@Edge 不能使用 `/tmp` 目录，全部在内存中处理 |
| **冷启动延迟** | 首次请求或长时间不活动后有冷启动延迟 |
| **图片格式** | BMP、TIFF、ICO 等非常见格式可能无法正确处理 |
| **严格模式风险** | `s=宽x高` 强制缩放可能导致图片变形，建议优先使用 `w`/`h` |
| **费用** | 每次图片处理都会产生 Lambda 执行费用和 S3 请求费用 |

---

## 十一、核心源码结构

```
cloudfront-lambda-s3-picture.py
├── lambda_handler()          # 入口函数 (L246)
├── parse_query_string()      # 解析 URL 参数 (L54)
├── should_skip_processing()  # 判断是否跳过处理 (L105)
├── resize_s3_image()         # 从 S3 下载并处理 (L119)
├── resize_image()            # 图片处理主逻辑 (L226)
│   ├── _calc_target_size()   # 计算缩放尺寸 (L168)
│   └── _resize_animated()    # 动图缩放+抽帧 (L192)
├── build_response()          # 构造 200 响应 (L143)
├── redirect_response()       # 构造 301 重定向 (L158)
└── upload_to_s3()            # 回写 S3 (L136)
```

---

## 十二、参考链接

- [AWS Lambda@Edge 官方文档](https://docs.aws.amazon.com/lambda/latest/dg/lambda-at-edge.html)
- [Lambda@Edge 函数限制](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/lambda-at-edge-function-restrictions.html)
- [AWS 官方博客：Lambda@Edge 处理事务](https://aws.amazon.com/cn/blogs/china/use-cloudfront-lambdaedge-for-transaction-processing/)
- [Pillow 官方文档](https://pillow.readthedocs.io/)