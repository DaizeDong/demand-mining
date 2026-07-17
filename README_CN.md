# demand-mining

已发布产品的每日用户需求挖掘 + 竞品/热点追踪 + EOD 头脑风暴 + RICE/Kano 量化迭代排序。

[![Claude Code Skill](https://img.shields.io/badge/Claude%20Code-Skill-orange?style=flat)](https://docs.anthropic.com/en/docs/claude-code)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Status](https://img.shields.io/badge/status-v0.1%20offline%20skeleton-green?style=flat)](ROADMAP.md)
[![Languages](https://img.shields.io/badge/Languages-EN%20%2F%20CN-blue?style=flat)](#languages)
[![Roadmap](https://img.shields.io/badge/Roadmap-v0.1.2-purple?style=flat)](ROADMAP.md)

[English](README.md) | [中文版](README_CN.md)

---

## ⭐ 先读这里, 设计理念

**LLM 提议,确定性 gate 裁决,而 gate 首先守护隐私。** 已发布产品的用户信号杂、敏感、易误排。
所以每个判断(读 Discord 会话、还原意图与 JTBD、提议打分)交给模型,但每个**裁决**,什么算需求、
什么该合并、什么该做、什么该推,由 fail-closed 的纯 Python gate 做出;且在模型看到任何消息**之前**,
`redact.py` 先脱去 PII。需求池只存脱敏提炼项,绝不存原始对话。

它是 `market-intel` 预留的编排产品、`daily-hotspots` 的孪生:只拥有 *seam*(节律、池、打分、推送),
**深活全部委托**,绝不重写检索、验证、Discord 监听层或热点扇出。

📜 **[完整设计理念 -> PHILOSOPHY.md](PHILOSOPHY.md)**

---

## 它是什么(不是什么)

**是:** 单个已发布产品的每日需求雷达。摄取产品社群信号(Discord),抽取真实需求(显性 + 隐性,
JTBD 还原),跨日去重 + 按独立人累计强度入池,三轴正交排序(RICE 定顺序 / Opportunity 定强度 /
WSJF 定紧迫 / Kano 定性质),产出 EOD 头脑风暴 + 量化迭代方向队列。

**不是:** 第二个 Discord bot(共享 auto-support 监听层)、热点采集器(消费 daily-hotspots)、
竞品调研引擎(受闸委托 market-intel)、数据库(需求池=schedule-reminder 基座,仅 CLI)。它是薄 seam,
不是引擎。

## 安装

```
/plugin install github:DaizeDong/demand-mining
```

或手动克隆:

```bash
git clone https://github.com/DaizeDong/demand-mining.git ~/.claude/plugins/demand-mining
```

## 快速开始

```bash
# 离线预览——跑完整确定性尾链,不写库不联网
python skills/demand-mining/scripts/run.py --in candidates.json --dry-run --no-ledger

# 真实 EOD(headless,经调度 wrapper)
powershell -ExecutionPolicy Bypass -File skills/demand-mining/scripts/register-task.ps1 -Time 21:53
```

`candidates.json` 是候选需求簇列表(由 SKILL 的 LLM 层从实时 Discord + 外部扇出产出);gate 跑
redact → score → dedup → verify → push → pool → digest → watermark。

## 如何触发

触发词:**需求挖掘 · demand mining · 迭代建议 · EOD 汇总**,或每日定时运行。

## 示例输出

**推送到 Discord**, 每日一条排序「需求头条」(top ≤5 合格需求),不再逐需求发卡片:

```
📊 **需求头条** · 2026-07-15
合格 8 · 精选 5 · 剔噪 1 · 候选 12

**1.【立即·刚需】可靠地导出我的数据**
用户反复手动逐月下载再转表格,几十分钟重复劳动,论坛高频抱怨。建议:一键区间导出 CSV/Excel。
A 78 · RICE=9 · 3证据
...
📄 完整版(全部字段 + RICE + 证据): 私有归档 2026/2026-07-15.md
```

【】标签是需求的**紧迫度·需求性质**(立即/本周/本月 · 刚需/期望/惊喜)。与 `daily-hotspots` 孪生不同,
头条**不含任何链接**, 本 skill 挖私密对话,fail-closed 出口门遇链接即中止,证据保持私有,完整 digest
用纯文本指针指向私有归档。

**归档**的 digest 文件保留完整迭代方向队列,每行三轴齐显:

```
1. [tier0/immediate] reliably export my data — final 78 · RICE(R=6,I=3,C=1.0,E=2)=9 ·
   Opp=16(intensity 12, 4 人) · WSJF=4.8 · Kano=must_be · 竞品 competitorX · 证据×3
```

加 Quick-win / Big-bet 双池。空日诚实打印 `今日无合格新需求`。

## 局限

- v0.1 为**离线骨架**:确定性尾链(脱敏/抽取/去重/打分/gate/汇总)真实且已测;实时 Discord tap +
  真实 secrets + 竞品 changelog diff 在 v0.2(见 ROADMAP)。产品代码根与 Discord bot 接线 `@DEFERRED`,
  待提供。
- 隐性需求召回是死穴,靠持续扩充对抗 fixture 迭代提升。
- Kano 为 LLM 代理(无问卷),上线后用真实社群样本校准。

## 配置

`demand-mining` 是**带 config 的 skill**, 每产品的可调参数(RICE 权重、阈值、Kano 映射、taxonomy、
推送上限)与密钥(假名 HMAC salt、Discord 凭证)都放在一个**独立、私有**的伴随 config 仓里。完整规范见
[CONFIG.md](CONFIG.md)。缺失则回落内置 `scripts/lib.py:DEFAULT_CONFIG`。

- **挂载(发现顺序):** `$DEMAND_MINING_CONFIG` → `~/.demand-mining-config/` →
  `~/.config/demand-mining-config/`。命中第一个即用;都没有则跑默认值。
- **首次配置:**
  ```bash
  python scripts/init_config.py --product <slug>  # 生成骨架(确定性)
  export DEMAND_MINING_CONFIG=~/.demand-mining-config                   # 或给 init 传 --out <dir>
  python scripts/verify_config.py                  # doctor:逐项 PASS/FAIL 报缺
  ```
- **切换 config(即插即用):** 把环境变量指向另一个 config 目录即可, config 自包含,无需别的改动:
  `export DEMAND_MINING_CONFIG=~/configs/work` ↔ `~/configs/personal`。
- **密钥:** Mode B, `secrets/*` 已 gitignore,永不入库,请用库外备份。假名 salt 也可改由
  `$DEMAND_MINING_PSEUDONYM_SALT` 提供。

## 语言

中文 (`README_CN.md`) · English (`README.md`, 权威版)

## Roadmap · 贡献 · 许可

见 [ROADMAP.md](ROADMAP.md) · [CONTRIBUTING.md](CONTRIBUTING.md) · [LICENSE](LICENSE)(MIT)。
