# Docker 影响清单（创建/启动前人工审核）

创建或启动前，按实际部署环境核查以下影响。下表是 compose.yaml 当前实际创建的内容，经 `docker compose config` 解析核对。

|项目|新服务配置|对现有容器的影响与边界|
|---|---|---|
|项目名|codex-hub-v1|独立 Compose 项目；不复用现有项目名|
|容器|`codex-hub-v1-db-1`、`-api-1`、`-attachments-1`、`-runner-1`；可选 `im` profile 的 `-im-feishu-1`、`-im-dingtalk-1`|由 Compose 生成，不使用 `container_name`，与已有容器无重名|
|端口|`${HUB_BIND:-127.0.0.1}:${HUB_PORT:-18200}` → api:18200；其余服务不发布端口|端口由 `.env.docker` 的 `HUB_PORT` 决定。本机已有直接运行的服务占用 18200 时，用别的端口（本次验证用 18210）；启动前确认端口空闲|
|网络|`codex-hub-v1_hub_data`（internal）、`hub_runner`（internal）、`hub_egress`|新网络；不加入任何已有网络；PG 只在 internal 网络里，不能出网|
|卷|`codex-hub-v1_hub_pgdata`、`codex-hub-v1_hub_attachments`|新卷；不读取或修改已有卷。附件卷只挂给 api 与 attachments，runner 不挂|
|数据库|栈内自带 PostgreSQL 16.14 容器，只创建 `hub` 库|不使用也不连接已有 PG（含 1panel 的 55432、本机直接运行的 55439）和 Redis|
|镜像|`codex-hub-v1-api:amd64-local`（api、attachments、im 共用）、`codex-hub-v1-runner:amd64-local`、`postgres:16.14-bookworm`|新标签，不覆盖 `ghcr.io/dongliyaaa/codex-im-*` 等已有标签；新增磁盘占用|
|架构|所有服务 `linux/amd64`|Apple Silicon 上是模拟运行，性能和资源消耗需实测|
|共享依赖|无现有 PG、Redis、Docker socket、本机 HOME 或凭据目录挂载|不接触已有数据库和容器管理接口|
|资源|db 768 MiB/1 CPU，api 768 MiB/1 CPU，attachments 512 MiB/1 CPU，runner 1.5 GiB/2 CPU（pids 192）；可选 IM 各 384 MiB/0.5 CPU/64 PID|上限合计约 3.5 GiB（含 IM 约 4.25 GiB）。仍共享 Docker Desktop 宿主资源，不能声称零性能影响；amd64 构建期 CPU 会冲高，另计|
|IM 常驻进程|可选 `im` profile，默认不启动|飞书同一应用只允许一个长连接；本机已有直接运行的 IM 进程时不要同时启动，否则消息会被两边各分走一部分。启动前先停掉其中一边|
|IM 数据库|共享本栈专属 PG，使用 im_connections 心跳表|每进程 5 秒一次状态写入；需要 API 先完成业务表迁移，由 api 健康检查保证启动顺序|
|Claude CLI（可选）|runner 镜像额外安装固定版本的 Claude CLI（2.1.286），多占一部分镜像体积；`HUB_AGENTS` 默认只有 codex|不新增容器、端口、网络或卷。启用 claude 后 runner 另外访问 `CLAUDE_BASE_URL`（留空为 Anthropic 官方端点），与 Codex 的出站规则一致，仍无 Docker socket 与宿主 HOME 挂载|
|外部服务|runner 访问模型服务和已授权 HTTPS MCP；api、attachments 访问 IM 平台|需要独立凭据；默认无凭据，不会自动发送 IM 消息|
|安全|非 root（uid 10001）、只读根文件系统、`cap_drop: ALL`、`no-new-privileges`、tmpfs 限额|Codex Linux 沙箱兼容性需要实际验证；不得为解决兼容性静默启用 privileged|

## 测试库（compose.test.yaml，仅跑测试时临时存在）

由 `scripts/test_backend.sh` 创建，测试结束（含失败、中断）即 `down -v` 删除；发现上次残留时报错退出，不复用、不覆盖。

|项目|配置|对现有容器的影响与边界|
|---|---|---|
|项目名/容器|`codex-hub-v1-test` / `codex-hub-v1-test-db-1`|独立 Compose 项目，与 `codex-hub-v1` 及其他容器无重名，不会被 `codex-hub-v1` 的 compose 命令管理|
|端口|`127.0.0.1::5432`，宿主端口由 Docker 从空闲临时端口中分配|不占用任何固定端口，不会与 1panel 55432、原生 55439、Hub 18210 等冲突；只绑定回环|
|网络|`codex-hub-v1-test_default`（新建）|不加入已有网络，结束删除|
|卷|无；数据在 tmpfs（512 MB）|不创建命名卷或匿名卷，不读写已有卷|
|镜像|`postgres:16.14-bookworm`（linux/amd64），`pull_policy: never`|与 Hub 的 db 共用本机已有镜像，只读使用、不拉取不改标签|
|资源|1 CPU / 512 MB|跑测试期间占用宿主资源|
|凭据|每次随机生成的测试库密码，只存在于脚本进程环境|与业务库凭据无关|

## 已知的边界（如实记录）

- **宿主机本地服务对出网容器可见。** api、attachments、runner 在带出网的 `hub_egress` 网络里，Docker Desktop 允许它们通过 `host.docker.internal` 连到宿主机本地端口。本次实测：运行器能与宿主机上已有的 1panel PostgreSQL（55432）、本机直接运行的 PostgreSQL（55439）和 API（18200）完成握手。这是 Docker Desktop 的通用行为（已有容器同样如此），不会改变那些服务，数据库仍需要账号密码；但它意味着出网容器不是对本机服务的隔离边界。需要更严格时，应在宿主机或 Docker 虚拟机层做出站防火墙，Compose 本身无法做到。
- **运行器和数据库之间的隔离是真实的。** 运行器不在数据库所在的 `hub_data` 网络里，连数据库真实地址会超时。注意：本机代理开启 Fake-IP 时，运行器里 `db` 会被解析成 `198.18.x.x` 并显示"可达"，这只是代理接受了虚拟地址，没有任何 PostgreSQL 回应，验证隔离时要用数据库的真实地址测。
- **附件解析默认失败关闭。** 见 README；容器里没有系统层面的"禁止联网"，不要在未评估前把 `ATTACHMENT_ALLOW_PROCESS_ONLY` 设为 1。
- **运行器 `unhealthy` 有两种预期原因，`/health` 会如实报出**：没有模型凭据（`MODEL_API_KEY_NOT_CONFIGURED`），或 Codex 沙箱起不来（`SANDBOX_UNAVAILABLE`）。
- **Apple Silicon 上默认的 amd64 运行器永远是 `SANDBOX_UNAVAILABLE`。** 原因（经对照实验确认）：amd64 容器由 Rosetta 翻译，Codex 沙箱按 x86_64 编译的 seccomp 过滤器被 arm64 内核拒绝；同样的代码、同样的配置，原生 arm64 容器里整条链路通过（沙箱预检通过，经 `POST /execute` 调用 `gpt-6.1-sol` 返回正常答复）。容器参数无法修复，只能换成原生 arm64 运行器，见下节。
- **第三方模型端点的出站边界。** 配置 `CODEX_BASE_URL` 后，运行器会把 API Key 发给该地址，所以只接受公网 https、禁止账号/查询串/`..`、非 443 端口、本地与私网地址，且不能与 ChatGPT 登录模式并用。端点属于你信任的第三方：它能看到所有发给模型的提示词和你的 Key，换了端点就等于换了数据接收方。

## 可选：原生 arm64 运行器（`compose.runner-arm64.yaml`）

默认栈全部 amd64。要让运行器在 Apple Silicon 上真正可用，需要你明确选择这个覆盖（你的规则默认 amd64，所以它不是默认）。启动前的影响清单：

|项目|变化|对现有容器的影响|
|---|---|---|
|范围|只替换 `codex-hub-v1-runner-1` 一个容器；`db`、`api`、`attachments` 不动|无|
|架构|运行器 `linux/arm64`，其余仍 `linux/amd64`|无|
|镜像|新增标签 `codex-hub-v1-runner:arm64-local`（约 1 GB，构建因原生执行比 amd64 快）|不覆盖 amd64 标签，也不覆盖任何已有镜像|
|端口、网络、卷|不变（仍只在 `hub_runner` 与 `hub_egress`，不发布端口、不挂卷）|无|
|seccomp|换成 `deploy/seccomp/runner.json`：Docker 官方默认配置 + 一条规则，放行 `clone`、`unshare`、`setns`、`mount`、`umount`、`umount2`、`pivot_root`、`sethostname`|其余容器仍用 Docker 默认配置。仅该运行器可创建用户命名空间，内核攻击面更大|
|不变的加固|`cap_drop: ALL`、只读根文件系统、`no-new-privileges`、非 root、内存/CPU/PID 上限、与数据库网络隔离|无|
|回滚|`docker compose --env-file .env.docker -f compose.yaml up -d --no-deps runner`（不带覆盖文件）会换回 amd64 运行器（命令行的 `-f` 优先于 `COMPOSE_FILE`）。如果 `.env.docker` 里固定了 `COMPOSE_FILE`，回滚后还要删掉那一行，否则下次不带 `-f` 的命令会再换成 arm64|无|

```bash
docker compose --env-file .env.docker -f compose.yaml -f compose.runner-arm64.yaml up -d --build --no-deps runner
```

用 `-p` 指定另一个项目名可以先在隔离环境里试：它只会新建自己的网络和一个运行器容器，不碰本栈。

## 使用 GitHub 预构建镜像（`deploy/compose.ghcr.yaml`）

只想用、不想本地编译时用这份。服务、网络隔离、资源上限、安全选项与 `compose.yaml` 完全一致，只把"本地构建"换成"拉取镜像"。

|项目|内容|对已有环境的影响|
|---|---|---|
|项目名|`codex-im-packages`|与源码构建版 `codex-hub-v1` 是两个互不相干的实例|
|镜像|`ghcr.io/dongliyaaa/codex-im-api:v-0.0.2`、`ghcr.io/dongliyaaa/codex-im-runner:v-0.0.2`、`postgres:16.14-bookworm`|只新增标签；镜像为私有，拉取前需 `docker login ghcr.io`（令牌只需 `read:packages`）|
|网络|`codex-im-packages_hub_data`（internal）、`codex-im-packages_hub_runner`（internal）、`codex-im-packages_hub_egress`|新网络，不加入任何已有网络|
|卷|`codex-im-packages_hub_pgdata`、`codex-im-packages_hub_attachments`|新卷，不读取已有卷；不会接管源码构建版的数据|
|端口|默认 `127.0.0.1:18200`，可用 `HUB_BIND`、`HUB_PORT` 修改|与源码构建版同时运行时，先把其中一个改成不同端口|

固定版本：在 `.env` 里设置 `CODEX_IM_API_IMAGE`、`CODEX_IM_RUNNER_IMAGE` 为 `ghcr.io/...@sha256:...`，避免 `latest` 在不知情时变化。

```bash
python3 scripts/init_env.py
docker compose --env-file .env -f deploy/compose.ghcr.yaml config      # 先看最终配置
docker compose --env-file .env -f deploy/compose.ghcr.yaml pull
docker compose --env-file .env -f deploy/compose.ghcr.yaml up -d --no-build
# 停止并保留数据：down；连数据一起删除：down -v（不可恢复）
```

## 启动与停止

人工审核此表后：

```bash
python3 scripts/init_env.py .env.docker 18210        # 已存在则拒绝覆盖
docker compose --env-file .env.docker config --quiet
docker compose --env-file .env.docker up -d --build db api attachments runner
```

停止只用本目录 `docker compose --env-file .env.docker down`，不加 `-v`；数据库备份和卷删除需要独立决定。不要运行全局 docker prune。重置本栈内的验证数据时，只操作本栈的 `codex-hub-v1-db-1` 容器，不要碰其他数据库容器。

## 验证记录

- 镜像与运行时架构均为 linux/amd64；两个官方 CLI 在 uid 10001、只读根文件系统下可运行。
- 经已发布端口端到端验证：首次注册管理员、重复注册被拒（409）、错误密码被拒（401）、登录与会话、跨来源写请求被拒（403）、附件上传后由 worker 按失败关闭处理、消息经 api 到达 runner（无模型凭据时按预期失败）。
- 网络隔离、加固参数以运行中的容器实测（见上节边界）。部署前对已有容器、网络、卷、镜像做了快照；本栈的命令只创建自己的容器、网络和卷，不含任何 prune 或删除其他资源的命令。快照对比只在部署当时有效：同一台机器上其他人或其他工具之后做的清理，会让前后差异出现，不能直接归因于本栈，需要结合 Docker 日志里的请求来源判断。
- 运行器对接（2026-10-05）：在隔离的临时 Compose 项目里用 arm64 覆盖启动运行器，`/health` 为 ready 且容器 healthy，经 HTTP `POST /execute` 用 `gpt-6.1-sol` 得到正常答复；临时项目已整体拆除。运行器 `/health` 现在包含沙箱预检，默认 amd64 运行器如实显示 `SANDBOX_UNAVAILABLE`。
- 启用 arm64 覆盖后（2026-10-05，真实栈）：运行器容器 healthy，镜像为 arm64，加固参数（`cap_drop: ALL`、只读根文件系统、`no-new-privileges`、非特权）与内联的 seccomp 配置都在；主页检查接口显示沙箱 `ok`、模型端点连通；经 api → runner 发一次最小任务，HTTP 200，约 6 秒得到答复。重建只涉及运行器一个容器，`api`、`attachments`、`db` 与其他容器没有重启。
- 验证时发现并修复了登录后立即请求会被拒绝的竞态：FastAPI 新版在响应发出之后才执行 `get_db` 的提交。所有数据库依赖现统一为 `Depends(get_db, scope='function')`，并有回归测试防止退回。
