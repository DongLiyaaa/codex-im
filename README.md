# codex-im — Agent Hub

基于 Codex CLI、PostgreSQL、FastAPI 与 React 的 AI 协作工作台，支持角色权限、资源授权、聊天执行队列、审计与飞书/钉钉集成。

## 个人平台连接与按需授权

每位已登录员工可打开 `#/connections` 的「个人平台连接」。模型通过受控内部 MCP 调用 `get_platform_authorization_status(provider)` 和 `request_platform_authorization(provider)`，provider 仅允许 `feishu` / `dingtalk`。developer instruction 引导模型在用户需要受限文档时先查状态、按需请求；没有关键词自动触发器。当前个人授权工具**尚未接入在线文档/链接读取**；本次消息直接发送的文件由下述独立附件服务处理，无需个人 OAuth。授权成功后必须重新发送任务，不自动恢复此前操作。

|平台|已核实官方版本和协议|要求与限制|
|---|---|---|
|飞书|lark-cli 1.0.96 支持 `auth login --no-wait --json`、`--device-code`；Hub 使用其固定官方设备 API|独立用户 OAuth 应用凭据、开通 `docx:document:readonly`；不使用默认 all / recommend 权限，也不继承机器人 token|
|钉钉|dws 1.0.62 设备流输出是终端展示；Hub 使用其官方 device/code、flowId 轮询、用户授权码交换及 CLI 组织权限检查|独立 OAuth AppKey/AppSecret，组织对本人开通 CLI 数据访问；初始仅 `openid corpid` 身份权限，不批量申请推荐业务权限|

协议来源：[飞书设备流 v1.0.96](https://github.com/larksuite/cli/blob/v1.0.96/internal/auth/device_flow.go)、[飞书端点](https://github.com/larksuite/cli/blob/v1.0.96/internal/auth/paths.go)、[钉钉设备流 v1.0.62](https://github.com/DingTalk-Real-AI/dingtalk-workspace-cli/blob/v1.0.62/internal/auth/device_flow.go)、[钉钉 OAuth 交换与组织检查](https://github.com/DingTalk-Real-AI/dingtalk-workspace-cli/blob/v1.0.62/internal/auth/oauth_helpers.go)。采用直接协议适配，运行时不启动平台 CLI，不依赖主机 HOME/XDG/Keychain，不安装主机工具、不读取管理员既有登录态。

服务配置（本轮通过环境配置，个人应用凭据不与 IM 配置自动混用）：

- backend：`PLATFORM_BRIDGE_KEY` 至少 32 字符随机秘密；`PLATFORM_FEISHU_CLIENT_ID`、`PLATFORM_FEISHU_CLIENT_SECRET`；`PLATFORM_DINGTALK_CLIENT_ID`、`PLATFORM_DINGTALK_CLIENT_SECRET`。
- runner：`PLATFORM_BRIDGE_URL` 为部署者配置的单一内部服务地址，例如 `http://127.0.0.1:18200/internal/platform-mcp`。模型/请求不能指定此 URL；普通资源 MCP 仍要求公共 HTTPS 443。跨主机部署应使用受保护网络和 TLS；不向公网暴露 runner。
- backend 可显式配置 Fernet 格式 `PLATFORM_AUTH_KEY`；未设置时从 `SESSION_SECRET` 按独立域派生。备份必须同时安全保存 PostgreSQL 密文和密钥，不能把数据库备份、密钥、设备码写入公开日志或版本库；轮换密钥前需迁移密文，否则须重新授权。
- `APP_ORIGIN` 应为员工可达的 HTTPS Hub 地址。本地 localhost/127.0.0.1 不视为远端可用公网入口，IM 返回明确配置提示。

缺配置或上游拒绝设备授权返回 `setup_required`，不会显示假连接。状态包括未连接、待授权、已连接、配置缺失、过期。本人可发起、刷新、取消或断开。设备材料只在本人登录 API 返回，响应 `no-store`；管理员监管历史及模型工具结果只能看到状态和不含材料的 Hub 入口，群聊不会收到直接 OAuth URL/code。页面不会代替用户点击同意。

PG 以 `(user_id, provider)` 隔离，事务 advisory lock 串行重复发起；待授权请求幂等、按 interval 限制轮询、最多 900 秒过期。无内存进程依赖，重启后可继续刷新或明确过期。内部 capability 使用独立 HMAC 密钥，绑定 run/user/conversation、240 秒有效期和固定 audience；服务端只接受 running run，并重新核验用户活跃状态、会话、群成员及 IM 映射。工具不接收 userId，也不提供任意 CLI 命令。

访问令牌仅加密保存，最多按官方返回的 7200 秒有效期保留。**本版不保存或自动使用 refresh token**，到期/401 后重新授权，不自动扩 scope。刷新已连接状态会验证平台身份/组织权限；断开和取消删除 Hub 当前密文，属于本地 logout，不声称撤销平台授权，历史加密备份也需按保留策略清理。彻底撤权需本人进入平台授权管理。设备授权没有浏览器回调路由，device code 与本人 PG 行绑定，不接收外来 callback/state。

## 独立附件服务（阶段1）

网页会话支持多文件上传和仅附件消息；上传完成后显示等待解析、解析中、已就绪或错误，发送前可移除。默认每文件20MiB、每消息5个，服务限制见 `.env.example` 的 `ATTACHMENT_*`；前端提示采用默认上限。附件原件位于项目私有 `.runtime/attachments`（可用 `ATTACHMENT_ROOT` 指定独立目录），不提供静态URL。目录0700，文件受私有目录保护；服务生成UUID路径并拒绝符号链接，用户文件名仅作展示。

|格式|支持能力|限制|
|---|---|---|
|JPEG/PNG/WebP|Pillow验证、限制像素、去EXIF、规范化PNG，经可信runner `--image` 输入|默认2500万像素，缩放至4096边长；不读取其他本地路径|
|PDF|pypdf文字层提取，保留页码|最多200页；含无文字层页面明确失败，尚无OCR；请将所需扫描页转为图片另发|
|DOCX|正文段落、表格文字|逻辑块编号不是Word物理页码；页眉页脚、文本框与嵌入图像暂不提取|
|XLSX|工作表、范围分块读取，公式表达式与缓存值区分|不执行公式/宏；缓存可能缺失或过期；默认10万单元格|
|CSV/TXT|UTF-8/BOM UTF-16/GB18030严格解码，CSV表格识别|拒绝二进制控制字符与不支持格式；不执行CSV公式|
|DOC/XLS|明确返回暂不支持|后续需要独立隔离转换，当前不启动Office转换器|

PG独立保存 attachments、attachment_jobs、attachment_artifacts，并绑定message/run。草稿只对上传者可见，发送事务中行锁claim，禁止重复使用、跨会话及跨用户使用；群成员可查看已发送附件元数据，监管沿用会话只读策略并审计。模型只能读取本run绑定附件，不自动获得其他MCP能力。每次能力调用、下载、发布解析结果、执行与保存/发送回复重新验证原用户/群/IM应用授权。附件失效或解析失败时任务失败，不静默忽略附件。

附件worker独立进程使用PG `FOR UPDATE SKIP LOCKED`、180秒租约与有限重试。消息使用 `waiting_attachments` 状态，归档、群修改与原工作表情清理兼容。上传API只接收并登记，IM ack只登记加密资源引用，不执行下载/解析。macOS解析使用系统sandbox-exec禁止网络与存储目录外写入，附加CPU/文件/描述符/时间限额；它不是完整文件读取隔离沙箱。Linux当前默认失败关闭，必须部署网络/文件系统隔离后再使用，`ATTACHMENT_ALLOW_PROCESS_ONLY=1`仅显式允许资源受限子进程，不可称为真沙箱。

内部 `/internal/attachment-mcp` 提供 list_attachments、get_attachment_status、read_document、list_sheets、read_sheet_range、search；结果含source、页/表/范围、truncated和continuation，单次响应有界。附件MCP与图片取件分别使用独立audience、240秒run/user/conversation能力。runner只从部署配置的内部地址取件并验证checksum、大小和实际PNG，独立临时目录清理，沿用OAuth互斥锁。

飞书支持已验证的WS/webhook text/image/file/post资源引用；钉钉支持text/picture/richText，以及限定 `content.downloadCode/fileName` 的file适配。钉钉下载按官方SDK `POST /v1.0/robot/messageFiles/download`、downloadCode/robotCode、downloadUrl协议。下载仅允许HTTPS、域名白名单、每跳公网DNS，TLS连接固定到已验证IP；不跨host转发凭据，限制字节和时间。真实平台自然附件事件尚需现场验收，不能据mock断言所有群聊/私聊文件schema均兼容。未知身份只记录discovery元数据，不能下载正文。机器人收到的文件不要求个人OAuth。

本地启动（先启动已迁移API，再启动worker；不涉及Docker）：

```bash
.venv/bin/python scripts/run_attachment_local.py
```

worker以PG advisory lock保证本地唯一实例，周期清理过期未发送草稿及孤立目录。部署容器时API与worker需共享专属附件卷、runner不挂原件卷；新volume/服务名/网络/CPU内存/端口与旧服务影响必须先人工审核，当前未创建或启动容器。配置及备份不得暴露签名URL、downloadCode、token或用户文件。

## 安装与首次初始化

准备 Python 3.12、Node.js 与 PostgreSQL。使用 Docker 前先审核 [容器影响清单](docs/DOCKER_IMPACT.md)，确认端口、资源、网络、卷与现有服务隔离。

现有 `compose.yaml` 仅提供基础工作台部署，尚未接线独立附件 worker、API/worker 共享附件卷及个人平台授权内部桥接配置。下述基础启动命令不代表这些新增功能已可在容器中使用；附件解析在 Linux 默认失败关闭，启用前须完成隔离方案、配置与实际验收。本次源码同步不发布或更新 Release/GHCR 镜像。

```bash
python3 scripts/init_env.py
# 根据部署环境配置 .env 中的模型及可选 IM 凭据
docker compose config --quiet
docker compose up -d --build
```

默认仅监听 http://127.0.0.1:18200。在本机浏览器打开，填写「初始化管理员」的姓名、邮箱、至少 12 位密码与确认密码。第一个成功注册的账号成为启用的超级管理员，随后返回登录页。其余账号由管理员创建。

首次初始化必须在仅本机可访问时完成，再配置 HTTPS 反向代理对外开放，并设置 APP_ORIGIN。空站开放公网后，任何首先注册的人都会取得管理员权限。默认 Docker 回环绑定应保持；远程部署可用 SSH 端口转发完成初始化。

初始化状态仅返回布尔值。PostgreSQL 事务锁串行化首次注册；创建账号与永久初始化标记同事务提交。已有任何用户（含停用用户）的实例自动标记已初始化；删除全部用户也不会重新开放注册。失败事务可重试；并发注册仅一项成功，其余返回 409。

.env.example 不包含默认管理员账号或密码，init_env.py 只生成服务随机密钥且拒绝覆盖已有 .env。可选 BOOTSTRAP_ADMIN_EMAIL / BOOTSTRAP_ADMIN_PASSWORD 必须显式一起设置，走相同初始化锁和标记；修改它们不会改变已有账号密码。SESSION_SECRET 至少 32 字符，保持稳定以保护会话及加密配置。真实 .env、数据库、运行日志和认证文件不得提交。

## 无容器运行

先创建专用 PostgreSQL 数据库并配置 DATABASE_URL；不要复用其他应用数据库。安装并配置 runner 所需 Codex CLI（固定版本见 runner/Dockerfile）。

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r backend/requirements.txt -r runner/requirements.txt pytest
npm --prefix frontend ci
npm --prefix frontend run build
# 将 .env 的必要配置安全加载到环境，并配置 DATABASE_URL、RUNNER_URL、STATIC_DIR
PYTHONPATH=backend .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 18200
# 另一个终端，配置相同 RUNNER_TOKEN 与所需模型认证
.venv/bin/python -m uvicorn runner.main:app --host 127.0.0.1 --port 18202
```

scripts/run_local.py 和 run_runner_local.py 是可选项目开发启动器，使用项目相对运行目录；使用前需准备其配置的专用 PG/socket 和 CLI。它们不会自动安装 PostgreSQL 或创建数据库。空库创建表，已有库运行幂等增量迁移；setup_state 为独立新增表，不依赖 create_all 补充既有列。

## 权限

|角色|聊天访问|管理范围|
|---|---|---|
|super_admin|自己、下级私聊；可监管的群聊|全局下级用户、群、资源与授权|
|org_admin|自己、同组织下级私聊；全部成员在管理范围内的群聊|同组织下级、群、资源与授权|
|team_lead|自己、同团队普通成员私聊；全部成员在管理范围内的群聊|团队普通成员、团队群；不能创建或分配资源|
|member|本人私聊、自己所在群聊|无管理权限|

同级之间不能借管理员身份互看私聊；管理员监管私聊只读，不能代发，产生审计记录。群成员本身可参与群聊。

私聊生效资源为用户授权；群聊生效资源为发送用户授权与该群授权的交集。资源还必须启用并符合组织范围。管理员可给自己授权；每次执行前和保存回复前再次检查权限。

## 页面使用

1. 完成首次管理员注册并登录，在「用户与角色」新增表单按名称选择组织和部门，可直接新增中文命名目录，ID 自动生成；再创建组织管理员、团队主管或普通成员。
2. 资源管理新增 Skill（完整Markdown，含name和description frontmatter）或HTTPS MCP（仅443端口，支持headers，不支持stdio/OAuth登录流程）。
3. 授权管理为用户、群分别绑定资源。群聊必须两边均授权才生效。
4. 聊天页创建私聊或群聊；右侧查看当前生效资源。执行队列异步刷新，同一会话只允许一个待执行任务。
5. 左侧「IM 集成」中，超级管理员在「飞书应用配置」或「钉钉应用配置」填写应用ID、密钥、Robot Code与模式，点击「保存应用配置」。员工先向机器人发送消息，再在同页「待接入发现」刷新、选择内部用户、登记群并明确确认成员；平台ID自动预填。保留手动入口。授权后必须重发，发现不等于授权。配置加密保存在PG，空白密钥保留、勾选明确清除；独立IM监督进程每5秒检测并重连，API/worker每次操作读取新配置。启动方法与密钥轮换边界见 [IM_SETUP.md](docs/IM_SETUP.md)。

资源和用户支持创建与查看，授权支持撤销；组织/部门及群组的编辑、删除语义见下文。尚未提供用户禁用、用户编辑删除或密码修改页面。

### 会话移除与刷新恢复

会话列表的移除按钮由后端 `can_delete` 决定，确认后服务端归档，从所有工作台列表移除；历史消息、运行、IM 去重记录和审计保留，**不会清除飞书或钉钉平台聊天，也不是物理删除**。本人私聊可移除；监管私聊始终只读（含超级管理员）；群会话仅满足既有 `can_manage_group` 策略的管理员可移除，普通群成员或创建者身份本身不赋予删除权。

归档与发送共用会话行锁，并在获锁后重新读取归档状态。存在 queued/running 任务，或关联飞书工作表情尚未 cleared 时返回 409，失败保留会话。归档后 messages/state/capabilities/runs/send 均不可通过旧 ID 访问。IM 后续新消息建立新会话，旧事件去重记录保留，防止重放。

URL hash 记录当前页面与会话，例如 `#/chat/<conversation-id>`、`#/resources`，支持刷新及浏览器前进后退；无需全局 localStorage。登录后重新获取当前账号权限与会话列表，不可见或已归档会话清空选择；成员不可访问审计页，未知路由回概览。主动退出清除当前目标，过期重新登录保留目标并重新核验。新建、选择、移除会话同步 URL；移除最后一条显示空态。




### Codex认证配置

|模式|配置|优势|限制|
|---|---|---|---|
|ChatGPT OAuth|`CODEX_AUTH_MODE=chatgpt`，`CODEX_OAUTH_AUTH_FILE`为显式绝对路径|复用项目独立登录，自动写回CLI刷新后的凭据|同一凭据源只允许一个执行；冲突返回429 `OAUTH_BUSY`|
|API key|`CODEX_AUTH_MODE=api`，`CODEX_API_KEY`或`OPENAI_API_KEY`|保留最多两个并行执行|需要独立API凭据及对应额度|

runner本身及Docker默认`api`，本地launcher默认`chatgpt`；在环境变量或项目`.env`显式设置模式可覆盖。OAuth模式不会向子进程传入API key。固定版本源码已核对`forced_login_method="chatgpt"`与`cli_auth_credentials_store="file"`，真实运行使用`--strict-config`。

每次任务仍有独立HOME、CODEX_HOME、workspace与tmp；只复制指定auth.json，绝不加载宿主HOME/keyring。源文件必须为当前运行用户所有的普通文件、0600、非符号链接且只有一个硬链接，父目录建议0700且必须可写。稳定的同目录`auth.json.lock`以非阻塞`flock`锁住整个OAuth执行。子进程退出（包括失败、超时、取消）后校验凭据，使用0600临时文件、fsync和原子replace写回刷新结果；源文件被外部改动时拒绝覆盖。输出会对刷新前后令牌与MCP头部值脱敏。

该专用凭据目录只能交给此runner管理；运行期间不要用其他CLI登录/刷新同一源，不要删除锁文件。文件锁只协调遵循相同锁约定、共享可靠文件系统的runner；无法协调外部登录程序。SIGKILL或主机断电发生于刷新与写回之间时仍可能丢失新令牌，当前不声明崩溃恢复保证。

Docker可选OAuth配置：经容器人工审核后，在独立Compose override中为runner配置以下内容。仅挂专用目录，不挂整个HOME；必须挂目录而非单个文件，以支持锁和原子替换。容器UID 10001必须拥有该目录与0600的auth.json；不要修改其他应用的凭据权限。

```yaml
services:
  runner:
    environment:
      CODEX_AUTH_MODE: chatgpt
      CODEX_OAUTH_AUTH_FILE: /var/lib/codex-oauth/auth.json
    volumes:
      - /absolute/dedicated/codex-oauth:/var/lib/codex-oauth:rw
```



## 测试

测试需要独立 PostgreSQL 测试库；检查测试 fixture 的 DATABASE_URL 配置后运行。使用随机隔离 schema，不要指向业务库。

```bash
PYTHONPATH=backend .venv/bin/python -m pytest backend/tests tests runner/test_runner.py -q
npm --prefix frontend test -- --run
npm --prefix frontend run build
```

测试覆盖权限、IM 协议与事务、目录/群/会话生命周期、首次管理员初始化及前端交互。测试模拟外部平台请求，不代表真实平台、Linux 容器沙箱或生产部署已验收。

## 部署前必须补齐的边界

- Codex Linux沙箱探测失败时拒绝执行；不自动降级为危险模式。Docker默认策略可能限制沙箱，需实际验证，不要直接启用privileged。
- MCP初始URL/DNS拒绝私网，但仍需要部署层出站网络策略防范DNS重绑定和重定向。当前Compose未实现该策略；只应接入可信MCP。
- Skill指令与模型提示不构成安全边界。当前禁用shell/写文件等能力，主要使用授权HTTP MCP；不支持需要脚本执行的任意Skill包。MCP内部的细粒度写操作审批尚未实现。
- 数据库中的MCP配置由权限保护，但尚未使用KMS字段加密；需补密钥托管、备份恢复、TLS反代、审计保留、监控告警、限额与负载验证。
- 飞书支持 webhook/websocket，钉钉支持 legacy webhook/stream；Stream 应用 API 支持已授权群与单聊，legacy webhook 仍只回复固定群。配置与启动见 [IM_SETUP.md](docs/IM_SETUP.md)。连接配置与实际状态以运行时查询为准，未做真实平台收发验收。
- IM输出截取前1800字符，完整内容保留网页；发送失败记录错误，不自动重试。刷新后按 URL 恢复所选会话，通过 state 接口继续同步运行状态。

这些边界未验证或未实现，当前交付不声明达到生产企业级验收标准。
