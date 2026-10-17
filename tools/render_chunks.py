#!/usr/bin/env python3
"""Render an already baked project as image frames in separate Blender processes.

Controller: python3 render_chunks.py PROJECT --blender /path/to/blender \
    --start 600 --end 3240 --chunk-size 32 --memory-limit 8G

Linux workers run in a user systemd cgroup, with swap disabled and a hard RAM cap.
No render is started merely by installing the add-on. Movie output is unsupported.
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys


def worker():
    import bpy
    args = sys.argv[sys.argv.index('--') + 1:]
    assert args[0] == '--worker'
    start, end = map(int, args[1:3])
    scene = bpy.context.scene
    if scene.render.image_settings.file_format in {'FFMPEG', 'AVI_RAW', 'AVI_JPEG'}:
        raise RuntimeError('分段渲染需要 PNG / EXR / JPEG 等图像序列输出，请先保存输出设置。')
    scene.frame_start, scene.frame_end = start, end
    scene.render.use_persistent_data = False
    bpy.ops.render.render(animation=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('project', type=Path)
    parser.add_argument('--blender', required=True, help='Blender executable')
    parser.add_argument('--start', type=int, required=True)
    parser.add_argument('--end', type=int, required=True)
    parser.add_argument('--chunk-size', type=int, default=32)
    parser.add_argument('--memory-limit', default='8G', help='Linux systemd MemoryMax, default 8G')
    args = parser.parse_args()
    if args.end < args.start or args.chunk_size < 1:
        parser.error('起止帧或分段长度无效')
    project = args.project.resolve()
    if not project.is_file():
        parser.error('找不到工程文件')
    blender = shutil.which(args.blender)
    if not blender:
        parser.error('找不到 Blender 可执行文件')
    if sys.platform != 'linux' or not shutil.which('systemd-run'):
        parser.error('此工具需要 Linux 用户 systemd，以限制内存并保护整机。')
    script = str(Path(__file__).resolve())
    for start in range(args.start, args.end + 1, args.chunk_size):
        end = min(start + args.chunk_size - 1, args.end)
        print(f'渲染 {start}–{end}；内存上限 {args.memory_limit}', flush=True)
        unit = f'blender-physics-render-{os.getpid()}-{start}'
        command = ['systemd-run', '--unit=' + unit, '--user', '--wait', '--pipe', '--collect', '--quiet',
                   '-p', f'MemoryMax={args.memory_limit}', '-p', 'MemorySwapMax=0',
                   blender, '--background', str(project), '--python-exit-code', '1',
                   '--python', script, '--', '--worker', str(start), str(end)]
        # Run sequentially. A failed/OOM worker stops the queue; no automatic retries.
        try:
            result = subprocess.run(command)
        except KeyboardInterrupt:
            subprocess.run(['systemctl', '--user', 'stop', unit], check=False)
            print('已中止当前分段。已输出的图像保留。', file=sys.stderr)
            return 130
        if result.returncode:
            print(f'{start}–{end} 失败，已停止。已输出的图像保留；检查日志后从失败段重启。', file=sys.stderr)
            return result.returncode
    print('全部分段完成。工程文件未改写。')
    return 0


if __name__ == '__main__':
    if '--' in sys.argv and '--worker' in sys.argv[sys.argv.index('--') + 1:]:
        worker()
    else:
        sys.exit(main())
