# Agent Hub 前端

React + TypeScript + Vite 中文企业工作台，接口以 `../CONTRACT.md` 为准。

```sh
# 从仓库根目录运行
cd frontend
npm install
npm run dev
```

开发地址 `http://127.0.0.1:18201`，Vite 将 `/api` 代理至 `http://127.0.0.1:18200`。后端开发环境需将 `APP_ORIGIN` 配置为浏览器实际使用的 `http://127.0.0.1:18201`，否则写请求的 Origin 校验会拒绝。前端不会重写 Origin 或绕过校验。登录使用管理员创建的账号，Cookie 请求固定 `credentials: same-origin`。

```sh
npm run typecheck
npm run build
```

生产构建位于 `dist/`。由现有服务器托管静态文件，并将同源 `/api` 转发至后端；生产 `APP_ORIGIN` 必须与公网来源一致。仅构建不会自动部署。`npm run preview` 仅检查静态构建，默认没有 API 代理。

页面包含登录、概览、聊天与任务轮询、用户角色、群组成员、Skill/MCP 资源、授权、IM 身份及状态、审计。所有数据由实际 API 提供，没有模拟消息或业务数据。监管私聊和非成员群聊禁用发送，后端 403 同样展示并阻止继续代发。资源内容以纯文本展示，MCP Headers 仅在创建表单提交，不保存至本地存储。

契约暂未提供历史未完成 Run 列表接口，因此同一聊天组件内切换会话可恢复本次发送的轮询；刷新浏览器后可重新加载消息，但无法重新发现运行中的任务 ID。
