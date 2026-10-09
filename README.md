# codex-im — Agent Hub

**让团队在网页、飞书、钉钉里，用同一个 AI 助手（Codex CLI 或 Claude Code CLI，按需选用）干活，并且谁能用什么、做过什么，都由管理员说了算。**

当前版本：**v-0.0.3**（[更新日志](CHANGELOG.md)）

## 它能做什么

- **在飞书、钉钉里直接对话。** 员工私聊机器人或在群里 @ 它，机器人接到任务后会在那条消息上打一个"处理中"的表情，做完再把表情去掉并回复。
- **替你操作文档和表格。** 对话里说一句"创建一份钉钉文档"，助手会以你本人的身份创建、读取、修改飞书/钉钉的文档与表格。
- **两种 Agent 并列可选。** 除了 Codex CLI，也支持 Claude Code CLI；每个会话固定用其中一个，上下文互不混用，用法见下文「Claude Code CLI」。
- **管得住。** 按组织、部门、群来分配 Skill 和 MCP；管理员决定哪些人可以发起本人授权；高风险操作需要本人确认；所有对话与操作都有审计记录。
- **自己的数据留在自己手里。** 数据库、密钥都在你自己的服务器上，Codex 用你配置的 OpenAI 兼容接口，Claude Code CLI 用你配置的 Anthropic 接口（API Key）。

## 最快上手（用预构建镜像，不用编译）

需要一台装了 Docker 的 x86_64（amd64）机器。Codex 执行服务的沙箱对容器权限有要求，见下方「使用预构建镜像的说明」最后一条。

```bash
git clone https://github.com/DongLiyaaa/codex-im.git && cd codex-im
python3 scripts/init_env.py            # 生成随机密钥到 .env，已有文件不会被覆盖
# 打开 .env，填上 OPENAI_API_KEY（以及 CODEX_BASE_URL / CODEX_MODEL，如果你用的是第三方接口）
docker compose --env-file .env -f deploy/compose.ghcr.yaml up -d
```

然后在本机浏览器打开 http://127.0.0.1:18200，**第一个注册的账号就是超级管理员**，请在对外开放之前自己先完成注册。

镜像是私有的，拉取前需要登录一次（令牌只需要 `read:packages` 权限）：
`gh auth token | docker login ghcr.io -u 你的GitHub用户名 --password-stdin`。

想从源码构建、不用 Docker 运行，或要接入飞书/钉钉，看下面各章，按需阅读即可。

## 接下来看哪里

| 我想…… | 看这里 |
|---|---|
| 接入飞书或钉钉机器人 | [docs/IM_SETUP.md](docs/IM_SETUP.md) |
| 先搞清楚 Docker 会动到我机器上的什么 | [docs/DOCKER_IMPACT.md](docs/DOCKER_IMPACT.md) |
| 让员工用本人身份创建/修改文档 | 本页「本人平台按需授权」 |
| 了解每个版本改了什么 | [CHANGELOG.md](CHANGELOG.md) |
| 从源码构建或开发 | 本页「安装与首次初始化」「测试」 |

## 使用预构建镜像的说明

| 组件 | 镜像 | 说明 |
|---|---|---|
| API、网页、飞书/钉钉接入进程 | `ghcr.io/dongliyaaa/codex-im-api:v-0.0.3` | linux/amd64 |
| 执行服务（Codex，可选 Claude） | `ghcr.io/dongliyaaa/codex-im-runner:v-0.0.3` | linux/amd64 |
| 数据库 | `postgres:16.14-bookworm` | 官方镜像，随 compose 一起拉取 |

- 部署文件 `deploy/compose.ghcr.yaml` 与源码构建用的 `compose.yaml` 内容一致，只是把"本地构建"换成"拉取镜像"。项目名是 `codex-im-packages`，**不会复用**源码构建版的数据库，也不是旧实例的原地升级。
- 生产环境建议在 `.env` 里用 `CODEX_IM_API_IMAGE` / `CODEX_IM_RUNNER_IMAGE` 固定到经过核验的 `ghcr.io/...@sha256:...`，而不是跟着 `latest` 走。
- 镜像里不含任何账号、密钥、数据库内容或授权凭据，这些都由你自己的 `.env` 和数据库提供。
- 需要常驻飞书/钉钉连接时，等 API 起来后加 `--profile im` 再启动对应进程。
- **Codex 沙箱**：Runner 启动后要能创建命名空间，Docker 默认的安全策略会拦住它，此时首页「Codex CLI 接入检查」会显示 `SANDBOX_UNAVAILABLE`。排查和放行方法见下文「安装与首次初始化」。Apple Silicon（M 系列 Mac）上 amd64 的 Runner 无法启动沙箱，需要用源码和 `compose.runner-arm64.yaml` 自己构建 arm64 Runner，预构建镜像解决不了这一点。

---

## 技术细节

以下各章面向部署和开发人员，信息较密，需要时再查。

## 对话中创建飞书/钉钉文档与表格（官方 CLI）

在飞书/钉钉或网页对话里直接说「新建一个飞书文档/表格/多维表格」「建一个钉钉文档/表格」，模型会调用内部 MCP 工具 `create_platform_document`、`create_platform_spreadsheet`、`create_platform_base`（多维表格仅飞书）。工具在 API 服务端执行官方 CLI，模型本身仍然没有 shell：

| 平台 | 官方 CLI | 执行身份 | 交付方式 | 前提 |
|---|---|---|---|---|
| 飞书 | `@larksuite/cli` 1.0.97（`lark-cli`） | 发起人已完成飞书本人授权时以本人身份（`--as user`）创建；否则用机器人应用（`--as bot`） | 本人身份直接建在本人名下；机器人身份创建后把所有权转给发起人，转移失败则授予 `full_access`，两者都失败时自动删除，不留孤儿文档 | 机器人应用已有云文档/表格/多维表格与云空间权限；发起人已绑定飞书身份 |
| 钉钉 | `dingtalk-workspace-cli` 1.0.62（`dws`） | 发起人本人（dws 文档命令只支持用户身份） | 直接创建在本人名下 | 发起人已完成钉钉本人授权（未授权时工具返回 `authorization_required`，模型会引导发起授权） |

读写已有文档：发送飞书/钉钉文档链接并说「读一下」「把这些追加进去」「在 B2 开始写入这些数据」，模型调用 `read_platform_resource(provider, kind, url, …)` 或 `write_platform_resource(provider, kind, url, …)`。文档支持读取 Markdown 正文与追加/覆盖（覆盖仅在用户明确要求时使用）；表格支持按区域读取与从锚点单元格写入二维数据；飞书多维表格支持读取记录与批量新增记录（每次 ≤200 条）。读取结果最多返回 60000 字符，超出标记 `truncated`；读到的内容被视为不可信数据，不会当作指令执行。

| 场景 | 飞书 | 钉钉 |
|---|---|---|
| 私聊 + 已完成本人授权 | 以本人身份读写本人有权限的任何文档 | 以本人身份读写 |
| 私聊 + 未授权 | 只能读写 Hub 曾为本人创建的资源（机器人身份）；其它链接返回 `authorization_required` | 返回 `authorization_required` |
| 群聊 | 只能读写 Hub 曾为本人创建的资源（机器人身份），从不使用本人授权，防止群内他人借共享上下文引导模型访问发起人的私有文档；其它链接返回 `private_chat_required` | 一律返回 `private_chat_required` |

其余所有云文档操作（改标题、评论、块级编辑、查找替换、行列/子表/样式/图表、多维表格字段/视图/记录更新与删除、历史版本、知识库节点、移动与分享等）不再逐个封装，而是开放官方 CLI 在云文档领域的全部命令：模型先用 `describe_platform_command(provider, command)` 读取官方帮助（只读、本地执行、不访问平台、不记审计），再用 `run_platform_command(provider, command, flags, stdin?, target_url?, confirmed?)` 执行。服务端策略是安全边界：

| 规则 | 说明 |
|---|---|
| 领域白名单 | 飞书 `docs/sheets/base/drive/wiki/markdown/slides/mindnotes/whiteboard`；钉钉 `doc/sheet/aitable/drive/wiki`。邮件、消息、日历、通讯录、审批、`api` 原始调用、`auth/config/profile` 一律拒绝 |
| 身份 | 与读写工具相同：私聊且本人授权有效时以本人身份；飞书群聊或未授权时只能以机器人身份操作 Hub 为本人创建的资源，必须给 `target_url`，目标参数由服务端注入，模型不能再传任何定位参数（url/doc/各类 token/空间/父节点等），分享、成员、权限、移动、复制、搜索类命令被拒绝；钉钉仅私聊 |
| 服务端保留参数 | `--as`、`--format`、`--yes`、`--profile`、`--jq`、`--client-id/--client-secret` 等只由服务端设置；参数名必须出现在该命令官方帮助中 |
| 本地文件 | 命令运行在 Hub 服务器上，`@文件` 写法、文件/目录/输出路径类参数，以及上传、下载、导入、导出、同步等命令全部拒绝；长文本用 `stdin` 并把对应参数设为 `-` |
| 高风险审批 | 官方帮助标为 `high-risk-write`（飞书）或 `risk=high` / 需确认（钉钉）的命令，以及覆盖整篇文档（`mode=overwrite`），不会按模型的说法执行，见下文「高风险操作审批」 |
| 反馈与审计 | 失败时返回清洗后的参数校验信息 `error_detail`（凭据替换为 `***`）供模型修正；审计 `platform.workspace.command` 只记命令路径、参数名、身份、是否高风险与目标资源 ID，不记参数值和正文；每人每分钟最多 30 次执行、60 次帮助查询 |

链接只接受飞书/钉钉官方域名的 HTTPS 地址，类型与链接不符返回 `kind_mismatch`，资源不存在或无权访问返回 `resource_not_found`。

安全边界：每次调用使用一次性 HOME/配置目录与最小环境变量，正文经 stdin 传入；飞书机器人身份由 Hub 自己换取短期 tenant token 并以 `LARKSUITE_CLI_TENANT_ACCESS_TOKEN` 注入，App Secret 不交给 CLI；本人身份只注入短期 user access token；只返回飞书/钉钉官方域名下的链接、资源 ID 与结构化数据，不返回 CLI 原始错误或令牌；审计 `platform.workspace.create/read/write` 只记录平台、类型、身份、资源 ID 与写入方式，不记录标题、正文和读到的内容；每人每分钟最多 10 次（创建、读、写分别计数）。CLI 安装在项目内 `.runtime/platform-cli`（`npm install @larksuite/cli@1.0.97 dingtalk-workspace-cli@1.0.62`），也可用 `PLATFORM_LARK_CLI` / `PLATFORM_DWS_CLI` 指向其它路径；Docker 镜像构建时会单独下载并内置 linux/amd64 版本的这两个 CLI（开发机上的 macOS 版本不能放进镜像），路径由 `PLATFORM_LARK_CLI` / `PLATFORM_DWS_CLI` 指定；已在 uid 10001、只读根文件系统下验证可运行，但容器内的真实飞书/钉钉调用尚未做过验收。钉钉链路已有自动化测试，但因当前环境未配置钉钉应用，尚未做真实平台验收。

## 高风险操作审批（参考 cc-connect 的权限确认，但由服务端核验）

模型自己声明“用户已同意”不能算数：被提示注入的文档或群消息可以让模型把这个标志置为真。所以审批不经过模型：

1. 模型触发高风险命令或覆盖整篇文档时，服务器**把完整请求（含正文）存入 `platform_approvals`**，生成 6 位审批码（不含 0/O/1/I/L），返回 `approval_required`，并**由服务器直接**向发起人所在聊天发送确认通知（飞书/钉钉经 `im_outbox`，网页写入会话消息）。通知里的操作描述由真实请求生成，模型无法改写。
2. 只有发起人**本人**在同一会话里发送 `/approve 审批码`（别名 `/批准`、`/确认`、`/同意`）才会批准，`/deny 审批码`（`/拒绝`）拒绝；只有一个待确认项时可省略审批码。群里其他人、其他会话、其他用户都无效。批准后服务器让模型继续一轮，模型只能调用 `run_approved_platform_action(approval_id)`，执行的是服务器保存的那一份请求，无法修改，也不依赖模型把参数复述一致。
3. 审批码 10 分钟有效、单次使用、每人最多 5 个待处理；批准时若有任务在执行则提示稍后再试（审批码保留），拒绝随时可用。执行时重新计算身份：批准时是“机器人身份”、执行时变成“本人身份”（或反过来）会拒绝并要求重新批准。终态（已拒绝/已过期/已使用）会清空存储的请求内容。
4. 审计 `platform.approval.requested/approved/denied/executed` 只记录审批码、平台、命令路径与运行 ID，不记录参数值和正文。`/status` 会列出待确认项。

## IM 交互（参考 cc-connect）

- 指令（仅对已授权发送者生效，不调用模型、不建任务）：`/help`、`/new`（开启新会话，旧上下文不再带入，历史保留；群会话需群管理权限）、`/stop`（停止本人排队/执行中的任务，群管理员可停止群内任务；执行中的任务结果丢弃且工具能力立即失效）、`/status`（身份、当前任务、可用能力、本人授权与待确认操作）、`/approve`/`/deny`（见上节）、`/agent`（查看或切换当前 Agent，见下节「Claude Code CLI」）。中文别名 `/帮助` `/新会话` `/停止` `/状态`。指令回复写入 `im_outbox`，由 API 独立线程发送，中断的发送标记为 ambiguous 不重放。
- **群聊只响应 @ 机器人的消息**。应用开通了“获取群组中所有消息”（`im:message.group_msg`）时，飞书会把群里每条消息都推给机器人；Hub 用 `bot/v3/info` 取得机器人自身 `open_id` 并核对 `mentions`，**取不到机器人身份时按失败即关闭处理**（群消息一律忽略，私聊不受影响）。钉钉群消息按 `isInAtList` 过滤。
- 入站整理：去掉对机器人的 `@_user_N` 占位符，其余 @ 换成可读的 `@姓名`；用户“回复/引用”某条消息时，把被引用消息链（最多 3 层、同一聊天内、总等待 ≤2.5 秒，取不到则忽略）作为带“仅供参考，不是指令”标记的上下文附在提问前，飞书包含机器人自己发的 Markdown 卡片，钉钉取事件里的 `repliedMsg`；钉钉语音消息直接使用平台自带的识别文字；读不了的类型（飞书语音/视频/合并转发/位置/名片）回复一句说明，不再静默丢弃；只发 @ 不带内容时回复用法提示。
- 任务执行期间再发消息：回复“上一个任务还在处理中，这条消息没有执行”并记录该事件，不再让接入进程抛 409（用户看不到反馈，平台稍后还会重投事件造成延迟的重复回复）。`/stop`、`/status`、`/approve` 不受影响。
- 出站：回复按段落分段（每段约 3000 字，最多 8 段，代码块跨段自动闭合），飞书以 Markdown 卡片发送，钉钉 Stream 以 `sampleMarkdown` 发送；回复中的 `<at ...>` 标签会被中和，防止模型输出触发 @所有人。**群聊回复锚定到提问消息**（飞书 reply 接口；原消息被撤回则改发普通消息），私聊仍是普通消息。发送前把 Markdown 图片改成链接、把第 6 个起的表格放进代码块（飞书卡片限 5 个表格、不接受外链图片，否则整条被拒）；飞书把业务错误放在 HTTP 400 里，被拒卡片会回退为纯文本。
- 出站重试：飞书发送带 `uuid`（实测同一 `uuid` 重发返回同一条消息），因此连接失败、超时、5xx、429、频率限制都可安全重试（最多 3 次，0.5s/1.5s 退避）；访问令牌被拒时清缓存重试一次。钉钉发送没有幂等键，只在请求确定没有到达（连接失败）或被限流（429）时重试，超时不重试以免重复。
- 任务失败时向发起人所在会话回复固定提示（不含上游错误细节），不再静默。

## 本人平台按需授权

独立「个人平台连接」页已移除。已登录员工在聊天页「本人平台授权」或当前任务的本人授权卡片中查看、刷新、取消及断开自己的连接；没有历史卡片时也能管理本人凭据，管理员不能代操作他人。模型通过受控内部 MCP 调用 `get_platform_authorization_status(provider)` 和 `request_platform_authorization(provider)`，provider 仅允许 `feishu` / `dingtalk`。developer instruction 引导模型在用户需要受限文档时先查状态、按需请求；没有关键词自动触发器。完成本人授权后，上文的在线文档创建/读取/写入工具会在私聊中使用本人身份；本次消息直接发送的文件由下述独立附件服务处理，无需个人 OAuth。授权成功后必须重新发送任务，不自动恢复此前操作。网页仅作内网交互管理；官方设备链接私发本人、后台轮询仅需出站 HTTPS，不依赖 Hub 公网或浏览器回调。IM 不返回 localhost 管理链接，也不会提示必须配置公共 origin。仅网页来源且存在安全可用 origin 时可附当前聊天入口。当前真实个人 OAuth 应用配置仍需管理员确认，移除页面不表示授权已打通。

|平台|已核实官方版本和协议|要求与限制|
|---|---|---|
|飞书|lark-cli 1.0.96 支持 `auth login --no-wait --json`、`--device-code`；Hub 使用其固定官方设备 API|用户 OAuth 应用凭据（可复用机器人应用）；申请范围由代码固定为应用已开通的整个文档域（89 项，见 `platform_auth.SCOPES`）：云文档读写与评论、权限与分享、导入导出、电子表格、多维表格（含删除、仪表盘、表单、角色、工作流）、白板、云空间（移动、删除、版本、上传下载）、知识库（节点与成员）、文档搜索；不申请邮件、消息、日历、通讯录、审批、任务等非文档权限，不使用默认 all / recommend，也不继承机器人 token。申请的每一项必须已在应用里作为“用户身份”权限开通，否则飞书会拒绝设备授权请求。授权必须至少包含 `docx:document`，且平台返回的范围不得带有文档域之外（邮件、消息、日历、通讯录、审批、任务、消息搜索）的权限，否则拒绝，错误码为 `SCOPE_NOT_WRITABLE` 或 `SCOPE_OUT_OF_DOMAIN:<权限域名>`（只含域名，不含令牌与权限全文）；平台自动附带的基础范围（如 `offline_access`）不影响授权。扩权前授权的连接需本人重新授权才会用于本人身份读写|
|钉钉|dws 1.0.62 设备流输出是终端展示；Hub 使用其官方 device/code、flowId 轮询、用户授权码交换及 CLI 组织权限检查|独立 OAuth AppKey/AppSecret，组织对本人开通 CLI 数据访问；初始仅 `openid corpid` 身份权限，不批量申请推荐业务权限|

协议来源：[飞书设备流 v1.0.96](https://github.com/larksuite/cli/blob/v1.0.96/internal/auth/device_flow.go)、[飞书端点](https://github.com/larksuite/cli/blob/v1.0.96/internal/auth/paths.go)、[钉钉设备流 v1.0.62](https://github.com/DingTalk-Real-AI/dingtalk-workspace-cli/blob/v1.0.62/internal/auth/device_flow.go)、[钉钉 OAuth 交换与组织检查](https://github.com/DingTalk-Real-AI/dingtalk-workspace-cli/blob/v1.0.62/internal/auth/oauth_helpers.go)。采用直接协议适配，运行时不启动平台 CLI，不依赖主机 HOME/XDG/Keychain，不安装主机工具、不读取管理员既有登录态。

服务配置（不自动混用机器人凭据）：超级管理员在「IM 集成」的个人 OAuth 配置中可独立填写，或点击「使用当前机器人应用（管理员确认）」并二次确认。服务端复制加密快照，不向前端返回密钥；检查 OAuth revision、机器人 revision 及快照指纹，拒绝掩码和过时配置。复制不代表设备授权能力已开通。机器人变更不自动同步；旧快照停用，需重新确认。钉钉 legacy webhook 只有群机器人密钥，不能复用为应用，必须配置 Stream AppKey/AppSecret。保留空白保留与明确清除规则；OAuth 与本人绑定身份必须属于同一应用。

### 管理员在飞书/钉钉对话内触发平台应用配置

`super_admin` 与 `org_admin` 私聊机器人时（群聊不生效，会被拒绝并提示改用私聊），可用自然语言（如"帮我配置飞书个人授权"）让模型调用两个仅管理员可见的内部 MCP 工具：`get_platform_application_status(provider)`（只读，查配置状态与是否可复用机器人应用，绝不返回密钥）和 `configure_platform_application(provider)`（写操作，仅在管理员于同一对话明确确认后才可调用）。这两个工具对非管理员完全不可见，服务端也在每次调用时重新校验角色（不依赖工具列表隐藏），入参只有 `provider`，不接受任何模型编造的 revision/快照参数——一切状态都由服务端重新查库。

- **可复用机器人应用**：服务端直接把已加密存储的机器人 App ID/Secret 复制为个人 OAuth 应用配置，全程不产生新密钥、不经过模型或聊天记录，写入后记录审计事件 `platform.configuration.update_via_im`。
- **需要独立应用**：工具只回复一条指向网页「IM 集成 → 个人 OAuth 配置」的安全入口链接（仅 `super_admin` 可见此链接，且仅当 Hub 配置了可达的公网 `APP_ORIGIN` 时才附带）；`org_admin` 触发到此分支会收到"需联系超级管理员完成独立应用配置"的提示，不下发链接——因为该网页页面本身仍是 `super_admin` 专属，此举避免"IM 允许触发但网页打不开"的落差，且不扩大现有网页权限边界。任何情况下都不会在对话里索要、接受或复述 Client Secret。

- backend：`PLATFORM_BRIDGE_KEY` 至少 32 字符随机秘密；`PLATFORM_FEISHU_CLIENT_ID`、`PLATFORM_FEISHU_CLIENT_SECRET`；`PLATFORM_DINGTALK_CLIENT_ID`、`PLATFORM_DINGTALK_CLIENT_SECRET`。
- runner：`PLATFORM_BRIDGE_URL` 为部署者配置的单一内部服务地址，例如 `http://127.0.0.1:18200/internal/platform-mcp`。模型/请求不能指定此 URL；普通资源 MCP 仍要求公共 HTTPS 443。跨主机部署应使用受保护网络和 TLS；不向公网暴露 runner。
- backend 可显式配置 Fernet 格式 `PLATFORM_AUTH_KEY`；未设置时从 `SESSION_SECRET` 按独立域派生。备份必须同时安全保存 PostgreSQL 密文和密钥，不能把数据库备份、密钥、设备码写入公开日志或版本库；轮换密钥前需迁移密文，否则须重新授权。
- `APP_ORIGIN` 只影响管理网页链接与 Web 安全校验，不是设备授权前提；可以保持内网/本机地址，IM 授权不需要 Hub 对公网开放。

缺配置返回 `configuration_missing`；平台不支持、拒绝、无效配置、组织未开通、身份缺失/应用不匹配、私发不支持/失败分别返回独立状态与 `next_action`，不会假报已连接或已私发。设备接口是否允许具体自建应用仍需平台开通与现场验收；不回退官方默认 CLI client/proxy，不伪造 QR 或代理应用回调。私发失败清除材料并终止，未知发送结果不重放。本人可发起、刷新、取消或断开。设备材料只在本人主动打开的卡片及本人登录 API 返回，响应 `no-store`；管理员监管历史及模型工具结果只能看到状态和不含材料的 Hub 入口，群聊不会收到直接 OAuth URL/code。页面不会代替用户点击同意。

PG 以 `(user_id, provider)` 隔离，事务 advisory lock 串行重复发起；待授权请求幂等、按 interval 限制轮询、最多 900 秒过期。无内存进程依赖，重启后可继续刷新或明确过期。飞书设备流轮询遵循 RFC 8628：用户尚未完成授权时，令牌接口返回 **HTTP 400** 加 `authorization_pending`（轮询过快返回 `slow_down`），这是正常答复而不是失败；`request()` 把这两种答复交给轮询逻辑，`slow_down` 使间隔增加 5 秒。令牌发出前的瞬时网络错误或 5xx 最多容忍连续 12 次（约 1 分钟，收到正常答复即清零），超过后以 `PLATFORM_POLL_FAILED` 结束；令牌一旦发出，设备码即已消耗，之后的身份核验失败直接终止，不再轮询。钉钉的一次性授权码不会被重复领取，其轮询失败始终终止。内部 capability 使用独立 HMAC 密钥，绑定 run/user/conversation、240 秒有效期和固定 audience；服务端只接受 running run，并重新核验用户活跃状态、会话、群成员及 IM 映射。工具不接收 userId，也不提供任意 CLI 命令。

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

worker以PG advisory lock保证本地唯一实例，周期清理过期未发送草稿及孤立目录。容器部署时API与worker共享专属附件卷（`hub_attachments`）、runner不挂原件卷；新volume/服务名/网络/CPU内存/端口与旧服务影响必须先人工审核（见 [容器影响清单](docs/DOCKER_IMPACT.md)）。配置及备份不得暴露签名URL、downloadCode、token或用户文件。

## 安装与首次初始化

准备 Python 3.12、Node.js 与 PostgreSQL。使用 Docker 前先审核 [容器影响清单](docs/DOCKER_IMPACT.md)，确认端口、资源、网络、卷与现有服务隔离。

```bash
# 端口按需选择（本机已有服务占用 18200 时用别的端口）；生成独立随机密钥，文件权限 0600，已存在则拒绝覆盖
python3 scripts/init_env.py .env.docker 18210
# 根据部署环境填写 .env.docker 中的模型凭据；IM 凭据可以留空，在网页里配置并加密保存到 PG
docker compose --env-file .env.docker config --quiet
docker compose --env-file .env.docker up -d --build db api attachments runner
```

Compose 栈由自带的 `db`（PostgreSQL 16）、`api`、`attachments`（附件解析 worker）、`runner` 四个服务组成，全部 `linux/amd64`、非 root、只读根文件系统、`cap_drop: ALL`。启动顺序按健康检查串联：`db` 健康后启动 `api`，`api` 健康（业务表已迁移）后才启动 `attachments`。镜像内置官方 `lark-cli` 1.0.97 与 `dws` 1.0.62（构建时下载 linux/amd64 版本，开发机上的 macOS 版本不能放进镜像），路径由 `PLATFORM_LARK_CLI` / `PLATFORM_DWS_CLI` 指定。网页只发布到 `${HUB_BIND:-127.0.0.1}:${HUB_PORT:-18200}`，`APP_ORIGIN` 必须与浏览器实际访问的地址一致。

几个容易踩的点：

- **runner 就绪需要同时满足两件事，`/health` 如实反映**：模型凭据（`OPENAI_API_KEY`，或下文的 ChatGPT OAuth 目录），以及 Codex 沙箱能真正启动。没有凭据是 `MODEL_API_KEY_NOT_CONFIGURED`，沙箱起不来是 `SANDBOX_UNAVAILABLE`（沙箱预检结果缓存 60 秒，不会联系模型）。
- **主页的「Codex CLI 接入检查」**：超级管理员打开「工作台概览」会看到六项检查（执行器连接、Codex CLI 与版本、模型与端点、模型凭据、模型端点连通、沙箱）和一个总判断，其他角色看不到也不会发起请求。它通过 `GET /api/system/codex-status` 读取 runner 需要令牌的 `GET /status`；不改变 `/health`，所以模型服务商的临时故障不会让容器被判为不健康。模型端点连通检查只请求 `<端点>/models`（不产生模型调用、不消耗额度），不跟随重定向，结果缓存约 30 秒，「重新检查」最短每 5 秒刷新一次。页面和接口只会得到模型 id、端点主机名、版本号和状态码，不会返回 Key、完整地址或服务商的响应内容。端点不提供 `/models` 时显示「无法验证」，不算失败。升级时要同时更新 API 和 runner：旧 runner 没有 `/status`，页面会提示「执行器版本较旧」。
- **第三方模型端点**：`CODEX_MODEL` 设模型 id，`CODEX_BASE_URL` 设 OpenAI 兼容的公网 https 地址（如 `https://models.example.com/v1`），并配 `OPENAI_API_KEY`、`CODEX_AUTH_MODE=api`。运行器会拒绝带账号/查询串/`..` 路径段的地址、非 443 端口、localhost、私网或保留地址；ChatGPT 登录模式下配置端点会直接报 `CODEX_BASE_URL_REQUIRES_API_MODE`，避免把登录令牌发给第三方。Key 只通过进程环境变量交给 Codex，不会写进 `config.toml`。模型 id 要与端点 `/models` 返回的完全一致（例如端点列的是 `gpt-6.1-sol`，写 `6.1-sol` 会得到 `model_not_found`）。
- **Apple Silicon 上 amd64 的运行器无法启动 Codex 沙箱。** Docker Desktop 用 Rosetta 翻译 amd64 容器，Codex 为沙箱安装的 seccomp 过滤器是按 x86_64 编译的，arm64 内核拒绝它（`Invalid argument`），容器里没有任何办法绕过。同时 Docker 默认 seccomp 配置也拦截了沙箱需要的命名空间调用。因此默认栈里运行器会一直显示 `SANDBOX_UNAVAILABLE`。要让它在 Apple Silicon 上真正工作，显式启用原生 arm64 运行器（只有运行器改架构，其余服务仍是 amd64）：`docker compose --env-file .env.docker -f compose.yaml -f compose.runner-arm64.yaml up -d --build runner`。它会换上 `deploy/seccomp/runner.json`——Docker 官方默认配置加**一条**规则（放行 `clone`、`unshare`、`setns`、`mount`、`umount`、`umount2`、`pivot_root`、`sethostname`，由 `scripts/make_runner_seccomp.py` 生成，上游文件按 SHA-256 固定，改动上游必须先审核）。代价是运行器容器可以创建用户命名空间，内核攻击面比默认配置大；`cap_drop: ALL`、只读文件系统、`no-new-privileges`、网络隔离都不变。在 x86_64 的 Linux 主机上则不需要这个覆盖，只要放行同样的命名空间调用即可。
- **让 arm64 选择对这个部署长期生效**：在本部署自己的 `.env.docker`（不进仓库）里加一行 `COMPOSE_FILE=compose.yaml:compose.runner-arm64.yaml`。否则以后有人执行不带 `-f` 的 `docker compose --env-file .env.docker up -d runner`，runner 会悄悄变回 amd64、沙箱检查重新失败。加了之后所有 `--env-file .env.docker` 的命令都自动带上覆盖；要回到 amd64，删掉这一行再重建 runner。`compose.yaml` 本身仍然默认 amd64。
- **轮换模型 API Key**：先在模型服务商控制台生成新 Key，然后运行 `python3 scripts/rotate_model_key.py`（默认处理 `.env.docker`），在隐藏提示里粘贴新 Key——不要把 Key 写进命令行或贴到聊天里。如果要让别人（例如协助的 Agent）代为执行，不要把 Key 发给对方，而是先把它从剪贴板写进一个只有自己能读的文件：`umask 077; pbpaste > .runtime/new-model-key`，再运行 `python3 scripts/rotate_model_key.py --key-file .runtime/new-model-key`。密钥文件必须是属于当前用户、不能被组或其他人访问的普通文件（符号链接、目录、管道、超过 512 字节的文件都会被拒绝），新 Key 验证通过并写入配置后脚本会删除这个文件；验证没通过时文件保留，方便重试。脚本先用新 Key 请求端点的 `/models`（与 runner 一样直连，除非配置了 `CODEX_PROXY_URL`；不跟随重定向），模型在列表里才原子改写配置文件（权限 0600，其余行原样保留）；然后再用旧 Key 请求一次，确认服务商已拒绝它，最后只输出 HTTP 状态码。写完后执行 `docker compose --env-file .env.docker up -d --no-deps runner` 让 runner 用新 Key 重建。旧 Key 仍然有效时脚本以非零状态退出并提示去控制台吊销。
- **附件解析在容器里默认失败关闭**：只有 macOS 能在系统层面禁止解析进程联网，容器不能，所以 `ATTACHMENT_ALLOW_PROCESS_ONLY` 默认为 0，上传的附件会得到「此平台尚未配置解析进程网络隔离」。设为 1 表示接受“仅进程级资源限制、没有网络隔离”，请先评估风险。
- **飞书只允许同一应用有一个长连接**：`im` profile 的 `im-feishu` / `im-dingtalk` 容器不在默认启动列表里。如果本机已有直接运行的 IM 进程，不要同时启动它们，否则两边会各收到一部分消息。
- **迁移数据必须带上同一个 `SESSION_SECRET`**（以及 `PLATFORM_AUTH_KEY`、`IM_CONFIG_KEY`，若设置过），否则库里加密保存的 IM 配置、个人授权和 MCP 请求头密钥无法解密。
- `CODEX_PROXY_URL` 在容器里要写 `http://host.docker.internal:端口`；运行器会自动让 `PLATFORM_BRIDGE_URL`、`ATTACHMENT_BRIDGE_URL` 里的服务名（如 `api`）绕过代理。
- 带出网网络的容器（api、attachments、runner）在 Docker Desktop 上能通过 `host.docker.internal` 访问宿主机本地服务，这是 Docker Desktop 的通用行为，不是本栈的配置；详见影响清单。

默认仅监听本机（上面示例是 http://127.0.0.1:18210；直接运行为 http://127.0.0.1:18200）。在本机浏览器打开，填写「初始化管理员」的姓名、邮箱、至少 12 位密码与确认密码。第一个成功注册的账号成为启用的超级管理员，随后返回登录页。其余账号由管理员创建。

首次初始化必须在仅本机可访问时完成。网页可继续只在内网管理；只有明确需要对外开放时才配置 HTTPS 反向代理与 APP_ORIGIN。空站开放公网后，任何首先注册的人都会取得管理员权限。默认 Docker 回环绑定应保持；远程部署可用 SSH 端口转发完成初始化。

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

1. 完成首次管理员注册并登录。**员工不需要 Agent Hub 账号，整个平台只需要超级管理员账号**：员工通过飞书/钉钉使用，由管理员在「IM 集成 → 待接入发现」一键接入（见第 5 步）。「用户与角色」只用于创建需要登录网页的管理账号（组织管理员等），新增表单按名称选择组织和部门，可直接新增中文命名目录，ID 自动生成。
2. 「Skill和MCP管理」新增 Skill 或 MCP，可限定给某个组织或某个部门使用（不选组织 = 全局；选了部门，只有该部门的成员和群能用，未设部门的组织级群不能用）。**Skill** 只需填写名称、用途说明和纯文字内容，也可直接导入 .txt / .md 文件（≤256KB）；技术头部由系统自动生成，不要求任何格式或符号。**MCP** 填写 HTTPS 服务地址（仅443端口，不支持stdio/OAuth登录流程），再选「不需要请求头」或「需要请求头」；需要时可添加多个请求头（最多10个），每个都有名称和密钥输入框，密钥保存时加密，列表和接口永远不返回明文。
3. 授权管理为用户、群分别绑定资源。群聊必须两边均授权才生效。
4. 聊天页创建私聊或群聊；右侧查看当前生效资源。执行队列异步刷新，同一会话只允许一个待执行任务。
5. 左侧「IM 集成」中，超级管理员在「飞书应用配置」或「钉钉应用配置」填写应用ID、密钥、Robot Code与模式，点击「保存应用配置」。员工先私聊机器人，再在同页「待接入发现」刷新，点「处理接入」：默认「新建 IM 成员（无需网页账号）」，填写姓名（有昵称自动带入）、角色、组织和部门即可，成员创建与 IM 身份绑定在同一事务内完成；也可改选「绑定已有用户」。**「IM 集成」只处理私聊；群聊的一切（群登记、群聊发送者接入、群成员确认、群名和群成员昵称的刷新）都在「协作群组」页**：先把机器人加入群、@ 机器人发一条消息，再到「协作群组」的「群聊发送者待接入」和「已发现群 / 待绑定」处理。平台ID自动预填。保留手动入口。授权后必须重发，发现不等于授权。配置加密保存在PG，空白密钥保留、勾选明确清除；独立IM监督进程每5秒检测并重连，API/worker每次操作读取新配置。启动方法与密钥轮换边界见 [IM_SETUP.md](docs/IM_SETUP.md)。

**仅 IM 成员的边界**：只有超级管理员能创建，且只能是「成员」或「团队负责人」，管理员角色必须在「用户与角色」单独创建带凭据的账号；组织和部门必须是目录中已存在且未归档的条目，不接受手填 ID；请求体不接受邮箱、密码或启用状态，服务端写入不可验证的锁定密码（`!`，不是 `盐:摘要` 格式，任何输入都无法通过校验）和保留域 `im.invalid` 下的占位邮箱，所以这类成员不可能登录网页，列表里显示为「仅 IM 接入」而不显示占位邮箱，接口以 `login_enabled=false` 标记。该发送者已有绑定时只能走「绑定已有用户」，不会重复创建；并发双击由配置锁串行，只会创建一个成员。创建、身份绑定与审计在同一事务内，任何一步失败整体回滚。审计 `user.create`（`via=im_discovery`）和 `im.discovery.approve`（`created_user=true`）不记录姓名。新成员默认没有任何 Skill / MCP，仍需在「绑定与授权」单独授权。

**批量接入、自动接入与停用**（详见 [IM_SETUP.md](docs/IM_SETUP.md)）：
- **批量接入**：`POST /api/im/discoveries/onboard-batch`，勾选多人统一角色、组织和部门，单次 ≤ 50 人且只能同一平台；每人在独立保存点内处理，逐人返回结果，一人失败不影响其他人；同一批次按平台配置锁、再按目录锁的固定顺序加锁，两个方向相反的批次并发也只会各接入每人一次。
- **自动接入策略**（`/api/im/onboarding-policy`，仅超级管理员，**默认关闭**）：同组织员工首次私聊即自动成为成员。仅私聊；飞书校验 `tenant_key`、钉钉校验 `senderCorpId` 与 `chatbotCorpId`，缺失即按外部人员处理；角色固定为成员、归属固定为策略指定的组织与部门；每平台每日上限；任何失败退回人工接入；没有默认 Skill / MCP。数据存 PostgreSQL 表 `im_onboarding_policies`（启动时只新建、不改旧表）。开启前应先在飞书/钉钉后台限定应用可用范围。钉钉的组织校验字段未做真实平台验收。
- **停用 / 启用 / 改名**：`PATCH /api/users/{id}`（只接受 `name`、`active`），权限沿用「只能管理比自己低一级且在自己范围内的用户」，不能操作自己，因此超级管理员不能在网页停用。停用在同一事务内取消排队/执行中任务、删除网页会话、清除飞书/钉钉本人授权令牌与进行中的授权流程、关闭待确认审批；历史、群成员关系与 IM 身份保留；重新启用不恢复本人平台授权。

资源和用户支持创建与查看，授权支持撤销；组织/部门及群组的编辑、删除语义见下文。仍没有用户删除或密码修改页面；已停用的成员可以在「用户与角色」里重新启用。

### 会话移除与刷新恢复

会话列表的移除按钮由后端 `can_delete` 决定，确认后服务端归档，从所有工作台列表移除；历史消息、运行、IM 去重记录和审计保留，**不会清除飞书或钉钉平台聊天，也不是物理删除**。本人私聊可移除；监管私聊始终只读（含超级管理员）；群会话仅满足既有 `can_manage_group` 策略的管理员可移除，普通群成员或创建者身份本身不赋予删除权。

归档与发送共用会话行锁，并在获锁后重新读取归档状态。存在 queued/running 任务，或关联飞书工作表情尚未 cleared 时返回 409，失败保留会话。归档后 messages/state/capabilities/runs/send 均不可通过旧 ID 访问。IM 后续新消息建立新会话，旧事件去重记录保留，防止重放。

URL hash 记录当前页面与会话，例如 `#/chat/<conversation-id>`、`#/resources`，支持刷新及浏览器前进后退；无需全局 localStorage。登录后重新获取当前账号权限与会话列表，不可见或已归档会话清空选择；成员不可访问审计页，未知路由回概览，旧 `#/connections` 书签安全重定向至 `#/chat`，不会自动加载或开放授权材料。主动退出清除当前目标，过期重新登录保留目标并重新核验。新建、选择、移除会话同步 URL；移除最后一条显示空态。




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



## Claude Code CLI（可选的第二个 Agent）

Claude Code CLI（命令行 `claude`，不是桌面版）是和 Codex CLI **并列的一个选项**，不是替代，也不是回退。默认只启用 Codex，行为和以前完全一致；管理员在 `.env` 里加上 Claude 后，用户可以各自选择用哪个。

- **启用**：`HUB_AGENTS=codex,claude`，并填写 `CLAUDE_API_KEY`（Claude 只支持 API Key 方式）。可选 `CLAUDE_MODEL`、`CLAUDE_BASE_URL`（第三方网关，公网 https，不带结尾的 `/v1`）、`CLAUDE_PROXY_URL`。`DEFAULT_AGENT` 是没选过时的默认值。Codex 的配置互不影响，两个 Agent 的模型、密钥、网关各自独立。
- **怎么选**：网页新建会话时选 Agent；在飞书、钉钉里发 `/agent` 查看当前使用的 Agent，发 `/agent claude` 或 `/agent codex` 切换。
- **会话和上下文不混用**：每个会话创建时就绑定一个 Agent，之后不变。`/agent` 切换会归档当前会话并开启新会话，旧上下文不会带过去，历史仍保留在 Hub；`/new` 保持当前会话的 Agent。私聊的选择会记为这个人的偏好；群里切换需要群管理权限，且不修改任何人的个人偏好。
- **能力对等**：个人平台授权、资源范围、Skill、附件（含图片）、高风险审批、审计、「处理中」表情、`/stop`、超时与输出限制，Claude 与 Codex 走同一套 Hub 逻辑。只有各自 CLI 的调用方式放在执行器的适配层里。
- **安全边界**：Claude 每次任务在独立临时目录里运行，关闭内置的读写、命令、联网工具，只能使用 Hub 按当前用户授权下发的 MCP 工具；执行器启动后会核对实际加载的 MCP 与工具，不一致就整个任务失败、丢弃输出。
- **没有启用时**：某个会话绑定的 Agent 后来被管理员关掉，用户会收到提示，任务不会执行，也不会悄悄换成另一个 Agent。
- **不在范围内**：不接管或恢复你在终端里已有的 Claude/Codex 会话；不支持 Claude 的订阅（OAuth）登录。
- 首页「接入检查」在启用 Claude 后会多出一组 Claude Code CLI 的检查项（CLI 版本、模型与网关、密钥、网关连通）。

## 测试

测试需要独立 PostgreSQL 测试库；使用随机隔离 schema，不要指向业务库，也不要另起固定端口的 PG 容器。`scripts/test_backend.sh` 按 `compose.test.yaml` 临时起一个测试库（独立项目 `codex-hub-v1-test`、Docker 分配的空闲端口、tmpfs 无卷、amd64、1 CPU/512MB），跑完无论成败都删除；上次残留时直接报错，不复用也不覆盖。

```bash
PYTHON=.venv/bin/python scripts/test_backend.sh            # 默认 backend/tests tests runner，可传 pytest 参数
npm --prefix frontend test -- --run
npm --prefix frontend run build
```

测试覆盖权限、IM 协议与事务、目录/群/会话生命周期、首次管理员初始化及前端交互。测试模拟外部平台请求，不代表真实平台、Linux 容器沙箱或生产部署已验收。

## 部署前必须补齐的边界

- Codex Linux沙箱探测失败时拒绝执行；不自动降级为危险模式。Docker默认策略可能限制沙箱，需实际验证，不要直接启用privileged。
- MCP初始URL/DNS拒绝私网，但仍需要部署层出站网络策略防范DNS重绑定和重定向。当前Compose未实现该策略；只应接入可信MCP。
- Skill指令与模型提示不构成安全边界。当前禁用shell/写文件等能力，主要使用授权HTTP MCP；不支持需要脚本执行的任意Skill包。MCP内部的细粒度写操作审批尚未实现。
- MCP 请求头的密钥值以 `enc1:` 前缀加密后保存（Fernet，密钥由 `SESSION_SECRET` 以独立域标签派生，与 IM 配置密钥互不相同）；仅在发起任务时由服务端解密发给 runner，网页与接口只显示请求头名称。旧版明文请求头保持可用。**更改 `SESSION_SECRET` 会使已保存的请求头密钥无法解密，任务会报 503 并提示重新创建该 MCP。** 服务地址、Skill 内容仍是普通字段，尚未使用 KMS；需补密钥托管、备份恢复、TLS反代、审计保留、监控告警、限额与负载验证。
- 飞书支持 webhook/websocket，钉钉支持 legacy webhook/stream；Stream 应用 API 支持已授权群与单聊，legacy webhook 仍只回复固定群。配置与启动见 [IM_SETUP.md](docs/IM_SETUP.md)。连接配置与实际状态以运行时查询为准，未做真实平台收发验收。
- IM输出截取前1800字符，完整内容保留网页；发送失败记录错误，不自动重试。刷新后按 URL 恢复所选会话，通过 state 接口继续同步运行状态。

这些边界未验证或未实现，当前交付不声明达到生产企业级验收标准。
