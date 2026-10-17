"""Deterministic animated Wiggle rig with a downstream skinned mesh."""
import bpy


def create():
    scene = bpy.data.scenes.new('BoneChain benchmark')
    bpy.context.window.scene = scene
    data = bpy.data.armatures.new('BenchmarkBones')
    arm = bpy.data.objects.new('BenchmarkRig', data)
    scene.collection.objects.link(arm)
    arm.select_set(True)
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode='EDIT')
    root = data.edit_bones.new('Anchor')
    root.head, root.tail = (0, 0, 0), (0, 0, 1)
    for chain in range(8):
        parent = root
        for row in range(4):
            bone = data.edit_bones.new(f'Chain{chain}_{row}')
            bone.head = (chain * 0.2, 0, 1 + row * 0.25)
            bone.tail = (chain * 0.2, 0, 1.25 + row * 0.25)
            bone.parent = parent
            bone.use_connect = row > 0
            parent = bone
    bpy.ops.object.mode_set(mode='OBJECT')
    for frame, angle in ((595, 0), (600, 0), (615, 0.6), (630, -0.2), (645, 0.3)):
        root = arm.pose.bones['Anchor']
        root.rotation_mode = 'XYZ'
        root.rotation_euler.y = angle
        root.keyframe_insert('rotation_euler', frame=frame)
    scene['wiggle_updating'] = True
    scene.wiggle_enable = True
    arm.wiggle_enable = True
    arm.wiggle_mute = False
    for bone in arm.pose.bones:
        if bone.name != 'Anchor':
            bone.wiggle_enable = True
            bone.wiggle_tail = True
            bone.wiggle_stiff = 60
            bone.wiggle_damp = 3
    scene['wiggle_updating'] = False
    bpy.ops.mesh.primitive_grid_add(x_subdivisions=120, y_subdivisions=120, size=2)
    mesh = bpy.context.object
    mesh.name = 'Benchmark downstream mesh'
    group = mesh.vertex_groups.new(name='Chain0_0')
    group.add(list(range(len(mesh.data.vertices))), 1.0, 'REPLACE')
    modifier = mesh.modifiers.new('Skin', 'ARMATURE')
    modifier.object = arm
    modifier = mesh.modifiers.new('Subdivision', 'SUBSURF')
    modifier.levels = 2
    bpy.context.view_layer.objects.active = arm
    arm.select_set(True)
    # Include a collision input so the dependency-preservation assertion is exercised.
    bpy.ops.mesh.primitive_uv_sphere_add(segments=12, ring_count=8, radius=0.1, location=(1, 0, 1))
    collider = bpy.context.object
    collider.name = 'Benchmark collider'
    bone = arm.pose.bones['Chain0_0']
    bone.wiggle_collider = collider
    bpy.context.view_layer.objects.active = arm
    return arm, collider
