# Rusted Warfare LLM Pilot — 项目进度与现状评估（交接文档）

> 更新时间：2026-10-20 凌晨（**dashboard cli 指挥舱上线**——面板不再是空壳：五席状态/任务单状态机/Jev 决策流/board.md 尾部/事件流直读 cliloop 活局，legacy/cli 一壳两身自动切换；**修 muted legacy bug**：LLM_OFF=1 不再把 cli_mode 踢回旧循环（静音可验 cli 快层，席位不建/Jev 关）；账本 §〇.1 launch 闭环 + 代理权拆除批三刀全落见 §五.2）；**2026-10-19 讨论沉淀见 §七.7；同日 grill-me 设计拷问定案见 docs/decisions.md §十三；10-19 深夜批次交接见 §〇.0**。适用读者：新窗口/新人接手。
> 本文件只放**活的**：速览 / 现行架构 / 纪律 / 环境 / 待办 / 最近一节。历史工作日志与决策记录已拆至 `docs/`，按需查阅（索引见 §八）。
> **新窗口先读 §〇（30 秒速览）→ §七（最近工作记录=现行接线）→ §五（待办队列）。**
> 当前版本：**v4.0**（军种三层指挥链，旧循环保留未删）；**架构二期 cli_mode 已开真机在测**（档案 2026-09.md §二十八/§二十九）
> **git：今日全部改动在 `arch2-cli` 分支**（main 保持干净；回合制合回由用户定）

## 〇、最新状态速览（新窗口先读这个，30 秒上手）

### 〇.0 交接（2026-10-20 凌晨，今夜批次已提交推送）：明天第一件事=全线接入首秀

**一句话现状**：二级操作全部实弹闭环（含 launch）；代理权拆除批三刀全落；dashboard 指挥舱上线；开局经济链（池/厂/建造者）在 10 人图真机盘活；十套回归全绿。**arch2-cli 已合入 main 并推送（GitHub）**——本分支从此是主干。

**明天唯一大事：全线接入首秀**（用户已定调"不要有脚本干扰"）。
1. **顺序铁律：先开游戏进局，后起桥接**——pilot 有"游戏关闭>90s 自动退出"，反了桥接会自杀。
2. 步骤：`tools/run-with-agent.bat` 开游戏进对 AI 局 → 起 Kev（`cd extern/kev && KEV_FUSED=0 HF_ENDPOINT=https://hf-mirror.com C:\Users\zengd\uv-venvs\kev\Scripts\python.exe -m kev.serve --run jaredpalmer/kev-0.8b --port 8009`）→ `run-bridge.bat`（普通 cmd，**不带** LLM_OFF）。
3. **首秀判据**：开局骨架走任务单（3池+1厂+3建造者，op-econ 带 train 段）；~60s 总指挥出 plan.md；陆/建席 25s 一拍写 tasks/*.json+board.md；**构成一旦设定工厂才产兵（之前静默=无脚本干扰的直证）**；小队接任务后 Jev 菜单直驾（面板 Jev 卡看决策）。
4. **无脚本干扰验收**：出现 LLM 未指令的自动产兵/防线/升级/抢修/侦察=拆除批漏网，报修。快层仅剩：开局骨架+②采集器到目标数+任务单物化+姿态执行+谓词+force 通道。

**今晚修复链（全部带正反双例钉死）**：①launch 弹药判读 `cB`→`cE`（反编译 custom/d/b.java:444-478 实据：energy→cB、ammo→cE；核弹其实早造出来了，发射被误拒）；②pilot 修 muted legacy bug（LLM_OFF=1 不再踢 cli 回旧循环；静音=不建席/Jev 关，快层照跑）；③dashboard 指挥舱（三栏：决策/状态/战场；席位状态灯+最近一拍输出 last_out、任务单状态机、Jev 决策流、board 尾部、事件流格式化；cli/legacy 一壳两身自动切换）+ 修 served JS 语法 bug（Python 模板 `\n` 转义成真换行致整段 JS 失效）；④pool:k 安全池冷启动全灭→宽限安全优先（45s 内重试安全池，耗尽就近全量池兜底照派+警告 note，定案#2 同口径）；⑤spawns 盟友排除（ally_ccs 参数，10 人图不再把盟友 seed 成敌情——实测 5v5 排除全对）；⑥开局骨架补 train 段（cli 下①建造者维持已拆，骨架不带补员=0 建造者死锁，真机资金堆 5.5 万钓出）。

**环境备忘**：jar 状态 `rw-agent.jar`=20:08 cE 修复版（旧件 rw-agent-prev-ammo.jar / prev-launch.jar）；游戏速度与窗口焦点强相关（失焦 0.01-0.17x，焦点 180+ ticks/s，造弹等长等待保持游戏在前台）；桥接=我起的话用后台任务，`tools/kill-bridge.ps1` 杀（别关窗口防孤儿）；静音变量 `LLM_OFF=1`；探针 `tools/probe.py`（units/send/train/build/waitborn/watch）。

**未验清单（老四样）**：LLM 生产单位/造建筑（=明天载体验证）、两栖喷气机潜浮、残骸回收（D 类）、109/110 自升级含义。已知信息噪音：10 人图「点位危险」警告部分为 seed 过热误报（信息面非否决，LLM 会从回执学）。

**今天的产物**（全未 commit，arch2-cli 工作区）：tools/sim_economy.py+test_sim_economy（第十套）、tools/calibrate_durations.py+durations_calibrated.json（**timeMs 口径，tick 差 2 倍勿用**）、tools/extract_unit_actions.py+unit_actions.json、tools/set_speed.py、bridge/send.py 三动词、Commands.java=load/unload/launch+speed+bs 导出（jar 14:05 版已验，new 版待换）、cliloop units.md 生成器升级（可制造三态+变体实价）、executor=轨C 防线塔升级+force 通道+①cli 守卫+升级跳过归因日志。

**动作 ID 实词汇总技巧**（今天顿悟）：对任意单位下假动作 `train __x__`，回执「可用: …」即该单位**真实动作表**——比 pedia 全且是引擎口径（航母=空军基地/鹦鹉螺=潜艇船坞/堡垒=武装运输工厂/机械师=万能移动建造者，全是这么挖出来的）。

**铁律提醒**（今天实锤的坑）：静音=`LLM_OFF=1`（`RW_LLM_MUTE` 只挡客户端不挡循环分支）；杀桥=`tools/kill-bridge.ps1`（会话内停任务会孤儿化 python 出双桥接）；每改必跑十套；游戏运行锁 jar，重打包 `OUT_JAR=dist/rw-agent-new.jar bash agent/build.sh`。

### 〇.1 二级操作验证账本（2026-10-19 深夜封存；**2026-10-20 凌晨二轮实弹补账**，用户口述校准）

**✅ 已实弹（一轮）**：装载/卸货全链路（运输机 dropship：load 入舱→空运→unload 落地）；变换形态×4（鹦鹉螺上浮/气垫船部署/mech 起飞/蜘蛛槽装模块）。

**✅ 已实弹（二轮，新沙盒卡司 21 单位+16 炮塔靶）**：
- **平台产兵×4**（产物全部 snapshot 实弹等出）：航母#237→c_helicopter#259、**鹦鹉螺#236 潜态直接产** gunBoat#256、堡垒#233→c_tank#257、气垫船#231→hoverTank#258。
- **平台建造×4**（build 指令 `builder:` 参数可点名建造者，Commands.java:415）：机械师#221→c_turret_t1#260、战斗工程师#222→c_turret_t1#261、建造船#239→seaFactory#262（水上）、建造者#220→extractorT1#263（自动吸附池点）。
- **鹦鹉螺潜↔浮闭环**：潜→`nautilusSubmarineSurface_5`→浮态；浮→**`nautilusSubmarine_7`**→潜态（`_6` 在浮态表存在但条件不满足，正解 `_7`）。
- **蜘蛛槽 6 模块全装**：smallgunturret/lightning/artillery/antiair/fabricator/laserdefense（槽位表=`modularSpider_*_0..9`，假动作回执可挖全）。
- **堡垒装载闭环**（上轮半验尾）：产出的 c_tank#257 自行走到堡垒#233 旁入舱（位置冻结）→unload 载员 1→在卸货点 55px 处重现。

**⚠️ 存疑**：`109/110` 在堡垒/气垫船/浮态鹦鹉螺表都存在，回执 ok 但**无类型翻转**（疑自升级/武器升级，效果未目验）；气垫船动作表**只有产兵无建筑**（截图里建筑菜单疑为混选单位所致，引擎表为准）。

**❌ 仍未验证**：
1. **LLM 生产单位**——cli 模式 land 席 composition→⑦produce 链（中层四席零真机，这是载体）
2. **LLM 造建筑**——build 席工单→物化（同上）
3. ~~launch（jar 待换）~~ → **✅ 2026-10-20 凌晨实弹闭环**：train `_0`（=buildNuke，ini 序第 1 动作）造弹 cE+1 → `launch` 打靶 (260,260) → 9 座炮塔整群蒸发（21→12）。**根因修复**：旧 handleLaunch 弹药判读 `am.cB`（=energy，custom/d/b.java:444-478 实据），核弹其实在 `am.cE`（int，ini 键 ammo）——cE 修复一行（rw-agent.jar 20:08 构建，旧件 rw-agent-prev-ammo.jar）；`_1`=launchNuke 回退定位正确
4. 两栖喷气机潜↔浮（用户说进下一轮核爆批次）
5. 残骸回收（D 类记账未做）

**⚠️ 环境新坑（2026-10-20 实锤）**：游戏推进速率与窗口焦点/活动强相关——聊天失焦时实测 0.01-0.17x（tick 个位数/s），焦点在游戏时 180+ ticks/s。造弹类长时间验证要么保持游戏窗口聚焦，要么按 4x+ 等待窗口跑探针。

**验证方法已备**：对平台下 `train <动作id>` → 等 snapshot 里新单位出生（产物型）；变换类看类型翻转（已用）；载荷类看位置冻结/重现（已用）。沙盒无限资金，一轮全清。探针脚本 `tools/probe.py`（units/send/train/build/waitborn/watch）。

**我们在哪**（2026-09-26 深夜）：架构二期**已开真机**（`cli_mode=true`+`jev_drive=true`+`cli_middle_enabled=true`（陆/建））。三局冒烟战绩（档案 2026-09.md §二十九）：①首局抓 pool 热身期瞬死→已修（45s 宽限+物化等 CC+认领 10min 放弃）；②二局 Jev 直驾全 unsure→margin 标定修复+可观测性三洞（聊天台四路/dashboard cli 接入）补齐；③用户怒点"开局只给基础任务"→**开局 v2**：脚本只剩 3 池+1 陆厂+建造者 3，防线/构成/小队任务模板全删归 AI 层。中层陆/建席 prompt 调教+分席会话（致命 -c 互串修复）+ref/units.md 已落地，**未真机**。
**当前重心（按序）**：①中层陆建两席真机首秀复盘（首局产物已落：build 席防线工单+board 纪律好评；land 席 composition 小数口径待验）→ ②**Jev 驾驶位面试**（§七：margin 放行后菜单选择质量——推进/驻守/撤退/集火靠不靠谱）→ ③四席全开+prompt 迭代 → ④切主循环删旧决策层+reflex.py → ⑤选址重做（先量再治）→ ⑥托管 Jev 对照+域内微调 → ⑦ACP 常驻（中层拍 25s→5-10s）。
**刚修完未真机（下局先观察）**：四刀（档案 2026-09.md §二十六）；pool 宽限复活；Jev margin 放行质量；**Jev 驾驶位全链路**（§七：行军心跳 15s/接敌 3s/菜单执行）；中层分席 sid 互串。**jar 已重打包生效**（`rw-agent.jar` 2026-09-25 17:30）。
**实局判决**：**Kev-0.8B 直驾不挣工分**（engage 恒 0.55 零行动，只能当报警器；margin 修复后再给一次面试）；总指挥 CLI 节拍实测 49-101s/拍（>50s 间隔，串行无碍）。
**git**：工作区干净，今日改动全在 `arch2-cli` 分支（四刀+Kev 探针+架构二期骨架+三连修+开局 v2+中层）。
**测试纪律（用户定）**：**必须重开游戏才算真机测试**；**能不调用 LLM 就不调用**（规则层验证 `RW_LLM_MUTE=1`）；**每改必跑十套** `tools/test_*.py`（fog/dashboard/build_phase2/combat_phase3/squads_branch/multiagent/exec_slots/buildchat/**cli/sim_economy**）。
**环境速查**（2026-10-19 勘误）：静音变量是 `LLM_OFF=1`（pilot 读它决定走纯执行层），`RW_LLM_MUTE=1` 只挡 LLM 客户端调用、**不挡循环分支**——文档旧写法会把桥接跑进 cliloop 空转；杀桥一律 `tools/kill-bridge.ps1`（会话内 TaskStop 杀管道会孤儿化 python，已实锤出双桥接互打）（**别用 rw-ai.bat，会带起旧代码孤儿桥接烧 token+抢 8321**）；桥接手动起=`cd bridge && python -u pilot.py`；杀桥接=`tools/kill-bridge.ps1`；面板=127.0.0.1:8321（cli 模式也起）。**Kev 服务**=`cd extern/kev && KEV_FUSED=0 HF_ENDPOINT=https://hf-mirror.com C:\Users\zengd\uv-venvs\kev\Scripts\python.exe -m kev.serve --run jaredpalmer/kev-0.8b --port 8009`；**探针**=`python tools/test_kev_probe.py --lang en`。
**模型/硬件底数**：Kev-0.8B 本地（RTX 4060 8GB，eager 无 fla）——6 队并发 293ms；质量地板（方向对、硬判断不稳、概率全挤 0.47-0.72、choice 置信上限 ~0.47→用相对 margin 别用绝对阈值）。提质三路：Kev-4B（需 Modal L40S）/域内微调（~$1/次）/托管 Jev（~$0.09/局，未注册）。LLM 全角色=`deepseek-flash`（V4.1-Flash，pro 已退役）。
**架构二期一句话**：总指挥（1 CLI）只看大局→中层四席（陆海空建各 1 CLI 会话，留言板异步）出详细任务单→快层=确定性 2s 拍+Jev 事件/直驾；任务触发只用系统谓词；单位分配归系统；**一切撤退归 Jev，系统不自动挪兵**；模型只出意图，并行分发归系统；开局只给基础任务。

## 一、项目是什么

把大语言模型接入即时战略游戏《铁锈战争》（Rusted Warfare）：
Java javaagent 挂游戏 JVM 实时提取战况（快照/NDJSON/TCP 41234），Python 桥接层决策，指令经游戏原生命令队列回注。
**仅限单机研究，严禁多人对战。**（RW 本体 libRocket 鼠标处理有 latent bug，与 agent 无关，已鉴定）
公平性设计：只转发本队视野内信息，指令走正常命令队列，全程限速（拟人 APM）。

## 二、现行架构（架构二期，`cli_mode=true` 生效中）

```
总指挥（1 CLI 会话）       只看大局出布局（plan.md）；节拍沿用 commander_interval，实测 49-101s/拍
中层四席（各 1 CLI 会话）   陆/海/空/建；收布局→发详细任务单；留言板 board.md 异步（禁直接对话防死循环）；拍 25s
快层                      确定性 2s 拍（TaskEngine 谓词判定+工单调度+编队认领，全系统零 LLM）
                          + Jev 事件流/驾驶位（1.5s 超时降级=不动+记事件）
```

铁律与边界（用户拍板，档案 2026-09.md §二十七/§二十八全程）：
- **任务触发只用系统谓词词表**（到达/建成/数量达标/敌情/团灭），自由文本禁止；"完成与否"系统判，零 LLM
- **单位分配=系统的活**（编队/认领执行器），中层只调参数（几队/每队几人/配比）
- **一切撤退归 Jev**：受击撤退/基地回防/建造者召回全删，系统不自动挪兵（reflex 层判死刑，新循环不接线）
- **模型只出意图，并行分发归系统**——串行 token 流驱动 N 队必然连点收敛（结构性病，非 prompt 问题）
- **开局只给基础任务**（opening.py：3 池+1 陆厂+建造者 3；造什么兵/小队去哪全归 AI 层）
- Jev 驾驶位：领了 active mission 的小队全归 Jev 直驾——接敌（1600px 有可见敌）每 3s 一拍，行军/驻防 state 变了才问、没变 15s 心跳；**菜单式单题**（系统生成候选：推进/驻守/撤退回家/集火×敌组，恒≥3 项），margin 置信（顶≥0.35 且≥1.5×次高）才执行，不置信维持上一动作
- 进阶玩法（空降/蜘蛛自爆/核）=慢层按名调"操作包"，确定性层出脚本化序列，快层/反射只有中止权

旧循环（v4.0，`cli_mode=false` 仍可用）：总指挥/建造指挥（flash）出计划书/工单 → 军种中层（flash）出小队任务书 → squad chat ×N + 建造 chat ×4 执行；规则快层+反射层兜底。细节与 bug 教训见 `docs/decisions.md` 与档案 2026-07.md。

## 三、环境速查与运维纪律

- 排查顺序：`bridge/activity.jsonl`（⚙ 规则层/🔨 建造/⚔ 战斗/📋 指挥/✗ 失败）→ `bridge/pilot.log`（LLM 时序）→ `agent/logs/agent.log`（回执原文）。先找"没生成"还是"没送达"。
- 游戏暂停时快照不流动，循环会对着冻结状态空转——看数据先看 tick 动不动。
- 所有 bat 纯 ASCII；`secret.toml` 永不入库；提交规范 `feat:/fix:/docs:` 一条说清一件事。
- 杀桥接进程按命令行匹配 `pilot.py`（match_metrics.py 也是 41234 客户端，别误杀；游戏 JVM 也会被 awk 误抓，别误杀）。**改了桥接代码必须杀旧进程重启（`tools/kill-bridge.ps1`）——旧代码的 LLM 循环会继续烧钱**；主菜单态桥接停在"等待游戏地图数据"（零 LLM 零费用），开新局自动继续，不用管。
- jar 被运行中的游戏锁定，重打包需先关游戏；可打包到 `dist/rw-agent-new.jar` 再换名。**构建后要查 class 新鲜度**（有过编译报错被忽略、jar 里跑旧类的先例）。
- **同图重开=新对局**：agent 会重发地图、桥接按快照 tick 回退检测重置（旧 bug 类已修，见档案 2026-07.md §十五）。

## 四、文件结构速查（当前生效）

```
agent/src/rw/agent/   javaagent（快照/百科/迷雾/地图/指令分发/overlay UI；产物 dist/rw-agent.jar）
bridge/
  cliloop.py        架构二期主循环：局文件夹 matches/<ts>/（state.json 2s 覆写+board.md+notes/）
                    +任务单状态机+Jev 事件流+CLI 席位调度；不接 reflex
  opening.py        开局任务单生成器（顶部数据表可手调；v2=3 池+1 陆厂+建造者 3）
  predicates.py     谓词词表+TaskEngine 状态机（tasks/*.json mtime 热装载；认领 10min 放弃）
  jev.py            Jev 薄封装（margin 置信带 0.35-0.65；1.5s 超时降级=不动+记事件）
  cliseat.py        kimi CLI 席位 spawn（首拍抓 sid，续拍 kimi -S <sid>；分席防互串）
  （旧循环 cli_mode=false 生效）multiagent.py 编排器 / executor.py 执行内核（cli_mode 裁剪态）
                    / powergrid.py / fog.py / reflex.py / dashboard.py / summarize.py / llm.py
tools/  九套回归 test_*.py（含 test_cli.py/test_kev_probe.py）+ replay_parse.py/replay_distill.py
        + match_metrics.py/analyze_pace.py
docs/   decisions.md（决策与教训，拆出）+ progress-archive/2026-07.md、2026-09.md（历史日志，拆出）
        + 专项分析（jev-fast-layer/building-layout/DATASETS/quant-audit）
bridge/matches/     对局档案（gitignored）
```

## 五、待办队列（2026-10-19 grill-me 后重排；研究优先，定案全文 `docs/decisions.md` §十三）

1. **经济模拟器三级走（最高优先）**：①✅粗版 `tools/sim_economy.py`（收入常数代码化+按进度分段扣款建模+建造序列→资金/产能曲线，含第十套回归）②✅Pedia.java 补 `bs`(buildSpeed) 导出+`cmd=speed`(0.1-64x) hook，**jar 已重打包生效**（2026-10-19 14:05，javap 抽查编入）；`tools/set_speed.py` 就绪 ③✅沙盒标定完成（2026-10-19：10x 变速局，`prog` 差分 + **timeMs 为 ground truth**——tick 口径系统性差 2 倍勿用；原始数据 `tools/durations_calibrated.json`）：厂/制造仪 16.6-16.7s、炮塔/防空 20.8s（与 7 月 1x 锚"单塔施工 20s"吻合）、实验厂 ~96s、激光/反核 8.4s；表已写回 sim_economy（逐项 src=calibrated/price-analogy/heuristic）。理论背书/边界见 decisions.md §十三.9
2. **cli_mode 全链路真机**（消融前置）：中层陆建两席首秀复盘→四席全开；期间落地双层闸（战略闸改信息呈现）+ 刚性军令默认 A + 催促信息行（军队=0/工单池空）+ **DEFAULT_PROD 兜底拆除**（代理权拆序第一刀，decisions.md §十三.14）。沙盒标定局顺手修（2026-10-19 用户实锤）：**⑤升级轨加轨C**——炮塔/防空 T2（≥1500，活路径 _routine_tick 候选曾只有矿/厂两轨；死分支 autopilot 里倒有旧轨C从未执行）；**①建造者维持补 cli 守卫**（实锤 cli 下仍自动补建造者，test_cli D 段改反例钉死）；**升级单走 force 高优先通道**——根因实锤为 `MAX_CMDS_PER_TICK=4` 被 autopilot 路经济/生产指令吃光，三轨升级全被静默饿死（探针归因链：入口有候选→emit 返 False→位满；修复=emit 加 force 参数跳过上限检查，30s 建筑冷却自然节流；§20e 回归钉死）。**2026-10-20 代理权拆除批三刀落地（用户 GO 在 decisions §十三末节，十套全绿）**：①`_next_production` cli 兜底全拆——构成真空/过滤光=静场，DEFAULT_PROD 静态表+百科首可用兜底不再产兵（test_cli C2 反例钉死）；②③.7 海军链路并入 cli 裁剪复核（1538 行守卫 10-19 已预置）；③抢修兜底 cli 拆除（烂尾/受损修理由任务单/中层决定，test_cli D 反例钉死）+ **双层闸战略闸降格**——工单坐标单实控闸/危险判定三家在 cli 模式不再否决，改 note 附「战略闸警告:…（照派，结果看回执）」，执行结果回执教 LLM（test_cli D2 反例钉死）；合法性闸（可达性/资金/限速）保留。**开局骨架（opening.py 3池+1陆厂+建造者3）按拆序留最后一刀**——等中层席位能自己开局才退役
3. **三臂消融**：地板臂先行（纯脚本零 LLM，**现在纳入测试盘**）；A/B 臂等中层真机——裁决=用户目测推进/经济/阵地/配合，胜负不重要
4. 切主循环删旧决策层+reflex.py（multiagent 三循环/看门狗/squad chat 退役）；**拆人类节奏基准**（改由模拟器供机理时长锚）+ 抢修派工移交（代理权拆序第二刀）
5. 时钟+时间谓词：游戏内秒（暂停不走）进状态；任务单 `at=T/in=Xs` 到点齐动（确定性层活，可夹带进批次 1/2）
6. Kev 提质：~~温度重测~~ ✅已判（`docs/kev-temp-probe-20261019.md`：概率挤压=校准温度 2.35 的输出假象，原始概率置信到 0.5-0.86；但 S2 劣势拒战两档都错向——**硬判断虚不是温度造成的**，0.8B 维持"报警器"判决）。后续：jev.py 切 `KEV_TEMPERATURE=1` 原始概率+阈值重标+本项目标注自拟合温度；域内微调（`--init_from`，$1 级）升首选；Modal 4B；托管 Jev 降背景对照
7. 选址重做（生成器泛化+先量再治，`docs/building-layout-2026-09-22.md`）；开局骨架退役（席位能自己开局后，代理权拆序最后一刀）；ACP 常驻
8. **拷问 6 问已定案**（decisions.md §十三.10-15）：失败两分法（决策失误=数据/机制错=必修）｜门票冻结降格脚注｜节奏基准拆改机理版｜催促不代决策｜拆序=DEFAULT→抢修→开局骨架｜地板臂先行

## 六、测试纪律

**用户定**：**必须重开游戏才算真机测试**；**能不调用 LLM 就不调用**（规则层验证 `RW_LLM_MUTE=1`）；**每改必跑十套** `tools/test_*.py`（fog/dashboard/build_phase2/combat_phase3/squads_branch/multiagent/exec_slots/buildchat/**cli/sim_economy**）。改桥接必跑全套再启动，别只 py_compile（有过 typo 每 30s 崩 tick 的先例）。**排查工具**：executor 已带升级轨跳过归因（`升级轨X全跳过: {原因字典}`，60s 节流，executor.py ⑤段）——再遇「某轨不动作」先看它，别猜。

## 七、最近工作记录：Jev 顾问→驾驶位纠偏 + 0.8B 实局判决（2026-09-26 深夜末班）

> 接档案 2026-09.md §二十九。用户诊断："你是不是把 JEV 挂错地方了？"——对，挂错了。原设计（用户 grilling 原话）"小队长出任务，**jev 接取并执行**"，我落地成了顾问（事件问一句+接敌三题），接取/执行全是 TaskEngine 纯代码。本节纠偏。

**1. 驾驶位接线（现行）**：领了 active mission 的小队全部归 Jev 直驾——接敌（1600px 有可见敌）每 3s 一拍，行军/驻防 state 变了就问、没变 15s 心跳（`jev_drive_calm_every=5` 拍）。问法从三题改**单道菜单题**（"下一步行动"）：系统按现场生成候选（推进/原地驻守/撤退回家/集火<类型#id>×敌组，恒≥3 项），Jev 点菜，margin 置信（顶≥0.35 且≥1.5×次高）才执行，不置信维持上一动作。独立的 retreat noul 题删除，撤退=菜单项；事件急查（受击/敌情）并入同一套菜单（state 里带"为什么被问"）；卡死放弃题保留 noul。系统职责不动：任务激活/触发/认领/单位分配/谓词判定。

**2. config 键语义**：`jev_drive`=驾驶位总开关（不再是顾问开关）；`jev_focus_top/margin` 转任菜单题闸；`jev_engage_hi/lo` 只剩卡死题用；`jev_retreat_hi` 闲置备用。

**3. 0.8B 实局判决（二连证据链）**：探针（概率挤 0.47-0.72、choice 上限 0.47）+ 二局直驾 16 连 unsure（engage 恒 0.55±0.03）——**0.8B 在驾驶位不挣工分，当前只能当报警器**。驾驶位能否成立，等 margin 放行后的目标选择质量面试（下局）+ 提质三路（4B 需 Modal L40S/域内微调 ~$1/托管 Jev ~$0.09 局未注册）。

**4. 用户哲学定论（存档）**："只有选择=脱裤子放屁"——对选择模型的质疑成立条件=菜单薄。菜单够厚（多目标/多姿态/时机）时点菜是真活；0.8B 答不出不等于范式错。另记金句："全交给一个大概率炸"（单脑否决依据）、"中层缓存命中 98% vs 直连 50-60%"（CLI 化核心理由）。

**5. 已知小瑕疵（不急）**：事件急查路径 unsure 控制台行不走 15s 节流（事件流自身 20s/队冷却兜底）；陆军席 composition 写小数比例（0.35）而非整数——引擎消费口径待下局验证。

**6. git**：全入 `arch2-cli` 分支（b115224 + 本节驾驶位纠偏），main 干净。

**7. 讨论沉淀（2026-10-19，纯讨论零实施：Kev 1.0 网络核查 + 设计辩论，用户推动）**

- **勘误（用户指正）**：`6 队并发 293ms` 出自 `test_kev_probe.py` 合成压测，**从未实战**；cli_mode 仅三局冒烟验证管线，**中层四席零真机**。所有 Kev 性能数字=实验室值，讨论中勿当实战依据。
- **Kev 1.0 核查**（jaredpalmer/kev，2026-10-01 发布，四模型家族 0.8B/4B/9B/27B）：本地 0.8B 即 1.0 权重（rev `9a45d25e`）。**每个 checkpoint 默认带拟合温度发货（0.8B=2.35），服务端吐校准后概率；`KEV_TEMPERATURE=1` 才拿原始概率**——探针"概率挤 0.47-0.72 / choice 上限 0.47"是校准输出上的判决，"终身残疾"结论待复核（温度不改排序，margin 规则不受影响，绝对阈值全受影响）。5% 错误预算可自动化率：0.8B=14%，4B/9B/27B=52-69%，托管 Jev=70%。
- **4B 本地方案判决**：权重 8.4GB、adapter 合并瞬时 15.9GB，4060 8GB 装不下 → 4B 正确形态=Modal L40S scale-to-zero（$1.95/h 按用量、冷启 35s 需预热或超时兜底）。Kev 是判定端点**原理上不能驱动 CLI 席位**（无文本生成，只有 /v1/systemone）；CLI 席位与本机 GPU 零交互。4B 验证上下文 8,192 tokens（小队菜单 state 仅几百 tokens，无压力）；L40S 并发 51.4 req/s vs 实需 ~2 req/s。
- **"几百 M"核查**：官方 Jev 参数不披露（1.14=10-14 发 `jev-preview`：图像/box/262k 上下文）；"几百 M"实为开源替代品：**Von 395M**（System One 协议兼容，jev.py 接入成本最低；但基座是英文 ModernBERT，中文 state 要试 **Laya 322-421M 的 mmBERT 多语档**）、NanoJev 0.6B——全是新生儿（4 周 20 个替代品），无独立评测，数字勿轻信。**轻量不是本项目约束（瓶颈是 OOD 判定质量），要试只能当消融实验的一个臂，先探针后上岗。**
- **域内微调升首选路**：官方 `--init_from` 热启动（从头训 0.33 vs 保留 0.83+新域 0.88 的对照实测），几百条标注够，4B 一次 ~$1/15min/H100，有 coding-agent skill（`npx skills add jaredpalmer/kev@kev-finetune`）半自动化；标注自产=events.jsonl（菜单+答案+结局已在记）。**此路把"社区更新"变量从方程删除**（自有 adapter 钉在冻结基座上）。附：`extern/kev` 可 pull 服务修复、`--run` 改带 `@v1.0` tag。
- **设计定论（用户质疑推动，后续实施照此）**：
  1. "随机选择一样有效"=正确检验形式 → **三基线消融**（Jev 实选 vs 随机选 vs 永远推进）+ **菜单去观点化**（查候选生成是否已把正确答案焊死——若焊死，模型与随机必然同效，Jev 是道具）；
  2. **纯脚本地板**（旧 executor + RW_LLM_MUTE + 角色关）为对照组：一切 AI 层价值=臂胜率−地板；三臂实验=纯脚本 / 纯慢层刚性军令(A) / 全程含 Jev(B)；
  3. 快层三选项：A=刚性军令到下一拍（姿态标签制现成）/ B=Jev 菜单 / C=脚本反射（已删）——"不要快层"=A，是正当研究臂；
  4. "完全剔除脚本"改**协议制**：每个脚本须记录 ①防什么失败（bug 表条目） ②什么实验能除名它 ③知识类（pedia/连通域，永留） vs 代理权类（阈值/默认项，逐个消融）；
  5. Jev 地位=**待证伪假设**不是组件；消融未跑前，驾驶位面试（待办②）按此验收标准执行。
- **grill-me 设计拷问已完成（2026-10-19）**：定案 9 条（研究优先/根原则"一切动作 LLM 决定"/双层闸/刚性军令默认/三臂消融/用户目测裁决/时钟+时间谓词/人类基准出局/模拟器三级走）+ 待裁决 6 问，全文见 `docs/decisions.md` §十三。

## 八、档案索引（历史与决策，按需查阅）

- `docs/decisions.md` — **关键 bug 教训表**（拆分前 §五，30+ 条）+ **路线决策记录**（§十一：1v9 终极目标/门票定义/阶段排期 0-5）+ **调研落地决策**（§十二：七份报告拷问沉淀）。长期查阅型，治"别再犯"。
- `docs/progress-archive/2026-07.md` — 拆分前 §一~§廿三：v3.2 旧架构全景（能力清单/未实测清单/真人实测评价）+ 7 月逐日工作记录（4.2 验收/建造快速执行层/执行层四连修/实控纪律/回放解析与蒸馏/平静期三病根/按厂科技档）。已冻结。
- `docs/progress-archive/2026-09.md` — 拆分前 §二十四~§二十九：Jev 评估与建造"乱"专项（引 docs/ 两份分析）、Kev 本地部署与探针、"造不好"四刀修复、架构二期设计共识→终稿→首局三连修+中层上岗+开局 v2。
- 拆分前节号与 P:xxx 行号在两份档案内沿用旧全局编号，仅近似对应；拆分前完整原文见 git 历史。
- 本文件是唯一权威状态源：改完大事先更新本文件（速览 §〇 与待办 §五必同步）；逐日工作记录攒够一段再折入档案，别再写回本文件。
