# 数据集索引（写文章用）

> 本文档登记项目的全部数据资产：位置、格式、内容、文章可用点。更新于 2026-07-23（v3.1）。

## 一、过程数据（自动产生，持续累积）

### `bridge/activity-*.jsonl` + `bridge/activity.jsonl`（活动流，★核心叙事数据）
- **轮转存档**（v3.1 起，此前每局清空，历史已失）：pilot 启动时把上轮改名为 `activity-YYYYmmdd-HHMMSS.jsonl`
- 格式：每行 `{"ts": "HH:MM:SS", "kind": "...", "text": "..."}`
- kind 全集：`cmd`（指令下发/回显）、`status`（角色轮次/系统事件/计划书）、`error`（失败回执）、`alert`（反射事件）、`note`（LLM raw 输出/诊断）、`chat`（玩家留言）
- 前缀：`⚙` 规则层、`🔨` 建造规划员、`⚔` 战斗、`📋` 总指挥、`⚡` 反射层、`💬` 玩家
- **文章可用点**：完整决策时间线（可还原任意一局的"AI 在想什么"）、失败案例库（bug 表每一条都能在这里找到现场）、用户实测语录配时间戳

### `bridge/pilot.log`（LLM 调用时序）
- 每行 `[llm] HH:MM:SS 调用开始/结束 model=X 线程=rw-角色 耗时=Ns`
- **文章可用点**：三角色并发实证（"是不是顺序执行"）、API 延迟分布（chat 42~48s、reasoner 112s 的原始记录）

### `tools/metrics_history.jsonl`（对局度量，match_metrics.py 产出）
- 每局一行：`{ts, label, verdict, duration_s, first_extractor_s, first_factory_s, first_turret_s, army_peak, funds_end, own_bld_end, enemy_bld_end}`
- **文章可用点**：建造时间线对比（版本迭代前后：首池/首厂/首防线提前了多少秒）、胜率表

### `agent/logs/agent.log`（游戏侧回执）
- 指令 ok/FAIL 原文（含失败原因）、BuildWatch 建造追踪（BW+/✓/✗/心跳）、tick 心跳（units/funds 曲线）
- **文章可用点**：失败率统计原料（51.4% build 失败率那组数字就出自这里）、游戏内真实延迟

## 二、领域数据（提取/分析产物）

### `tools/pedia-dump.json`（全单位百科）
- 173 单位：价格/科技/hp/移动/武器（对地g/对空a/dmg/dps/rng/门数）/占地 fp(w,h,ox,oy)/动作表
- **文章可用点**："AI 如何知道谁打得动谁"的数据基础；12 个硬编码单位伤害读不到的花絮

### `tools/defense_sample.json`（用户防线实战样本）
- 837 座建筑坐标+类型（用户沙盒摆的 22 簇阵型：密集防线/工厂长蛇/纯制造仪/纯防空/分散小阵）
- 已提取参数：分层（维修湾 826px→火炮 1238→主线 1330→防空 1423）、配比（135:182:132:50:44）、节距（小建筑 40px/工厂 60px）、2x2 建筑+3 格缝=100px 防线甜点
- **文章可用点**："人类专家经验如何蒸馏成模板"的完整案例（这是整项目最独特的一手素材）

### `bridge/map.txt`（ASCII 地图）、`bridge/config.toml`（运行配置）
## 三、分析工具（可复现统计）

- `tools/analyze_pace.py`：耗时/吞吐/失败率统计（**基准数字**：轮次中位 52s、chat 42~48s/轮、commander 112s、失败回执 42.9%、3h build 失败率 51.4%、空转率 40~50%、有效吞吐 ~2.8 动作/分钟、规则层占比 50%）
- `tools/test_exec_slots.py` / `tools/test_multiagent.py`：离线回归（槽位坐标/工单调度/闭环规则断言）

## 四、叙事素材（文章骨架）

- **演进弧**：纯 LLM 微操（1 动作/分钟）→ 三层架构 v2 → 多 agent 轮询 v3.0（2.8 动作/分钟）→ 计划/工单制 v3.1（LLM 出计划、规则层执行、事件定召唤）
- **用户语录史**（PROGRESS.md 第六节）："完全不可用" → "和脑瘫玩游戏一样" → "玩家建造优势=规划+拉条" → "防线挨打就不去造了是吧" → "用钱看每秒收入"
- **bug 表 25 条**（PROGRESS.md 第五节，每条带教训——最好的"踩坑实录"章节素材）
- **调研结论**（PROGRESS.md 第二节）：TextStarCraft II+CoS（arXiv:2312.11865）、SwarmBrain（2401.17749）、HEP（2502.11122）、LLM-PySC2（2411.05348，负结果最全）、HFTBench 事件触发（2505.19481）；RW 生态（rw_analysis 逆向文档、Impossible AI=3.64x 收入、.replay 回放格式已逆向）
- **游戏本体彩蛋**：RW libRocket 鼠标处理 latent bug（三份崩溃日志同签名，与 agent 无关）

## 五、数据纪律

- `secret.toml` 永不入库；活动流只进 `activity-*.jsonl`（gitignore 已含 `activity.jsonl`，轮转文件同属运行产物，不入库——文章素材手动导出再脱敏）
- 对局度量标签（--label）建议统一：`v3.0轮询` / `v3.1计划工单` / `事件触发` / 后续版本号，方便版本间对比
- 写完文章引用数字时一律给 `analyze_pace.py` 可复现命令，不给手抄数
