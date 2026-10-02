# 市政道路桥梁养护管理平台

覆盖道路巡查、桥隧定检、路面病害、交安设施、绿化管养、除雪防汛及养护工程管理的市政道桥全要素养护后台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 路段管理 | `road_section` | 管养路段 | 路段编号、路段名称、起止桩号 |
| 日常巡查 | `patrol` | 巡查记录 | 巡查编号、巡查路段、巡查日期 |
| 路面病害 | `pavement` | 病害记录 | 病害编号、所属路段、病害类型 |
| 桥梁定检 | `bridge` | 检测记录 | 检测编号、桥梁名称、检测类型 |
| 桥梁档案 | `bridge_info` | 桥梁 | 桥梁编号、桥梁名称、桥型结构 |
| 隧道管养 | `tunnel` | 隧道 | 隧道编号、隧道名称、隧道长度 |
| 交安设施 | `traffic_facility` | 交安设施 | 设施编号、设施类型、所属路段 |
| 排水设施 | `drainage` | 排水设施 | 设施编号、设施类型、所属路段 |
| 绿化管养 | `green` | 绿化区域 | 区域编号、区域名称、植物品种 |
| 路灯照明 | `lighting` | 路灯设施 | 灯具编号、灯具类型、功率 |
| 除雪防滑 | `winter` | 除雪作业 | 作业编号、作业路段、作业日期 |
| 防汛应急 | `flood` | 防汛记录 | 记录编号、预警级别、影响路段 |
| 边坡防护 | `slope` | 边坡 | 边坡编号、所属路段、边坡类型 |
| 伸缩缝管理 | `expansion` | 伸缩缝 | 缝编号、所属桥梁、缝类型 |
| 支座维护 | `bearing` | 桥梁支座 | 支座编号、所属桥梁、支座类型 |
| 养护工程 | `project` | 养护工程 | 工程编号、工程名称、工程类型 |
| 养护车辆 | `vehicle` | 养护车辆 | 车辆编号、车辆类型、车牌号 |
| 养护材料 | `material` | 养护材料 | 材料编号、材料名称、材料类别 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 支座维护：谱系批次与事务边界

支座维护（`bearing`）按「谱系批次号」重构，规则集中在
`backend/app/services/bearing.py`：

1. **存量迁移**：启动时（`app.main` lifespan）以及 `POST /api/bearing/migration`
   会把存量桥梁支座按「支座编号」逐条回填谱系批次号与迁移时间，每个支座一条
   「存量迁移」事件。迁移幂等：已回填的支座重复执行只跳过、不追加事件；
   存量数据缺支座编号时整批回滚，不迁一半。
2. **补检事务**：`POST /api/bearing/inspections` 一次提交一个批次，检查证据、
   支座维护结论、维护建议三类事件写入同一谱系批次，同事务内更新支座状态、
   投影所属桥梁档案、生成工程待办与通知。任一步失败（档案缺失、结论非法、
   `模拟失败=true`）整体回滚，接口返回 409，不残留任何事件、待办、通知。
3. **同批次读取**：批次详情 `GET /api/bearing/batches/{批次号}`、所属桥梁档案
   `GET /api/bearing/bridge-archive`、工程待办 `GET /api/bearing/todos`、通知入口
   `GET /api/bearing/notifications` 都只从已提交批次派生，三处入口读同一份
   批次结果，返回数据都带回批次号可溯源。
4. **并发幂等**：补检按批次号幂等，「判重 + 落库」在 `store.batch_lock` 的同一
   持有区间内完成。相同批次号的并发/重复提交只有第一次写入，其余直接返回
   既有批次，不会追加事件、待办或通知。

后端测试（含 8 线程并发同批次号、事务回滚、HTTP 全链路）：

```bash
cd backend
python3 -m pytest tests/test_bearing_lineage.py -q
```

