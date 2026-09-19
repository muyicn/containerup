
# 容器守望者 (ContainerUp)

<p align="center">
  <strong>现代化轻量级 Docker 容器更新、检测、通知与安全回滚守望平台</strong><br>
  专为 NAS（群晖 Synology DSM、极空间、威联通 QNAP、飞牛 fnOS）及 Homelab 极客环境深度优化设计。
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Docker-learycn%2Fcontainerup-blue?logo=docker" alt="Docker Image" />
  <img src="https://img.shields.io/badge/Version-v1.1.5-emerald" alt="Version" />
  <img src="https://img.shields.io/badge/Tests-131%20passed-brightgreen" alt="Tests" />
  <img src="https://img.shields.io/badge/License-MIT-slate" alt="License" />
</p>

---


## 📖 平台简介

**ContainerUp（容器守望者）** 融合了自动更新执行引擎与双模式版本检测机制。不同于传统的 Watchtower 等简单粗暴的更新工具，ContainerUp 聚焦于**高保真配置重建**、**拓扑依赖感知**、**智能健康门控**与**秒级自动/手动回滚**，保证您在家用 NAS 或私有服务器上的关键容器永远稳定可用。

<img width="1862" height="1099" alt="Image" src="https://github.com/user-attachments/assets/920f42db-cf6d-4c7b-9666-bf0683344d13" />
---

## 🌟 核心功能特性清单

### 1. 🚀 智能更新引擎 (Update Engine)
- **Compose 项目拓扑感知**：自动读取容器 Compose 元数据与 `depends_on` 依赖关系，按依赖树精准确定重建顺序（先停依赖者，先起被依赖者）。
- **群晖 DSM 与 NAS 降级高保真重建**：
  - 针对群晖 Container Manager 屏蔽 compose 物理文件路径的场景，自动降级为全量运行时配置反解；
  - 完整继承端口映射（PortBindings）、卷与路径挂载（Binds/Mounts）、网络模式与网络别名（network_aliases）、静态 IP（IPAMConfig）、环境变量（Env）、重启策略（RestartPolicy）、额外 Hosts（ExtraHosts）及非镜像自定义标签。
- **预拉取校验与多级镜像名自愈 (Canonical Resolution)**：
  - 严格防御裸 `sha256:` 镜像哈希拉取，自动从持久化标签、宿主机 RepoTags、本地版本台账中反解标准 `repo:tag` 镜像引用，杜绝 `pull access denied for sha256` 异常。
- **停机状态保护 (Stop Preservation)**：
  - 处于关停（Stopped/Exited）状态的容器更新后依然保持关停，绝不会擅自开机唤醒，充分尊重用户意志。
- **全局并发互斥锁**：
  - 自动调度与手动触发互斥运行，忙时自动让行，避免任务积压与冲突。

### 2. 🛡️ 健康门控与秒级自动回滚 (Healthcheck & Rollback)
- **智能健康门控**：
  - 容器更新启动后自动监测健康状态；
  - 针对大型服务提供 `starting` 启动宽限期，避免慢启动容器被误判为失败。
- **零宕机风险自动回滚**：
  - 更新后容器若崩溃、非零退出或健康检测失败，更新引擎自动拉起旧版镜像进行定向回滚重建，确保业务零中断。
- **历史版本台账与任意版本回退**：
  - 数据库自动沉淀历史运行镜像摘要与语义化版本号；
  - 提供直观的可视化弹窗，随时自由选择回退到任意历史版本；
  - 回退成功后**自动关闭自动更新开关**，防止被后续自动巡检无脑覆盖。

### 3. 🔍 原生双模式镜像检测 (Detection Engine)
- **Auto / Digest-Only 模式**：
  - 追踪浮动 tag（如 `latest`）的 Manifest Digest 变化，上游构建发布新镜像即刻感知。
- **Pin-Watch 模式**：
  - 针对固定版本号 tag（如 `v1.2.0`、`1.25-alpine`），仅在仓库发布同系列更高 semver tag 时进行弱提醒，**坚决不越级跨版本自动更新**，保障大版本升级的绝对安全。
- **首巡基线防骚扰 (Baseline)**：
  - 首次扫描仅建立基线快照，不产生误报告警。
- **语义化真实版本号反解**：
  - 镜像即使使用 `latest` 标签，也能结合 OCI Labels 或远端 Tags 映射反解出形如 `v0.7.21` 的真实版本号。
- **远端镜像哨兵 (Remote Watches)**：
  - 无需在本地部署或运行容器，直接添加任意远端镜像引用即可实时跟踪更新动态。

### 4. 📢 两级通知中心与项目级聚合 (Notification Center)
- **强弱两级通知分流**：
  - `update` 强提醒：针对当前运行镜像摘要变化，提示立即更新；
  - `new-tag` 弱提醒：针对 Pin-Watch 发现的上游更高版本，提示可选升级。
- **Compose 项目级聚合通知**：
  - 同一 Compose 堆栈内多个容器同时有更新时，自动合并为一条项目卡片通知，告别通知轰炸。
- **智能防抖与目标达成自动已读**：
  - 相同摘要与 Tag 重复扫描不重发；容器成功更新到最新版本后，相关未读通知自动沉底标记为已读。
- **多渠道即时推送**：
  - 原生支持 **企业微信 (WeCom)**、**钉钉 (DingTalk)**、**飞书 (Feishu)**、通用 **Webhook (JSON)**，支持一键发送测试消息验证连通性。

### 5. 🎛️ 容器生命周期与完全隔离管理 (Isolation & Lifecycle)
- **完全绝缘隔离 (`managed=false`)**：
  - 容器添加标签 `dev.containerup.managed=false`，ContainerUp 扫描阶段完全忽略，不入库、不展示、不打扰。
- **受保护模式 (`protected=true`)**：
  - 容器添加标签 `dev.quenary.tugtainer.protected=true` 或在面板中开启，仅通知检测结果，严禁任何自动或手动更新操作。
- **界面一键忽略与移除记录**：
  - 支持将容器一键移入“已忽略”列表；
  - 针对宿主机上已物理删除的容器，巡检时**自动清除幽灵记录**，同时提供卡片菜单 **「移除监控记录」** 供随时手动下线。

### 6. ⏱️ 后台自动调度器 (Scheduler)
- **配置秒级热生效**：
  - 在前端设置页调整自动扫描周期（秒），无需重启容器，后台调度线程实时热加载。
- **发布延迟与合并等待窗口**：
  - `delay_update_sec`：支持为新发布的镜像设置冷静观察期，规避上游翻车；
  - `merge_wait_sec`：支持 Compose 组内合并等待窗口，消除组内镜像构建时间差。

### 7. 💻 现代化响应式 Web 控制台
- **Bento 风格仪表盘**：指标卡片直观汇总“监控总数”、“已是最新”、“有更新可用”、“已忽略”，点击即完成联动筛选。
- **灵活卡片流**：直观展示容器运行 tag、真实解析版本、最新版本、更新差异、生效策略及更新日志。
- **明暗主题无缝切换**：全套深色/浅色模式适配，移动端专属抽屉与底部导航，随时随地手机查阅与操作。

### 8. 🔒 企业级安全与演示沙盒
- **安全认证**：bcrypt 强密码哈希存储、基于 HttpOnly Cookie 的 JWT 认证、防暴力破解保护（连续 5 次失败自动封禁 15 分钟）。
- **演示模式 (Demo Mode)**：内置内存虚拟 Docker 引擎与多容器拓扑场景，无需真实 Docker 环境即可完整体验检测、更新、模拟故障与回滚全流程。

---

## 🏷️ 容器控制标签指南 (Labels)

在 `docker-compose.yml` 或 `docker run --label` 中配置以下标签，即可精准控制 ContainerUp 的托管行为：

| 标签 (Label) | 可选值 | 说明 |
| :--- | :--- | :--- |
| `dev.containerup.managed` | `false` | **完全绝缘**：ContainerUp 彻底忽略该容器，不入库、不检测、不更新。 |
| `dev.quenary.tugtainer.protected` | `true` | **受保护容器**：仅检测并发送更新通知，更新引擎硬编码拒绝执行任何更新。 |
| `dev.quenary.tugtainer.depends_on` | `容器名` | 自定义跨容器依赖顺序（支持逗号分隔多个容器）。 |
| `dev.containerup.canonical_image` | `repo:tag` | 手动显式指定规范镜像引用（通常系统会自动自愈识别，无需手动配置）。 |

---

## 🐳 部署与运行

### 方式 A：Docker CLI 单命令运行（推荐）

```bash
docker run -d \
  --name containerup \
  -p 9412:9412 \
  -v containerup-data:/data \
  -v /var/run/docker.sock:/var/run/docker.sock \
  --restart unless-stopped \
  learycn/containerup:latest
```

### 方式 B：Docker Compose 部署

```yaml
services:
  containerup:
    image: learycn/containerup:latest
    pull_policy: always
    container_name: containerup
    ports:
      - "9412:9412"
    volumes:
      - containerup-data:/data
      - /var/run/docker.sock:/var/run/docker.sock
    restart: unless-stopped

volumes:
  containerup-data:
```

> 💡 **提示**：
> 1. 初次启动后在浏览器中访问 `http://<NAS的IP>:9412`，按引导设置管理员密码即可开始使用。
> 2. 数据与密钥均持久化于 `/data` 卷中，更新 ContainerUp 镜像不会丢失任何配置与历史记录。

---

## ⚙️ 环境变量说明

| 环境变量 | 默认值 | 作用说明 |
| :--- | :--- | :--- |
| `VT_PORT` | `9412` | Web 界面与 API 监听端口 |
| `VT_DB_PATH` | `/data/vigiltainer.db` | SQLite 数据库文件路径 |
| `VT_JWT_SECRET` | 随机生成 | JWT 签名密钥（首次运行自动生成并保存在数据目录） |
| `VT_DEMO_MODE` | `false` | 是否开启无侵入演示沙箱模式（`true`/`false`） |
| `VT_SCAN_INTERVAL_SEC` | `0` | 后台自动检测巡检间隔（秒，可在设置页随时热修改，`0` 为仅手动） |
| `VT_DELAY_UPDATE_SEC` | `0` | 全局发布延迟（秒，新镜像发布后需等待冷静期才触发更新） |
| `VT_MERGE_WAIT_SEC` | `0` | Compose 项目合并等待窗口（秒） |
| `VT_REGISTRY_MIRROR` | 空 | Docker Registry 加速代理镜像源地址 |
| `VT_INSECURE_REGISTRIES`| 空 | 允许以 HTTP 协议访问的自建私有 Registry 白名单（逗号分隔） |

---

## 🧪 自动化测试与工程质量

ContainerUp 拥有严密的自动化测试工程体系，覆盖全链路核心逻辑：

```bash
# 运行完整测试套件（131 项单元测试与集成测试全量通过）
pytest -v
```

测试矩阵涵盖：
- Registry 协议栈认证与 Bearer Token 交互
- 裸 sha256 镜像 ID 拦截与多层级规范恢复自愈
- 容器运行时配置高保真重建（端口/卷/网络/别名/环境变量）
- Compose 拓扑排序、上游故障传播与降级保护
- 慢启动宽限期与健康检测失败秒级回滚机制
- 幽灵容器自动同步清除与监控记录维护
- 通知两级分流、项目聚合与目标达成自动已读
