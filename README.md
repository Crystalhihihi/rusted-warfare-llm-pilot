# Rusted Warfare LLM Pilot

中文 | **[English](README.en.md)**

> ## ⚠️ 严重警告
>
> **本项目处于实验阶段，严禁用于多人/联机对战。** 自动化操控在多人环境中违反
> 公平竞技原则，AI 的失误也会拖累队友。**仅限单机环境与本地逆向工程研究使用，
> 因违规使用造成的一切后果由使用者自负。**
>
> 当前已知限制（分层架构 v2 已修复早期的大部分操控缺陷）：
> - 水图已有基础支持（近水海厂/海军生产/水池采集/进攻水路拆分），特殊开局未充分测试
> - 战略层质量取决于所用 LLM，弱模型仍可能给出糟糕的攻防决策

将大语言模型接入即时战略游戏《铁锈战争》（Rusted Warfare）的研究项目：
以无侵入方式实时提取战况，交由 LLM 决策，再回注指令指挥自己的部队。

## 工作原理（三层架构）

```
Rusted Warfare (JVM)
  └─ javaagent: rw-agent.jar ──TCP 127.0.0.1:41234 / NDJSON──▶ bridge (Python)
                                                                 │
   ┌─ 战略层 LLM（约 25s/次，只做选择题：attack/defend/expand/build_army）
   │      token 预算约为纯 LLM 微操方案的 1/30
   ├─ 执行层 executor.py（确定性状态机：经济建造序列/生产调度/升级时机/建造者调度）
   └─ 反射层 reflex.py（纯规则毫秒级：受击撤退、基地回防）
                                                                 ▼
                                        原子指令经游戏正常命令队列回注执行
```

- **agent/**：Java `javaagent`，随游戏 JVM 启动挂载（不修改任何游戏文件），周期性输出
  战场快照（己方/盟友/可见敌人、资金、建造进度、生产队列、ASCII 地图与资源点）。
- **bridge/executor.py**：执行层。默认建造序列（补建造者→最近空闲池→工厂→饱和扩产），
  槽位化选址（厂区成行/后勤成排/海厂近水锚定），动作可用性自学习，建造者真并行，
  烂尾建筑自动续建；`LLM_OFF=1` 时可完全脱离 LLM 独立运营。
- **bridge/multiagent.py**：多 agent 架构（默认开启，`multi_agent=true`）。总指挥（50s/次，reasoner）
  产出持久《作战计划书》；建造 agent（10s/次）与战斗 agent（6s/次，警报 2.5s，快模型）并行异步，
  各在自己指令域行动；共享状态=快照摘要+计划书+目标构成，APM 配额隔离。
- **bridge/pilot.py**：单 LLM 战略层（`multi_agent=false` 时的回退路径）。把战场蒸馏为 12×12 战略格
  + 执行层状态，每 ~25 秒要一个战略动词，翻译为执行层目标或原子指令。
- **bridge/reflex.py**：反射层。小队受击撤退（血量急跌触发）、基地遇袭回防。
- **tools/**：分析探针源码与「带 agent 启动游戏」的启动脚本。

公平性设计：只转发本队视野内信息；指令走游戏正常命令队列；全程限速。

## 快速开始

1. **构建 agent**（需要 JDK 11+）：
   编辑 `agent/build.sh` 中的 `GAME_DIR` 指向你的游戏安装目录，然后执行
   `bash agent/build.sh`，产物为 `agent/dist/rw-agent.jar`。
   （游戏 jar 仅编译期引用，不会打包进 agent。）
2. **配置模型**：复制 `bridge/secret.toml.example` 为 `bridge/secret.toml` 并填入
   DeepSeek API Key（或设置环境变量 `DEEPSEEK_API_KEY`）。
   `bridge/config.toml` 可调整 `base_url`、`model`、节拍、APM 上限等参数。
3. **启动游戏**：编辑 `tools/run-with-agent.bat` 中的游戏目录与本项目路径后运行，
   游戏即携带 agent 启动。
4. **运行桥接**：
   - `python bridge/main.py` —— 战况摘要打印（联调验证用）
   - `LLM_OFF=1 python bridge/pilot.py` —— 纯执行层模式（零 token，验证自动运营）
   - `python bridge/pilot.py` —— 完整模式（执行层运营 + 反射层救急 + LLM 战略指挥）

   游戏内需先委托单位：点击 overlay 面板的「全队接管」，或框选单位后点「委托选中」。

## 合规声明

- **严禁用于多人/联机对战**（见顶部警告）：AI 操控存在大量已知缺陷，
  会拖累队友并违反公平竞技原则。
- 本项目仅供**单机环境与逆向工程学习研究**使用。
- 仓库不包含游戏本体及任何反编译产物；请支持正版游戏。
- 本项目与游戏开发商 Corroding Games 无任何关联。

## 开源许可

[MIT](LICENSE)
