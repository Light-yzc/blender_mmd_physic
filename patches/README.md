# 可选依赖兼容补丁

本目录保存当前开发环境使用的两份依赖补丁。先安装依赖并备份它的文件，再判断是否需要应用；新版依赖可能已经包含等效修复。

| 补丁 | 应用目录 | 修改文件 |
| --- | --- | --- |
| `ruri-8-write-order.patch` | `RuriClothPhysics` 插件根目录 | `blender_host/runtime.py` |
| `wiggle-rtx-pose-write.patch` | `wiggle_2` 扩展根目录 | `wiggle_2.py` |

## 应用方法

关闭 Blender，在对应依赖的目录中执行。将示例中的补丁路径换成这个仓库的实际位置。

```bash
git apply --check /path/to/blender_mmd_physic/patches/ruri-8-write-order.patch
git apply /path/to/blender_mmd_physic/patches/ruri-8-write-order.patch
```

Wiggle 同理：

```bash
git apply --check /path/to/blender_mmd_physic/patches/wiggle-rtx-pose-write.patch
git apply /path/to/blender_mmd_physic/patches/wiggle-rtx-pose-write.patch
```

如果 `--check` 失败，先核对依赖版本与文件。用 `git apply --reverse --check` 可以辅助判断补丁是否已经应用；不要强行覆盖不匹配的文件。

应用后重启 Blender。主插件仍然单独安装仓库根目录的 `自动物理.py`。

## 补丁作用

- **Ruri**：在动画评估前释放上一帧模拟输出，在当前帧评估后写回新姿态，避免动画覆盖物理输出或视口显示滞后一帧。
- **Wiggle RTX**：延后姿态写入到约束求解完成后，减少输入/输出姿态混用，修正批量重置中的写回时机。

出处与许可文本见 [THIRD_PARTY.md](../THIRD_PARTY.md)。
