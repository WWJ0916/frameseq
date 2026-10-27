# API约定与失败边界

邀请预览默认要求登录。浏览器在同源会话中提交CSRF；未登录的私有API返回401，其他用户的任务/项目返回404。请求体使用Pydantic严格校验，不允许任意额外工具字段。

2026-10-23现状：H3使用`POST /api/tasks`及`model:local-h3`进入本地双卡队列。新云生成、重试及查询恢复返回410；旧云作品仍可读取。外部语言助手API已移除。推理runtime仍需独立配置，见[安装边界](H3_AND_LANGUAGE_API.md)。

| 路径 | 用途 | 关键约定 |
| --- | --- | --- |
| `POST /api/auth/login` | 登录 | 不在错误响应中回显密码 |
| `POST /api/auth/register` | 注册 | 一次性邀请码；密码12–128字符 |
| `POST /api/auth/invites` | 创建邀请码 | 仅管理员 |
| `GET /api/models` | 模型与就绪情况 | 有真实结果和当前能否提交是不同字段 |
| `POST /api/tasks` | 提交本地任务 | `Idempotency-Key`配合同一参数重用；返回202与任务ID |
| `GET /api/tasks/{id}` | 状态 | queued/running/succeeded/failed/cancelled/paused |
| `POST /api/tasks/{id}/cancel` | 排队取消 | 只取消排队任务，不随意终止执行中的进程 |
| `GET /api/tasks/{id}/video` | 私有MP4 | 支持Range206；`?download=true`下载 |
| `POST /api/cloud/tasks` | 历史云生成入口 | 当前410，使用本地任务入口 |
| `POST /api/cloud/tasks/{id}/resume-result` | 历史查询恢复入口 | 当前410，不再发送供应商查询 |
| `GET /api/assistant/providers` | 语言提供商状态 | 与视频模型目录分开，默认禁用外部调用 |
| `GET /api/library` | 统一历史 | 本地/云来源明确保留 |
| `POST /api/agent/plan` | 生成需求方案 | 生成JSON方案，尚未提交视频 |
| `POST /api/agent/plans/{id}/submit` | 审阅后提交 | 同方案并发/重试只入队一次 |
| `POST /api/projects` / `PUT /api/projects/{id}` | 项目与剧集 | 更新携带revision；冲突409 |
| `POST /api/projects/{id}/episodes/{episode}/export` | 导出 | 已选素材足量才接受，CPU处理已有视频 |

本地请求示例（需已登录会话和CSRF）：

```json
{"prompt":"A blue paper boat floats gently on clear water.","model":"local-wan","preset":"story","seed":42}
```

本地H3当前固定124帧、24fps，约5.17秒；不支持凭请求任意改变时长。历史云API合同曾支持4–15秒整数，真正付费验收只覆盖已记录的4秒请求，该入口现已关闭。分镜目标时长与单次模型实际输出时长分别记录，不能将描述估算当成模型可精确输出任意时长。

常见失败：400提交标识不合法；401未登录；403权限/CSRF；404非本人或不存在；409参数冲突、模型未就绪、预算/素材条件未满足；422字段校验失败；429队列或频率限制。供应商POST结果不明确时留下unknown，不能用无条件自动重试绕过重复收费风险。

CogVideoX的`inference_profile`、步数、精度和调度器由服务器当前已验收配置补齐，不接受客户端覆盖。任务与下载记录保留这些字段，旧执行器未声明支持该版本时返回409。实验预览不承诺对任意描述的画质；多数空白帧检测与人工拒绝记录是额外边界。

## 个人视频API接口（2026-10-25）

所有接口使用现有登录会话和CSRF保护：`GET /api/video-api/providers`、`GET/POST /api/video-api/connections`、`PUT/DELETE /api/video-api/connections/{id}`、`GET/POST /api/video-api/tasks`、`GET /api/video-api/tasks/{id}`、`POST /api/video-api/tasks/{id}/cancel`、`GET /api/video-api/tasks/{id}/video`。

创建任务携带独立的 Idempotency-Key，以及 connection_id、prompt、整数duration、resolution、ratio、allow_paid_call。只有未提交的queued任务可以取消；供应商提交状态不明时保留unknown，不自动重发。旧的运营者`/api/cloud/tasks`入口仍关闭；新的个人API权限与旧预算无关。[完整说明](VIDEO_API.md)。
