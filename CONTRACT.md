# Agent Hub 实现契约

Python 3.12+ FastAPI + SQLAlchemy 2 同步 Session + PostgreSQL，前端 React/TypeScript/Vite。根目录 agent-hub。禁止创建或启动 Docker 容器。并发最多5。

四角色 super_admin / org_admin / team_lead / member。super_admin 全局；org_admin 同组织下级；team_lead 同团队 member；member 仅本人或自己参与的群聊。管理员查看下级私聊为只读且审计，不能代发。管理员之间无横向查看。群聊读取：成员可读；监管者必须有权监管全部成员。资源归属组织；super_admin 可创建全局资源。用户直属权限：资源绑定 subject_type=user/group + subject_id；私聊=user grants；群聊=user grants ∩ group grants。禁用资源排除。用户注册仅管理员创建。

目录 backend/app/{main,models,db,security,policy,schemas,service,im}.py，runner/{main.py,requirements.txt}，frontend/，tests/。

API /api；Cookie hub_session HttpOnly SameSite strict，写请求校验 Origin（无 Origin 非浏览器允许），登录限速。GET /api/health; POST /api/auth/login {email,password}; POST /api/auth/logout; GET /api/auth/me -> User；GET /api/overview -> {users,groups,conversations,runs}; GET/POST /api/users；GET/POST /api/groups；GET/POST /api/resources；GET/POST /api/bindings；DELETE /api/bindings/{id}；GET/POST /api/conversations；GET /api/conversations/{id}/messages；POST /api/conversations/{id}/messages {content} -> {user_message,run}; GET /api/runs/{id}; GET /api/conversations/{id}/capabilities -> {skills:[],mcps:[]}；GET /api/audit；GET /api/integrations/status。

所有列表直接 JSON 数组。User {id,email,name,role,org_id,team_id,active} 创建另有password。Group {id,name,org_id,team_id,member_ids:[],provider: web|feishu|dingtalk,external_id?}。Resource {id,name,kind:skill|mcp,description,org_id,enabled,config:{}}；skill config {content:string} 完整SKILL.md；mcp config {url:string,headers?:{}} 仅 HTTPS HTTP MCP，拒绝本地/私网/metadata地址，禁止stdio。读取掩码headers。Binding {id,subject_type:user|group,subject_id,resource_id}。Conversation {id,title,owner_id,group_id?,created_at} 创建 {title,group_id?}。Message {id,conversation_id,role:user|assistant|system,content,created_at}。Run {id,status:queued|running|succeeded|failed,error?,created_at}。

运行队列存PG，后台worker领取，启动恢复中断run为failed。执行前再次检查权限和grant。服务 POST runner /execute，Bearer RUNNER_TOKEN；payload {run_id,conversation_id,prompt,skills:[{name,content}],mcps:[{name,url,headers}]} -> {text}。runner独立不接触DB或app密钥；每run临时HOME/CODEX_HOME/workspace；codex exec --json --skip-git-repo-check --sandbox read-only，通过stdin prompt；模型apikey仅runner环境；不使用用户本机auth。限制并发2、timeout180、stdout上限。backend在worker中构造有界历史；无模型凭据运行应明确失败不得伪造AI回复。

IM 使用已配置环境变量验证飞书事件 token + 签名（启用加密时AES解密），钉钉 outgoing机器人 timestamp/sign签名。IM身份需要管理员显式绑定，不允许外部sender自由声明用户ID。新增 identity API GET/POST /api/identities {provider,external_user_id,user_id}。群聊必须external_id匹配已登记group且sender是成员；IM创建/复用conversation并调用同一权限服务。回调返回仅确认，worker异步发消息；去重事件持久化。IM实现者可在im.py提供router以及deliver_reply函数，依赖service.enqueue_message(db,user,conversation,content) -> dict，policy.can_read_conversation。

配置 DATABASE_URL, SESSION_SECRET(最少32), BOOTSTRAP_ADMIN_EMAIL/PASSWORD, RUNNER_URL/TOKEN, APP_ORIGIN=http://127.0.0.1:18200。只本地启动全新PG端口55439/socket在项目.runtime，无改动已有PG。Docker compose三服务 db/api/runner，amd64，api端口18200绑定127.0.0.1，独立network/volumes，不挂docker.sock或HOME。
