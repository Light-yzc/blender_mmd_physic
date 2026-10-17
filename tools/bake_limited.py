#!/usr/bin/env python3
"""Bake native node cloth in sequential, memory-limited Blender processes (Linux).

Use a saved project with disk caches, preferably an externalized copy. Each
object is simulated continuously for the entire requested range, never split
into independent time segments. Original project and existing caches are kept.
"""
import argparse
import hashlib
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
    module = plugin()
    path = Path(job_path)
    job = json.loads(path.read_text())
    scene = bpy.data.scenes[job['scene']] if job.get('scene') else bpy.context.scene
    if bpy.context.window:
        bpy.context.window.scene = scene
    if scene != bpy.context.scene:
        raise RuntimeError('无法激活计划中的场景')
    if stage == 'plan':
        props = scene.bonechain_props
        scope = job.get('scope', 'SCENE')
        if scope == 'SCENE':
            layers, arms = module._native_cloth_plan(bpy.context)
            if not layers:
                raise RuntimeError('没有启用的原生布料')
            targets = [obj for layer in layers for obj in layer]
            if any(mod.type != 'NODES' for obj in targets
                   for mod in module._native_cloth_modifiers(obj)):
                raise RuntimeError('隔离场景烘焙目前仅支持节点布料，场景包含传统 Cloth。')
            jobs = module._native_cloth_bone_jobs(bpy.context, arms, props.native_cloth_bake_bones)
        else:
            arm = bpy.data.objects.get(job.get('armature', ''))
            if not arm or arm.type != 'ARMATURE' or arm.name not in scene.objects:
                raise RuntimeError('找不到指定骨架')
            # Bone-only bake never requires cloth, nor evaluates its caches.
            targets = [obj for obj in scene.objects if module._native_cloth_modifiers(obj)]
            jobs = [('Ruri' if scope == 'RURI' else 'Wiggle', arm)]
        start = job.get('start')
        end = job.get('end')
        start = props.native_cloth_start if start is None else start
        end = props.native_cloth_end if end is None else end
        if end < start:
            raise RuntimeError('结束帧早于起始帧')
        job.update(scene=scene.name,
                   start=max(-1048574, start - props.native_cloth_preroll) if scope == 'SCENE' else start,
                   preroll=props.native_cloth_preroll, end=end,
                   modifiers=[{'object':obj.name, 'modifier':mod.name,
                               'visible':mod.show_viewport}
                              for obj in targets for mod in module._native_cloth_modifiers(obj)],
                   stages=([{'kind':kind, 'object':arm.name} for kind, arm in jobs]
                           + ([{'kind':'cloth', 'object':obj.name} for obj in targets] if scope == 'SCENE' else [])))
        path.write_text(json.dumps(job, ensure_ascii=False, indent=2))
    else:
        task = job['stages'][int(stage)]
        obj = bpy.data.objects[task['object']]
        records = job['modifiers']
        # Completed upstream cloth stays enabled; all future cloth is paused.
        done = {t['object'] for t in job['stages'][:int(stage)] if t['kind'] == 'cloth'}
        for record in records:
            owner = bpy.data.objects[record['object']]
            mod = owner.modifiers[record['modifier']]
            mod.show_viewport = record['visible'] and (owner.name in done or
                                   (task['kind'] == 'cloth' and owner == obj))
        restore_visibility = reveal_object(bpy.context, obj)
        try:
            if bpy.context.object and bpy.context.object.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
            bpy.context.view_layer.objects.active = obj
            if task['kind'] in {'Ruri', 'Wiggle'}:
                if job.get('scope', 'SCENE') != 'SCENE':
                    with bpy.context.temp_override(object=obj, active_object=obj):
                        if task['kind'] == 'Ruri':
                            result = bpy.ops.bonechain.ruri_bake('EXEC_DEFAULT',
                                frame_start=job['start'], frame_end=job['end'], preroll=job['preroll'])
                        else:
                            result = bpy.ops.bonechain.wiggle_bake('EXEC_DEFAULT',
                                frame_start=job['start'], frame_end=job['end'])
                    if 'FINISHED' not in result:
                        raise RuntimeError('骨架烘焙未完成')
                else:
                    generator = (module._ruri_bake_range(bpy.context, obj, job['start'], job['end'], 0)
                                 if task['kind'] == 'Ruri' else
                                 module._wiggle_bake_range(bpy.context, obj, job['start'], job['end']))
                    try:
                        for progress in generator:
                            print('BONE_PROGRESS', round(progress * 100, 1), flush=True)
                    finally:
                        generator.close()
            else:
                obj.use_simulation_cache = True
                for record in records:
                    if record['object'] != obj.name:
                        continue
                    mod = obj.modifiers[record['modifier']]
                    mod.bake_target = 'DISK'
                    token = hashlib.sha256((obj.name + '\0' + mod.name).encode()).hexdigest()[:20]
                    for bake in mod.bakes:
                        # A unique job directory ensures deleting/rebaking this
                        # bake cannot remove the source project's cache files.
                        bake.use_custom_path = True
                        bake.directory = job['cache_root'] + '/' + token + '/' + str(bake.bake_id)
                        bake.bake_target = 'DISK'
                        bake.use_custom_simulation_frame_range = True
                        bake.frame_start, bake.frame_end = job['start'], job['end']
                        with bpy.context.temp_override(object=obj, active_object=obj):
                            result = bpy.ops.object.geometry_node_bake_delete_single(
                                session_uid=obj.session_uid, modifier_name=mod.name, bake_id=bake.bake_id)
                            if 'FINISHED' not in result:
                                raise RuntimeError('清理目标缓存失败：' + obj.name)
                            bpy.context.scene.frame_set(job['start'])
                            result = bpy.ops.object.geometry_node_bake_single(
                                'EXEC_DEFAULT', session_uid=obj.session_uid,
                                modifier_name=mod.name, bake_id=bake.bake_id)
                            if 'FINISHED' not in result:
                                raise RuntimeError('烘焙失败：' + obj.name)
                        meta = Path(bpy.path.abspath(bake.directory)) / 'meta'
                        for frame in range(job['start'], job['end'] + 1):
                            if not (meta / f'{frame:05d}_00000.json').is_file():
                                raise RuntimeError(f'{obj.name} 烘焙未完整结束，缺少帧 {frame}')
        finally:
            restore_visibility()
            for record in records:
                bpy.data.objects[record['object']].modifiers[record['modifier']].show_viewport = record['visible']
    # Each successful stage creates the input for the next process. Source file
    # is never overwritten. Disable backup copies only in this worker process.
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
    parser.add_argument('--scope', choices=['SCENE', 'RURI', 'WIGGLE'], default='SCENE')
    parser.add_argument('--armature', default='')
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
           'end':args.end, 'scope':args.scope, 'armature':args.armature, 'cache_root':'//' + stem + '_physics_cache'}
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
        unit = f'blender-cloth-bake-{os.getpid()}-{stage}'
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
                  + '；失败阶段缓存可能不完整，不自动继续。', file=sys.stderr)
            return code
    print('完成：' + str(output))
    return 0


if __name__ == '__main__':
    raise SystemExit('5.36 已暂时撤下所有插件烘焙功能，此工具当前不执行任务。')
    if '--' in sys.argv and '--worker' in sys.argv[sys.argv.index('--') + 1:]:
        params = sys.argv[sys.argv.index('--') + 1:]
        worker(params[1], params[2])
    else:
        sys.exit(main())
