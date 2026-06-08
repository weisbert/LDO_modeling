# LDO 建模 GUI —— 操作手册（启动 / 黄区打包 / 红区部署与更新）

本手册覆盖五件事：**①启动 GUI ②黄区（Windows 联网）打包 ③传输到红区 ④红区初次部署 ⑤红区增量更新**。
所有命令均可直接复制。英文参考版见同目录 `README.md`；建模原理见仓库根目录 `GUI_DEPLOY_BUILD.md`。

> 角色约定
> - **黄区**：Windows，**有网络**。负责下载并审计 Linux 依赖轮子、打包。
> - **红区**：CentOS7 一类的 **Linux，glibc 2.17，已装 python3.11，无网络（隔离网/气隙）**。负责安装运行。
> - GUI 是**纯解析**对比（不调用任何仿真器），所以红区只需 numpy/scipy/matplotlib/PyQt5，**无需 ngspice/Spectre**。

---

## 0. 目录结构（红区安装后）

```
/opt/ldo_modeler/
  .venv/                 # 一次性建好的虚拟环境（增量更新不动它）
  wheels/                # 离线轮子（保留，供以后重建 venv）
  app/                   # 源码：harness/ cadence/ gui/ deploy/（每次增量更新被整体替换）
  results/               # 用户数据：导入的 npz —— 持久保留，软链到 app/results
  model/                 # 用户产物：导出的 .lib/.va/.tbl —— 持久保留，软链到 app/model
  MANIFEST.deployed.json # 当前已部署包的清单（含 requirements_hash）
  INSTALL.json           # 安装时间戳 + req-hash（增量更新的校验基准）
```
关键设计：**用户数据 (`results/`、`model/`) 放在 `app/` 之外**，再软链进 `app/`。这样增量更新 `rm -rf app/` 时数据不会丢。

---

## 1. 启动 GUI

### 1.1 红区（已部署后，日常使用）
```bash
/opt/ldo_modeler/.venv/bin/python /opt/ldo_modeler/app/gui/ldo_modeler.py
# 想直接载入一份已有参考数据看效果：
/opt/ldo_modeler/.venv/bin/python /opt/ldo_modeler/app/gui/ldo_modeler.py --ref /opt/ldo_modeler/results/<name>.npz
```

### 1.2 黄区/开发机（自检或试用，需先 `pip install PyQt5`）
```powershell
.\.venv\Scripts\python.exe gui\ldo_modeler.py                       # 空白启动
.\.venv\Scripts\python.exe gui\ldo_modeler.py --ref results\ref\v5_spur.npz   # 预载示例
```

### 1.3 无界面自检（部署冒烟测试用，不弹窗）
```bash
QT_QPA_PLATFORM=offscreen python gui/ldo_modeler.py --selftest --require-qt
# --require-qt：Qt 必须能导入，否则退出码非零（部署时用）。
# 去掉 --require-qt：Qt 不可用时只跑纯逻辑自检（裸容器演练用）。
```

---

## 2. GUI 使用流程（四个标签页 = 工程师的工作流）

1. **① Profile（建模配置）**：**只需填 3 个带 `*` 的**：模型名、供电电压 Vin、3 个负载电流（低→高，逗号分隔，如 `20u,121u,250u`）→ 点 **Apply profile**。其余可不管：标称角是下拉框、自动选中间档；**Cout/ESR 留空即从 Zout 自动提取**（只想核对才填）。每个框有鼠标悬停说明。
2. **② Import data（导入 + 护栏）**：最省事——把所有导出文件放一个文件夹（命名如 `z_20u.csv, p_20u.csv, noise_20u.csv, z_hf.csv, dc_loadreg.csv, dc_dropout.csv`），点 **Import from folder…** 自动填满表格；否则按格子手选。表格分 **Required（必需）** 与 **Optional（可跳过）** 两组。选复数格式（auto 读表头）；噪声若是 V²/Hz 勾选转换；点 **Import + preview** → 画原始数据 + 护栏告警（PSRR 当成 dB、噪声当 V²/Hz、Zout 方向反等），并提示还缺哪些必需项。**Measurement guidance** 看测试台建议。
3. **③ Fit（拟合）**：点 **Fit**（后台线程，不卡界面；缺必需数据会先提示、不会崩）→ 每角残差表（Zout/PSRR/噪声 dB）→ **Emit .lib/.va** 生成模型到 `model/`。
4. **④ Compare（对比）**：GT（实线）对比解析模型（虚线）：Zout 幅/相、PSRR 衰减/相、噪声 PSD；下拉切换负载角。**纯解析、不跑仿真**，曲线即拟合质量。

CSV 列格式约定见 `cadence/import_cadence.py` 头部 / `CADENCE_EXTRACTION.md`（npz 数据契约）。

---

## 3. 黄区打包（Windows，有网络）

> 黄区运行 `package.py` 只需 **Python 3.11 + pip**（不必装 PyQt5/numpy，打包脚本只下载+审计+打 tar）。

### 3.1 完整包（FULL）—— 初次部署 / 依赖有变时用
```powershell
.\.venv\Scripts\python.exe deploy\package.py full --out dist\
```
做了 6 件事：① 收集 `app/`（harness+cadence+gui+deploy）；② 按 cp311 / x86_64 / manylinux2014 **跨平台下载**红区 Linux 轮子；③ **审计每个轮子的 glibc 标签，发现 > 2.17 立即失败**并提示要降版的包；④ 冻结 `requirements.lock`；⑤ 写 `MANIFEST.json`（git SHA、版本、req-hash、逐文件 sha256 校验和）；⑥ 打 tar。

产物：
```
dist\ldo_modeler_full.tar.gz            # 给红区的完整包（约 146 MB）
dist\ldo_modeler_full.tar.gz.sha256     # 校验和
dist\MANIFEST.full.json                 # 供后续增量包做依赖比对基准
```
> 已验证可用的 glibc-2.17 版本集：numpy 1.26.4 / scipy 1.15.3 / matplotlib 3.9.4 / pillow 12.2.0 /
> PyQt5 5.15.10 / **PyQt5-Qt5 5.15.2** / PyQt5-sip 12.15.0。要改版本编辑 `deploy/requirements-gui.txt`，
> 重跑 `full`，审计会守门。

### 3.2 增量包（INCREMENTAL）—— 只改了代码、依赖没变时用
```powershell
.\.venv\Scripts\python.exe deploy\package.py incremental --out dist\
# 默认读 dist\MANIFEST.full.json 作基准；也可 --last <某个 MANIFEST.full.json>
```
- 只打 `app/` 源码（**不含轮子**，约几十 KB）。
- **强制护栏**：若 `requirements-gui.txt` 相对上次 full 变了 → **直接中止**，提示你改回去或重做 full。
- 产物：`dist\ldo_modeler_incremental.tar.gz`（+ `.sha256`）。

### 3.3 单独审计某个轮子目录（可选）
```powershell
.\.venv\Scripts\python.exe deploy\audit_wheels.py <轮子目录> --max-glibc 2.17 --arch x86_64
```

### 3.4 上气隙前演练（强烈建议，需 Docker）
```bash
deploy/dryrun_manylinux2014.sh dist/ldo_modeler_full.tar.gz
# 在真正的 glibc 2.17 镜像里、断网（--network none）跑一遍离线安装，提前暴露问题。
```

---

## 4. 传输到红区

用审批过的介质把 **`ldo_modeler_full.tar.gz`（及 `.sha256`）** 拷到红区。建议落地后先核对完整性：
```bash
sha256sum -c ldo_modeler_full.tar.gz.sha256
```
（bootstrap 内部还会再用 MANIFEST 的逐文件 sha256 校一遍。）

---

## 5. 红区初次部署（FULL → bootstrap.sh）

前置：红区有 `python3.11` 在 PATH 上（不在 PATH 可用 `PYTHON=/path/to/python3.11` 覆盖）。

```bash
tar xzf ldo_modeler_full.tar.gz -C /tmp/ldo_full
cd /tmp/ldo_full
./bootstrap.sh /opt/ldo_modeler          # 不给参数则默认 /opt/ldo_modeler
```
`bootstrap.sh` 五步：
1. **校验完整性**：用 MANIFEST 的 sha256 逐文件核对，发现缺失/损坏即中止。
2. **铺目录**：拷 `app/ wheels/ requirements.lock`；建持久的 `results/ model/` 并软链进 `app/`。
3. **建 venv**：`python3.11 -m venv .venv`（不打包解释器，用红区自带的）。
4. **离线装依赖**：`pip install --no-index --find-links=wheels -r requirements.lock`（`--no-index` = 完全不联网）。
5. **冒烟测试**：`QT_QPA_PLATFORM=offscreen gui --selftest --require-qt`（合成一份解析参考，跑 导入→拟合→预测→导出→Qt 渲染 全链路）。最后写 `INSTALL.json`。

> 裸容器（缺 Qt 的系统 .so）演练时设 `SMOKE_REQUIRE_QT=0 ./bootstrap.sh ...`：离线安装与 numpy/scipy 导入照样验证，仅不强制 Qt。真实红区跑 Virtuoso，xcb/libGL 已具备，用默认严格模式即可。

完成后启动见 §1.1。

---

## 6. 红区增量更新（INCREMENTAL → update.sh）

适用：**只改了代码、依赖没变**（依赖变了必须走 §5 的 FULL）。

```bash
tar xzf ldo_modeler_incremental.tar.gz -C /tmp/ldo_incr
cd /tmp/ldo_incr
./update.sh /opt/ldo_modeler
```
`update.sh` 做的事：
1. **依赖一致性护栏**：比对本包 req-hash 与已部署的 `INSTALL.json`/`MANIFEST.deployed.json`；**不一致就中止**，提示去做 FULL（增量包不带轮子，依赖变了装不上）。
2. 替换源码：`rm -rf app/` → 拷新 `app/` → **重建 `results/`、`model/` 软链**（用户数据原封不动）。
3. 重跑冒烟测试。
4. 更新 `MANIFEST.deployed.json`。

**`.venv/`、`wheels/`、`results/`、`model/` 全部保留**，所以增量更新很快且零数据丢失。

---

## 7. 什么时候用 FULL，什么时候用 INCREMENTAL

| 场景 | 用哪种 |
|---|---|
| 首次部署 | **FULL**（bootstrap.sh） |
| 改了 `requirements-gui.txt`（升/降依赖版本） | **FULL** |
| 只改了 Python 代码（harness/gui/importer/deploy 逻辑），依赖不变 | **INCREMENTAL**（update.sh） |
| update.sh 报 "requirements changed" 中止 | 改回 FULL |

判断依据是 `requirements-gui.txt` 的哈希：黄区打增量包时会拦，红区 update.sh 也会再拦一次。

---

## 8. 故障排查

| 现象 | 处理 |
|---|---|
| 黄区打包 `AUDIT FAIL: 需要 glibc > 2.17` | 按提示在 `requirements-gui.txt` 把对应包**降到仍有 manylinux_2_17 轮子的版本**，重跑 `package.py full`。 |
| 红区 `python3.11 not found` | 装 python3.11，或 `PYTHON=/绝对路径/python3.11 ./bootstrap.sh ...`。 |
| 红区 `INTEGRITY FAIL` | 包在传输中损坏，重新传输（先 `sha256sum -c`）。 |
| `update.sh` 报依赖变更中止 | 依赖确实变了 → 走 FULL 重新部署。 |
| 启动报 Qt `xcb` / `libGL` 缺失 | 红区一般装了 Virtuoso（Qt 应用）即具备；否则设 `QT_QPA_PLATFORM_PLUGIN_PATH` 或补 1~2 个 `.so`。无界面自检永远用 `QT_QPA_PLATFORM=offscreen`。 |
| 想确认装好了 | 跑 §1.3 的 `--selftest --require-qt`，输出 `GUI selftest PASS` 即正常。 |

---

## 9. 命令速查

```bash
# —— 黄区（Windows，有网）——
python deploy/package.py full --out dist/            # 完整包（下载+审计+打包）
python deploy/package.py incremental --out dist/     # 增量包（仅代码）
python deploy/audit_wheels.py dist/_stage_full/wheels # 单独审计轮子
deploy/dryrun_manylinux2014.sh dist/ldo_modeler_full.tar.gz  # Docker 离线演练

# —— 红区（Linux，无网）——
sha256sum -c ldo_modeler_full.tar.gz.sha256          # 落地校验
./bootstrap.sh /opt/ldo_modeler                      # 初次部署（FULL）
./update.sh   /opt/ldo_modeler                       # 增量更新（INCREMENTAL）
/opt/ldo_modeler/.venv/bin/python /opt/ldo_modeler/app/gui/ldo_modeler.py   # 启动 GUI
```
