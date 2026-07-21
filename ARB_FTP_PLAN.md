# 套利检测 + FTP 报价引擎 — 设计与计划

## 1. 目标与验收

- **交易前按需运行**的 Python pipeline:拉取当前 Bloomberg 市场数据(FXFA:spot + forward points,以及市场利率),对齐多渠道报价(pilot 仅 AFS,后续加内部总行融资等),秒级扫出套利机会。
- **监控市场 inefficiency**:Bloomberg 市场报价里不满足 CIP 的跨币种基差等。
- **产出 FTP 报价**,并保证**跨币种无套利**:任何人不能从我们这里借入一种货币、FX swap 成另一种、再借回给我们套利。
- 每个机会 flag 出:**收益(bps)** + **风险类型**(流动性风险等)。

对应实现:`quote_ocr/arb.py`(引擎)、`quote_ocr/bloomberg.py`(市场数据,live+mock)、`quote_ocr/pricing.py`(CIP)、`scan.py`(交易前 CLI)。当前可离线用 mock 跑通,接终端后加 `--live`。

## 2. 架构 / 数据流

```
渠道报价                     Bloomberg (blpapi, 交易前实时)
  AFS(OCR→quotes.db)   ┐        spot + forward points (FXFA)
  内部总行融资 (后续)   ├─▶ 引擎 ◀── 市场利率 OIS/HIBOR (basis 用)
  更多渠道 (后续)       ┘         │
                                 ▼
        ┌───────────────────────────────────────────┐
        │ 1. 构建 funding surface: ccy×tenor×{bid,offer}│
        │ 2. 构建 FX 市场: pair×tenor×{spot,points}     │
        │ 3. 三类套利检测(见 §5)                       │
        │ 4. FTP 报价生成 + 无套利修正(§6)             │
        │ 5. 风险分类 + bps(§7)                        │
        └───────────────────────────────────────────┘
                                 ▼
     机会清单(pair, tenor, bps, 风险类型, 渠道/路径) + FTP 报价面(无套利)
```

## 3. 数据模型

- **funding surface**:`{ccy: {'bid': {tenor: rate}, 'offer': {tenor: rate}}}`,rate 为年化小数(3.85% → 0.0385)。`arb.surface_from_store()` 从 `quotes.db` 直接构建。
- **FX 市场**:`{pair: {tenor: FxPoint(spot, points)}}`,`bloomberg.build_fx_market()` 产出。
- **约定(待你最终确认 AFS 列含义)**:`offer` = 借入该货币的利率,`bid` = 拆出/存放该货币的利率。这是 `arb.py` 的默认;确认后若相反,改一个 flag 即可。

## 4. 核心数学(CIP)

以 pair = BASE/QUOTE(报价为每 1 BASE 对应多少 QUOTE,如 USDCNH = CNH per USD)、spot S、远期外汇 F = S + points/pip、期限 t(ACT/360):

$$\frac{F}{S} = \frac{1 + r_{quote}\,t}{1 + r_{base}\,t}$$

- 借 BASE、FX swap 成 QUOTE ⇒ **合成 QUOTE 融资利率**:`implied_quote_rate = (F/S·(1+r_base·t) − 1)/t`
- 借 QUOTE、FX swap 成 BASE ⇒ **合成 BASE 融资利率**:`implied_base_rate = ((1+r_quote·t)/(F/S) − 1)/t`

**无套利**要求:任何"借一腿 + swap + 拆另一腿"的往返净收益 ≤ 0。

## 5. 三类套利检测

### (a) 渠道套利 —— 已实现(`scan_surface_noarb`)
用某渠道(pilot=AFS)两条腿的 funding + Bloomberg FX swap,找往返 > 阈值的:
- 借 BASE@offer → swap → 拆 QUOTE@bid:若 `synth_QUOTE_borrow < QUOTE_bid` → 我们套利,`bps = (QUOTE_bid − synth)·1e4`;
- 借 QUOTE@offer → swap → 拆 BASE@bid:对称。

这正是"从渠道价 + 市场 FX 找当下最新的套利空间"。`scan.py` 交易前跑一遍即可。

### (b) 市场 CIP basis —— scaffold(phase 2)
纯市场无效率(xccy basis):Bloomberg **市场利率(OIS/HIBOR)** vs **FX-swap 隐含利率** 的偏离。需要 OIS 等 ticker(§10 待确认)。结构上与 (a) 相同,只是两腿都用 Bloomberg 市场利率而非渠道价。风险类型标注为"basis / 需资产负债表容量"。

### (c) 自有 FTP 报价的跨币种无套利 —— 已实现(同引擎,作用于我们自己的报价面)
把我们发布的 FTP 面 `{ccy: bid/offer}` 喂给 `scan_surface_noarb(kind='ftp_self_arb')`。任何被 flag 的往返 = **我们被套利的漏洞**。你的例子:
> EUR 的 offer(客户从我们借 EUR)对应 implied USD 若低于我们 USD bid(客户把 USD 拆回给我们)⇒ 客户借 EUR→swap USD→拆回 USD 套我们。

引擎里就是 `implied_quote_rate(F/S, EUR_offer, t) < USD_bid` 触发。**FTP 面必须扫描后为空**才能发布。

## 6. FTP 报价生成 + 无套利修正

Pilot 版(AFS-only)分两步:
1. **成本加点**:FTP = 渠道最优融资成本 ± 内部利差(margin)。borrow 侧取各渠道 offer 的最优,lend 侧取 bid 的最优。
2. **无套利修正**:对生成的 FTP 面跑 (c)。若某处被套利,**收紧**违约的一侧(抬高 bid 或抬高 offer)直到 `edge ≤ 0`,取所有约束的紧边界。多币种时对每个 pair×tenor 的两个方向迭代到收敛。

Phase 2 把 §「Axpo 边际定价」框架接入:margin 不再是常数,而是该笔头寸对 **组合 variance + LCR/NSFR** 的边际贡献(见 `PRICING_FRAMEWORK` 讨论)。

## 7. 风险分类 + bps

- **bps**:往返净利差已年化,直接 `×1e4`。
- **风险类型**(`classify_risk`,可扩展):
  - 任一腿为离岸货币(CNH/HKD/…)→ `liquidity (offshore ccy funding)`;
  - 期限 ≥ 3M → `liquidity (term funding) + basis`;
  - 否则 → `execution / rollover`;
  - 恒附:对手方/渠道(counterparty),以及是否占用 LCR/NSFR/资产负债表(phase 2 量化)。

## 8. 运行流程(交易前,带终端)

```powershell
# 每次有交易需求前跑一遍,确保市场腿是当下最新 FXFA 数据:
python scan.py --db data\output\quotes.db --date 2026-07-16 --live
```
输出按 bps 降序的机会清单(pair / tenor / bps / 风险类型 / 借-swap-拆路径)。离线开发用不带 `--live`(mock 市场数据)。

## 9. 分阶段

1. **Pilot(现在)**:AFS 单渠道 + Bloomberg FX。检测 (a)、(c);(b) scaffold。✅ 引擎可离线跑通。
2. **+ 内部总行融资**:再加一个 channel 的 surface,`scan` 支持多渠道对比(跨渠道套利:A 渠道借、B 渠道拆)。
3. **+ 更多渠道 & 市场利率**:接 OIS ticker,启用 (b) 市场 basis 监控。
4. **+ 组合边际定价**:FTP margin 用 variance+LCR/NSFR 边际贡献(Axpo 思路)。

## 10. Bloomberg 字段待你在终端确认(§`bloomberg.py FX_TICKERS`)

- spot:`USDCNH Curncy` / `EURUSD Curncy` 的 `PX_LAST`?
- 远期:`USDCNH1M Curncy` 的 `PX_LAST` 返回的是 **forward points 还是 outright**?pip 因子(USDCNH 用 1e4?)。
- basis(phase 2):USD OIS(SOFR)、EUR OIS(ESTR)、CNH 的市场利率 ticker。
- 确认后只改 `FX_TICKERS` / `TENOR_CODE`,引擎不动。

## 11. 未决问题(影响数值,不影响结构)

1. **AFS bid/offer 的确切含义**:是否为货币的存/贷(borrow/lend)利率?哪一侧是借、哪一侧是拆?(决定 §3 约定)
2. 远期腿 ticker 返回 points 还是 outright、pip 因子。

以上两点确认后,数值即为可交易口径;当前引擎与路径判断已就绪。
