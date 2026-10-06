# 野外实验站 · 离线记录页（纪元迁移版）

离线记录页的索引结构升级方案：以**纪元（epoch）**为单位组织记录，迁移先把旧数据
复制到**候选纪元**并校验，再**原子发布**新纪元。停留在旧页面的录入员的迟到保存
在发布前后一律被拒绝并提示重新载入；重开后只能读到新纪元的完整记录。

仅依赖 Python 3 标准库（sqlite3 / http.server），适配离线环境。

## 运行

```bash
# 编排环境（推荐）
docker compose up --build app          # 记录页服务，http://localhost:8000

# 本地（无 Docker）
make local                             # 或 DB_PATH=./data/app.db python3 -m app.server
```

打开两个浏览器标签页即两个"页面"，各自持有纪元围栏；页面展示**当前纪元、
迁移阶段、持有围栏的页面及各页面失效状态**。

## 验收（verify 服务）

```bash
make verify          # = docker compose up --build --exit-code-from verify --abort-on-container-exit
# 或
sh scripts/verify.sh
```

`verify` 服务在编排网络内穿插执行：构建检查（编译/导入/静态资源）→ 单元测试 →
健康与页面 HTTP 冒烟 → API 冒烟 → 重复正文场景（两条正文相同+一条不同，跨批
复制/校验/发布/重开逐条保留）→ 场景一（两页面迁移 + 旧写拒绝 + 并发迁移不建
第二候选）→ 场景二（复制中断重开不展示部分数据 + 校验阶段续用同一候选）→
场景三（`POST /api/admin/shutdown` 触发进程退出，编排层按 `restart: unless-stopped`
拉起后，校验本地恢复的纪元/记录/失效状态一致）。跑完自行退出，退出码即结果。

本地无编排时：`python3 verify.py --no-restart`（跳过重启场景）。

## 迁移协议与恢复规则

```
idle ──发起迁移──▶ copying ──复制完成──▶ validating ──校验通过──▶ publishing ──原子发布──▶ published
                    │                       │                      │
   页面在此关闭 ──▶ 安全回收候选(aborted)   续用同一候选            恢复时补齐发布
```

- **读取**：永远只来自工作区指针指向的已发布纪元；候选纪元从不对外读取，
  因此读结果只能是完整旧纪元或完整新纪元。
- **逐条记录**：每条观测都是独立记录。正文完全相同、仅大小写或空白形式相近的
  观测绝不视为同一条；复制逐条进行（保留各自的序号、正文与创建时间），校验摘要、
  发布前复核与发布后读取对同一份逐条集合达成一致，记录在迁移前后数量、顺序不变。
- **并发**：迁移状态是工作区行上的唯一槽位，并发发起返回 409，不会创建第二个候选纪元。
- **发布**：单事务完成指针切换、旧纪元作废、旧纪元围栏页面全部失效。
- **迟到保存**：迁移进行中（发布前）与页面失效/纪元切换后（发布后），写入一律
  409 并提示"请重新载入"。
- **恢复**：页面在复制/校验/发布之间关闭（显式关闭、心跳过期或进程重启），
  由后来页面或下次启动依据持久化阶段恢复：copying→回收候选；validating→保留
  同一候选待续；publishing→补齐发布。
- **旧缺陷收敛**：曾被旧版本"按正文去重"发布掉记录的工作区，在重开页面或进程
  重启时安全收敛为完整记录（按原序号/正文/创建时间补回，可跨多次迁移逐级补齐）。
  只做追加：不回退当前纪元、不覆盖迁移后新增记录、不复活旧页面（旧页面依旧无写资格）。

## API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/healthz` | 健康检查（含 DB） |
| GET/POST | `/api/workspaces` | 列出 / 建立工作区 |
| GET | `/api/workspaces/{id}/state` | 当前纪元、迁移阶段、围栏页面及失效状态、当前纪元记录 |
| POST | `/api/workspaces/{id}/pages` | 打开页面（在当前纪元建立围栏） |
| POST | `/api/workspaces/{id}/pages/{pid}/heartbeat` | 心跳 |
| POST/DELETE | `/api/workspaces/{id}/pages/{pid}/close` | 关闭页面 |
| POST | `/api/workspaces/{id}/records` | 写记录（校验围栏/失效/迁移中） |
| POST | `.../migration/start` `copy` `validate` `publish` | 迁移四步 |
| POST | `/api/admin/shutdown` | 进程退出（需 `ALLOW_ADMIN_SHUTDOWN=1`，仅供编排验收） |

## 目录

```
app/db.py        纪元/迁移状态机存储层（SQLite，WAL）
app/server.py    HTTP API + 静态页
app/static/      记录页前端
tests/           单元测试（16 例）
verify.py        编排内验收服务（退出码报告结果）
docker-compose.yml / Dockerfile / scripts/verify.sh
```
