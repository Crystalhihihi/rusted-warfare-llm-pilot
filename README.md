# Rusted Warfare LLM Pilot

> ## ⚠️ 严重警告
>
> **本项目处于实验早期阶段，AI 对游戏的操控存在大量已知缺陷，包括但不限于：**
>
> - 资源点建造可能失败（选址/占用判定不完善）
> - 生产与建造种类单一、升级功能基本不可用
> - 部分开局场景下定位混乱（找不到主基地、远赴无关资源点）
> - 建筑可能被错误纳入移动指令、宏观操作基本串行
>
> **严禁将本项目用于多人/联机对战。**
> 一方面 AI 的失误操作会严重拖累队友、破坏他人游戏体验；
> 另一方面自动化操控在多人环境中违反公平竞技原则。
> **本项目仅限单机环境与本地逆向工程研究使用，因违规使用造成的一切后果由使用者自负。**

将大语言模型接入即时战略游戏《铁锈战争》（Rusted Warfare）的研究项目：
以无侵入方式实时提取战况，交由 LLM 决策，再回注指令指挥自己的部队。

## 工作原理

```
Rusted Warfare (JVM)
  └─ javaagent: rw-agent.jar ──TCP 127.0.0.1:41234 / NDJSON──▶ bridge (Python)
                                                                     │  OpenAI 兼容 API（默认 DeepSeek）
                                                                     ▼
                                              LLM 决策 → 指令经游戏正常队列回注执行
```

- **agent/**：Java `javaagent`，随游戏 JVM 启动挂载（不修改任何游戏文件），周期性输出
  战场快照（己方/盟友/可见敌人单位统计、资金、队伍状态、ASCII 降采样地图等）。
- **bridge/**：Python 桥接层（仅标准库，Python ≥ 3.11）。把快照组织为提示词调用
  OpenAI 兼容 API，将模型的工具调用翻译成游戏指令回注。内置自适应节拍
  （有敌情加速、无敌情降速）、APM 上限与拟人化间隔、编队委托热更新。
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
   - `python bridge/pilot.py` —— LLM 指挥官主循环

## 合规声明

- **严禁用于多人/联机对战**（见顶部警告）：AI 操控存在大量已知缺陷，
  会拖累队友并违反公平竞技原则。
- 本项目仅供**单机环境与逆向工程学习研究**使用。
- 仓库不包含游戏本体及任何反编译产物；请支持正版游戏。
- 本项目与游戏开发商 Corroding Games 无任何关联。

## 开源许可

[MIT](LICENSE)
