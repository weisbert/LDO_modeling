# Handoff — PMU 行为模型缺了 DC/大信号层（修 fit + emit）

> 下一场 ultracode 的任务：把老单口 `fit_model.emit_va` 有、新 PMU `emit_pmu_va` 被砍掉的
> **DC 操作点层**（线性调整 line-reg + 馈通 Cft + DC-vreg + 负载调度 vreg）移植进
> PMU 的 fit+emit。**纯代码修复**；多负载/温度/电源扫的重抽是用户那边（见 §数据侧）。

## 症状（用户在真实 PMU TB 里对比看到的）
1. **启动大 drop**：真实 LDO 启动时输出掉到 ~704mV（从 800），新 PMU 模型掉得不够。
2. **稳态高 20mV**：新模型 vdc 比真实高 ~20mV，且**负载一变就变**。
3. （5G 纹波偏高 = 另一层 HF Zout 保真度问题，本任务先放一边。）

## 关键事实（决定性）
- 用户**老的"手导入"单口模型**（`harness/fit_model.emit_va`，`slew_en=0` 纯 LTI）**完美复刻**
  了启动 drop + 那 20mV。→ **不是 LTI 做不到**，是新 PMU 流程把 DC 层砍了。
- 参考目标文件就在仓库：`model/ldo_base_spectre.va`（老单口，含完整 DC 层）。
  新 PMU 产物结构对照见下。

## 三层 root cause
- **L1 运行/数据**：这次建模是**单角点**跑的产物 —— 进 fit 的 npz `loads=['tt_25c']`（一个工艺
  角点标签，`step_import` 设 `loads=[corner]`），**不是**多负载扫。manifest 声明了 pll 5 + vco 6
  个负载 × 3 温度，但没用上 → fit 只见 1 个负载 → `emit_pmu_va` 走 **Single-OP** baked → **无负载调度
  → 无负载调整率 → 20mV（随负载变）**。引擎 `run_pmu_coverage_sweep` 存在且 `_has_coverage_sweep`
  对该 manifest=True，但产出此模型的那次没走它（**box 很可能还没 `bash apply` 多负载扫接线**）。
- **L2 manifest 声明**：`dc_linereg`（电源扫）和 `dropout` 扫 **压根没声明**（coverage 里 dropout:None，
  无 supply 扫）→ 就算重跑多负载也补不出启动那条。
- **L3 emit**：`emit_pmu_model._voltage_body` 丢了 `fit_model.emit_va` 的整层 DC：

| 功能 | 老单口 `ldo_model`（好用） | 新 PMU `emit_pmu_va`（缺） | 影响 |
|---|---|---|---|
| 调整后输出 | `vregeff = vreg + lrsh(vdd)` | 只有 `V(vrg)<+vreg`（死钉 0.8） | DC + 启动 |
| 线性调整 lrsh (R3-L1) | 有（`dc_linereg` 多项式跟电源） | **无** | 启动跟电源斜坡 |
| Cft 馈通 vin→vout | 有 (1.74e-13) | **无** | 启动/HF 电源耦合 |
| DC-vreg | 实测（≈0.7998）+ 负载调度 | baked=0.800000（目标值，非实测） | 20mV |
| voutdc / vdd 旋钮 | 有 | **无** | DC 钉位 |
| 负载调度 vreg/参数 | `ln(iload)` 过 {100,200,300µ} | 单点 | 负载调整率 |
| dropout/slew 支路 | 有（`slew_en`，用户用=0） | 无（低优先，LTI 核已够） | 压差 |

## 修复（CODE — 本场 ultracode 干）
全部 **opt-in / 默认惰性**：所需数据（`dc_linereg`/`dc_dropout`/多负载）缺席时 → 发射与今日**逐字节相同**
（不动现有单点跑的回归）。

### A. `harness/fit_multiport.py`（电压 fit 环，~line 100–180）
1. **Cft 接出**：已经调了 `FM.fit_cft()`（line 105）拿到 `FM.CFT`，但没传给 emit。把每条 rail 的 `cft`
   带进 fit_result，供 emit 用（`CFT>0` 才发）。
2. **线性调整多项式**：当 `dc_linereg` 在该 rail 的 npz 里 → 调 `fit_model` 的 line-reg 拟合
   （`fit_model.py:~1352`，"R3-L1 settable-VDD DC shift" 那段，返回 4 阶多项式系数+残差），带进 fit_result。
3. **DC-vreg 实测**：用实测的稳态输出当 vreg（不是 manifest 目标 0.8）。若有 DC 操作点读数就用它。
4. **dropout**（可选，较低优先）：`dc_loadreg`/`dc_dropout` 在 → 调 `fit_model` dropout 拟合（`~1245`）。

### B. `harness/emit_pmu_model.py`（`_voltage_block` / `_voltage_body` / `emit_pmu_va`）
1. **lrsh 线性调整**：发 `vddc=clamp(vdd,..)`；`lrsh = poly4(vddc)`；`vregeff = (voutdc>0)? voutdc+Ra*ic : vreg+lrsh`；
   `V(vrg)<+vregeff`。`dc_linereg` 缺 → `lrsh=0`，`vregeff=vreg`（与今日逐字节相同）。
2. **Cft 馈通**：`CFT>0` 时发 `I(vin,vout)<+Cft*ddt(V(vin,vout))`（参考老 .va 的 "feedthrough cap" 段）。
3. **vdd / voutdc 实例参数**：`vdd` 同时驱动 line-reg + PSRR 参考（注意把现在的 `vdc_<supply>` 与之统一/对齐）；
   `voutdc` 钉 DC。
4. **负载调度 vreg**：调度路径已支持（多负载时自动走）—— 确认多负载 npz 进来时确实调度。

> 直接照抄 `model/ldo_base_spectre.va` 的 DC 段（references / R3-L1 / Cft）作为目标模板；
> 老 `fit_model.emit_va` 的发射代码就是源头，逐段搬进 PMU 的 `<o>_` 命名空间。

## 数据侧（用户那边 / EXT，不在本 CODE 任务内）
1. **box `bash apply`** → `run_pmu_coverage_sweep` 接线生效 → 重跑多负载×温度 → 从那份多负载 npz 重 fit
   （负载调度 → 修负载调整率/20mV）。
2. **manifest 补两项扫**：`REAL_wur_pmu_top.json` 加 `dc_linereg`（电源扫）+ `dropout` 扫的 coverage 声明。

## 无回归红线
- `pytest harness/test_emit_pmu_model.py`（+ test_emit_pmu_current / test_fit_multiport_depth /
  test_antifootgun / cadence/insitu/test_pmu_corner）保持绿；**默认惰性路径逐字节相同**（加锁测试）。
- GUI selftest PASS（`QT_QPA_PLATFORM=offscreen python3 gui/ldo_modeler.py --selftest`）。
- 发射的 .va 过**本地 Spectre 18.1 `-64`** 编译（ahdlcmi 0 err；`cadence/spectre_run.py` 可用）。
- 新增 DC 层只在其数据存在时发射（opt-in，像 d2/admittance-zero/Cft 一样 keep-best/gated）。

## 验证（vs 独立 GT，不是 vs 自己）
`cadence/spectre_bench.py`（`spice_dut` 真实管子 / `va_dut` 模型）：
- `measure_dc_loadreg` / `measure_dc_linereg`：负载/电源扫，确认稳态 DC 与 GT 对齐（修 20mV）。
- `measure_loadstep` + 电源斜坡：确认启动/负载瞬态跟 GT（修启动 drop）。
- 参照本会话已建的 GT-vs-model load-step 对比方法（model 比 GT 多振铃那张图的套路）。

## 现成线索 / 文件
- 老单口参考（含 DC 层）：`model/ldo_base_spectre.va`（仓库内，`slew_en=0` 即纯 LTI 完美复刻版）。
- 新 PMU 产物：`harness/emit_pmu_model.emit_pmu_va` 输出（单 OP、无 DC 层）。
- 源码：`fit_model.py` emit_va 的 DC 段 + `fit_model.py:~1352`(line-reg) / `:~44`(fit_cft) / `:~1245`(dropout)。
- 运行引擎：`cadence/insitu/pmu_corner.run_pmu_coverage_sweep`（多负载扫，已存在）。
- 记忆：`[[redzone-real-ldo-debug-and-selfcontained-report]]`、`[[pmu-split-ground-export]]`。
