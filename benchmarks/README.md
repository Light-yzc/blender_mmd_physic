# Bake 性能对比（2026-10-17）

## 结果

同一机器、Blender 5.2.2 LTS、Ruri CUDA（RTX 4090），分别启动后台进程比较 5.38 与 5.39。两组均启用 cProfile、限制 RAM 为 4 GiB、禁用工作进程交换空间。计时包含烘焙准备、连续模拟、写入动作和恢复状态；**不包含文件加载、另存、GUI 重绘或进程退出**。

| 测试 | 5.38 | 5.39 | 结果 |
| --- | ---: | ---: | --- |
| 用户工程 Ruri，178 骨，600–629 帧，预模拟 5 帧 | 67.64 秒 | 1.86 秒 | 约 36.3 倍；耗时减少 97.2% |
| Wiggle 32 骨可动测试场景，同一区间 | 0.237 秒 | 0.235 秒 | 基本持平，差距不足以证明加速 |

用户工程峰值进程 RSS 从 2809.6 MiB 降到 1428.6 MiB。工程中原有的下游网格计算是主要耗时：5.39 在采样期间暂缓 356 个非输入网格的求值，保留骨架、碰撞输入及其声明的依赖。完成、中止和异常时恢复显示及选择状态。不改求解参数、不跳帧、不操作原生 Cloth 的缓存设置。

对比覆盖全部输出曲线，包括区间外关键帧与边界保护帧。Ruri 的 1,057,673 个关键帧记录、Wiggle 的 10,255 个记录逐项完全一致，数值最大误差为 0；Wiggle 测试中 129 个物理通道存在实际变化。这个对比证明测试区间的动作输出一致，不代表已验证所有工程或整个 3240 帧序列。

## 功能检查

- 正常完成后恢复网格显示及选择状态。
- 连续采样中关闭生成器（与 GUI Esc 使用同一清理路径），恢复原动作和网格显示。
- 注入异常后恢复网格显示。
- 声明为碰撞输入的网格始终保留求值。
- 遇到无法静态分析的 Python 驱动器，保留完整场景求值。
- 碰撞输入自身含原生 Cloth 等场景级模拟时，保留完整场景求值。
- 实际执行受限烘焙控制器、计划阶段、工作进程、保存及完成凭据流程：600–629 帧、预模拟 30 帧，依据原场景起点从 -30 连续模拟到 629，成功另存测试副本。原工程未覆盖。
- Python 语法检查通过。

正常／受限两个按钮共用优化后的采样路径。受限模式还会花时间加载和保存工程；GUI 的事件调度也会影响耗时。上表不能直接用于预测整段动画或受限模式的总等待时间。当前已保存工程只启用 Ruri；Wiggle 数据来自可复现测试场景。

## 复现

先将历史版本与当前版本各准备为独立 `.py` 源文件，确保当前插件和前置插件已安装。测试脚本只修改后台进程内加载的数据，不保存输入工程。为避免挤占当前工作内存，建议通过 Linux `systemd-run --user -p MemoryMax=4G -p MemorySwapMax=0` 运行。

```bash
blender -b project.blend --python-exit-code 1 \
  --python benchmarks/bake_performance.py -- \
  --source baseline.py --output before.json --start 600 --frames 30 --preroll 5

blender -b project.blend --python-exit-code 1 \
  --python benchmarks/bake_performance.py -- \
  --source 自动物理.py --output after.json --start 600 --frames 30 --preroll 5

python3 benchmarks/compare_bakes.py before.json after.json
```

Wiggle 使用 `--fixture wiggle`；中止清理测试使用 `--cancel-after 3`。脚本同时输出 cProfile 文件和耗时排名。比较器检查通道、帧、插值、值及边界帧，发现任何差异返回非零退出码。

机器可读摘要见 [results-2026-10-17.json](results-2026-10-17.json)。原始工程及大体积采样结果不上传仓库。
