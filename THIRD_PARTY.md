# 第三方依赖与补丁

## 主插件

`自动物理.py` 保留原始插件元数据中的作者署名 **WANG**。本次整理没有为主插件额外指定许可协议。

## RuriClothPhysics

- 项目：<https://github.com/ShiyumeMeguri/RuriClothPhysics>
- 本地安装元数据：8.0.0，作者 ShiyumeMeguri。
- 本地安装包的许可文件：GNU AGPL v3，原文保存在 `LICENSES/RuriClothPhysics-AGPL-3.0.txt`。
- 补丁：`patches/ruri-8-write-order.patch`，调整帧处理与姿态写回顺序。

## Wiggle 2: RTX Edition

- 项目：<https://github.com/mayalhc/blender-wiggle-2>
- 原始项目：<https://github.com/shteeve3d/blender-wiggle-2>
- 本地扩展清单：2.4.0，作者 shteeve3d、Chamiseul。
- 本地扩展声明：GPL-3.0-or-later；安装包许可原文保存在 `LICENSES/Wiggle2-GPL-3.0.txt`。
- 补丁：`patches/wiggle-rtx-pose-write.patch`，调整姿态写回、碰撞约束后的更新与批量重置。

两份补丁只针对本地适配时的对应文件，不代表适用于第三方项目的任意版本。仓库不包含这两个插件的完整源代码或安装包。
