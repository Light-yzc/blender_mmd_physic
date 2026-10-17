#!/usr/bin/env python3
"""Bake only Ruri/Wiggle bone physics in a RAM-limited Blender process (Linux).

The complete interval is simulated continuously. Outputs are animation actions
saved in a separate .blend copy. Native Cloth and its cache settings are never
configured, baked, deleted or toggled by this tool.
"""
import argparse
import importlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import uuid


def plugin():
    import bpy
    # Normal Blender startup loads the user's enabled physics dependencies.
    if not hasattr(bpy.types.Scene, 'bonechain_props'):
        module = importlib.import_module('自动物理')
        module.register()
    return sys.modules['自动物理']


def reveal_object(context, obj):
    states = [(obj, 'hide_viewport', obj.hide_viewport)]
    was_hidden = obj.hide_get()
    obj.hide_viewport = False
    obj.hide_set(False)
    def visit(layer):
        contained = obj.name in layer.collection.objects
        for child in layer.children:
            if visit(child):
                contained = True
        if contained:
            for owner, attr in ((layer, 'hide_viewport'), (layer.collection, 'hide_viewport')):
                states.append((owner, attr, getattr(owner, attr)))
                setattr(owner, attr, False)
        return contained
    visit(context.view_layer.layer_collection)
    def restore():
        for owner, attr, value in reversed(states):
            setattr(owner, attr, value)
        obj.hide_set(was_hidden)
    return restore


def worker(job_path, stage):
    import bpy
    plugin()
    path = Path(job_path)
    job = json.loads(path.read_text())
    scene = bpy.data.scenes[job['scene']] if job.get('scene') else bpy.context.scene
    if bpy.context.window:
        bpy.context.window.scene = scene
    if scene != bpy.context.scene:
        raise RuntimeError('无法激活计划中的场景')
    obj = bpy.data.objects.get(job.get('armature', ''))
    if not obj or obj.type != 'ARMATURE' or obj.name not in scene.objects:
        raise RuntimeError('找不到指定骨架')
    if stage == 'plan':
        props = scene.bonechain_props
        start = props.native_cloth_start if job['start'] is None else job['start']
        end = props.native_cloth_end if job['end'] is None else job['end']
        if end < start:
            raise RuntimeError('结束帧早于起始帧')
        job.update(scene=scene.name, start=start, end=end, preroll=props.native_cloth_preroll,
                   stages=[{'kind':job['scope'], 'object':obj.name}])
        path.write_text(json.dumps(job, ensure_ascii=False, indent=2))
    else:
        restore_visibility = reveal_object(bpy.context, obj)
        try:
            if bpy.context.object and bpy.context.object.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
            bpy.context.view_layer.objects.active = obj
            with bpy.context.temp_override(object=obj, active_object=obj):
                if job['scope'] == 'RURI':
                    if not bpy.ops.bonechain.ruri_bake.poll():
                        raise RuntimeError('当前骨架没有可用的 Ruri 配置')
                    result = bpy.ops.bonechain.ruri_bake('EXEC_DEFAULT', frame_start=job['start'],
                        frame_end=job['end'], preroll=job['preroll'])
                else:
                    if not bpy.ops.bonechain.wiggle_bake.poll():
                        raise RuntimeError('当前骨架不可执行 Wiggle 烘焙')
                    result = bpy.ops.bonechain.wiggle_bake('EXEC_DEFAULT', frame_start=job['start'],
                        frame_end=job['end'])
            if 'FINISHED' not in result:
                raise RuntimeError('骨架烘焙未完成，原工程保留')
        finally:
            restore_visibility()
    bpy.context.preferences.filepaths.save_version = 0
    bpy.ops.wm.save_as_mainfile(filepath=job['output'], compress=True)
    print('CHECKPOINT', job['output'], flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('project', type=Path)
    parser.add_argument('--blender', required=True)
    parser.add_argument('--start', type=int)
    parser.add_argument('--end', type=int)
    parser.add_argument('--memory-limit', default='8G')
    parser.add_argument('--scope', choices=['RURI', 'WIGGLE'], default='RURI')
    parser.add_argument('--armature', required=True)
    args = parser.parse_args()
    source = args.project.resolve()
    blender = shutil.which(args.blender)
    if not source.is_file() or not blender:
        parser.error('找不到工程或 Blender')
    if sys.platform != 'linux' or not shutil.which('systemd-run'):
        parser.error('需要 Linux 用户 systemd 来限制工作进程内存')
    token = uuid.uuid4().hex[:12]
    stem = source.stem + '-limited-bake-' + token
    output = source.with_name(stem + '.blend')
    job_path = source.with_name(stem + '.json')
    job = {'source':str(source), 'output':str(output), 'start':args.start,
           'end':args.end, 'scope':args.scope, 'armature':args.armature}
    job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2))
    script = str(Path(__file__).resolve())
    match = re.fullmatch(r'(\d+)([KMGT]?)(?:i?B)?', args.memory_limit, re.IGNORECASE)
    if not match or int(match[1]) == 0:
        parser.error('内存上限格式应为 8G、4096M 或字节数')
    requested_bytes = int(match[1]) * (1024 ** (' KMGT'.index(match[2].upper()) if match[2] else 0))
    def run(input_file, stage):
        # Account for the already-open GUI and other applications. The worker
        # cap is never larger than current available RAM minus a 1.5 GiB reserve.
        meminfo = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
        available = int(meminfo['MemAvailable'].split()[0]) * 1024
        limit = min(requested_bytes, available - 1536 * 1024 * 1024)
        if limit < 512 * 1024 * 1024:
            print('可用内存不足，未启动烘焙工作进程。', file=sys.stderr)
            return 1
        print(f'本阶段硬上限 {limit // (1024 * 1024)} MiB', flush=True)
        unit = f'blender-physics-bake-{os.getpid()}-{stage}'
        cmd = ['systemd-run', '--user', '--wait', '--pipe', '--collect', '--quiet',
               '--unit=' + unit, '-p', 'MemoryMax=' + str(limit),
               '-p', 'MemorySwapMax=0', blender, '--background', str(input_file),
               '--python-exit-code', '1', '--python', script, '--', '--worker', str(job_path), str(stage)]
        try:
            return subprocess.run(cmd).returncode
        except KeyboardInterrupt:
            subprocess.run(['systemctl', '--user', 'stop', unit], check=False)
            return 130
    code = run(source, 'plan')
    if code:
        return code
    job = json.loads(job_path.read_text())
    for index, task in enumerate(job['stages']):
        print(f"阶段 {index + 1}/{len(job['stages'])}: {task}; 内存上限 {args.memory_limit}", flush=True)
        code = run(output, index)
        if code:
            print('任务已停止；原工程保留。已完成阶段在 ' + str(output)
                  + '；失败阶段未完成，不自动继续。', file=sys.stderr)
            return code
    print('完成：' + str(output))
    return 0


if __name__ == '__main__':
    if '--' in sys.argv and '--worker' in sys.argv[sys.argv.index('--') + 1:]:
        params = sys.argv[sys.argv.index('--') + 1:]
        worker(params[1], params[2])
    else:
        sys.exit(main())
