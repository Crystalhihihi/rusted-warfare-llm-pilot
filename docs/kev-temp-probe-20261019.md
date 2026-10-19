# Kev 温度双探针对照（2026-10-19，午睡批次 stretch）

目的：验证"0.8B 概率全挤 0.47-0.72 / choice 置信上限 ~0.47 = 终身残疾"的判决是否
只是**默认校准温度（2.35）的输出假象**。同权重同探针（`tools/test_kev_probe.py --lang zh`），
唯一变量：`KEV_TEMPERATURE`（默认 2.35 vs =1 原始概率）。

## 结果对照

| 项 | 校准温度 2.35（默认） | 原始温度 1.00 |
|---|---|---|
| 质量小计 | 7/13 | 7/13（不变，温度不改排序✓预期） |
| S1 engage（该打） | noul 0.71 | **0.90** |
| S1 target conf | 0.24 | **0.52** |
| S2 action conf | 0.11 | 0.28 |
| S3 where conf | 0.20 | 0.47 |
| S4 target conf | 0.47 | **0.86** |
| S5 rush（不硬闯） | 0.34 | **0.17**（更锐） |
| S5 action conf | 0.25 | **0.56** |
| 6 队并发墙钟 p50 | 292ms | 386ms（CUDA graph 重捕，可忽略） |

## 判决（修正 PROGRESS §七.7 的上游结论）

1. **"概率挤压/置信上限 0.47"≈ 温度 2.35 的校准输出假象**——原始概率分离度好得多，
   choice 置信到 0.5-0.86，margin/绝对阈值都重新可用。
2. **但硬判断不稳不是全由温度造成**：S2 劣势拒战两档都错向（engage 0.53/0.58 都 >0.5、
   都选"原地死守"）——0.8B 在 OOD 硬题上依然瘸，方向感对、硬判断虚。
3. **对 jev.py 的操作含义**：用原始概率（`KEV_TEMPERATURE=1`）+ 在本项目标注上
   自拟合温度（上游 `--init_from` 同路径，"阈值量在自己标签上"）。jev.py 现有
   绝对阈值（jev_focus_top≥0.35 等）是按校准输出口径定的，切原始概率后**必须重标**。
4. 环境备忘：HF 下载需 `HF_HUB_DISABLE_XET=1`（xet CAS 对 hf-mirror 401，见本批踩坑）；
   探针脚本跑 Windows 控制台要 `PYTHONIOENCODING=utf-8`（✓ 字符 GBK 崩）。

## 原始记录

- 校准档全文：`/tmp/kev_probe_calibrated.txt`（会话临时，重启即失；关键数已录入上表）
- 原始档全文：`/tmp/kev_probe_rawt.txt`（同上）
