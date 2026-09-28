# Docker 影响清单（创建/启动前待人工审核）

本项目没有执行 Docker build / compose up / docker run。检查日期：2026-09-27。

|项目|新服务配置|对现有容器的影响与边界|
|---|---|---|
|项目名|codex-hub-v1|独立 Compose 项目；不复用现有项目名|
|端口|127.0.0.1:18200（网页与API）|初查空闲，现由本项目本地预览占用，容器启动前需停止该预览；不占用18080、18100、18120、6379、55432；PG/runner不发布端口|
|网络|codex-hub-v1_hub_data / hub_runner / hub_egress|新网络；不加入任何已有网络；PG仅内部网络|
|卷|codex-hub-v1_hub_pgdata|新PG卷；不读取或修改已有卷|
|镜像|项目api/runner镜像、postgres:16.14-bookworm|不覆盖已有镜像标签；新增磁盘占用|
|架构|所有服务linux/amd64|Apple Silicon使用模拟，性能和资源消耗需实测|
|容器名称|由Compose项目生成|不使用已有container_name|
|共享依赖|无现有PG、Redis、Docker socket或本机HOME挂载|不接触已有数据库、凭据目录和容器管理接口|
|资源|含可选IM服务运行上限合计3.75 GiB内存、5 CPU；构建另计|仍共享Docker Desktop宿主资源，不能声称绝对零性能影响；启动前确认宿主余量|
|IM常驻进程|可选 `im` profile 的 im-feishu / im-dingtalk，各384MiB、0.5CPU、64 PID|无新端口；仅加入本项目 hub_data 和 hub_egress；不加入runner网络、不挂载卷或宿主凭据；容器名由Compose生成；镜像包含固定官方SDK|
|IM数据库|共享本项目专属PG，新增im_connections心跳表|每进程5秒一次状态写入；已有IM事件reply_target增加模式字段，无破坏性迁移；需先运行API初始化业务表|
|外部服务|runner访问OpenAI和已授权HTTPS MCP；API访问IM|需要独立凭据；默认无凭据，不会自动发送IM消息|
|安全|非root、只读根文件系统、cap_drop、no-new-privileges|Codex Linux沙箱兼容性需要容器运行验证；不得为解决兼容性静默启用privileged|

人工审核此表后，在项目目录执行 `docker compose --env-file .env config --quiet`，再执行 `docker compose --env-file .env up -d --build`。本次实现阶段只准备和静态验证配置。

停止只用本目录 `docker compose down`，不加 `-v`；数据库备份和卷删除需要独立决定。不要运行全局docker prune。
