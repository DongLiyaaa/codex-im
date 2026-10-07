# 飞书长连接与钉钉 Stream 接入

本项目支持官方 SDK 常驻连接，API 不运行 SDK 事件循环。默认 transport 为 webhook，未配齐凭据不会启动长连接。平台收发与发现仍需自然事件验收；PostgreSQL 和 mock HTTP 测试不代表平台验收。

## 网页配置入口

登录 http://127.0.0.1:18200 → 左侧「IM 集成」→「飞书应用配置」/「钉钉应用配置」。仅超级管理员可查看和保存平台应用配置及全局连接状态；全局发现与 IM 身份、外部群映射由超级管理员显式处理，组织管理员仍可管理其权限内的网页资源授权。

选择 WebSocket / Stream 模式并填写 App ID / App Secret 或 Client ID / Client Secret / Robot Code，然后点击「保存应用配置」。Webhook 模式显示对应验签与固定群字段。已有密钥永不回传，输入框留空保留；勾选「保存时明确清除」才删除，清除会阻止环境变量回退。星号占位值被拒绝。遇到版本冲突先「重新加载」，再填写修改。

推荐使用同页「待接入发现」，保留「绑定员工 Identity」和群新增的手动入口。

配置整份加密存储在 PostgreSQL `im_settings`，Fernet 提供认证加密，事务 advisory lock 和 revision 防止并发覆盖；审计仅记录平台与版本，不记录字段值。首次保存将当前有效环境配置一起固化；未保存的平台仍从环境 fallback。API 回调和 worker 每次操作读取一致快照，已开始的操作可能按原快照完成。长连接监督进程每 5 秒读取配置，先终止旧 SDK 子进程再启动新进程；配置不齐或切回 webhook 时不启动 SDK。旧 SDK 收到消息时核对配置指纹，不再处理已轮换配置。状态也核对指纹，旧心跳不会显示当前配置已连接。

推荐在 API 与 IM 进程的受保护环境中配置同一 `IM_CONFIG_KEY`（Fernet 32字节随机密钥的 URL-safe Base64，可用 `.venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'` 生成，仅保存到0600配置文件）。未配置时，从至少32字符的 `SESSION_SECRET` 以独立域标签 SHA-256 派生。密钥不得存入数据库或网页。修改 IM_CONFIG_KEY 或派生源 SESSION_SECRET 会使旧密文不可读，必须先用旧密钥受控解密并以新密钥重新加密，同时更新所有进程；当前没有自动轮换工具。不可解密时失败关闭，不回退旧环境凭据。

保存配置不会自行安装或启动独立进程；每个平台必须按下方命令启动一个监督器。启动后即使未配置，也在本地等待配置，**不会建立外部连接**。刷新「服务连接状态」查看实际心跳。未提供“测试连接”按钮；连接状态不是发送权限验收，网页保存不会发送测试消息。

## 管理员在对话内配置个人 OAuth 应用

机器人已按上一节接好长连接/webhook 后，`super_admin` 与 `org_admin` 可以不打开网页，直接私聊机器人用自然语言触发个人 OAuth 应用配置，例如：

```
管理员（私聊机器人）：帮我看看飞书的个人授权应用配置好了没有
```

模型会先调用只读工具 `get_platform_application_status(provider)`，把结果（是否已配置、能否一键复用当前机器人应用、机器人 App ID 片段——绝无密钥）原样转述给管理员。若可以复用机器人应用，管理员在同一对话回复"确认复用"之类的明确指令后，模型才会调用写工具 `configure_platform_application(provider)`；服务器端直接从 `im_settings` 密文复制到 `platform_settings` 密文，不产生新密钥，写入后记审计 `platform.configuration.update_via_im`。若机器人应用不可复用（如尚未配置机器人，或钉钉走的是不支持复用的 legacy webhook），`super_admin` 会收到一条私信里的网页安全入口链接（`#/integrations`，仅当 `APP_ORIGIN` 是可达公网地址时才给出）；`org_admin` 则收到"需联系超级管理员完成独立应用配置"的提示，不下发链接，因为该网页页面本身仍是超级管理员专属，避免出现"IM 里能点、网页里打不开"的落差。

安全边界：

- **仅私聊生效**：群聊触发时，群内只收到"请改用私聊"提示，服务端连状态查询都不会执行，零信息泄露到群消息或审计。
- **角色来自服务端**：触发者身份来自内部 capability token 绑定的 `run.user_id` 反查的真实 `User.role`，不是消息文本自称，`member`/`team_lead` 即使让模型硬调用这两个工具也会被服务端拒绝（400 Unknown tool，和真正不存在的工具名返回同样的错误，不泄露工具存在性）。
- **绝不索要密钥**：工具入参只有 `provider`，服务端从不信任模型回传的任何状态型参数（revision、快照指纹等），一律重新查库；任何时候都不会在对话里要求、接受或复述 Client Secret。
- **复用不产生新密钥**：复用机器人应用时，Secret 只在服务器内部从一份密文复制到另一份密文，从不经过模型上下文或聊天记录。

## 选择模式与填写配置

| 平台/模式 | 必填环境变量 | 优势 | 局限 |
| --- | --- | --- | --- |
| 飞书 websocket | `FEISHU_TRANSPORT=websocket`、`FEISHU_APP_ID`、`FEISHU_APP_SECRET` | 无需公网回调及 Verification Token/Encrypt Key，群聊和单聊 | 需要独立常驻进程和出网 |
| 钉钉 stream | `DINGTALK_TRANSPORT=stream`、`DINGTALK_CLIENT_ID`、`DINGTALK_CLIENT_SECRET`、`DINGTALK_ROBOT_CODE` | 应用机器人群聊/单聊，应用 token 主动回复 | 需要发布机器人、开通发送权限及常驻进程 |
| 飞书 webhook | `FEISHU_TRANSPORT=webhook`、上述 App ID/Secret、`FEISHU_VERIFICATION_TOKEN`、`FEISHU_ENCRYPT_KEY` | 保持旧 HTTP 回调兼容 | 需要公网 HTTPS 和验签配置 |
| 钉钉 legacy webhook | `DINGTALK_TRANSPORT=webhook`、`DINGTALK_CLIENT_ID`、`DINGTALK_ROBOT_CODE`（应用身份隔离）、`DINGTALK_APP_SECRET`、`DINGTALK_ROBOT_ACCESS_TOKEN`、`DINGTALK_ROBOT_CHAT_ID`；开启加签另填 `DINGTALK_ROBOT_SECRET` | 保持旧 outgoing 回调和固定群机器人兼容 | 仅一个固定群可执行与回复；其他已验签会话仅登记发现 |

只修改 `.env` 的目标 IM 字段，不覆盖已有 SESSION_SECRET、RUNNER_TOKEN、OAuth 配置。长连接必填共五个凭据字段为飞书 APP_ID/APP_SECRET 和钉钉 CLIENT_ID/CLIENT_SECRET/ROBOT_CODE；webhook 专属凭据无需为长连接填写。

## 飞书开发者后台

1. 在 https://open.feishu.cn/app 创建企业自建应用，启用机器人能力。在「凭证与基础信息」取得 App ID、App Secret。
2. 在「权限管理」申请单聊权限「读取用户发给机器人的单聊消息」`im:message.p2p_msg:readonly`，群内 @ 权限「获取用户在群组中@机器人的消息」`im:message.group_at_msg:readonly`，以及「以应用的身份发消息」`im:message:send_as_bot`。仅需要 @ 消息时不要申请全群消息权限；即使已经开通了「获取群组中所有消息」`im:message.group_msg`（敏感权限），Hub 也只处理 @ 了机器人的群消息（用 `bot/v3/info` 取得机器人自身 open_id 并核对 mentions；取不到时群消息一律忽略，私聊不受影响）。读取被引用的消息需要 `im:message:readonly`（或 `im:message`），未开通时引用上下文会被忽略，不影响收发。
3. 配置应用可用范围包含测试用户，发布版本并完成企业审批，把机器人加入隔离测试群。
4. 填写本地 APP_ID/APP_SECRET 并启动飞书进程。在「事件与回调」选择「使用长连接接收事件」，添加「接收消息 v2.0」`im.message.receive_v1`，保存并发布。后台可能要求先建立连接才能保存。
5. SDK `lark-oapi==1.7.3` 使用 `ws.Client` 和 `register_p2_im_message_receive_v1`。WS 身份由 SDK 握手验证，不调用 HTTP 验签/AES 解密流程。只处理用户 text；机器人消息被忽略。
6. 回复固定发送到 `/open-apis/im/v1/messages?receive_id_type=chat_id`，content 为 text JSON 字符串，带稳定 uuid。
7. （对话内创建/读写云文档）在「权限管理」开通机器人身份的云文档、电子表格、多维表格与云空间权限；若要让员工以本人身份读写（复用机器人应用作为个人 OAuth 应用时），还需开通对应的**用户身份**权限。Hub 申请的范围是整个文档域，共 89 项，以 `backend/app/platform_auth.py` 的 `SCOPES` 为准：云文档（`docx:document*`、`docs:document.content:read`、评论、权限与分享、导入导出、复制）、电子表格（`sheets:spreadsheet*`）、多维表格（`base:app/table/field/record/view/form/dashboard/role/workflow/history:*`）、白板、云空间（`drive:*`、`space:*`）、知识库（`wiki:*`）、文档搜索 `search:docs:read`；不含邮件、消息、日历、通讯录、审批、任务。申请的每一项都必须已作为用户身份权限开通，否则飞书会拒绝授权请求。开通后创建新版本并由企业管理员审批；员工此前完成的授权需重新授权才会用于本人身份读写。
8. （可选，用于「待接入发现」显示飞书昵称和群名）在「权限管理」开通「获取用户基本信息」`contact:user.base:readonly`，并在「数据权限 → 通讯录权限范围」覆盖需要接入的员工；群名需要「获取群信息」`im:chat:readonly`（或 `im:chat`、`im:chat:read` 任一），它同时满足群成员兜底；只开 `im:chat.members:read` 时只能兜底昵称、拿不到群名。开通后须创建新版本并由企业管理员审批。未开通时发现列表会显示具体原因，不影响收发消息。

## 钉钉开发者后台

1. 在 https://open-dev.dingtalk.com 选中开发组织，新建企业内部应用；需要开发者/管理员权限。进入应用内「机器人与消息推送」，启用机器人配置，不使用与应用并列的旧机器人入口。
2. 消息接收模式选择 Stream，补全机器人信息并发布。应用信息中的 Client ID 对应 AppKey，Client Secret 对应 AppSecret；RobotCode 从该机器人配置复制，不从群自定义机器人 token 推测。
3. 在应用权限管理开通应用机器人发送消息权限（控制台通常为「企业内机器人发送消息权限」`qyapi_robot_sendmsg`），确保覆盖群聊发送和批量单聊发送接口。配置应用可用范围包含测试员工，把应用机器人加入隔离测试群。当前文档网页抓取未返回权限正文，最终以这两个接口在控制台列出的权限项为准。
4. 填写 CLIENT_ID/CLIENT_SECRET/ROBOT_CODE 并启动 Stream 进程。官方 0.24.3 实际常量是 `ChatbotMessage.TOPIC='/v1.0/im/bot/messages/get'`；注册 `ChatbotHandler` 子类，非不存在的 `ChatbotHandler.topic`。
5. 接收 `conversationType=1` 单聊、`2` 群聊。身份只采用 `senderStaffId`（SDK `sender_staff_id`），群映射采用 `conversationId`（SDK `conversation_id`），核对 robotCode。缺少员工身份直接拒绝，不能用 senderId、昵称替代。
6. 应用 token：POST `https://api.dingtalk.com/v1.0/oauth2/accessToken`，body `{appKey,appSecret}`。群发送 `/v1.0/robot/groupMessages/send`：`robotCode,openConversationId,msgKey=sampleText,msgParam`；单聊 `/v1.0/robot/oToMessages/batchSend`：`robotCode,userIds=[senderStaffId],msgKey=sampleText,msgParam`。msgParam 是 JSON 字符串 `{"content":"回复"}`；token 放 `x-acs-dingtalk-access-token` 头。
7. SDK OpenAPI 模型已核对响应 `processQueryKey`，以及单聊的 `invalidStaffIdList`、`flowControlledStaffIdList`、`filteredStaffIdList`。任一非空失败名单或缺少受理凭证均记投递失败。受理成功不等于员工已读。

## 先发消息，再分配权限

1. 超级管理员先配置并连接企业自建应用。**员工不需要 Agent Hub 账号**：整个平台只需要超级管理员账号，不必为每位员工在用户管理中创建邮箱和密码。
私聊和群聊在两个页面分开处理，同一类事情只在一个地方做：

| 页面 | 处理什么 |
|---|---|
| 「IM 集成」→「待接入发现」 | **只处理私聊**：把私聊机器人的员工接入为成员；它的「刷新发现」只补私聊发送者的昵称 |
| 「协作群组」→「已发现群 / 待绑定」和「群聊发送者待接入」 | **群聊的一切**：群登记、群聊发送者接入、群成员确认；这里的「刷新发现」补群聊发送者的昵称和群名 |

两个页面的列表由服务端按类型过滤（`GET /api/im/discoveries?chat=private|group`，昵称刷新 `POST /api/im/discoveries/nicknames?chat=private|group`），各自展示最近 500 条，群聊再多也不会把私聊挤出列表；不带 `chat` 参数时行为与以前相同（返回两类、两类都刷新）。

**私聊（IM 集成）**

1. 超级管理员先配置并连接企业自建应用。**员工不需要 Agent Hub 账号**：整个平台只需要超级管理员账号，不必为每位员工在用户管理中创建邮箱和密码。
2. 员工私聊机器人。平台仅在权限、订阅和可用范围允许时推送事件。
3. 超级管理员进入「IM 集成」→「待接入发现」→「刷新发现」。无需人工抄写 open_id 或 senderStaffId。
4. 点击「处理接入」，默认方式是「新建 IM 成员（无需网页账号）」：填写姓名（有昵称时自动带入）、角色（成员或团队负责人）、组织和部门，保存时在同一事务里创建成员并绑定该发送者的 IM 身份。成员没有邮箱、密码和网页登录，只能通过飞书/钉钉使用；管理员角色不能这样创建，需要在用户管理中单独创建带登录凭据的账号。该发送者已绑定内部用户时只能选「绑定已有用户」。
5. 身份写入在单一事务提交并审计；不会附加 Skill/MCP。前往「绑定与授权」授权用户。
6. 员工必须重新发送一条新消息。获权前的原消息只留去重摘要，不会自动执行，也不会因平台重投而回放。

**群聊（协作群组）**

1. 先把机器人加入群，再让群成员 @ 机器人发一条文本消息（飞书群聊只处理 @ 机器人的消息）。
2. 超级管理员进入「协作群组」页，在「群聊发送者待接入」点「刷新发现」，补全发送者昵称和群名。无需人工抄写 chat_id 或 conversationId。
3. 发送者还不是成员：点「处理接入」，用新建或绑定已有用户的方式接入。新建方式只创建成员和身份，不能同时登记群；接入后，群尚未登记的在上方「已发现群 / 待绑定」登记（新成员已作为候选成员出现），群已登记的再点一次「处理接入」，用「绑定已有用户」选这位成员并确认加入群。
4. 绑定已有用户时可以一并处理群：选择同平台、同组织/团队的已有可管理群，明确确认加入所选用户；已有外部映射必须已属于当前应用且与发现相同，不覆盖未知旧映射。也可选择「创建新群」：平台和外部 ID 自动预填，填写群名称，明确勾选成员（包含发送者），再勾选成员确认。
5. 身份、群与成员写入在单一事务提交并审计；不会附加 Skill/MCP。前往「绑定与授权」分别授权用户和群，群聊仍取两者交集。
6. 群成员必须重新发送一条新消息。获权前的原消息不会自动执行，也不会回放。

### 批量接入

在「待接入发现」（私聊）或「群聊发送者待接入」表里勾选多个待接入的发送者（点「批量接入」），为他们统一指定角色、组织和部门，每人单独填写姓名（有昵称时自动带入，没有昵称的必须手填）。一次最多 50 人，且只能是同一个平台；同一个发送者的多条发现只能选一行。每个发送者在自己的保存点里处理：已经绑定过的、姓名无效的、目录条目不存在的会逐人返回原因，不会影响也不会撤销其他人的接入；弹窗保留未成功的人，修正后可以再次提交。审计 `im.discovery.onboard_batch` 只记录请求数和成功数，不记录姓名。

### 自动接入策略（可选，默认关闭）

超级管理员在「IM 集成 → 自动接入策略」按平台分别开启后，**同组织员工首次私聊机器人就自动成为成员，不再逐个审批**，消息会立即被处理。它不是默认行为，下面的限制都是服务端强制的：

| 限制 | 说明 |
|---|---|
| 默认关闭 | 没有保存过策略时完全不生效；开启前必须确认已在飞书/钉钉后台限制了应用的可用范围（这是真正决定「谁能找到机器人」的一道门） |
| 只接受私聊 | 群聊永远不触发，只留在「协作群组 → 群聊发送者待接入」里等人工处理 |
| 同组织校验 | 飞书要求事件头 `tenant_key` 与发送者 `tenant_key` 一致；钉钉要求 `senderCorpId` 与 `chatbotCorpId` 一致；缺失或不一致都按外部人员处理，退回人工。**钉钉的这两个字段尚未做真实平台验收**（本环境没有配置钉钉应用），不满足时只会退回人工接入，不会放行 |
| 固定角色与归属 | 角色只能是成员；组织和部门由策略指定，必须是目录里存在且未归档的条目 |
| 每日上限 | 每个平台近 24 小时自动接入人数上限（1–200，默认 20），超过后其余发送者退回人工 |
| 失败即退回 | 组织或部门被删除、姓名异常、并发冲突等任何失败都不保留半成品，发送者留在待接入发现里 |
| 无默认能力 | 自动接入的成员没有任何 Skill / MCP，仍需在「绑定与授权」逐个授权（刻意不做默认授权，避免自动放行带凭据的能力） |

姓名：钉钉取事件里的 `senderNick`；飞书事件不含昵称，先用「飞书用户 + open_id 后 6 位」占位，管理员可以在「用户与角色」里点「改名」。每次自动接入写入审计 `im.auto_onboard.<平台>`（用于每日上限统计，不含姓名和外部 ID）和 `user.create`（`via=im_auto`，操作人为空表示系统）。

### 停用与离职

「用户与角色」里对自己有管理权限的成员（超级管理员管全部下级，组织管理员管同组织下级，团队负责人管同团队成员；不能操作自己，也不能操作同级或更高级，所以超级管理员账号不能在网页停用）显示「停用 / 启用 / 改名」。停用在同一个事务里：

- 立即取消该成员排队、等待附件、执行中的任务（效果同 `/stop`：之后产生的答复被丢弃，任务的工具调用立即失效）
- 删除其网页登录会话（只有网页账号才有）
- 清除其飞书/钉钉本人授权的加密令牌，并取消正在进行的授权流程（这是本地登出，不代表在飞书/钉钉那边撤销了授权，彻底撤权需要在平台的授权管理里操作）
- 关闭其待确认的高风险操作审批，丢弃已保存的请求
- 之后该成员的消息会被记录为 `inactive_user` 并拒绝处理

历史会话、群成员关系和 IM 身份都保留：身份必须继续指向这个已停用的成员，否则同一个人再发消息会被当成新发送者而重复接入。重新启用恢复访问，但本人平台授权已被清除，需要重新授权。停用/启用/改名分别写入审计 `user.deactivate`（含取消的任务数、清除的授权、关闭的审批数）、`user.activate`、`user.rename`（不记录姓名）。

发现表仅存平台、非秘密应用身份摘要、发送者/会话 ID、类型、已有昵称、首次/最近时间、固定拒绝原因。钉钉直接读取事件已有的 senderNick，不调用任何钉钉接口。飞书消息事件只含 open_id，不含昵称：只有超级管理员点击「刷新发现」时，服务端才用当前机器人应用的 tenant token 调用 `GET /open-apis/contact/v3/users/{open_id}?user_id_type=open_id` 查询姓名；失败且该发送者出现在群聊时，回退 `GET /open-apis/im/v1/chats/{chat_id}/members`（最多 5 页）。收到消息时不查询，避免未授权发送者驱动出站调用。每次最多查询 20 人、总时限约 10 秒，剩余的下次刷新继续；网络请求在数据库事务外执行，写回前在配置锁下重新核对应用作用域，应用已切换则丢弃结果，且只填充仍为空的昵称。失败原因（不在通讯录权限范围、未开通权限、机器人不在群、外部群、查询失败等）缓存在 API 进程内：配置类错误 1 小时后重试，临时错误 5 分钟后重试，并显示在昵称列。审计 `im.discovery.nickname_refresh` 只记录数量和状态，不记录姓名或群名。

群名单独存于 `im_chat_names`（平台 + 应用作用域 + 群 ID，上限 10000 条，超限淘汰最久未更新的），仅用于展示和绑定群时预填群名称，从不作为授权依据。钉钉在群消息事件中直接取 `conversationTitle`（Stream 与 legacy webhook 均如此），群改名后下一条消息自动更新，私聊的 title 不保存。飞书在同一次「刷新发现」中对尚无群名的群调用 `GET /open-apis/im/v1/chats/{chat_id}` 读取 `name`（为空时取 `i18n_names.zh_cn` / `en_us`），每次最多 20 个群，与昵称共用时限、失败缓存、事务外请求和写回前的应用作用域复核；已取到的飞书群名不会随改名自动刷新。「已发现群 / 待绑定」的绑定弹窗会用该群名预填。不保存拒绝消息正文、原始 payload、token 或 sessionWebhook。只接收验签通过的 webhook 或认证 SDK 事件。

应用作用域来自飞书 App ID，或钉钉 Client ID + Robot Code；密钥轮换不改变作用域。换应用后旧发现不可审批，已绑定的旧应用映射返回冲突，不静默覆盖。旧身份没有作用域证据时保持待确认且审批返回409，不允许将未知归属绑定直接认领到当前应用；未知旧群外部映射不自动补归属。需要受控迁移旧映射时，本版没有删除/迁移 UI。

发现元数据全局上限 10000 条，达到上限淘汰最近最久未出现的一条；页面只显示最近 500 条并手动刷新。拒绝事件的固定长度摘要保留在 im_events 防回放，尚无自动归档，需监控库增长；不能随意删除摘要后重投旧事件。本功能不代表生产级抗滥用容量验收。

## 本地启动

先启动现有本项目 PostgreSQL 与 API（由 API 初始化业务表和运行队列 worker）；不新建容器。项目目录执行：

```bash
uv pip install --python .venv/bin/python -r backend/requirements.txt -r backend/requirements-im.txt
.venv/bin/python scripts/run_im_local.py --provider feishu
# 另一个终端
.venv/bin/python scripts/run_im_local.py --provider dingtalk
```

每个平台仅运行一个监督进程，通过 PostgreSQL session advisory lock 强制互斥。`run_im_local.py` 加载 .env 中 IM/DATABASE_URL/SESSION_SECRET/IM_CONFIG_KEY 字段，默认使用本项目 `.runtime/pgsocket:55439`。数据库连接丢失会停止子进程并退出，需由服务管理器重新启动。SDK 子进程失败时每5秒重启；配置变更终止最长等待8秒，随后强制停止，避免新旧连接并行。Docker 内命令为 `python -m app.im_connections --provider feishu|dingtalk`。现有 Compose 尚未给 IM 服务传入加密主密钥，未来容器发布前必须给 API 与两个 IM 服务注入相同的 `IM_CONFIG_KEY`（或相同 `SESSION_SECRET` 派生源），否则网页保存后的密文无法被 IM 服务读取。Compose `im` profile 提供两个独立服务，无发布端口；必须先人工审核 DOCKER_IMPACT.md，本文不表示已获启动许可。

SDK 依赖独立在 `backend/requirements-im.txt`，固定 lark-oapi 1.7.3 / dingtalk-stream 0.24.3，websockets 为 `>=11,<16`，API/IM image 安装，runner image 不改。API 与 runner 如共用虚拟环境，升级依赖后应一起执行回归。

## 状态、事务与安全边界

登录后 `/api/integrations/status` 返回 transport/configured/missing/state。`configured` 只表示配置齐全；`connection_unobserved` 表示尚无进程状态，`connecting` 表示连接未打开/重连，`connected` 仅表示 SDK socket OPEN，`stale` 表示心跳超过 20 秒，`stopped` 表示进程结束。connected 不保证应用权限、订阅或出站链路可用。心跳每 5 秒写 PostgreSQL `im_connections`，无正文、票据或外部响应；没有虚构 ready。

独立进程关闭 SDK 原始日志，避免 SDK 默认输出 ticket URL、消息和响应。错误只输出固定代码。未知身份/畸形消息确认为永久拒绝，409/数据库异常等返回 500（飞书 handler 抛静态错误）让平台决定重投；不承诺平台重试时限。

两种适配器复用 `_enqueue`，按平台和应用作用域内事件摘要去重。未知身份、未知群和非成员正常 ack，发现与拒绝事件摘要在同事务提交，不创建 Run；数据库异常仍回滚。事务锁使用绑定参数。模式保存在每条事件 reply_target，切换环境模式不会把旧钉钉事件改投另一通道；历史无模式事件视为 webhook。

投递前再次检查用户 active、身份、群成员、组织及外部群映射。token 在进程内按凭据摘要缓存，提前 60 秒失效；不写数据库，不记录明文。sessionWebhook 不使用、不存储。回复最多 1800 字符。外部 API 错误只保存 `IM_DELIVERY_FAILED`。出站不自动重试，以免网络超时造成重复；不承诺外部 exactly-once。

legacy 回调仍为 `/api/im/feishu/callback`、`/api/im/dingtalk/callback`，长连接模式下对应 HTTP 路由返回 404。飞书 webhook 检查 token、5 分钟窗口、SHA256(timestamp+nonce+encrypt_key+原始body)，AES-CBC/PKCS7 兼容。钉钉 legacy 验签 timestamp 毫秒与 AppSecret HMAC；已验签的未知群/单聊也会 ACK 并提交最小发现，但固定群以外不执行或回复。审批后如仍提示「固定群回调不支持此会话」，需由管理员切换 Stream 并配置完整凭据，再发送新消息。固定群通过服务器固定自定义机器人 URL 回复。

## 消息下方的处理提示

网页打开会话后，通过受原有会话读取权限保护的 `/api/conversations/{id}/state` 每 3 秒读取消息和真实 Run 状态。`active_run` 仅在 queued/running 返回，`latest_run` 保留终态错误；来源由 Run 关联的 IMEvent.provider 决定，不由会话标题推断。对应用户消息气泡下显示「飞书：工作」「钉钉：工作」或网页来源的「工作中」。有权限的监管账号也能看到，发送权限不变。首次打开仍通过 messages 接口记监管读取审计，状态轮询不重复写审计。

## 审计日志里的「操作人」

审计表只存内部 ID，所以页面的「操作人」列在读取时按行补上"人和地点"，原始 ID 仍作为小字保留，方便追溯：

| 内容 | 来源 |
|---|---|
| 人 | 飞书/钉钉昵称；没有昵称时用 Hub 账号名；昵称与账号名不同时另一行写「Hub 账号：…」 |
| 渠道标签 | 任务对应的入站事件平台（飞书/钉钉）；没有入站事件的任务是「网页」；`platform.*` 记录本身就带平台 |
| 群或私聊 | 审计行指向的任务→会话→群；`conversation.*`、`im.group.bind` 和 `details.group_id` 直接给出群；飞书/钉钉来的会话没有群时写「私聊」 |

规则：昵称取**操作人自己**在该渠道的身份（例如管理员用 /stop 取消别人的任务，显示的是管理员的昵称，不是任务主人的）；多个昵称取最近一次解析到的。群名只在查看者本来就能读取这个群时显示，否则写「群：无权查看」，群已删除写「群：已删除」，已归档的群在其组织/团队范围内仍显示名字并标「（已归档）」。没有操作人的系统操作（如运维清理）只显示「—」。昵称是平台上的显示名，只有「刷新发现」成功解析（飞书）或事件自带（钉钉）才会有；没有时退回账号名。后端每页最多 10 次批量查询，不随行数增长（有测试守护）；旧版服务端不返回这些字段时，页面退回只显示 ID。

当前 runner/API 为非流式：完整 assistant 正文落库并首次同步显示时撤销提示，没有逐 token 输出。失败或 worker 恢复时标记的中断终态撤销并显示错误；状态同步失败时撤销旧提示并提示重试。切会话中止轮询，刷新从服务端恢复；历史最后一条为用户消息不会单独触发“工作”状态。

飞书客户端原生标记现已实现：新授权消息入库时独立 `im_reactions` 表保存原始 message_id，原去重摘要不变；拒绝消息、已有历史消息不补建标记。worker 在调用 Codex 前添加官方 `Typing` 键盘工作表情，在发送完整正文前删除。官方表情清单未列出 `WORKING`，客户端文案及图标以飞书实际版本为准，不承诺字面显示“工作”。当前仍为非流式回复。

超级管理员须在飞书开发者后台「权限管理」开通 `im:message.reactions:write_only`（创建/删除；已有 `im:message` 也可），以及 `im:message.reactions:read`（异常恢复读取；已有 `im:message:readonly` 也可），随后创建发布版本并完成管理员审批。代码不会修改平台权限，也没有向真实历史消息发起测试。管理员 `/api/integrations/status` 的 `native_work_status` 提供权限步骤和静态错误码，`unverified` 表示尚无新事件验证。

独立表通过 `app.im_migrations.migrate` 在 API 启动时执行幂等增量建表，不为现有表加列。需先更新 API 再更新 IM 接收进程。事件和标记在同一事务提交；网络在提交后执行。进程间 advisory lock 串行化同一 Run，配置共享锁阻止请求期间替换应用配置；不同应用 scope 不操作旧消息。失败、中断、取消的终态由 worker 每轮检查，清理失败间隔至少30秒重试。创建超时或崩溃时，读取消息上当前应用自己的 Typing 标记再删除，绝不删除用户或其他应用表情；成功清理后删除原始 message_id 和 reaction_id。网络或权限失败不会阻止正常生成/回复，但飞书不可达、权限被撤销或应用已切换时无法保证远端标记立即消失，状态会保留待清理错误，需恢复原应用及权限。仅安装代码不等于真实平台验收。

钉钉的消息表情：我们此前写的“官方目录未证实存在该接口”并不准确。开源项目 cc-connect 的钉钉适配调用了 `POST /v1.0/robot/emotion/reply` 与 `/v1.0/robot/emotion/recall`（参数 `robotCode`、`openMsgId`、`openConversationId`、`emotionType=2`、`textEmotion`），即回复和撤回用户消息上的表情，说明接口存在。本版尚未实现：当前没有配置钉钉应用，无法做真实验证，且文本表情用到的模板 ID 是它写死的常量，需要在真实应用上确认后再接入。交互卡片（钉钉 AI 卡片流式更新）可作为后续方案，但需要另行确定权限、卡片模板和交互，不自动追加永久工作文本。

参考：[飞书创建表情回复](https://open.feishu.cn/document/server-docs/im-v1/message-reaction/create)、[飞书删除表情回复](https://open.feishu.cn/document/server-docs/im-v1/message-reaction/delete)。

## 测试与真实验收

```bash
PYTHONPATH=backend .venv/bin/python -m pytest tests backend/tests runner/test_runner.py -q
```

测试使用本项目真实 PostgreSQL 的临时独立 schema，验证适配器并发去重、群和单聊、未知身份/撤权/跨组织拒绝、回滚、SDK 字段、连接状态过期、HTTP 模式隔离，以及 MockTransport 出站结构/token缓存/失败名单。不会发送真实平台消息。真实验收仍需应用凭据、平台发布/审批完成和员工自然发送事件；由管理员配置后验证私聊、群 @、重复推送及撤权后不回复。模拟测试成功不代表平台已打通。

## 官方来源

- https://open.feishu.cn/document/server-docs/im-v1/message/events/receive
- https://open.feishu.cn/document/server-docs/im-v1/message/create
- https://github.com/larksuite/oapi-sdk-python
- https://github.com/open-dingtalk/dingtalk-stream-sdk-python
- https://github.com/open-dingtalk/developerpedia/blob/main/docs/explore/tutorials/stream/bot/python/create-bot.md
- https://open.dingtalk.com/document/orgapp/robot-batch-send-one-on-one-chat-messages
- https://pypi.org/project/alibabacloud-dingtalk/ （核对 2.2.60 robot_1_0 模型的请求/响应字段；未作为运行依赖安装）

## 已发现群的组织与部门选择

若只有无组织的 Super Admin，先在「协作群组」→「群聊发送者待接入」确认其内部身份（如已绑定则无需再绑），再在同页「已发现群 / 待绑定」→「绑定 / 一键带入」。在弹窗内新增中文命名组织，按需新增部门或保留「组织级 / 跨部门」，勾选 Super Admin 和成员确认后绑定。创建目录不会迁移该管理员，也不增加 Skill/MCP 授权。旧发现消息不回放，完成后需发送新消息。

组织和部门分开选择，部门隶属组织；唯一可信内部组织/部门自动带入。同组织多部门可以选组织级群，多个组织不能自动合并。成员始终来自当前应用已绑定且有权限管理的活跃用户，切换范围重新勾选；已有群保留归属及成员。后续可在「用户与角色」用同一命名目录创建实际组织成员，再显式绑定其 IM 身份。每位用户仅有一个主部门，不支持多部门身份。

超级管理员作为全局身份仅在显式群 membership 下参与；其他组织管理员或普通用户不能跨组织加入。群内执行资源按群组织过滤，并保留用户与群授权交集；超级管理员给自己的组织资源授权不会在其全局私聊中生效。接入、执行与投递共用成员校验，撤权后不能继续执行。目录仅创建/查看；历史字符串归属兼容，不自动改写。当前事件没有部门信息，不进行通讯录同步，不扩大平台权限。
