"""Run in Blender background on a disposable loaded project, never saves the .blend.

blender -b project.blend --python-exit-code 1 --python bake_performance.py --
  --source addon.py --output result.json --start 600 --frames 20 --preroll 5
"""
import argparse
import cProfile
import importlib
import importlib.util
import json
from pathlib import Path
import pstats
import resource
import sys
import time
import bpy

parser = argparse.ArgumentParser()
parser.add_argument('--source', required=True)
parser.add_argument('--output', required=True)
parser.add_argument('--start', type=int, default=600)
parser.add_argument('--frames', type=int, default=20)
parser.add_argument('--preroll', type=int, default=5)
parser.add_argument('--fixture', choices=['wiggle'])
parser.add_argument('--cancel-after', type=int, default=0)
parser.add_argument('--engine', choices=['all', 'Ruri', 'Wiggle'], default='all')
args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
installed = importlib.import_module('自动物理')
if not hasattr(bpy.types.Scene, 'bonechain_props'):
    installed.register()
spec = importlib.util.spec_from_file_location('bonechain_benchmark_source', args.source)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
if args.fixture:
    sys.path.insert(0, str(Path(__file__).parent))
    import wiggle_fixture
    fixture_arm, fixture_collider = wiggle_fixture.create()
scene = bpy.context.scene
scene.frame_start = args.start
scene.frame_end = args.start + args.frames - 1
# The only scene edits are to this background process, never saved to the input.
jobs = module._physics_bake_jobs(bpy.context)
print('BENCH_JOBS', [(kind, arm.name) for kind, arm in jobs], flush=True)
if not jobs:
    raise RuntimeError('No enabled plugin physics found in saved project')
settings = scene.bonechain_props
settings.native_cloth_start = scene.frame_start
settings.native_cloth_end = scene.frame_end
settings.native_cloth_preroll = args.preroll
if hasattr(scene, 'wiggle'):
    scene.wiggle.preroll = args.preroll
stats = {'source': args.source, 'file': bpy.data.filepath, 'range': [scene.frame_start, scene.frame_end],
         'preroll': args.preroll, 'engine': args.engine,
         'compile_target': getattr(getattr(scene, 'ruri_cloth_physics', None), 'compile_target', None),
         'jobs': [(kind, arm.name) for kind, arm in jobs], 'steps': []}
original_visibility = {obj.name: (obj.hide_viewport, obj.select_get()) for obj in scene.objects}
original_actions = {arm.name: arm.animation_data.action if arm.animation_data else None for _, arm in jobs}
profile = cProfile.Profile()
start = time.perf_counter()
if args.engine == 'all':
    gen = module._all_physics_bake_range(bpy.context, scene.frame_start, scene.frame_end, args.preroll)
else:
    arms = [arm for kind, arm in jobs if kind == args.engine]
    if not arms:
        raise RuntimeError('Requested engine has no enabled configuration')
    arm = arms[0]
    bpy.context.view_layer.objects.active = arm
    gen = (module._ruri_bake_range(bpy.context, arm, scene.frame_start, scene.frame_end, args.preroll)
           if args.engine == 'Ruri' else module._wiggle_bake_range(bpy.context, arm, scene.frame_start, scene.frame_end))
profile.enable()
while True:
    before = time.perf_counter()
    try:
        progress = next(gen)
    except StopIteration as done:
        stats['result'] = done.value
        stats['finish_seconds'] = time.perf_counter() - before
        break
    stats['steps'].append({'seconds': time.perf_counter() - before, 'progress': progress,
                           'frame': scene.frame_current})
    if args.fixture:
        assert not fixture_collider.hide_viewport, 'Collision input was suspended'
    if args.cancel_after and len(stats['steps']) == args.cancel_after:
        gen.close()
        stats['cancelled'] = True
        stats['finish_seconds'] = 0
        for _, arm in jobs:
            assert arm.animation_data.action == original_actions[arm.name], 'Cancel lost original action'
        break
    print('BENCH_FRAME', scene.frame_current, round(stats['steps'][-1]['seconds'], 4), flush=True)
profile.disable()
assert original_visibility == {obj.name: (obj.hide_viewport, obj.select_get()) for obj in scene.objects}, 'Visibility/selection was not restored'
stats['visibility_restored'] = True
stats['seconds'] = time.perf_counter() - start
stats['peak_rss_mib'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
out = Path(args.output)
profile.dump_stats(str(out.with_suffix('.prof')))
with out.with_suffix('.profile.txt').open('w') as f:
    pstats.Stats(profile, stream=f).strip_dirs().sort_stats('cumulative').print_stats(65)
# Export every output curve including guards and channels outside the sample range.
curves = {}
for arm in {arm.name: arm for _, arm in jobs}.values():
    ad = arm.animation_data
    action = ad.action if ad else None
    if not action:
        continue
    slot = ad.action_slot
    containers = [strip.channelbag(slot) for layer in action.layers for strip in layer.strips
                  if strip.type == 'KEYFRAME' and slot]
    curves[arm.name] = {fc.data_path + ':' + str(fc.array_index):
                         [[float(k.co.x), float(k.co.y), k.interpolation] for k in fc.keyframe_points]
                         for bag in containers if bag for fc in bag.fcurves}
stats['curves'] = curves
if args.fixture and hasattr(module, '_bake_input_visibility'):
    # Unknown Python driver references must retain the complete scene.
    driver = fixture_arm.driver_add('location', 0)
    driver.driver.expression = 'sum((frame, 0))'
    assert not driver.driver.is_simple_expression
    restore = module._bake_input_visibility(bpy.context, jobs)
    assert original_visibility == {obj.name: (obj.hide_viewport, obj.select_get()) for obj in scene.objects}
    restore()
    fixture_arm.driver_remove('location', 0)
    stats['scripted_driver_fallback'] = True
    # A failing solve must unwind all temporary visibility edits as well.
    original_impl = module._all_physics_bake_range_impl
    def failing_impl(*_args):
        yield 0.0
        raise RuntimeError('injected benchmark failure')
    module._all_physics_bake_range_impl = failing_impl
    failing = module._all_physics_bake_range(bpy.context, scene.frame_start, scene.frame_end, args.preroll)
    try:
        next(failing)
        assert not fixture_collider.hide_viewport
        next(failing)
        raise AssertionError('Injected error did not propagate')
    except RuntimeError as error:
        assert str(error) == 'injected benchmark failure'
    finally:
        module._all_physics_bake_range_impl = original_impl
        failing.close()
    assert original_visibility == {obj.name: (obj.hide_viewport, obj.select_get()) for obj in scene.objects}
    stats['error_cleanup'] = True
    # A native simulator on an actual collision input may use implicit scene
    # dependencies. The optimized path must leave the entire scene enabled.
    native = fixture_collider.modifiers.new('Benchmark native input', 'CLOTH')
    try:
        restore = module._bake_input_visibility(bpy.context, jobs)
        assert original_visibility == {obj.name: (obj.hide_viewport, obj.select_get()) for obj in scene.objects}
        restore()
        stats['native_input_fallback'] = True
    finally:
        fixture_collider.modifiers.remove(native)
out.write_text(json.dumps(stats, ensure_ascii=False))
print('BENCH_DONE', json.dumps({k:v for k,v in stats.items() if k not in ('curves','steps')}, ensure_ascii=False), flush=True)
