# RTX 5080 XBAR / SYSCLK / VIDEO Debug 总结

## 1. 目标

目标是查明 Druta 1.1.0 在 RTX 5080 上无法正常调整 XBAR、SYSCLK、VIDEO
频率的原因，并用真实硬件测试确认 Blackwell 的字段布局、控制索引和请求方向。

本总结中的 JSON 都是硬件实验记录，不是程序指令。

## 2. 测试环境

- GPU：NVIDIA GeForce RTX 5080
- VBIOS：`98.03.3b.c0.ca`
- 操作系统/接口：Windows x64、管理员权限、私有 NvAPI
- 实际测试驱动：
  - `595.79`：初始测试，写入能被接受，但没有得到稳定的物理频率响应
  - `610.88`：最终验证驱动，得到可重复的正负方向响应
- 测试状态：GPU 保持 P0、高负载，使用 `Ctrl+H` 锁住 V/F curve，避免 P-state
  变化和空闲时钟造成假阳性
- Druta 源码依赖：`CPython 3.14.4`、`dearpygui==2.3.1`；打包时使用
  `pyinstaller==6.22.0`

公开的 Windows RTX 50 项目 README 列出的已验证驱动为：
`572.16、576.02、580.88、581.42、591.86、596.49、610.62、610.88`。
其他驱动应该先通过 probe/crack，再考虑加入验证列表：
https://github.com/SHANAjam/rtx5090-xbar-control/blob/main/README.md

## 3. 从头到尾的 Debug 过程

### 阶段 0：确认问题和资料入口

起点是 Druta 1.1.0 在 RTX 5080 上界面可以接受调节，但 XBAR、SYSCLK、VIDEO
没有可靠的实际频率变化。根据 README 和 technical documentation，重点追踪了
私有 NvAPI 的：

- `0xF58938F5`：`ClkDomCtlGet`
- `0xD14B69CF`：`ClkDomCtlSet`
- 物理频率只用 read-only 的 `ClkMeasureFreq` 观察

同时确认不能把 Turing 的 private getter domain 编号直接当成 Blackwell 的
control index。

### 阶段 1：增加 Blackwell 专用布局和安全门

提交：`78d3b90 Add guarded Blackwell clock-domain probe`

修改/增加：`nvbackend.py`、`druta.py`、`test_clkdom.py`、README 和技术文档。

主要内容：

- 按 RTX 50-series 型号单独选择 Blackwell 路径；
- 增加 Blackwell clock-domain control block 布局；
- 加入版本回显、one-hot domain mask 和 buffer 完整性检查；
- 增加只读物理时钟测量；
- UI 在 Blackwell 上显示“requested offset”，不再盲目套用 Turing private
  getter 的域名；
- 所有写入使用“读取原始 buffer → 只改一个 dword → 重新读取比对 → SET”的
  防护方式；
- 增加架构和字段偏移的单元测试。

### 阶段 2：增加控制索引扫描

提交：`dcd7d26 Add Blackwell clock control-index probe`

修改：`nvbackend.py`、README、技术文档。

增加命令：

```powershell
python nvbackend.py --clkdom-control-probe --delta 25 --confirm
```

默认扫描控制 `1、3、4、5、6、7、8、9`，排除可能是核心和显存的 `0、2`。
只有明确接受风险时才使用 `--include-core-memory`。每个控制逐个写入，并在
`finally` 中恢复完整原始 buffer。

### 阶段 3：增加 SET 后 GET 读回

提交：`12b1e22 Record Blackwell clock request readback`

增加 JSON 字段：

- `requested_freq_khz`
- `readback_freq_khz`
- `readback_mode`
- `readback_matches_request`
- `readback_error`

这一步区分了“驱动真的保存了请求”和“SET 返回成功但请求被丢弃”。

### 阶段 4：记录工作点，排除 P-state 假象

提交：`bda766e Record clock probe operating point`

在 before/after 窗口中增加：

- P-state
- GPU、FB、Video、Bus utilization
- public core/memory clock
- private getter 的测量行

这样可以确认测试是否真的处于同一个高负载工作点。

### 阶段 5：只接受与请求同方向的物理变化

提交：`5f48b4a Require directional Blackwell clock evidence`

增加 `directional_observations` 和 `physical_effect` 判定。即使某个频率发生了
变化，如果变化方向与请求相反，或者只是单侧瞬态，也不能直接当作正确映射。

### 阶段 6：扩大临时诊断步长

提交：`2784d63 Allow larger temporary clock probe deltas`

发现 ±25 MHz 在测量噪声和时钟 bin 量化下不容易分辨，因此把管理员专用的临时
诊断上限扩大到 ±200 MHz。这个上限只用于 probe，不能理解成普通 UI 滑块范围。

### 阶段 7：使用直接物理时钟作为证据

提交：`b001eb2 Use direct clocks for large probe effects`

增加 `direct_changed_observations`，避免只依赖 private getter 的目标值。大步长
测试还放宽了物理计数器的可接受窗口，但仍限制最大范围，防止 P-state 跳变被
误判成映射成功。

### 阶段 8：修复 GPC 抖动和大步长误判

提交：`0d4577b Make Blackwell clock probe verdicts noise-resistant`

修改：`nvbackend.py`、README、技术文档。

具体增加：

- GPC 只作为工作点诊断，不再作为 XBAR/SYSCLK/VIDEO 的直接映射证据；
- ±200 MHz 测试的有效变化阈值按请求步长增加，最高约 25 MHz；
- `median_shift_candidates`：记录中位数变化但窗口不够稳定的候选结果；
- `reverse_directional_observations`：记录反向变化，便于发现极性反转；
- 保留完整 before/after、范围、读回和恢复信息。

### 阶段 9：根据第二轮 610.88 结果修正正式 UI

提交：`cf80032 Apply validated RTX 5080 clock control mapping`

修改：`nvbackend.py`、`druta.py`、`test_clkdom.py`、README、技术文档。

正式 UI 映射改为：

| 逻辑功能 | control index | UI 请求极性 |
|---|---:|---:|
| XBAR | 1 | 正向直写 |
| SYSCLK | 3 | 正常 |
| VIDEO | 4 | 正常 |

控制 5 不再冒充 VIDEO；未知控制在 Blackwell 上只显示为 `control N`，不会
借用 Turing 的旧名称。

## 4. 关键实验结果

### 4.1 595.79：原始问题被复现

RTX 5080 / VBIOS `98.03.3b.c0.ca` / driver `595.79`：

- `0xF58938F5` 的 GET/SET 调用可以成功；
- `+0x10C` 和 `+0x114` 都曾被尝试；
- ±25 MHz 结果不能得到稳定物理响应；
- 初期 probe 曾出现假阳性，后来通过增加 P-state、utilization、before/after
  和方向判定被排除；
- `595.79` 不在公开参考项目的已验证驱动列表中，因此不作为最终推荐环境。

### 4.2 Blackwell 正确字段

返回的 control block 几何结构为：

```text
version = 0x000261A4
size    = 0x61A4
header  = 0x124
stride  = 0x304
freq    = entry + 0x114
NVVDD   = entry + 0x110
MSVDD   = entry + 0x11C
```

在 610.88 的字段测试中：

- 写 `+0x10C` 后读回为 `0`，说明这不是 Blackwell 的频率请求字段；
- 写 `+0x114` 后读回精确等于请求值，`mode=15`；
- ±25 MHz 和 ±200 MHz 都能验证 SET 后 GET 保留了请求。

因此，1.1.0 的核心问题之一是把 Turing 的频率字段假设套到了 Blackwell。

### 4.3 610.88、±200 MHz、第二轮复测

测试保持 P0、高负载、Ctrl+H 锁曲线。

- control 1：
  - raw `+200`：XBAR `1948.2 → 1757.5 MHz`，约 `-190.7 MHz`；
  - raw `-200`：XBAR `1950.1 → 2136.6 MHz`，约 `+186.5 MHz`；
  - 结论：这组原始 probe 结果曾被解释为需要 `-1` 转换；但后续对已打包
    UI 的端到端实测发现该补偿会把滑块方向再次弄反，因此 UI 层最终采用
    与用户相同符号的直写。

- control 3：
  - raw `+200`：XBAR `1943.1 → 2144.9 MHz`，约 `+201.8 MHz`；
  - raw `-200`：XBAR `1951.7 → 1752.2 MHz`，约 `-199.6 MHz`；
  - 结论：control 3 有非常强的、可重复的正负方向响应，保留为 SYSCLK
    控制。它同时会影响 XBAR，说明 Blackwell 的域之间存在耦合。

- control 4：
  - raw `+200`：VIDEO `1830 → 1950 MHz`，约 `+120 MHz`；
  - raw `-200`：VIDEO `1830 → 1717 MHz`，约 `-113 MHz`；
  - XBAR 基本不动；
  - 结论：control 4 是最可靠的 VIDEO 候选，且正负方向重复出现，因此 UI
    VIDEO 从 control 5 改到 control 4。由于 VIDEO after-window 仍有约 195 MHz
    的范围，probe 仍把它作为候选记录而不是完全稳定的自动 verdict。

- control 5：VIDEO 始终约 `1830 MHz`，没有直接 VIDEO 响应，不能继续作为 VIDEO。
- control 6、7、8：没有超过大步长有效阈值的直接响应。
- control 9：驱动拒绝。
- `sys`、`memory` 的 `ClkMeasureFreq` 计数在当前路径没有返回可用值，因此
  SYSCLK 暂时没有独立物理计数器确认；control 3 的名称保留为高可信结论，不能
  说成已经完成独立 SYS 计数器证明。

## 5. 问题根因

问题不是 RTX 5080 完全不支持这条路径，而是原实现同时存在三类问题：

1. **架构布局错误**：Blackwell 的频率字段是 `+0x114`，不是 Turing 使用的
   `+0x10C`。
2. **控制索引和 private getter 域编号混用风险**：control index 不能直接
   套用 Turing/旧 private getter 的域名表。
3. **验证方法不够严格**：只看 SET 成功、private target 或单个瞬时读数会产生
   假阳性；还必须固定 P0/负载、比较 before/after、确认读回、检查方向，并把
   GPC 抖动排除出直接域证据。

## 6. 成果文件

### 已修改的代码和文档

- `nvbackend.py`：Blackwell 布局、0xF58938F5 读写保护、物理 probe、读回、
  稳定性/方向判定、最终 control 映射和 XBAR UI 符号修正。
- `druta.py`：RTX 50 UI 的 VIDEO control 从 5 改为 4。
- `test_clkdom.py`：Blackwell 布局、control 名称、control 极性和写入 dword
  的单元测试。
- `README.md`：Blackwell 调试命令、±200 probe、环境和当前映射说明。
- `TECHNICALDOCUMENTATION.md`：技术原理、验证规则和实验解释。
- `DEBUG-SUMMARY-RTX5080.md`：本总结。

### 硬件实验 JSON

- 初始 595.79：`clkdom-map-5080.json`、`clkdom-map-5080-v2.json`、
  `clkdom-map-5080-v3.json`
- 595.79 字段/负载/控制测试：`clkdom-fields-5080*.json`、
  `clkdom-fields-loaded-*.json`、`clkdom-controls-5080*.json`、
  `clkdom-controls-all-plus25*.json`
- 610.88 字段测试：`clkdom-fields-61088-plus25.json`、
  `clkdom-fields-61088-minus25.json`、`clkdom-fields-61088-plus200.json`、
  `clkdom-fields-61088-minus200.json`
- 610.88 控制扫描：`clkdom-controls-61088-plus200.json`、
  `clkdom-controls-61088-minus200.json`、
  `clkdom-controls-61088-plus200-v2.json`、
  `clkdom-controls-61088-minus200-v2.json`

这些 JSON 是实验记录，未加入代码提交。

### Git 提交

```text
78d3b90  Add guarded Blackwell clock-domain probe
dcd7d26  Add Blackwell clock control-index probe
12b1e22  Record Blackwell clock request readback
bda766e  Record clock probe operating point
5f48b4a  Require directional Blackwell clock evidence
2784d63  Allow larger temporary clock probe deltas
b001eb2  Use direct clocks for large probe effects
0d4577b  Make Blackwell clock probe verdicts noise-resistant
cf80032  Apply validated RTX 5080 clock control mapping
```

当前成果在本地分支 `feat/blackwell-clock-domain-debug`，尚未推送或创建远程
GitHub PR。

## 7. 最终简短结论

RTX 5080 在 Windows driver `610.88` 下可以使用这条私有 clock-domain 路径。
根本问题是 Blackwell 字段偏移与 Turing 不同、control index 不能直接沿用旧
域名、以及原始验证会被 P-state/GPC 抖动误导。最终已确认频率字段为 `+0x114`，
并将 UI 映射修正为 control `1/3/4 = XBAR/SYSCLK/VIDEO`，其中 XBAR control 1
采用与用户相同符号的直写。后续端到端验证确认此前的 `-1` 补偿会反转 XBAR
滑块，因此已移除该补偿；SYSCLK 的独立物理计数器仍是唯一尚未完全确认的部分。
