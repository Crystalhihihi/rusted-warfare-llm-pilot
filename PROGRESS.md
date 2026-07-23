# Rusted Warfare LLM Pilot — 项目进度与现状评估（交接文档）

> 更新时间：2026-07-23。适用读者：新窗口/新人接手。本文件如实记录当前进展、真人实测评价、**未实测清单**与下一步计划。
> 当前版本：**v3.1**（LLM 出计划/工单，确定性层执行，事件定召唤；v2=三层架构、v3.0=多 agent 轮询，均成历史）

## 一、项目是什么

把大语言模型接入即时战略游戏《铁锈战争》（Rusted Warfare）：
Java javaagent 挂游戏 JVM 实时提取战况（快照/NDJSON/TCP 41234），Python 桥接层决策，指令经游戏原生命令队列回注。
**仅限单机研究，严禁多人对战。**（RW 本体 libRocket 鼠标处理有 latent bug，与 agent 无关，已鉴定）

## 二、架构（v3.1 当前生效形态）

```
LLM 层（DeepSeek，3 个角色，全部事件触发，非轮询）：
  总指挥（reasoner ~50s）：只产出《作战计划书》（阶段/经济/配兵/攻防/布局）
  建造规划员（chat 单模型）：被召唤时产出《建造工单》（未来 2~3 分钟有序任务序列）
  战斗（chat 起步+空转升 reasoner）：进攻/防守/集结/配兵/军工构成
确定性层（零 token）：
  BuilderManager（executor 内嵌 2s/拍）：工单调度——按序派任务、buildq 拉条、完成推进
  规则快层（executor routine）：采集器补给/工厂扩建升级/生产排队/防线网格弧/抢修/前线哨戒/判死黑名单
  反射层 reflex（毫秒级）：受击撤退/基地回防/赴险召回
  领土控制图 powergrid：战力标签（dps×hp/100 分对地/对空）+ 400px 格安全值/占领状态/安稳判定
游戏内 UI（agent 内嵌）：AI 聊天台（可打字）/委托面板/设置面板（三角色开关）/主栏
```

**核心方法论（调研实锤，见下）**：LLM 出计划/工单（JSON），确定性层按拍执行，事件决定何时召唤 LLM。
文献共识（TextStarCraft II/SwarmBrain/LLM-PySC2/HFTBench）：没有任何工作能让 LLM 在真实时钟下反应更快，
全部智慧是"让 LLM 更少被需要"——事件触发、宏微分离、动作队列、超时默认动作。
实测耗时数据（tools/analyze_pace.py）：chat 单轮 42~48s、commander 112s、轮询制有效吞吐仅 ~2.8 动作/分钟——这是 v3.1 的直接动机。

## 三、怎么跑起来

1. `bash agent/build.sh`（JDK 11+，自动下载 ASM shade）
2. `bridge/secret.toml` 填 DeepSeek key
3. `rw-ai.bat`（一键：游戏+桥接）；`tools/run-bridge.bat` 只重启桥接（不动游戏）
4. 游戏内开遭遇战 → 点 overlay「全队接管」→ 聊天台可直接打字指挥
5. 数据面：`bridge/activity.jsonl`（活动流）、`bridge/pilot.log`（LLM 调用时序/并发）、`tools/match_metrics.py`（对局度量，存 metrics_history.jsonl）、`tools/analyze_pace.py`（耗时分析）

## 四、已建成的能力（v3.1 全景）

- **数据链路**：快照 0.5s（含 income/expense 收支速率，游戏 UI"(+26)"同款 `team.b()/a()`）、Pedia（价格/科技/血量/移动/武器 atk/建筑占地 fp）、ASCII 地图/资源点/出生点 tmx 解析/战争迷雾过滤
- **游戏内 UI**：主栏+AI 聊天台（拖动/缩放/增量 wrap 防卡/面板内打字，ASM hook 织入 keyPressed/keyReleased/mouseWheelMoved 抑制游戏快捷键，libRocket 弹窗让行）+ 委托面板（单位图标/聚合血条）+ 设置面板（三角色开关）+ 主栏按钮
- **建造工单系统**：LLM 出 JSON 工单 → BuilderManager 按序调度（资金累计闸门/槽位现场解析 pool:k/factory:k/defense:k/misc:k/water、完成推进、90s 烂尾重派）→ **buildq 原生拉条**（`e` 命令对象 `e=true` 追加模式，e.java:13，游戏 y.g[] 路点队列完工 ax() 自动推进，与玩家 shift 拉条同路径，队列上限 29）
- **槽位网格**：fp 占地表驱动——厂区贴死密排、防线弧格距=建筑宽+3 格（用户实战甜点：2x2→100px）、防空压后排不占弧尖、主通道留空、内圈维修湾、后勤成排、海厂近水；占位图含在途/判死格
- **战力与领土**：powergrid 战力标签（对地/对空分开，防空车对地=0）、安全值=军力×1.0+建筑×1.5+离家加成−威胁热力、占领四类（军+建筑/纯军队/纯建筑/未控）、安稳判定（≥30 且持续 20s）——采集器只去安稳池或近家 600px
- **收入率经济观**：快照 income/expense 全链路——摘要"资金 X (收入+Y/s 支出-Z/s)"、制造仪触发=收入<8、实验厂=收入≥15、prompt 硬规则"看收入不看余额、不许编造原因"
- **海军链路**：水图判定→近水海厂→海军生产（T1/T2）→builderShip 水池采集；进攻 naval_split 拆水路；**海厂集结点自动落最近水域、海军回防去近家水域**（rally 按厂分水域）
- **防线圈豁免**：防守类建筑（炮塔/防空/激光/维修湾/墙）离家 ≤1200px 内危险/召回黑名单照盖（防线挨打正是该盖时）；扩张类照禁（治开局乱跑）
- **协调闭环**：反射召回→点位黑名单(10min)+规则层池判死（不可达永久）；失败回执自学习（300s/120s 封禁）；同值去重（rally/构成）；需求板（战斗 T2 需求→建造可见）；跨域回执判域（train 按动作归属，u_builderShip 超串先判）；计划书按变更注入（省 ~700 字×2 路/轮）
- **LLM 工具**：query 目录（roster/单位详情/价格表/地图/战术手册/基地），每路查询预算 1（总指挥 4），查询螺旋强制纯文本输出
- **离线测试**：tools/test_exec_slots.py（槽位/防线/工单/规则层）、tools/test_multiagent.py（mock LLM 三循环+闭环规则）、tools/analyze_pace.py（耗时统计）

## 五、修过的关键 bug（历史教训，别再犯）

| bug | 教训 |
|---|---|
| 战略动词被 `cmd=None` 清空，LLM 从未真正进攻 | 指令组装后被无条件重置，静默丢失 |
| query+动作同消息缺 tool 应答，API 400 整轮作废 | tool_calls 必须每个都有应答 |
| 执行层吃光 APM 额度，LLM 军事指令被限速 | 机器节奏与人类节奏配额必须分开 |
| 施工中 act=repair→ 被判空闲，盖一半被拉走 | 忙闲判定要"非 idle 即忙" |
| 执行层被 LLM 阻塞（每周期仅 1 拍，生产饿死） | 执行层必须独立线程 |
| mechFactoryT2 队列反射失败(q=None) 堆货 40 | 队列缺失要有本地节流兜底 |
| T3 加强/取消死循环烧钱 | 终态建筑与回退动作要排除出升级轨 |
| bat 文件含中文，cmd 解析崩溃 | Windows 批处理必须纯 ASCII |
| executor 引用未定义 cc，每拍崩静默 | tick 异常要暴露，不能裸吞 |
| 换局后 map_msg/system 不更新，LLM 全程看旧图 | 换局重置必须覆盖所有地图下游状态，make_system() 统一重建 |
| builderShip 被当陆地建造者乱派反复失败 | `"uilder" in t` 这类隐性子串匹配要显式枚举例外 |
| query 应答后 break，commander 永远没机会产出正式回复 | query 循环必须"应答→再调用拿真正决策" |
| 回执可用列表含 u_builder，战斗域回执误判给建造域 | train 回执按"被 train 的动作"判域，不能按文本含啥 |
| LLM 省略 u_ 前缀（heavyTank）被误判拦截 | 动作 ID 先归一化再判守卫 |
| 战斗/建造 chat 空转，升级 reasoner 也只是继续空转 | 分析癖模型要用"空过违规+强制动作"，升级路径要按角色需要配 |
| 规则层无视价格每 2s 刷"funds < price"失败 | 生产/建造必须先过 pedia 价格感知 |
| 工单资金闸门查错键（"b_x" vs pedia 的 "u_x"） | 价格表键格式统一 `u_` 前缀，测试钉死 |
| 三个建造者挤同一个池 | 池选择去重：在途池 40px 内不重派 |
| 热重启杀不干净出双桥接；awk 误杀游戏进程 | 杀桥接按命令行匹配 pilot.py；度量器也连 41234 别误杀 |
| 防线挨打不敢盖炮塔（危险一刀切） | 判定分家：扩张类危险照禁，防守类防线圈(≤1200px)豁免 |
| LANE 主通道撞 0 号弧位导致任务误判 failed | defense_slot 返 LANE 时调用方跳号重试 |

## 六、真人实测评价（用户原话史）

- 早期："完全不可用，目前仅供娱乐"（慢/乱/配兵混/无海军）
- 中期："和脑瘫玩游戏一样""建造 ai 似乎和死了一样""感觉他们是抢活干"
- 用户核心洞察（已被采纳进架构）：
  - "玩家建造优势=规划+工单队列（拉一长条连干），不是一个一个点" → 建造工单系统+buildq
  - "防线挨打就不去造了是吧？" → 防线圈豁免
  - "远征军队打头阵，安稳了才能盖；占领分纯军/纯建筑/军+建筑不同安全值" → powergrid 控制图
  - "用钱看每秒收入不是余额" → income/expense 全链路
  - "游戏原生 AI 建造就傻逼（有钱就造到点升级），写死不如它" → 不写死模板，LLM 出规划、规则层执行

## 七、未实测清单（已构建未验证，接手后逐条过）

1. **buildq 原生拉条**（新指令）：jar 已带，javac 过，机制有反编译铁证；未进游戏实测。验证：建造者是否连干多个落点
2. **建造工单全链路**（LLM set_work_order→BuilderManager 调度）：离线测试过，未实测
3. **战斗事件触发 + 规则层产兵**：稳态期零 LLM 是否影响军备节奏
4. **powergrid 安稳闸门**：采集器是否只在安稳池落地（观察是否有"该去不去"或"不该去硬去"）
5. **income/expense 字段**：单位是否为 /s（快照数值与游戏 UI"(+N)"对账）
6. **海厂水域集结点**：船是否泊近家水面而非上岸
7. **设置面板三开关**（AgentPanel）：真机点击验证
8. **防线圈豁免**：家里挨打时炮塔是否照盖
9. **LLM 调用时序日志**（pilot.log）：验证三角色并发（回答"是不是顺序执行"）

## 八、下一步计划（按优先级）

1. **逐项实测上表**（先 1/2/5）——有异常直接回来看对应模块
2. **战斗条件计划队列**：LLM 出 {"序列":["集结到R","兵力>X攻B","遇伏撤C"],"失效":[...]}，规则层按拍执行监测失效（文献 CoS 动作队列同款，已完成事件触发，计划队列是下一步）
3. **计划书参数化**：自由文本 → 结构化字段（采集器目标/构成表/主攻方向/防线方向），规则层强制消费（治"计划书没人听"）
4. **防线弧锚点改安全值等高线**（防线跟实控边界走）；远征事件（池被军队打下→召唤建造规划员）
5. **评测闭环**：固定图 vs 内置 AI 记胜率（match_metrics 已备；回放 .replay 格式社区已逆向可解析，见 rw_analysis）
6. **1v9 最高难度路线**：Impossible=3.64x 收入、套路固定，龟缩-反核-多线牵制策略蒸馏成规则

## 九、文件结构速查

```
agent/src/rw/agent/   javaagent（快照/指令/百科/地图/绕行点/聊天命令/overlay UI/ASM hook）
  Snapshot.java       快照（含 income/expense 收支速率）
  Pedia.java          百科（价格/科技/hp/武器 atk/占地 fp/动作表；dumpPedia=<path> 免开战导出）
  Commands.java       指令分发（build/train/rally/resume/buildq 拉条/delegate/move/attack 等）
  Hooks.java          ASM transformer（按键/滚轮拦截）；OverlayInput.java 输入胶水
  Ui/ChatPanel/DelegPanel/AgentPanel/OverlayData.java  游戏内 UI
bridge/
  multiagent.py       编排器：三角色事件触发循环/翻译守卫/工单通道/需求板/回执判域/聊天推送
  executor.py         执行内核：BuilderManager 工单调度+规则快层（例行/防线/生产/升级/哨戒/抢修/判死）+槽位网格
  powergrid.py        战力标签+领土控制图（安全值/占领状态/安稳判定/战力概况）
  reflex.py           反射层（事件钩子回灌黑名单）
  summarize.py        摘要（含真实收入率）；danger.py 威胁热力；connmap.py 连通域；spawns.py 出生点
  llm.py              OpenAI 兼容客户端（调用时序日志→pilot.log）
  config.toml         multi_agent/routine_layer/model(_fast)/三路节拍/超时
tools/
  match_metrics.py    对局度量器（结果存 metrics_history.jsonl）；analyze_pace.py 耗时分析
  test_exec_slots.py / test_multiagent.py  离线回归；gui_step.py GUI 驱动（慎用，曾触发游戏 libRocket 崩溃）
rw-ai.bat             一键启动；tools/run-bridge.bat 只重启桥接
```

## 十、给接手者的提醒

- 排查顺序：`bridge/activity.jsonl`（⚙ 规则层/🔨 建造/⚔ 战斗/📋 指挥/✗ 失败）→ `bridge/pilot.log`（LLM 时序）→ `agent/logs/agent.log`（回执原文）。先找"没生成"还是"没送达"。
- 游戏暂停时快照不流动，三循环会对着冻结状态空转——看数据先看 tick 动不动。
- 所有 bat 纯 ASCII；`secret.toml` 永不入库；提交规范 `feat:/fix:/docs:` 一条说清一件事。
- 杀桥接进程按命令行匹配 `pilot.py`（match_metrics.py 也是 41234 客户端，别误杀；游戏 JVM 也会被 awk 误抓，别误杀）。
- jar 被运行中的游戏锁定，重打包需先关游戏；可打包到 `dist/rw-agent-new.jar` 再换名。
- 上下文压缩后本文件是唯一权威状态源，改完大事先更新本文件。
