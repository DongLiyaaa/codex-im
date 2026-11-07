# IM 指南扩展上线记录

完成时间：2026-11-07T07:35:04+08:00。目标：现有本机 Docker codex-hub-v1，http://127.0.0.1:18210。

| 项目 | 结果 |
|---|---|
| 内容 | 13 个主题、5 类案例；IM 集成、独立说明书和本页说明共用内容 |
| 发布镜像 | codex-hub-v1-api:im-guide-20261107，linux/amd64 |
| 镜像 ID | sha256:ea566c865c452785572bb29facd42ec3dce66fcca4fcb0734300b8dd4ecb4015 |
| API 容器 ID | 06a7f8dbf52e479773e094982c0bebcd5853b7127d89aa55676cf65aa708d404 |
| 健康与重启 | healthy；0 次重启；health HTTP 200 |
| 静态资源 | index-BApFA9XY.js、index-DFtE8mTP.css；线上响应与本地已验收构建逐字节相同 |
| 后端与配置 | 部署前核验后端 Python 文件哈希、环境配置均与运行版本一致；无新增数据库迁移 |
| 其他容器 | 14 个运行容器的 ID、镜像、启动时间及重启次数保持不变 |
| 登录边界 | 匿名 auth/me 返回 401；setup/status 为 true |
| 启动日志 | 未发现 traceback、fatal、exception 标记 |
| 回滚镜像 | codex-hub-v1-api:pre-im-guide-20261107 |

本地验证沿用本轮说明书验收：267 项测试通过，搜索展开微调后相关 30 项再次通过；本次 Docker 内 TypeScript 与 Vite 构建通过。既有 bundle 大小提示不影响构建。

仅替换 API；没有新建测试容器、调整平台应用凭据、发送真实 IM 消息、调用模型、提交或推送 Git。部署后未登录真实用户验证新页面交互；已验证线上静态资源与本地浏览器验收构建一致。

回滚：将上述回滚镜像标记为 codex-hub-v1-api:amd64-local，使用当前 compose.yaml 与 compose.runner-arm64.yaml、.env.docker 对 api 执行 up -d --no-deps --no-build --wait。无须恢复数据库。

部署快照、源码哈希和 receipt.json 存放在 .runtime/releases/20261107-im-guide/；不含环境密钥值。
