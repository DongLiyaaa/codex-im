import type { Page } from './Management';
import type { Role } from './api';
import { imHelpSections } from './imHelpContent';
import type { HelpSection } from './imHelpContent';

export type HelpPage = Page | 'login' | 'setup' | 'connection';
type Instruction = { text: string; roles?: readonly Role[] };
type Question = { question: string; answer: string; roles?: readonly Role[] };
type Guide = { purpose: string; steps: Instruction[]; notes: Instruction[]; questions: Question[]; sections?: HelpSection[] };
const resourceAdmins: readonly Role[] = ['super_admin', 'org_admin'];
const managers: readonly Role[] = ['super_admin', 'org_admin', 'team_lead'];
const superAdmin: readonly Role[] = ['super_admin'];
const readers: readonly Role[] = ['team_lead', 'member'];

const guides: Record<HelpPage, Guide> = {
  onboarding: {
    purpose: '按步骤熟悉模型接入、组织成员、资源授权和试用流程，进度按本人账号保存。',
    steps: [
      { text: '从左侧步骤选择需要了解的环节，阅读操作说明与注意事项。' },
      { text: '点击对应入口前往实际页面操作；返回导航第一项“初始化引导”可继续阅读。' },
      { text: '使用“下一步”保存阅读位置；暂时离开可点“稍后继续”，最后一步点击“完成阅读”。' },
    ],
    notes: [
      { text: '首次注册的管理员第一次登录时自动进入；旧账号不强制跳转，也不会新增公开注册流程。' },
      { text: '阅读进度不代表配置检测结果。引导不会自动创建成员、授予资源、发送消息或调用模型。' },
      { text: '步骤按角色显示，不能通过引导获得额外权限；进度保存失败时请重试。' },
    ],
    questions: [
      { question: '完成后还能重看吗？', answer: '可以。导航第一项一直保留，进入后点击“重新查看引导”即可重新阅读。' },
      { question: '离开引导后设置会丢失吗？', answer: '已成功保存的阅读位置会保留，业务页面的设置仍需在各自页面明确保存。' },
    ],
  },
  manual: {
    purpose: '集中阅读各页的用途、操作步骤、注意事项与常见问题，内容与右下角“本页说明”保持一致。',
    steps: [
      { text: '从目录选择要阅读的章节，或输入关键词查找相关说明。' },
      { text: '阅读正文的四个部分；没有搜索结果时清空关键词再选择章节。' },
      { text: '有访问权限的业务章节可点击“前往页面”，实际操作仍以该页面的权限校验为准。' },
    ],
    notes: [
      { text: '说明根据当前角色过滤，不展示无权执行的管理员操作步骤。' },
      { text: '说明书只供阅读，不会自动修改配置、授权资源或执行任务。' },
    ],
    questions: [
      { question: '这里与“本页说明”有什么区别？', answer: '两处使用同一份正文；浮窗适合边操作边查阅，独立说明页适合搜索与完整阅读。' },
      { question: '为什么找不到某个管理章节？', answer: '当前角色可能没有该页面的访问权限或操作权限，请联系管理员核对。' },
    ],
  },
  overview: {
    purpose: '了解当前工作空间的可见数据，并从快捷入口进入会话、资源或集成页面。',
    steps: [
      { text: '查看“工作空间概况”；数字按你的权限范围统计，点击“刷新”更新。' },
      { text: '点击“开始智能会话”，进入会话页后选择已有会话或创建新会话。' },
      { text: '查看 Codex CLI 接入检查；按当前 API 或 OAuth 方式核对状态，必要时点击“重新检查”。', roles: superAdmin },
      { text: '需要本人 ChatGPT 登录时，点击“ChatGPT 授权”，在官方页面完成授权；连接后可读取并选择本人可用模型。', roles: superAdmin },
    ],
    notes: [
      { text: '页面显示的数据和操作受角色、组织及部门范围限制，不一定是全系统总量。' },
      { text: '执行器就绪只说明运行基础设施可用，不等于本人已授权、模型有额度或远端工具可访问。', roles: superAdmin },
      { text: '系统 API 已配置时优先使用系统 API；个人 OAuth 不会自动替换它。版本号仅供参考，实际执行仍可能受 CLI 能力限制。', roles: superAdmin },
    ],
    questions: [
      { question: '为什么我看不到 CLI 接入检查？', answer: '该检查区仅向超级管理员展示。你可以在“个人设置”查看本人 ChatGPT 连接状态。' },
      { question: '为什么刷新后数字与同事不同？', answer: '每个账号看到的范围可能不同；先核对角色和成员归属，而不是把差异当作数据丢失。' },
    ],
  },
  chat: {
    purpose: '向已选择的 Agent 提交任务，查看执行进度、回复及当前会话可用的 Skill/MCP。',
    steps: [
      { text: '从左侧选择会话，或点击“创建会话”加号，填写名称并选择本人私聊或已加入的群组。' },
      { text: '系统启用多个 Agent 时，在创建会话时选择；已有会话的 Agent 固定，切换需新建会话。' },
      { text: '输入任务与所需资料；Enter 发送，Shift+Enter 换行。添加附件后等状态变为“已就绪”再发送。' },
      { text: '查看消息下的任务状态。需要飞书或钉钉个人文档权限时，打开“本人平台授权”，按提示由本人确认。' },
      { text: '权限调整后点击“刷新消息与能力”；电脑右侧可查看当前能力，小屏可能隐藏或将其移到下方。' },
    ],
    notes: [
      { text: '每条消息最多 5 个附件，每个非空且不超过 20MiB；附件解析失败时先移除或重新上传。' },
      { text: '只读监管会话不能代替成员发送消息；群聊必须加入该群且符合其组织范围。' },
      { text: '群聊 Skill/MCP 取发送者授权与群授权的交集。能使用 Skill 不代表能查看其原文。' },
      { text: '移除会话是从工作台归档，保留历史与审计；不会清空飞书或钉钉聊天。有任务进行时可能无法移除。' },
    ],
    questions: [
      { question: '为什么发送按钮不可用？', answer: '检查是否选择了可发送的会话、消息或附件是否为空、附件是否就绪，以及上一项任务是否仍在排队或执行。' },
      { question: '授权成功后任务会自动继续吗？', answer: '不会自动重放原任务。确认授权状态成功后，请重新发送任务；ChatGPT 登录与飞书/钉钉文档授权是不同授权。' },
      { question: 'Agent 说没有 Skill，怎么确认？', answer: '查看当前能力或请管理员检查生效预览；在已接入的 IM 私聊或群聊中发送 /skills，可取得服务端核验的名称清单。' },
    ],
  },
  directory: {
    purpose: '维护组织与部门目录，用于成员归属、群组范围及资源授权。',
    steps: [
      { text: '在左侧选择组织，右侧查看该组织的部门；“全部组织”显示全部可见部门。' },
      { text: '点击“新增组织”创建目录，然后在该组织下新增部门。', roles: superAdmin },
      { text: '点击“新增部门”，确认所属组织并填写名称后保存。', roles: ['org_admin'] },
      { text: '组织的编辑入口在“目录详情”中；部门可从对应行编辑。名称可改，ID 和所属组织不会随之改变。' },
      { text: '需要排查绑定时展开“查看 ID”，使用“复制 ID”；删除前先处理引用该目录的成员、群组和资源。' },
    ],
    notes: [
      { text: '创建目录不等于授予 Skill/MCP 使用权限，还需到“绑定与授权”配置。' },
      { text: '存在引用的目录不能删除；删除不会级联删除成员或资源，历史 ID 不可复用。' },
      { text: '仅维护本账号有管理权限的目录；组织管理员不能创建新的顶级组织。' },
    ],
    questions: [
      { question: '改了部门名称会影响授权吗？', answer: '改名不会改变目录 ID，已有基于 ID 的关联保留。成员迁移到别的部门则是另一项操作，会重新计算权限。' },
      { question: '为什么删除失败？', answer: '目录可能仍被用户、部门、群组或资源引用，或你无权删除。按错误提示核对，不要反复点击。' },
    ],
  },
  users: {
    purpose: '查看可见成员的角色、组织、部门和 IM 渠道；有权限时维护下级用户账号。',
    steps: [
      { text: '先核对成员姓名、角色、归属和状态；“仅 IM 接入”表示没有网页登录账号。' },
      { text: '点击“新增”，为需要网页登录的用户填写名称、邮箱、至少 12 位初始密码和允许分配的角色。', roles: managers },
      { text: '使用行内“编辑”修改下级成员姓名、角色或归属；确认权限变化提示后保存。', roles: managers },
      { text: '需要恢复网页登录时，可为有权限管理的下级网页用户“重置密码”；也可确认后停用或启用用户。', roles: managers },
      { text: '需要修改本人显示名字和头像时，点击头像进入“个人设置”；其他账号或归属调整请联系管理员。', roles: ['member'] },
    ],
    notes: [
      { text: '只能管理权限范围内、级别低于自己的用户，不能在这里管理自己或同级管理员。', roles: managers },
      { text: '停用会撤销网页登录会话、取消任务并清除个人授权；重新启用后个人授权需重新连接。历史和 IM 身份保留。', roles: managers },
      { text: '调整组织或部门会重新计算权限，可能移出原群或撤销不再符合范围的个人授权。', roles: managers },
      { text: '飞书/钉钉的身份绑定与网页账号是独立关系，同名不表示同一个内部用户；角色以实际绑定的内部账号为准。' },
    ],
    questions: [
      { question: '为什么没有编辑或重置按钮？', answer: '该用户可能不在你的管理范围、与你同级，或是仅 IM 接入的成员；仅 IM 成员没有网页密码可重置。' },
      { question: '普通员工必须创建网页登录账号吗？', answer: '不必。可由超级管理员通过 IM 待接入发现为员工建立仅 IM 身份，再单独分配所需能力。' },
    ],
  },
  groups: {
    purpose: '查看协作群、成员及来源，并在权限允许时登记或维护群关系。',
    steps: [
      { text: '核对群名称、组织/部门、成员、来源及外部群 ID，避免将同名群当作同一个群。' },
      { text: '点击“新增”或行内“编辑”，确认群范围和成员后保存；操作只作用于 Hub 中的登记关系。', roles: managers },
      { text: '接入 IM 群时，先把机器人加入群并让成员 @ 机器人发送文本，再在“已发现群 / 待绑定”或“群聊发送者待接入”处理。', roles: superAdmin },
      { text: '完成身份、群登记和成员确认后，配置资源授权，再让成员重新发送消息。', roles: superAdmin },
      { text: '没有管理操作时，请联系管理员完成群登记、成员确认及能力授权。', roles: ['member'] },
    ],
    notes: [
      { text: '登记群或加入成员不会自动授予 Skill/MCP；群聊仍检查发送者和群两侧的授权交集。' },
      { text: '当前列表和已发现群不代表平台所有群或全租户通讯录；未收到机器人事件的群可能不会出现。' },
      { text: '从工作台移除群不会删除外部飞书/钉钉群或清空平台聊天；有任务或待发送回复时可能不能编辑。', roles: managers },
    ],
    questions: [
      { question: '个人私聊能用 Skill，群里却不能用？', answer: '检查群侧是否也获得该资源、资源范围是否覆盖群、发送者是否属于群范围且是群成员。' },
      { question: '群接入后为什么旧消息没执行？', answer: '接入前的消息不会回放。身份和授权完成后必须重新发送；飞书群聊只处理 @ 机器人的消息。' },
    ],
  },
  resources: {
    purpose: '查看或管理 Skill 与 MCP。Skill 提供任务指导，MCP 提供外部工具，二者仍需明确授权才能使用。',
    steps: [
      { text: '查看资源名称、类型、启用状态和适用范围，确认是否符合你的任务需要。' },
      { text: '点击“新增 Skill / MCP”。Skill 填名称（可用中英文双语）和完整 Markdown，可从 .md/.txt 文件导入，无需另填用途说明。', roles: resourceAdmins },
      { text: '新增 MCP 时填写公网 HTTPS 服务地址；若服务要求认证，将密钥填入请求头，不放进聊天或地址。', roles: resourceAdmins },
      { text: '选好组织/部门范围，按需勾选整体授权；否则保存后去“绑定与授权”分配。使用“编辑”抽屉更新现有资源。', roles: resourceAdmins },
      { text: '在可发送的智能会话或已接入 IM 中描述任务，让 Agent 使用已授权资源；不能管理的资源无需自己配置。', roles: readers },
    ],
    notes: [
      { text: '“适用范围”只是可以授权给谁，不等于已经授予使用权；停用的资源不会参与执行。' },
      { text: 'Skill 可用与原文可见是两种权限：仅有资源管理权限的管理员能查看原文。隐藏原文不能保证模型绝不复述。' },
      { text: '导入文件最多 256KB；完整 Skill 含系统补齐头部最多 64000 字符。有效头部和额外元数据会保留。', roles: resourceAdmins },
      { text: 'MCP 仅允许公网 HTTPS 443；DNS 解析到内网或代理 fake-IP 也会被拒绝。已保存的请求头密钥不会回显。', roles: resourceAdmins },
      { text: '删除资源会撤销其所有相关授权，不能从界面恢复；保存或删除前先确认影响范围。', roles: resourceAdmins },
    ],
    questions: [
      { question: '为什么页面有 Skill，Agent 却说没有？', answer: '可管理或可见不代表当前会话已获授权。请核对身份、启用状态和用户/群授权；IM 中可用 /skills 查看服务端核验的名称。' },
      { question: '填了 https:// 为什么 MCP 仍报错？', answer: '还会检查端口、地址格式及 DNS 是否全部解析到公网。按具体错误排查，HTTPS 前缀本身并不保证能访问。', roles: resourceAdmins },
      { question: '为什么看不到“查看技能内容”？', answer: '使用者可以调用能力，但原文仅向有资源管理权限的人开放；这不是内容被清空。' },
    ],
  },
  bindings: {
    purpose: '查看资源授权，并在管理范围内分配或调整用户、群组、部门和组织的 Skill/MCP 使用权。',
    steps: [
      { text: '用“搜索授权”和“筛选对象类型”找到对象，核对对应资源，避免同名误选。' },
      { text: '点击“新增授权”，选择对象类型、具体对象和 Skill/MCP；搜索框只筛选选项，不会自动改变选择。', roles: resourceAdmins },
      { text: '点击行内“编辑授权”修改组合；有有效变更才可保存，失败时保留原授权和输入。', roles: resourceAdmins },
      { text: '保存后点“查看生效结果”，或点“查看生效权限”，选择具体用户与私聊/群聊范围核验。', roles: resourceAdmins },
      { text: '如需授权或调整权限，请将目标会话和所需资源告知管理员；不要通过复制 Skill 原文绕过授权。', roles: readers },
    ],
    notes: [
      { text: '部门/组织授权会覆盖范围内的成员和群；群聊仍取发送者与群两侧授权交集。' },
      { text: '撤销一条授权不一定使资源完全不可用，用户或群还可能通过部门/组织等其他来源获得授权。' },
      { text: '预览只读，仅覆盖你有资源管理权限的资源，且基于已保存授权；它不检查模型额度或远端工具网络。', roles: resourceAdmins },
      { text: '组织/部门授权需选具体成员预览，不能用一个成员的结果代表所有人。刷新失败时可能展示标注为旧的数据。', roles: resourceAdmins },
    ],
    questions: [
      { question: '为什么给群授权后仍不可用？', answer: '发送者本人也必须获得该资源，且符合群成员关系及资源作用域。只有群侧授权还不够。' },
      { question: '预览显示“用户侧未授权”或“群侧未授权”怎么办？', answer: '核对对应一侧的直接、部门或组织授权；仅在明确业务需要且权限允许时添加缺失授权。', roles: resourceAdmins },
      { question: '为什么不能保存重复组合？', answer: '相同对象与资源的授权只能存在一条。搜索已有授权后编辑即可，不需要重复新增。', roles: resourceAdmins },
    ],
  },
  integrations: {
    sections: imHelpSections,
    purpose: '连接飞书/钉钉并核对外部身份与内部用户的关系。平台配置、员工接入和本人文档授权相互独立。',
    steps: [
      { text: '先查看“服务连接状态”；配置机器人应用和接入模式后刷新状态，再进行实际收发验收。', roles: superAdmin },
      { text: '让员工私聊机器人，在“待接入发现”刷新并处理：新建仅 IM 成员，或明确绑定已有内部用户。', roles: superAdmin },
      { text: '需要群接入时转到“协作群组”；身份确认后仍须单独授予 Skill/MCP，并让员工重新发消息。', roles: superAdmin },
      { text: '需要本人飞书/钉钉文档能力时，核对个人 OAuth 应用及可用人员；普通聊天不等于已经获得文档权限。', roles: superAdmin },
      { text: '核对当前可见的 IM 身份绑定；需要接入或变更绑定时联系超级管理员。', roles: ['org_admin', 'team_lead', 'member'] },
      { text: '涉及本人文档时，在智能会话的“本人平台授权”中按提示连接，或在 IM 私聊发起对应平台授权。', roles: ['org_admin', 'team_lead', 'member'] },
    ],
    notes: [
      { text: '“已连接”只表示连接进程已接通，不代表平台权限、消息收发和文档操作均已验证。' },
      { text: '同名飞书用户与网页管理员不会自动合并。权限按实际绑定的内部用户、组织和部门判定。' },
      { text: '保存应用配置会影响当前平台接入；密钥留空保留原值，勾选清除会删除已有值，缺少必填凭据可能停止接入。', roles: superAdmin },
      { text: '自动接入不会自动授予技能；启用前确认平台侧人员范围，并选择目标组织、部门和限额。', roles: superAdmin },
      { text: '不要在聊天中发送 App Secret、API Key、登录令牌或一次性授权码；群聊中也不要转发个人授权材料。' },
    ],
    questions: [
      { question: '为什么机器人要求“飞书个人授权”？', answer: '通常是为了访问本人飞书文档，不是要求登录 Codex/ChatGPT；请看清提示的平台及授权用途。' },
      { question: '管理员在网页有权限，飞书里为什么没有？', answer: '先确认该飞书身份是否明确绑定到同一个内部管理员账号；仅同名不能证明身份相同，也不会继承权限。' },
      { question: '为什么待接入发现为空？', answer: '先确认员工已向当前机器人发消息、连接正常，再刷新。该列表只包含系统实际收到并记录的发现，不是全平台通讯录。', roles: superAdmin },
    ],
  },
  audit: {
    purpose: '查看当前权限范围内的操作记录，用于核对操作人、对象和时间，辅助追溯变更。',
    steps: [
      { text: '选择每页 50 或 100 条，使用上一页、下一页，或输入页码后点击“跳转”。' },
      { text: '按时间、操作、操作人、目标 ID 与详情核对事件；记录 ID 可用于向维护人员定位问题。' },
      { text: '点击“刷新”获取当前页最新数据；加载失败时用“重试”，不要把报错视为没有记录。' },
    ],
    notes: [
      { text: '审计范围由角色和可管理用户决定，不一定显示其他组织或全部系统记录。' },
      { text: '审计记录用于说明发生过什么，不是修改、撤销或重新执行操作的入口。' },
      { text: '页面保留本账号的分页选择；查看其他身份的记录不能绕过服务端权限限制。' },
    ],
    questions: [
      { question: '为什么有些操作看不到？', answer: '可能不在当前权限范围，或当前页尚未刷新；核对账号、时间和页码后再向管理员反馈。' },
      { question: '详情能当成执行成功的证明吗？', answer: '应结合具体事件、任务状态与外部结果判断，单条审计事件不代表整条业务流程都成功。' },
    ],
  },
  settings: {
    purpose: '修改自己的显示资料，并查看或管理本人 ChatGPT 连接及个人模型选择。',
    steps: [
      { text: '填写名字，或选择 PNG、JPG、WebP 头像；头像自动裁成正方形，确认后点击“保存”。' },
      { text: '查看“本人 ChatGPT 授权”的凭据来源。系统 API 或明确的共享登录模式下，无需把它再次配置成个人授权。' },
      { text: '个人 OAuth 模式下点击“登录 ChatGPT 授权”，在官方页面输入一次性代码，由本人确认并等待状态更新。' },
      { text: '连接成功后可刷新模型列表，从账号实际返回的模型中选择并“保存模型”；随后重新发送任务。' },
    ],
    notes: [
      { text: '登录邮箱和角色在这里只读；没有变更时资料保存按钮不可用。' },
      { text: '个人连接仅供本人任务使用；授权不保证订阅、额度或所有模型可用。系统 API 优先，不会在失败后自动切换他人账号。' },
      { text: '断开只清除 Hub 中的本人连接，不会注销所有 OpenAI 设备。没有提供 API Key 时，错误的第三方端点配置也不会转为个人登录。' },
      { text: '模型列表查询与本人任务共用凭据锁，忙碌时稍后重试；不要在聊天或截图中公开一次性代码。' },
    ],
    questions: [
      { question: '为什么没有“登录 ChatGPT 授权”按钮？', answer: '可能正在使用系统 API 或共享模式，或 Codex 尚未启用/配置不可用。查看当前状态，而不是删除他人的配置。' },
      { question: '为什么模型下拉列表为空？', answer: '先确认本人连接成功并点击“刷新模型列表”。只显示实际返回的可用模型；“Codex 默认模型”表示交给 CLI 选择，不是虚构的模型列表。' },
    ],
  },
  login: {
    purpose: '使用管理员开通的网页账号进入工作空间。这里的登录与 ChatGPT、飞书或钉钉个人授权不同。',
    steps: [
      { text: '输入完整登录邮箱和密码，点击“登录工作空间”。' },
      { text: '如果提示服务连接异常，先确认当前地址和服务可用，再用页面上的重新检查入口重试。' },
      { text: '没有网页账号时联系管理员；仅通过飞书/钉钉接入的成员不一定拥有网页登录账号。' },
    ],
    notes: [
      { text: '本系统没有默认通用管理员密码，也没有匿名直接重置密码入口。' },
      { text: '用户名、头像或 IM 昵称不是登录邮箱。请勿在聊天中发送密码、令牌或服务密钥。' },
    ],
    questions: [
      { question: '忘记密码怎么办？', answer: '联系有权管理员为下级网页账号重置；管理员自己遗忘密码时，需要服务器维护人员恢复。' },
      { question: '为什么密码正确仍无法进入？', answer: '可能账号已停用或尝试次数触发限制。按页面提示处理，无法确认时联系管理员，不要连续反复提交。' },
    ],
  },
  setup: {
    purpose: '为尚未初始化的工作空间创建首个超级管理员。该流程只用于首次设置，不是公开员工注册。',
    steps: [
      { text: '填写显示姓名和有效邮箱，设置至少 12 位密码，并再次输入确认。' },
      { text: '点击“创建管理员”，等待成功提示后，用刚设置的邮箱和密码登录。' },
      { text: '安全保存管理员凭据；后续成员账号和接入由管理员按权限管理。' },
    ],
    notes: [
      { text: '初始化成功后该入口关闭；如果提示已初始化，请返回登录，不要重复创建。' },
      { text: '姓名可以相同，但它不能代替邮箱和密码进行认证；请勿使用公开示例密码。' },
    ],
    questions: [
      { question: '初始化失败能否直接继续登录？', answer: '只有收到成功提示或明确确认已存在账号后才能使用相应凭据登录；网络失败时先核对状态。' },
      { question: '两次密码不一致怎么办？', answer: '重新输入相同的强密码，再提交；确认前不会成功创建管理员。' },
    ],
  },
  connection: {
    purpose: '当前尚未确认工作空间的初始化状态，先恢复与服务的连接，避免误开注册或登录流程。',
    steps: [
      { text: '查看页面显示的错误，确认访问地址和网络是否正确。' },
      { text: '点击“重新检查服务连接”；检查成功后，系统会显示登录或初始化页面。' },
      { text: '持续失败时把错误文字和发生时间提供给维护人员，不要附带密码或密钥。' },
    ],
    notes: [
      { text: '连接失败不代表系统未初始化，不要清空数据库或重新创建账号来尝试修复。' },
      { text: '维护人员需检查应用、数据库、反向代理和来源配置；此浮窗不会代替你修改服务设置。' },
    ],
    questions: [
      { question: '为什么暂时不显示注册按钮？', answer: '初始化状态未确认时会保持关闭，防止已有工作空间被误当成新系统。' },
      { question: '刷新页面能解决所有连接错误吗？', answer: '不能。服务停止或代理配置错误需要维护人员处理；反复刷新不会恢复服务。' },
    ],
  },
};

export function getPageHelp(page: HelpPage, role?: Role): Guide {
  if ((page === 'directory' && !resourceAdmins.includes(role as Role)) || (page === 'audit' && !managers.includes(role as Role))) {
    return {
      purpose: '当前角色无权使用此管理页面。',
      steps: [{ text: '返回可访问页面，或联系管理员核对角色。' }],
      notes: [{ text: '使用说明不会授予页面或数据访问权限。' }],
      questions: [{ question: '为什么不能访问？', answer: '此页面有角色限制，以服务端权限和当前登录账号为准。' }],
    };
  }
  const guide = guides[page];
  const allowed = (item: { roles?: readonly Role[] }) => !item.roles || (!!role && item.roles.includes(role));
  return { purpose: guide.purpose, steps: guide.steps.filter(allowed), notes: guide.notes.filter(allowed), questions: guide.questions.filter(allowed), sections: guide.sections?.filter(allowed) };
}
