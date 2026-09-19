---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: '97044efe-4586-4011-a31b-44af2eb7d2ac'
  PropagateID: '97044efe-4586-4011-a31b-44af2eb7d2ac'
  ReservedCode1: '904af496-9fc2-4e26-96e2-cd60fa8b697a'
  ReservedCode2: '904af496-9fc2-4e26-96e2-cd60fa8b697a'
---

# 容器守望者

一体化 Docker 容器更新守望平台：融合 [Tugtainer](https://github.com/Quenary/tugtainer)（自动更新执行引擎）与 [Vigil](https://github.com/jingyuan9527/vigil)（双模式检测与两级通知）的设计语义，全新实现。

> 本仓库为 M1-Lite 落地版本（对应《容器守望者产品需求方案》P0 范围 + Compose 专题 partial 策略）。

## 功能（已实现并验证）

- **双模式检测引擎**：Digest-Only（浮动 tag 追摘要）/ Pin-Watch（版本 tag 追新版本号 tag），首巡基线不告警，Registry 原生 API（HEAD manifest / If-None-Match 304 / Bearer token 流 / insecure registry）
- **两级通知**：update（有新版本，强提醒）/ new-tag（可选更新，弱提醒），防抖 + 目标达成自动已读 + 已读裁剪
- **项目聚合通知**：同 compose 项目同批次发现聚合为一条，两级分开聚合互不吞没
- **Compose 更新引擎（PRD 5.2 partial 策略）**：label 反推项目（无需 compose 文件）、依赖拓扑排序（先停依赖方、先起被依赖方）、受影响容器停止后重启重连、失败传播（上游失败下游跳过且保持运行）、**健康门控失败自动回滚（旧镜像 ID 定向重建）**
- **手动版本回退**：版本台账记录每个容器运行过的镜像（基线/更新/回退），卡片菜单打开版本列表弹窗，任选历史版本回退（默认选中上一版）；回退后**自动关闭该容器的自动更新开关**，由用户决定何时恢复（重新打开「自动」或手动更新）
- **后台自动调度**：设置页配置检测间隔（秒级热生效，无需重启），到点自动扫描 → 候选就绪即自动执行更新（发布延迟/合并窗口/freeze/ignored 全部生效），任务完成自动通知（trigger=auto）；与手动操作互斥
- **通知渠道**：Webhook（JSON）/ 钉钉 / 飞书 / 企业微信（markdown），单渠道连通性测试
- **认证安全**：bcrypt + JWT httpOnly Cookie（SameSite=Lax）、登录限流（5 次失败锁 15 分钟）、首次部署引导初始化
- **卡片式响应式 Web UI**：Bento 指标卡（点击即筛选）、容器卡片流、状态徽章、暗色模式、移动端底部导航
- **远端镜像监控（哨兵模式）**：无需本机运行/装 Docker，直接盯住任意 registry 引用；上游摘要变化 → update 通知，pin-watch 新版本 tag → new-tag 通知（首检基线不告警、同摘要/同 tag 去重）；页面展示生效模式、上游状态（基线一致/上游已更新）、可用新版本列表、最近检查时间
- **演示模式**：无 Docker 环境也能完整体验（内置 compose 三容器场景 + 模拟发布 + 模拟失败回滚剧本）

## 快速开始

```bash
# 演示模式（推荐首次体验，内置 admin/admin123）
# Windows PowerShell
$env:VT_DEMO_MODE='1'; python run.py
# Linux/macOS
VT_DEMO_MODE=1 python run.py
# 浏览器打开 http://127.0.0.1:9412

# 正常模式（自动探测 Docker CLI；无引擎时自动降级 Mock 并在界面标注）
python run.py
```

首次部署：浏览器打开后按引导设置管理员密码（或 `VT_DEMO_ADMIN_PASSWORD` 预置）。

## Docker 部署

镜像已发布至 Docker Hub：[learycn/containerup](https://hub.docker.com/r/learycn/containerup)，由 GitHub Actions 自动构建推送：推送 `main` → 重建 `latest`；推送 `v*` 版本 tag（如 `v1.1.5`）→ 生成 `1.1.5` / `1.1` / `latest` 三个镜像标签。

```bash
docker run -d \
  --name containerup \
  -p 9412:9412 \
  -v vigiltainer-data:/data \
  -v /var/run/docker.sock:/var/run/docker.sock \
  --restart unless-stopped \
  learycn/containerup:latest
```

或使用 docker compose：

```yaml
services:
  containerup:
    image: learycn/containerup:latest
    pull_policy: always
    container_name: containerup
    ports:
      - "9412:9412"
    volumes:
      - vigiltainer-data:/data
      - /var/run/docker.sock:/var/run/docker.sock
    restart: unless-stopped

volumes:
  vigiltainer-data:
```

> `pull_policy: always` 防止 `latest` 被本地旧缓存卡住（Docker 不会自动重拉已存在的 `docker compose up`）；旧版部署请先 `docker compose pull && docker compose up -d`。

- 镜像内置 Docker CLI（官方静态客户端）；运行时挂载 `/var/run/docker.sock` 后平台直接管理宿主机引擎，访问 `/api/public/health` 看到 `"docker_impl":"local"` 即接入成功（CI 已在真实引擎上冒烟验证该路径）；未挂载 sock 时自动降级为无引擎模式并在界面标注
- 数据库与 JWT 密钥均持久化于 `/data` 卷，升级镜像不丢配置；容器内置 HEALTHCHECK（探测 `/api/public/health`）
- 演示模式：追加环境变量 `VT_DEMO_MODE=1`（即使挂载了 sock 也优先走内存剧本，适合无侵入体验）
- 安全提示：`docker.sock` 等同宿主机 Docker 的 root 级权限，请仅将服务暴露给可信网络

## 自动更新什么时候执行？

四种触发路径（对应 PRD 5.3）：

1. **周期自动**（后台调度器）：设置页「自动检测间隔」> 0 时，激活/改配置后**立即执行一轮**，随后每 N 秒一轮：扫描 → 发现更新 → 候选就绪（通过发布延迟/合并窗口、未 freeze/ignored）→ 自动执行更新 → 任务通知（trigger=auto）。配置改动秒级热生效；`0 = 仅手动`（默认）。
2. **手动全量**：仪表盘「更新全部可用」→ 跳过延迟/合并窗口（用户明确意志），仍按拓扑序执行。
3. **手动单容器**：容器卡片「更新」按钮。
4. **手动扫描**：仅检测+通知，不执行更新。

保护语义（所有路径共用）：自动更新**永不更换版本号 tag**（大版本升级必须人工）；protected 标签容器永不参与；全局互斥保证同一时刻仅一个更新任务（忙时立即让行不排队）。

## 验证与复现

```bash
# 单元 + 集成测试（88 项：检测分流/拓扑计划/回滚/版本回退/防抖/聚合/限流/API 全链路/调度器热生效/watch 哨兵通知）
python -m pytest tests/ -q

# L0 端到端验证（真实 uvicorn HTTP + 真实 socket 驱动 21 项断言）
python verify_e2e.py
```

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `VT_PORT` | `9412` | 服务端口 |
| `VT_DB_PATH` | 项目目录 `vigiltainer.db` | SQLite 数据库 |
| `VT_JWT_SECRET`（支持 `_FILE`） | 自动生成持久化 | JWT 签名密钥 |
| `VT_DEMO_MODE` | `false` | 演示模式（内存 Mock + 种子场景） |
| `VT_LOGIN_MAX_FAILS` / `VT_LOGIN_LOCK_SEC` | `5` / `900` | 登录限流 |
| `VT_DELAY_UPDATE_SEC` | `0` | 全局发布延迟（新镜像观察期） |
| `VT_SCAN_INTERVAL_SEC` | `0` | 自动检测间隔基线（设置页可覆盖，热生效；0=仅手动） |
| `VT_MERGE_WAIT_SEC` | `0` | 项目合并等待窗口（消除组内异步更新中间态） |
| `VT_REGISTRY_MIRROR` | 空 | 注册表镜像主机 |
| `VT_INSECURE_REGISTRIES` | 空 | http registry 白名单（逗号分隔） |

## 目录结构

```
containerup/
├── backend/
│   ├── config.py      # 配置（env/_FILE secrets）
│   ├── db.py          # SQLite（WAL + 线程锁）
│   ├── registry.py    # 检测引擎：双模式 + Registry 协议栈
│   ├── docker.py      # DockerClient 抽象：Local(CLI) / Mock(内存)
│   ├── engine.py      # 更新引擎：compose 组 + 拓扑 + 回滚状态机 + 版本台账/手动回退
│   ├── detect.py      # 扫描编排：采集→检测→防抖→聚合→自动已读
│   ├── scheduler.py   # 后台调度器：周期扫描→自动更新（热生效/互斥/状态上报）
│   ├── notify.py      # 通知中心：两级通知 + 渠道投递
│   ├── auth.py        # bcrypt + JWT + 登录限流
│   └── app.py         # FastAPI 全量 REST API
├── frontend/          # Vue 3 + Vite + Tailwind CSS（现代组件化前端）
│   ├── src/components/  # StatCard / ContainerCard / AppNav(移动抽屉) / LogPanel(底部折叠日志) / Toast / Modal / Icon
│   ├── src/views/       # Dashboard / Containers / Notifications / Watches / Settings(含修改密码)
│   └── dist/            # 构建产物（后端直接托管）
├── tests/             # pytest 测试套件（88 项）
├── verify_e2e.py      # L0 端到端验证脚本（21 项断言）
└── run.py             # 启动入口
```

### 前端开发

```bash
cd frontend
npm install        # 首次
npm run dev        # 开发（Vite 热更新，/api 代理到 9412）
npm run build      # 构建产物到 frontend/dist（后端自动托管）
```

## 已知限制（诚实披露）

1. **真实引擎验证范围有限**：CI 已在真实 Docker 引擎上完成 Local 引擎接入冒烟（sock 挂载 + CLI 探测 + 容器列表可见，`docker_impl=local`）；但**更新/重建/回滚等写操作未经真实引擎回归**（开发与 CI 均未执行真实更新），容器重建命令为简化参数集（env/cmd/restart/labels），复杂容器（挂载、网络别名、自定义端口映射）重建保真度有限，真实联调时需补齐 inspect→create 的全量配置映射——首次在真实环境部署时请先用非关键容器试点。
2. M1-Lite 未含：多主机 Agent、Hooks、健康监控独立任务、prune、OIDC、Apprise 扩展渠道（见 PRD M2 范围）。
3. 演示模式的 registry 状态为内存静态源，服务重启后回到初始摘要（持久化仅 DB 侧）；生产模式无此问题。

> AI生成