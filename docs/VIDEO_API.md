# 用户自己的视频 API

`/video-api` 是视频模型接入页。用户选择供应商、模型和区域，保存自己的 API Key 后，`/studio` 增加一个仅该账号可见的「连接名称 · API」选项。本地 H3、Wan 与 CogVideoX 的入口、执行器及原有就绪检查保持独立。不会自动将失败的本地任务转成收费调用。

## 当前协议

| 供应商 | 模型 | 配置 |
| --- | --- | --- |
| MiniMax | MiniMax-H3 / H3-Highspeed / H3-Lite / H3-Max | 中国站 cn 或国际站 intl，需对应站点视频 API 密钥 |
| 火山方舟 | doubao-seedance-2-5-260628 | cn-beijing，账号需有 Seedance 2.5 的调用权限 |
| 阿里百炼 | wan3.0-video / wan3.0-video-prime | 工作空间 ID 与对应区域，需视频模型权限 |

平台先提供文生视频、画幅、分辨率和整数时长，单次最多 15 秒。每个模型另有最小时长和规格约束；例如 H3-Max 至少 5 秒。暂未接入参考图、参考视频、首尾帧、扩展镜头或语音参数。其他供应商、任意代理地址和聊天接口不接受，以避免把用户密钥发给未知站点。

官方协议依据：[MiniMax H3 创建任务](https://platform.minimax.cn/docs/api-reference/video-generation-v2-create)、[Seedance 创建视频任务](https://console.volcengine.com/ark/region:cn-beijing/docs/create-video-generation-task-api)、[Wan 3.0 视频 API](https://help.aliyun.com/en/model-studio/wan3-video-generation-api-reference)。模型权限、价格和站点应以供应商控制台为准；网页会员不等于 API 余额。

## 费用与安全

保存配置没有网络生成请求，不验证余额，也不扣生成费用。新提交必须带 `allow_paid_call: true`；前端每次复用作品、切换模型或成功提交后都重新要求确认。平台不替用户估算固定价格或保证账户费用上限，不使用运营者旧的 H3 预算与密钥。

每个用户最多 8 个连接、2 个未完成 API 任务、每天 10 次 API 提交；全局最多 20 个未完成 API 任务。朋友入口继续遵守本地/API 合计每天 10 次、最多 1 条未完成任务。状态不明的提交也计入限制。

SQLite 表分别保存账号所属的连接和任务。AES-GCM 加密密钥，以账号和连接/任务 ID 作为认证附加信息；主密钥独立保存在 `.secrets/video-api-master.key`，Linux 权限 0600。连接和任务的任何查询、修改、删除、文件下载均检查账号所属关系；管理员也不会通过这些个人接口得到他人的密钥。数据库自身权限也限制为 0600。备份必须同时保护数据库与主密钥；丢失主密钥无法解密。它们均不得提交到 Git。

更新连接需要版本号，留空密钥表示保留原值。任务使用提交时独立加密的配置快照；删除连接会取消未提交任务，但已经取得供应商 ID 的任务仍可继续查询。成功、失败或取消的任务会移除其密钥快照。

## 任务可靠性

独立的 CPU worker 不占本地 GPU。提交前持久化状态，POST 不自动重试；连接超时、响应缺少任务 ID 或提交进程中断时标记 `unknown`，请用户核对官方控制台，避免重复收费。取得 ID 后只做 GET 查询与下载重试，最长查询 24 小时。

下载不带 API Key，只允许供应商 CDN 的 HTTPS 公网地址，逐跳检查重定向并锁定已验证 IP，保留原始 TLS 主机验证。最多下载 128MiB。结果完整解码、时长及 SHA 检查后才标记成功。作品进入个人历史，也可关联短剧镜头、选择版本和用于片段/整集剪辑；素材不足仍拒绝导出，不以静帧填充。

## 部署

平台使用 Python 3.10+；已有实验室环境已安装这些依赖。独立主机需要 CPU 媒体依赖，不需要安装推理用的 CUDA/PyTorch：

```bash
python -m pip install -r requirements-video-api.txt
```

`VIDEO_PLATFORM_ROOT` 应指向管理员自己的持久化目录，`data/`、`runs/`、`logs/`、`.secrets/` 必须由应用账号持有。管理员在 `data/video-api-policy.json` 显式设置 `{"enabled": true}`，再用服务管理器运行一份：

```bash
python -m video_platform.video_api_worker
```

worker 有文件锁，防止误启动第二份。独立进程应交由同一账号的进程管理器监督；容器 CPU 审阅版仍默认禁用生成，不会自动启用收费请求。该变更不代表 Docker 重新构建、新主机部署或自动重启已验收。

## 本轮验收边界

协议请求、异步状态、超时不重发、账号隔离、密钥加密、并发幂等、朋友入口限制已用模拟供应商进行 CPU 测试。服务器线上验证配置保存、模型目录的动态增删和现有登录。浏览器检查设置页与规格选择；没有用任何用户真实密钥提交本轮付费视频，不能把模拟结果称为三家模型真实生成成功。
