# 飞书长连接与钉钉 Stream 接入

本项目支持官方 SDK 常驻连接，API 不运行 SDK 事件循环。默认 transport 为 webhook，未配齐凭据不会启动长连接。平台收发与发现仍需自然事件验收；PostgreSQL 和 mock HTTP 测试不代表平台验收。

## 网页配置入口

登录 http://127.0.0.1:18200 → 左侧「IM 集成」→「飞书应用配置」/「钉钉应用配置」。仅超级管理员可查看和保存平台应用配置及全局连接状态；全局发现与 IM 身份、外部群映射由超级管理员显式处理，组织管理员仍可管理其权限内的网页资源授权。

选择 WebSocket / Stream 模式并填写 App ID / App Secret 或 Client ID / Client Secret / Robot Code，然后点击「保存应用配置」。Webhook 模式显示对应验签与固定群字段。已有密钥永不回传，输入框留空保留；勾选「保存时明确清除」才删除，清除会阻止环境变量回退。星号占位值被拒绝。遇到版本冲突先「重新加载」，再填写修改。

推荐使用同页「待接入发现」，保留「绑定员工 Identity」和群新增的手动入口。

配置整份加密存储在 PostgreSQL `im_settings`，Fernet 提供认证加密，事务 advisory lock 和 revision 防止并发覆盖；审计仅记录平台与版本，不记录字段值。首次保存将当前有效环境配置一起固化；未保存的平台仍从环境 fallback。API 回调和 worker 每次操作读取一致快照，已开始的操作可能按原快照完成。长连接监督进程每 5 秒读取配置，先终止旧 SDK 子进程再启动新进程；配置不齐或切回 webhook 时不启动 SDK。旧 SDK 收到消息时核对配置指纹，不再处理已轮换配置。状态也核对指纹，旧心跳不会显示当前配置已连接。

推荐在 API 与 IM 进程的受保护环境中配置同一 `IM_CONFIG_KEY`（Fernet 32字节随机密钥的 URL-safe Base64，可用 `.venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'` 生成，仅保存到0600配置文件）。未配置时，从至少32字符的 `SESSION_SECRET` 以独立域标签 SHA-256 派生。密钥不得存入数据库或网页。修改 IM_CONFIG_KEY 或派生源 SESSION_SECRET 会使旧密文不可读，必须先用旧密钥受控解密并以新密钥重新加密，同时更新所有进程；当前没有自动轮换工具。不可解密时失败关闭，不回退旧环境凭据。

保存配置不会自行安装或启动独立进程；每个平台必须按下方命令启动一个监督器。启动后即使未配置，也在本地等待配置，**不会建立外部连接**。刷新「服务连接状态」查看实际心跳。未提供“测试连接”按钮；连接状态不是发送权限验收，网页保存不会发送测试消息。

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
2. 在「权限管理」申请单聊权限「读取用户发给机器人的单聊消息」`im:message.p2p_msg:readonly`，群内 @ 权限「获取用户在群组中@机器人的消息」`im:message.group_at_msg:readonly`，以及「以应用的身份发消息」`im:message:send_as_bot`。仅需要 @ 消息时不要申请全群消息权限。
3. 配置应用可用范围包含测试用户，发布版本并完成企业审批，把机器人加入隔离测试群。
4. 填写本地 APP_ID/APP_SECRET 并启动飞书进程。在「事件与回调」选择「使用长连接接收事件」，添加「接收消息 v2.0」`im.message.receive_v1`，保存并发布。后台可能要求先建立连接才能保存。
5. SDK `lark-oapi==1.7.3` 使用 `ws.Client` 和 `register_p2_im_message_receive_v1`。WS 身份由 SDK 握手验证，不调用 HTTP 验签/AES 解密流程。只处理用户 text；机器人消息被忽略。
6. 回复固定发送到 `/open-apis/im/v1/messages?receive_id_type=chat_id`，content 为 text JSON 字符串，带稳定 uuid。

## 钉钉开发者后台

1. 在 https://open-dev.dingtalk.com 选中开发组织，新建企业内部应用；需要开发者/管理员权限。进入应用内「机器人与消息推送」，启用机器人配置，不使用与应用并列的旧机器人入口。
2. 消息接收模式选择 Stream，补全机器人信息并发布。应用信息中的 Client ID 对应 AppKey，Client Secret 对应 AppSecret；RobotCode 从该机器人配置复制，不从群自定义机器人 token 推测。
3. 在应用权限管理开通应用机器人发送消息权限（控制台通常为「企业内机器人发送消息权限」`qyapi_robot_sendmsg`），确保覆盖群聊发送和批量单聊发送接口。配置应用可用范围包含测试员工，把应用机器人加入隔离测试群。当前文档网页抓取未返回权限正文，最终以这两个接口在控制台列出的权限项为准。
4. 填写 CLIENT_ID/CLIENT_SECRET/ROBOT_CODE 并启动 Stream 进程。官方 0.24.3 实际常量是 `ChatbotMessage.TOPIC='/v1.0/im/bot/messages/get'`；注册 `ChatbotHandler` 子类，非不存在的 `ChatbotHandler.topic`。
5. 接收 `conversationType=1` 单聊、`2` 群聊。身份只采用 `senderStaffId`（SDK `sender_staff_id`），群映射采用 `conversationId`（SDK `conversation_id`），核对 robotCode。缺少员工身份直接拒绝，不能用 senderId、昵称替代。
6. 应用 token：POST `https://api.dingtalk.com/v1.0/oauth2/accessToken`，body `{appKey,appSecret}`。群发送 `/v1.0/robot/groupMessages/send`：`robotCode,openConversationId,msgKey=sampleText,msgParam`；单聊 `/v1.0/robot/oToMessages/batchSend`：`robotCode,userIds=[senderStaffId],msgKey=sampleText,msgParam`。msgParam 是 JSON 字符串 `{"content":"回复"}`；token 放 `x-acs-dingtalk-access-token` 头。
7. SDK OpenAPI 模型已核对响应 `processQueryKey`，以及单聊的 `invalidStaffIdList`、`flowControlledStaffIdList`、`filteredStaffIdList`。任一非空失败名单或缺少受理凭证均记投递失败。受理成功不等于员工已读。

## 先发消息，再分配权限

1. 超级管理员先配置并连接企业自建应用；在用户管理中创建内部用户。
2. 员工私聊机器人或在机器人所在群发文本消息。平台仅在权限、订阅和可用范围允许时推送事件。
3. 超级管理员进入「IM 集成」→「待接入发现」→「刷新发现」。无需人工抄写 open_id、senderStaffId 或 chat_id/conversationId。
4. 点击「处理接入」，选内部已有用户。可先仅绑定身份，随后从同一条发现继续群登记。
5. 群聊可选择同平台、同组织/团队的已有可管理群，明确确认加入所选用户；已有外部映射必须已属于当前应用且与发现相同，不覆盖未知旧映射。也可选择「创建新群」：平台和外部 ID 自动预填，填写群名称，明确勾选成员（包含发送者），再勾选成员确认。
6. 身份、群与成员写入在单一事务提交并审计；不会附加 Skill/MCP。前往「绑定与授权」分别授权用户和群，群聊仍取两者交集。
7. 员工必须重新发送一条新消息。获权前的原消息只留去重摘要，不会自动执行，也不会因平台重投而回放。

发现表仅存平台、非秘密应用身份摘要、发送者/会话 ID、类型、已有昵称、首次/最近时间、固定拒绝原因。钉钉仅读取事件已有 senderNick；飞书事件无昵称时显示无昵称，不额外调用通讯录 API。不保存拒绝消息正文、原始 payload、token 或 sessionWebhook。只接收验签通过的 webhook 或认证 SDK 事件。

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

当前 runner/API 为非流式：完整 assistant 正文落库并首次同步显示时撤销提示，没有逐 token 输出。失败或 worker 恢复时标记的中断终态撤销并显示错误；状态同步失败时撤销旧提示并提示重试。切会话中止轮询，刷新从服务端恢复；历史最后一条为用户消息不会单独触发“工作”状态。

飞书客户端原生标记现已实现：新授权消息入库时独立 `im_reactions` 表保存原始 message_id，原去重摘要不变；拒绝消息、已有历史消息不补建标记。worker 在调用 Codex 前添加官方 `Typing` 键盘工作表情，在发送完整正文前删除。官方表情清单未列出 `WORKING`，客户端文案及图标以飞书实际版本为准，不承诺字面显示“工作”。当前仍为非流式回复。

超级管理员须在飞书开发者后台「权限管理」开通 `im:message.reactions:write_only`（创建/删除；已有 `im:message` 也可），以及 `im:message.reactions:read`（异常恢复读取；已有 `im:message:readonly` 也可），随后创建发布版本并完成管理员审批。代码不会修改平台权限，也没有向真实历史消息发起测试。管理员 `/api/integrations/status` 的 `native_work_status` 提供权限步骤和静态错误码，`unverified` 表示尚无新事件验证。

独立表通过 `app.im_migrations.migrate` 在 API 启动时执行幂等增量建表，不为现有表加列。需先更新 API 再更新 IM 接收进程。事件和标记在同一事务提交；网络在提交后执行。进程间 advisory lock 串行化同一 Run，配置共享锁阻止请求期间替换应用配置；不同应用 scope 不操作旧消息。失败、中断、取消的终态由 worker 每轮检查，清理失败间隔至少30秒重试。创建超时或崩溃时，读取消息上当前应用自己的 Typing 标记再删除，绝不删除用户或其他应用表情；成功清理后删除原始 message_id 和 reaction_id。网络或权限失败不会阻止正常生成/回复，但飞书不可达、权限被撤销或应用已切换时无法保证远端标记立即消失，状态会保留待清理错误，需恢复原应用及权限。仅安装代码不等于真实平台验收。

钉钉机器人官方能力目录未证实存在修改用户消息下 reaction 的接口，本版不虚构同类能力。交互卡片可作为后续方案，但需要另行确定权限、模板和交互，不自动追加永久工作文本。

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

若只有无组织的 Super Admin，先在「IM 集成」→「待接入发现」确认其内部身份（如已绑定则无需再绑），再进入「协作群组」→「已发现群 / 待绑定」→「绑定 / 一键带入」。在弹窗内新增中文命名组织，按需新增部门或保留「组织级 / 跨部门」，勾选 Super Admin 和成员确认后绑定。创建目录不会迁移该管理员，也不增加 Skill/MCP 授权。旧发现消息不回放，完成后需发送新消息。

组织和部门分开选择，部门隶属组织；唯一可信内部组织/部门自动带入。同组织多部门可以选组织级群，多个组织不能自动合并。成员始终来自当前应用已绑定且有权限管理的活跃用户，切换范围重新勾选；已有群保留归属及成员。后续可在「用户与角色」用同一命名目录创建实际组织成员，再显式绑定其 IM 身份。每位用户仅有一个主部门，不支持多部门身份。

超级管理员作为全局身份仅在显式群 membership 下参与；其他组织管理员或普通用户不能跨组织加入。群内执行资源按群组织过滤，并保留用户与群授权交集；超级管理员给自己的组织资源授权不会在其全局私聊中生效。接入、执行与投递共用成员校验，撤权后不能继续执行。目录仅创建/查看；历史字符串归属兼容，不自动改写。当前事件没有部门信息，不进行通讯录同步，不扩大平台权限。
