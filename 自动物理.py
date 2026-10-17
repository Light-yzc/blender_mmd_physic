bl_info = {
    "name": "骨骼链阻尼追踪",
    "author": "WANG",
    "version": (5, 39),
    "blender": (3, 0, 0),
    "location": "View3D > 侧边栏(N) > 骨骼链",
    "description": "Wiggle/Ruri 实时物理、碰撞、骨骼区间烘焙与原动作恢复",
    "category": "Rigging",
}

import bpy
import hashlib
from math import pi, sqrt
from bpy.props import (FloatProperty, FloatVectorProperty, StringProperty, CollectionProperty,
                       IntProperty, PointerProperty, BoolProperty, EnumProperty)
from bpy.types import (Operator, Panel, PropertyGroup, UIList)
from mathutils import Euler, Matrix, Vector


# ===============================================================
# 数据结构
# ===============================================================
class BoneItem(PropertyGroup):
    name: StringProperty(name="骨骼名")


def _get_active_group(context):
    props = context.scene.bonechain_props
    if 0 <= props.group_index < len(props.groups):
        return props.groups[props.group_index]
    return None


def _ruri_armature_for_group(context, grp):
    names = {item.name for item in grp.flex_bones if item.name}

    def matches(obj):
        return (obj is not None and obj.type == 'ARMATURE'
                and obj.name in context.scene.objects
                and all(name in obj.pose.bones for name in names))

    if grp.ruri_armature is not None:
        return grp.ruri_armature if matches(grp.ruri_armature) else None
    if matches(context.active_object):
        return context.active_object
    selected = [obj for obj in context.selected_objects if matches(obj)]
    if len(selected) == 1:
        return selected[0]
    candidates = [obj for obj in context.scene.objects if matches(obj)]
    return candidates[0] if len(candidates) == 1 else None


def _update_collision(self, context):
    """尺寸、强度原地更新，避免拖动时反复重建依赖图。"""
    grp = _find_group_of_collitem(context, self)
    if grp and grp.collision_mode == 'OLD':
        _rebuild_collision_item(context, self)
        _sync_group_collision(context, grp, changed_item=self)
    elif grp and grp.collision_mode == 'RURI':
        _update_ruri_collider_radius(self)


def _ruri_item_object(item, name_attribute, uid_attribute):
    pointer = 'ruri_collider' if name_attribute == 'ruri_collider_name' else 'ruri_end'
    target = getattr(item, pointer, None)
    if target is not None:
        return target
    return bpy.data.objects.get(getattr(item, name_attribute, ''))


def _replace_ruri_object(old, new):
    for obj in bpy.data.objects:
        settings = getattr(obj, 'ruri_cloth_physics', None)
        if obj.type == 'ARMATURE' and settings:
            for config in settings.configs:
                for reference in config.collider_collision.collider_references:
                    if reference.collider == old:
                        reference.collider = new
    for scene in bpy.data.scenes:
        settings = getattr(scene, 'ruri_cloth_physics', None)
        if settings:
            for entry in settings.colliders:
                if entry.object == old:
                    entry.object = new
        props = getattr(scene, 'bonechain_props', None)
        if props:
            for group in props.groups:
                for item in group.coll_bones:
                    if item.ruri_collider == old:
                        item.ruri_collider = new


def _update_ruri_collider_radius(item):
    start = _ruri_item_object(item, 'ruri_collider_name', 'ruri_collider_uid')
    if start is None or not hasattr(start, 'ruri_cloth_physics_collider'):
        return False
    import importlib
    geom = importlib.import_module('RuriClothPhysics.blender_host.collider_geom')
    constraint = geom.bone_constraint(start)
    arm = constraint.target if constraint else None
    settings = start.ruri_cloth_physics_collider
    if arm is None or arm.type != 'ARMATURE' or settings.shape == 'MESH':
        return False
    end = geom.end_object(settings)
    arm_scale = max(abs(v) for v in arm.matrix_world.to_scale())
    endpoints = [(start, item.radius)]
    if end is not None:
        endpoints.append((end, item.ruri_end_radius if item.ruri_split_radius else item.radius))
    for obj, radius in endpoints:
        # Ruri 自己还会乘物体世界缩放，因此这里先消除该乘数，避免重复缩放。
        object_scale = max(sqrt(sum(obj.matrix_world[row][axis] ** 2 for row in range(3)))
                           for axis in range(3))
        if object_scale > 1e-8:
            obj.empty_display_size = radius * arm_scale / object_scale
    return True


def _ruri_shape_mesh(shape):
    """Closed, outward wound unit shapes; XYZ sizes are real collision geometry."""
    import bmesh
    mesh = bpy.data.meshes.new('BC_' + shape)
    bm = bmesh.new()
    if shape == 'BOX':
        bmesh.ops.create_cube(bm, size=1.0)
    else:
        bmesh.ops.create_uvsphere(bm, u_segments=16, v_segments=8, radius=0.5)
    bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
    bm.to_mesh(mesh)
    bm.free()
    mesh['bonechain_ruri_shape'] = shape
    # Keep the proxy in the render dependency graph so collision also works
    # during rendering. A transparent surface prevents it appearing in output.
    material = bpy.data.materials.get('BC_CollisionInvisible')
    if material is None:
        material = bpy.data.materials.new('BC_CollisionInvisible')
        material.use_nodes = True
        material.node_tree.nodes.clear()
        output = material.node_tree.nodes.new('ShaderNodeOutputMaterial')
        transparent = material.node_tree.nodes.new('ShaderNodeBsdfTransparent')
        material.node_tree.links.new(transparent.outputs[0], output.inputs['Surface'])
    mesh.materials.append(material)
    return mesh


def _ensure_ruri_item_geometry(context, grp, item, arm, collection=None):
    import importlib
    geom = importlib.import_module('RuriClothPhysics.blender_host.collider_geom')
    operators = importlib.import_module('RuriClothPhysics.blender_host.operators')
    bone = arm.pose.bones.get(item.name)
    if bone is None:
        return None
    start = _ruri_item_object(item, 'ruri_collider_name', 'ruri_collider_uid')
    if start is None and collection is None:
        return None  # Store edits before configuration without creating stray objects.
    collection = collection or operators._collider_collection(context, arm)
    end = item.ruri_end
    if end is None and start is not None:
        end = start.ruri_cloth_physics_collider.end_object
    mesh_shape = item.ruri_shape in {'BOX', 'ELLIPSOID'}
    needs_mesh = mesh_shape and (start is None or start.type != 'MESH')
    needs_empty = not mesh_shape and (start is None or start.type != 'EMPTY')
    if needs_mesh or needs_empty:
        old = start
        data = _ruri_shape_mesh(item.ruri_shape) if mesh_shape else None
        start = bpy.data.objects.new('碰撞体_' + item.name, data)
        collection.objects.link(start)
        if old is not None:
            old_name = old.name
            old_settings = old.ruri_cloth_physics_collider
            start.ruri_cloth_physics_collider.enabled = old_settings.enabled
            for key in ('bonechain_ruri_collision_id',):
                if key in old:
                    start[key] = old[key]
            was_selected = old.select_get()
            was_active = context.view_layer.objects.active == old
            _replace_ruri_object(old, start)
            _remove_collision_object(old)
            start.name = old_name
            start.select_set(was_selected)
            if was_active:
                context.view_layer.objects.active = start
        geom.attach_to_bone(context.view_layer, start, arm, bone.name)
    elif mesh_shape and start.data.get('bonechain_ruri_shape') != item.ruri_shape:
        old_mesh = start.data
        start.data = _ruri_shape_mesh(item.ruri_shape)
        if old_mesh.users == 0:
            bpy.data.meshes.remove(old_mesh)
    settings = start.ruri_cloth_physics_collider
    settings.is_collider = True
    native_shape = 'MESH' if mesh_shape else item.ruri_shape
    if settings.shape != native_shape:
        settings.shape = native_shape
    if item.ruri_shape == 'CAPSULE' and end is None:
        end = operators._link_empty(context, collection, geom.end_name(item.name),
                                    geom.END_DISPLAY_TYPE, item.radius,
                                    arm.matrix_world @ bone.tail)
        geom.attach_to_bone(context.view_layer, end, arm, bone.name)
    settings.end_object = end
    local_rotation = Euler(item.ruri_rotation, 'XYZ').to_matrix().to_4x4()
    offset = Vector(item.ruri_offset)
    center = Vector((0, 0 if item.ruri_shape == 'CAPSULE' else bone.bone.length / 2, 0))
    local = Matrix.Translation(center + offset) @ local_rotation
    if mesh_shape:
        local = local @ Matrix.Diagonal((*item.ruri_dimensions, 1.0))
    constraint = geom.bone_constraint(start)
    start.matrix_basis = constraint.inverse_matrix.inverted_safe() @ local
    if end is not None:
        if item.ruri_shape == 'CAPSULE':
            tip = Vector((0, bone.bone.length * item.ruri_length_scale, 0))
            tip += Vector(item.ruri_end_offset)
            endpoint = offset + local_rotation.to_3x3() @ tip
            end_constraint = geom.bone_constraint(end)
            end.matrix_basis = end_constraint.inverse_matrix.inverted_safe() @ Matrix.Translation(endpoint)
        shared_capsule_end = any(obj != start
                                 and geom.settings_of(obj)
                                 and geom.settings_of(obj).is_collider
                                 and geom.settings_of(obj).shape == 'CAPSULE'
                                 and geom.settings_of(obj).end_object == end
                                 for obj in bpy.data.objects)
        if not shared_capsule_end:
            end.hide_set(item.ruri_shape != 'CAPSULE')
        end.hide_render = False
        end['bonechain_ruri_collision_id'] = grp.collision_id
        item.ruri_end = end
    start['bonechain_ruri_collision_id'] = grp.collision_id
    start.display_type = 'WIRE'
    start.show_in_front = True
    start.hide_render = False
    item.ruri_collider_name = start.name
    item.ruri_collider = start
    item.ruri_collider_uid = item.ruri_end_uid = ''
    geom.sync_display(start)
    context.view_layer.update()
    _update_ruri_collider_radius(item)
    return start


def _update_ruri_geometry(item, context):
    grp = _find_group_of_collitem(context, item)
    if grp is None or grp.collision_mode != 'RURI':
        return
    arm = _ruri_armature_for_group(context, grp)
    if arm is not None:
        _ensure_ruri_item_geometry(context, grp, item, arm)


def _update_ruri_particle_radius(grp, context):
    if grp.collision_mode != 'RURI':
        return
    arm = _ruri_armature_for_group(context, grp)
    settings = getattr(arm, 'ruri_cloth_physics', None) if arm else None
    if settings and grp.ruri_config_name:
        config = settings.configs.get(grp.ruri_config_name)
        if config:
            config.radius.value = grp.wig_radius


def _update_collision_influence(self, context):
    grp = _find_group_of_collitem(context, self)
    if grp and grp.collision_mode == 'OLD':
        _sync_group_collision(context, grp, changed_item=self)


def _is_descendant_or_self(bone, ancestor):
    while bone is not None:
        if bone == ancestor:
            return True
        bone = bone.parent
    return False


def _ruri_collision_issues(arm, grp):
    """Identify missing colliders and colliders following the simulated chain."""
    flexible = {item.name for item in grp.flex_bones if item.name}
    issues = []
    for index, item in enumerate(grp.coll_bones):
        bone = arm.pose.bones.get(item.name)
        if bone is None:
            issues.append({'index': index, 'name': item.name, 'reason': 'missing'})
            continue
        ancestor = bone
        while ancestor is not None:
            if ancestor.name in flexible:
                issues.append({'index': index, 'name': item.name, 'reason': 'flexible',
                               'ancestor': ancestor.name})
                break
            ancestor = ancestor.parent
    return issues


def _repair_tracking_cycles(arm):
    """仅移除指向自身/后代的阻尼追踪，保留无环的其他约束。"""
    removed = 0
    for bone in arm.pose.bones:
        for con in list(bone.constraints):
            if con.type != 'DAMPED_TRACK' or con.target != arm:
                continue
            target = arm.pose.bones.get(con.subtarget)
            if target and _is_descendant_or_self(target, bone):
                bone.constraints.remove(con)
                removed += 1
    return removed


def _collision_radius(citem, length):
    radius = citem.radius + citem.margin
    # 中点采样球覆盖整个显示圆柱；不增加约束数量。
    # 注意这是圆柱覆盖保证，不是网格或运动路径的碰撞保证。
    if citem.fill_gaps:
        radius = sqrt(radius * radius + (length / (2 * citem.subdiv)) ** 2)
    return radius


def _find_group_of_collitem(context, citem):
    props = context.scene.bonechain_props
    for g in props.groups:
        for c in g.coll_bones:
            if c == citem:
                return g
    return None


class CollisionItem(PropertyGroup):
    name: StringProperty(name="碰撞骨骼名")
    capsule_name: StringProperty()
    ball_group: StringProperty()
    ruri_collider_name: StringProperty()
    ruri_collider: PointerProperty(type=bpy.types.Object)
    ruri_end: PointerProperty(type=bpy.types.Object)
    ruri_collider_uid: StringProperty(options={'HIDDEN'})
    ruri_end_uid: StringProperty(options={'HIDDEN'})
    radius: FloatProperty(
        name="粗细", default=0.03, min=0.001, soft_max=1.0,
        update=_update_collision)
    subdiv: IntProperty(
        name="细分", default=3, min=1, max=20,
        update=_update_collision)
    influence: FloatProperty(
        name="强度", default=1.0, min=0.0, max=1.0,
        update=_update_collision_influence)
    fill_gaps: BoolProperty(
        name="填补球间隙", default=False, update=_update_collision,
        description="扩大实际碰撞球以覆盖整段圆柱；球数少时会明显鼓起，可增加细分减小鼓起")
    margin: FloatProperty(
        name="碰撞余量", default=0.0, min=0.0, soft_max=0.05,
        update=_update_collision,
        description="在粗细之外预留衣物厚度，单位与骨架局部尺寸相同")
    ruri_shape: EnumProperty(
        name="形状", default='CAPSULE', update=_update_ruri_geometry,
        items=[('CAPSULE', "胶囊", "两端球心之间的连续碰撞体"),
               ('SPHERE', "球", "单个球形碰撞体"),
               ('BOX', "盒体", "可独立调整 XYZ 尺寸的闭合网格碰撞体"),
               ('ELLIPSOID', "椭球", "可独立调整 XYZ 尺寸的闭合网格碰撞体")])
    ruri_offset: FloatVectorProperty(
        name="XYZ 偏移", size=3, subtype='TRANSLATION', default=(0, 0, 0),
        description="沿绑定骨骼的局部 XYZ 轴移动整个碰撞体，随骨骼运动",
        update=_update_ruri_geometry)
    ruri_rotation: FloatVectorProperty(
        name="XYZ 旋转", size=3, subtype='EULER', default=(0, 0, 0),
        description="相对绑定骨骼的局部旋转；胶囊绕起点转动",
        update=_update_ruri_geometry)
    ruri_dimensions: FloatVectorProperty(
        name="XYZ 尺寸", size=3, subtype='XYZ', default=(0.1, 0.1, 0.1),
        min=0.001, soft_max=1.0, update=_update_ruri_geometry,
        description="盒体/椭球沿自身 XYZ 轴的完整尺寸，单位为骨架局部尺寸")
    ruri_length_scale: FloatProperty(
        name="长度倍率", default=1.0, min=0.01, soft_max=3.0,
        update=_update_ruri_geometry)
    ruri_end_offset: FloatVectorProperty(
        name="终点 XYZ 微调", size=3, subtype='TRANSLATION', default=(0, 0, 0),
        description="在胶囊自身的局部 XYZ 方向上微调终点球心",
        update=_update_ruri_geometry)
    ruri_split_radius: BoolProperty(
        name="两端分别调粗细", default=False, update=_update_ruri_geometry)
    ruri_end_radius: FloatProperty(
        name="终点半径", default=0.03, min=0.001, soft_max=1.0,
        update=_update_ruri_geometry)


def _ruri_bake_start_get(group):
    return group.get('ruri_bake_start', getattr(group.id_data, 'frame_start', 1))


def _ruri_bake_start_set(group, value):
    group['ruri_bake_start'] = value


def _ruri_bake_end_get(group):
    return group.get('ruri_bake_end', getattr(group.id_data, 'frame_end', 250))


def _ruri_bake_end_set(group, value):
    group['ruri_bake_end'] = value


class ChainGroup(PropertyGroup):
    name: StringProperty(name="组名", default="新分组")

    collision_mode: EnumProperty(
        name="碰撞模式",
        items=[
            ('NEW', "新版 (Wiggle物理)", "使用Wiggle2进行物理防穿"),
            ('RURI', "Ruri (GPU骨骼布料)", "固定根骨骼、逐边胶囊碰撞，使用 Ruri Cloth Physics"),
            ('OLD', "老版 (限制距离)", "使用限制距离约束(胶囊防穿)")
        ],
        default='NEW'
    )

    flex_bones: CollectionProperty(type=BoneItem)
    flex_index: IntProperty(default=0)
    coll_bones: CollectionProperty(type=CollisionItem)
    coll_index: IntProperty(default=0)

    deform_targets: CollectionProperty(type=CollisionItem)
    deform_index: IntProperty(default=0)

    infl_head: FloatProperty(name="头部强度", default=1.0, min=0.0, max=1.0)
    infl_tail: FloatProperty(name="尾部强度", default=1.0, min=0.0, max=1.0)

    cyl_radius: FloatProperty(name="圆柱粗细", default=0.09, min=0.005, soft_max=0.5)
    wig_radius: FloatProperty(name="碰撞半径", default=0.03, min=0.001, soft_max=0.3,
                              update=_update_ruri_particle_radius)
    wig_mass: FloatProperty(name="质量", default=0.3, min=0.01, soft_max=5.0)
    wig_stiff: FloatProperty(name="劲度", default=200.0, min=0.0, soft_max=1000.0)
    wig_damp: FloatProperty(name="阻尼", default=3.0, min=0.0, soft_max=20.0)
    wig_stretch: FloatProperty(
        name="拉伸", default=0.0, min=0.0, max=1.0,
        description="0 保持骨骼原长；增大数值允许弹性拉伸，长发链建议使用 0")
    wig_bake_start: IntProperty(name="烘焙起始帧", default=1, min=-1048574, max=1048574)
    wig_bake_end: IntProperty(name="烘焙结束帧", default=250, min=-1048574, max=1048574)
    ruri_bake_start: IntProperty(
        name="起始帧", default=1, min=-1048574, max=1048574,
        get=_ruri_bake_start_get, set=_ruri_bake_start_set)
    ruri_bake_end: IntProperty(
        name="结束帧", default=250, min=-1048574, max=1048574,
        get=_ruri_bake_end_get, set=_ruri_bake_end_set)
    ruri_bake_preroll: IntProperty(
        name="预模拟帧数", default=30, min=0, max=10000,
        description="从场景起始帧或烘焙起始帧中较早的一帧再提前模拟，让头发先稳定；预模拟不写关键帧")
    wig_base_keys: BoolProperty(
        name="补齐未动画通道的基础姿态", default=True,
        description="为柔性骨骼未被关键帧或驱动器控制的变换通道添加起始帧关键帧，避免模拟结果逐帧累积；保留已有动画")
    wig_clean: BoolProperty(
        name="应用前停用冲突约束", default=True,
        description=("停用柔性骨骼上的阻尼追踪和旧限制距离约束，并解除目标引用。"
                     "Wiggle 会把阻尼追踪当'钉住',与链条互相拉扯导致炸飞"))
    coll_collection_name: StringProperty(default="")
    collision_id: StringProperty(default="", options={'HIDDEN'})
    ruri_config_name: StringProperty(default="", options={'HIDDEN'})
    ruri_physics_type: EnumProperty(
        name="Ruri 类型", default='BONE_CLOTH',
        items=[('BONE_CLOTH', '骨骼布料（头发／裙摆）', '固定链根，模拟下游骨骼'),
               ('BONE_SPRING', '骨骼弹簧（胸部）', '使用 Ruri 原生弹簧，让所选胸骨在动画原位附近小幅运动，不引入父骨')])
    ruri_simulate_roots: BoolProperty(
        name="所选根骨也参与模拟（胸部）", default=False,
        description="以所选骨骼现有的父骨作固定支撑，让列表中的第一根骨骼也参与模拟；不创建骨骼、不修改父子关系，修改后需重新配置")
    ruri_armature: PointerProperty(
        name="绑定骨架", type=bpy.types.Object,
        poll=lambda self, obj: obj.type == 'ARMATURE')
    ruri_geometry_open: BoolProperty(name="选中碰撞体：形状与偏移", default=False)


class BoneChainProps(PropertyGroup):
    groups: CollectionProperty(type=ChainGroup)
    group_index: IntProperty(default=0)
    # Keep legacy range keys so saved plugin bake ranges remain available.
    native_cloth_open: BoolProperty(
        name='插件物理烘焙', default=False, options={'SKIP_SAVE'},
        description='展开或折叠统一烘焙设置')
    native_cloth_start: IntProperty(
        name='起始帧', min=-1048574, max=1048574,
        get=lambda self: self.get('native_cloth_start', self.id_data.frame_start),
        set=lambda self, value: self.__setitem__('native_cloth_start', value))
    native_cloth_end: IntProperty(
        name='结束帧', min=-1048574, max=1048574,
        get=lambda self: self.get('native_cloth_end', self.id_data.frame_end),
        set=lambda self, value: self.__setitem__('native_cloth_end', value))
    native_cloth_preroll: IntProperty(name='预模拟帧数', default=30, min=0, max=10000)
    native_cloth_memory_gb: IntProperty(name='工作进程内存上限 (GiB)', default=8, min=2, max=1024)
    subdiv_count: IntProperty(
        name="细分段数", default=4, min=2, max=32,
        description="把每根选中骨骼切成几段")
    subdiv_weights: BoolProperty(
        name="转移权重", default=True,
        description="把原骨骼顶点组权重按位置分配到新段(实验性)")


# ===============================================================
# 链识别
# ===============================================================
def get_all_chains(bone_names, arm):
    pbones = [arm.pose.bones[n] for n in bone_names if n in arm.pose.bones]
    sel_set = set(b.name for b in pbones)
    heads = [b for b in pbones
             if not (b.parent and b.parent.name in sel_set)]
    if not heads:
        heads = list(pbones)
    chains = []

    def walk(bone, path):
        path = path + [bone]
        children = [c for c in bone.children if c.name in sel_set]
        if not children:
            chains.append(path)
        else:
            for c in children:
                walk(c, path)

    for h in heads:
        walk(h, [])
    return chains


# ===============================================================
# 老版本: 碰撞体 (限制距离)
# ===============================================================
def _remove_collision_object(obj):
    mesh = obj.data if obj.type == 'MESH' else None
    bpy.data.objects.remove(obj, do_unlink=True)
    if mesh is not None and mesh.users == 0:
        bpy.data.meshes.remove(mesh)


def _item_balls(citem):
    # 只匹配数字后缀，避免删除名称有相同前缀的其他骨骼碰撞体。
    if not citem.ball_group:
        return []
    prefix = citem.ball_group
    return [obj for obj in bpy.data.objects
            if obj.name.startswith(prefix) and obj.name[len(prefix):].isdigit()]


def _clear_item_objects(citem):
    cap = bpy.data.objects.get(citem.capsule_name)
    if cap:
        _remove_collision_object(cap)
    for obj in _item_balls(citem):
        _remove_collision_object(obj)


def _clear_ruri_collision_items(context, grp, items, clear_all=False):
    if not hasattr(bpy.types.Object, 'ruri_cloth_physics_collider'):
        return 0
    import importlib
    geom = importlib.import_module('RuriClothPhysics.blender_host.collider_geom')
    runtime = importlib.import_module('RuriClothPhysics.blender_host.runtime')
    selection = importlib.import_module('RuriClothPhysics.blender_host.selection_sync')
    viewport = importlib.import_module('RuriClothPhysics.blender_host.viewport')
    arm = _ruri_armature_for_group(context, grp)
    config = (arm.ruri_cloth_physics.configs.get(grp.ruri_config_name)
              if arm and grp.ruri_config_name else None)
    starts, ends = {}, {}

    def remember(target):
        if target is None or not geom.is_collider(target):
            return
        starts[target.as_pointer()] = target
        end = geom.end_object(target.ruri_cloth_physics_collider)
        if end is not None:
            ends[end.as_pointer()] = end

    for item in items:
        target = _ruri_item_object(item, 'ruri_collider_name', 'ruri_collider_uid')
        if target is None and config:
            matches = []
            for reference in config.collider_collision.collider_references:
                candidate = reference.collider
                constraint = geom.bone_constraint(candidate) if candidate else None
                if constraint and constraint.target == arm and constraint.subtarget == item.name:
                    matches.append(candidate)
            if len(matches) == 1:
                target = matches[0]
        remember(target)
        if item.ruri_end:
            ends[item.ruri_end.as_pointer()] = item.ruri_end
    if clear_all and grp.collision_id:
        for obj in bpy.data.objects:
            if obj.get('bonechain_ruri_collision_id') == grp.collision_id:
                remember(obj)
    if config:
        collision = config.collider_collision
        references = collision.collider_references
        with selection.suppressed():
            for index in range(len(references) - 1, -1, -1):
                target = references[index].collider
                if clear_all or (target and target.as_pointer() in starts):
                    references.remove(index)
            collision.active_collider_reference_index = max(0, min(
                collision.active_collider_reference_index, len(references) - 1))
    used = set()
    for obj in bpy.data.objects:
        settings = getattr(obj, 'ruri_cloth_physics', None)
        if obj.type == 'ARMATURE' and settings:
            used.update(reference.collider.as_pointer()
                        for cfg in settings.configs
                        for reference in cfg.collider_collision.collider_references
                        if reference.collider is not None)
    ignored = {item.as_pointer() for item in items}
    for scene in bpy.data.scenes:
        world = getattr(scene, 'ruri_cloth_physics', None)
        if world:
            used.update(entry.object.as_pointer() for entry in world.colliders if entry.object)
        props = getattr(scene, 'bonechain_props', None)
        if props:
            for group in props.groups:
                for item in group.coll_bones:
                    if item.as_pointer() in ignored:
                        continue
                    for target in (item.ruri_collider, item.ruri_end):
                        if target:
                            used.add(target.as_pointer())
    retained = 0
    for pointer, target in starts.items():
        owned = bool(grp.collision_id) and target.get('bonechain_ruri_collision_id') == grp.collision_id
        if pointer in used or not owned:
            retained += 1
        else:
            _remove_collision_object(target)
    for pointer, end in ends.items():
        if pointer in used or geom.is_collider(end):
            continue
        if not any(geom.settings_of(obj) and geom.settings_of(obj).end_object == end
                   for obj in bpy.data.objects):
            _remove_collision_object(end)
    for item in items:
        item.ruri_collider = item.ruri_end = None
        item.ruri_collider_name = item.ruri_collider_uid = item.ruri_end_uid = ''
    context.view_layer.update()
    if config:
        index = list(arm.ruri_cloth_physics.configs.keys()).index(config.name)
        if runtime.get_entry(arm, index) is not None:
            runtime.ensure_entry(arm, index, config, context.evaluated_depsgraph_get())
    viewport.tag_redraw()
    return retained


def _make_capsule_mesh(name, radius, length):
    import bmesh
    me = bpy.data.meshes.new(name)
    bm = bmesh.new()
    bmesh.ops.create_cone(
        bm, cap_ends=True, cap_tris=False, segments=12,
        radius1=radius, radius2=radius, depth=length)
    bmesh.ops.rotate(
        bm, verts=bm.verts, matrix=Matrix.Rotation(pi / 2, 3, 'X'))
    bm.to_mesh(me)
    bm.free()
    return me


def _rebuild_collision_item(context, citem):
    arm = context.active_object
    if not arm or arm.type != 'ARMATURE':
        return
    bone_name = citem.name
    if bone_name not in arm.pose.bones:
        return
    pb = arm.pose.bones[bone_name]
    length = pb.length
    if not citem.ball_group:
        from uuid import uuid4
        citem.ball_group = f"碰撞球_{uuid4().hex}_"
    n = citem.subdiv
    # 半径拖动只查找已有物体；细分变化才扫描并删除多出的球。
    cap = bpy.data.objects.get(citem.capsule_name)
    if cap is None or cap.get("bonechain_subdiv") != n:
        for obj in _item_balls(citem):
            if int(obj.name[len(citem.ball_group):]) >= n:
                _remove_collision_object(obj)
    for i in range(n):
        t = (i + 0.5) / n
        ball_name = f"{citem.ball_group}{i}"
        empty = bpy.data.objects.get(ball_name)
        if empty is None:
            empty = bpy.data.objects.new(ball_name, None)
            context.collection.objects.link(empty)
            empty.parent = arm
            empty.parent_type = 'BONE'
            empty.parent_bone = bone_name
        empty.empty_display_type = 'SPHERE'
        effective_radius = _collision_radius(citem, length)
        if empty.empty_display_size != effective_radius:
            empty.empty_display_size = effective_radius
        empty.show_in_front = True # 强制透视显示
        empty.location = (0, -length * (1 - t), 0)

    cap_name = f"碰撞胶囊_{bone_name}"
    if cap is None:
        me = _make_capsule_mesh(cap_name, 1.0, 1.0)
        cap = bpy.data.objects.new(cap_name, me)
        context.collection.objects.link(cap)
        cap.parent = arm
        cap.parent_type = 'BONE'
        cap.parent_bone = bone_name
        cap["bonechain_unit_mesh"] = True
    elif not cap.get("bonechain_unit_mesh"):
        # 旧文件的网格带实际尺寸：仅迁移一次，后续直接缩放。
        old_mesh = cap.data
        cap.data = _make_capsule_mesh(cap_name, 1.0, 1.0)
        if old_mesh.users == 0:
            bpy.data.meshes.remove(old_mesh)
        cap["bonechain_unit_mesh"] = True
    cap.scale = (citem.radius, length, citem.radius)
    if cap.get("bonechain_subdiv") != n:
        cap["bonechain_subdiv"] = n
    cap.display_type = 'WIRE'
    cap.hide_render = True
    cap.show_in_front = True # 强制透视显示
    cap.location = (0, -length / 2, 0)
    citem.capsule_name = cap.name


def _group_collision_prefix(grp):
    if not grp.collision_id:
        from uuid import uuid4
        grp.collision_id = uuid4().hex[:12]
    return f"BC_{grp.collision_id}_"


def _sync_group_collision(context, grp, changed_item=None):
    """复用约束；单项修改不触碰其他碰撞项的约束。"""
    arm = context.active_object
    if not arm or arm.type != 'ARMATURE':
        return
    prefix = _group_collision_prefix(grp)
    items = [changed_item] if changed_item is not None else grp.coll_bones
    # 查找物体、计算半径移到柔性骨骼循环之外。
    prepared = []
    legacy_prefixes = tuple(f"碰撞_{item.name}_" for item in items)
    flexible = [arm.pose.bones[b.name] for b in grp.flex_bones
                if b.name in arm.pose.bones]
    unsafe_colliders = set()
    for citem in items:
        collider_bone = arm.pose.bones.get(citem.name)
        if not collider_bone or not citem.ball_group:
            continue
        # 防止本组被柔性骨骼驱动的碰撞体反过来约束本组。
        if any(_is_descendant_or_self(collider_bone, b) for b in flexible):
            unsafe_colliders.add(citem.name)
        # 世界空间统一距离；非均匀缩放取最大轴，保守覆盖碰撞体。
        radius = _collision_radius(citem, collider_bone.length) * max(
            abs(v) for v in arm.matrix_world.to_scale())
        token = hashlib.sha1(citem.ball_group.encode('utf-8')).hexdigest()[:12]
        for i in range(citem.subdiv):
            ball = bpy.data.objects.get(f"{citem.ball_group}{i}")
            if ball:
                prepared.append((citem, collider_bone, f"{prefix}{token}_{i}", ball, radius))
    # 全量同步也清除已从柔性列表移除的骨骼上的本组约束。
    flex_names = {b.name for b in grp.flex_bones}
    bones = ([arm.pose.bones[n] for n in flex_names if n in arm.pose.bones]
             if changed_item is not None else arm.pose.bones)
    skipped = 0
    for pb in bones:
        existing = {c.name: c for c in pb.constraints
                    if c.type == 'LIMIT_DISTANCE' and c.name.startswith(prefix)}
        wanted = set()
        # v5.3 的名称没有组标识。只移除当前组可明确识别的旧名称。
        if pb.name in flex_names:
            for con in list(pb.constraints):
                if con.type == 'LIMIT_DISTANCE' and any(
                        con.name.startswith(p) and con.name[len(p):].isdigit()
                        for p in legacy_prefixes):
                    pb.constraints.remove(con)
            for citem, collider_bone, name, ball, radius in prepared:
                if citem.name in unsafe_colliders:
                    skipped += 1
                    continue
                wanted.add(name)
                con = existing.get(name)
                if con is None:
                    con = pb.constraints.new('LIMIT_DISTANCE')
                    con.name = name
                if con.target != ball:
                    con.target = ball
                if con.mute:
                    con.mute = False
                if con.limit_mode != 'LIMITDIST_OUTSIDE':
                    con.limit_mode = 'LIMITDIST_OUTSIDE'
                if con.owner_space != 'WORLD':
                    con.owner_space = 'WORLD'
                if con.target_space != 'WORLD':
                    con.target_space = 'WORLD'
                if con.distance != radius:
                    con.distance = radius
                if con.influence != citem.influence:
                    con.influence = citem.influence
        if changed_item is not None:
            token = hashlib.sha1(changed_item.ball_group.encode('utf-8')).hexdigest()[:12]
            item_prefix = f"{prefix}{token}_"
        else:
            item_prefix = None
        for name, con in existing.items():
            belongs = item_prefix is None or (
                name.startswith(item_prefix) and name[len(item_prefix):].isdigit())
            if belongs and name not in wanted:
                pb.constraints.remove(con)
    return skipped


# ===============================================================
# 操作符 - 全局可视化控制
# ===============================================================
class BONECHAIN_OT_toggle_collision_vis(Operator):
    bl_idname = "bonechain.toggle_collision_vis"
    bl_label = "显示/隐藏碰撞体"
    bl_description = "一键切换当前组内所有碰撞球、胶囊和圆柱的可见性"

    def execute(self, context):
        grp = _get_active_group(context)
        if not grp: return {'CANCELLED'}
        if grp.collision_mode == 'RURI':
            arm = _ruri_armature_for_group(context, grp)
            settings = getattr(arm, 'ruri_cloth_physics', None) if arm else None
            if settings is None:
                self.report({'WARNING'}, "没有找到当前 Ruri 分组所属的骨架")
                return {'CANCELLED'}
            visible = bool(settings.show_colliders or settings.show_collision_radius)
            settings.show_colliders = not visible
            settings.show_collision_radius = not visible
            self.report({'INFO'}, "已%s Ruri 碰撞体和覆盖线框" % ("显示" if not visible else "隐藏"))
            return {'FINISHED'}

        # 获取当前是该隐藏还是显示 (以找到的第一个物体的状态取反)
        is_hidden = False
        first_obj = None

        if grp.coll_bones and grp.coll_bones[0].capsule_name:
            first_obj = bpy.data.objects.get(grp.coll_bones[0].capsule_name)
        if not first_obj and grp.deform_targets:
            item = grp.deform_targets[0]
            first_obj = bpy.data.objects.get(item.capsule_name or f"碰撞柱_{item.name}")

        if first_obj:
            is_hidden = not first_obj.hide_get()

        count = 0
        # 切换旧版可见性
        for citem in grp.coll_bones:
            cap = bpy.data.objects.get(citem.capsule_name)
            if cap:
                cap.hide_set(is_hidden)
                cap.show_in_front = True
                count += 1
            if citem.ball_group:
                for obj in _item_balls(citem):
                    obj.hide_set(is_hidden)
                    obj.show_in_front = True
                    count += 1

        # 切换新版可见性
        for citem in grp.deform_targets:
            cyl = bpy.data.objects.get(citem.capsule_name or f"碰撞柱_{citem.name}")
            if cyl:
                cyl.hide_set(is_hidden)
                cyl.show_in_front = True
                count += 1

        if count == 0:
            self.report({'WARNING'}, "没有找到任何碰撞体，请先生成/应用碰撞")
        else:
            state = "隐藏" if is_hidden else "显示"
            self.report({'INFO'}, f"已{state} {count} 个碰撞体")

        return {'FINISHED'}


# ===============================================================
# 操作符 - 分组
# ===============================================================
class BONECHAIN_OT_group_add(Operator):
    bl_idname = "bonechain.group_add"
    bl_label = "新建分组"

    def execute(self, context):
        props = context.scene.bonechain_props
        g = props.groups.add()
        g.name = f"分组{len(props.groups)}"
        props.group_index = len(props.groups) - 1
        return {'FINISHED'}


class BONECHAIN_OT_group_remove(Operator):
    bl_idname = "bonechain.group_remove"
    bl_label = "删除分组"

    def execute(self, context):
        props = context.scene.bonechain_props
        idx = props.group_index
        if 0 <= idx < len(props.groups):
            grp = props.groups[idx]
            _clear_ruri_collision_items(context, grp, list(grp.coll_bones), clear_all=True)
            grp.flex_bones.clear()
            _sync_group_collision(context, grp)
            for citem in props.groups[idx].coll_bones:
                _clear_item_objects(citem)
            props.groups.remove(idx)
            props.group_index = max(0, idx - 1)
        return {'FINISHED'}


# ===============================================================
# 操作符 - 柔性组
# ===============================================================
class BONECHAIN_OT_add_flex(Operator):
    bl_idname = "bonechain.add_flex"
    bl_label = "添加选中到柔性组"

    def execute(self, context):
        grp = _get_active_group(context)
        if not grp:
            self.report({'ERROR'}, "请先新建/选择分组")
            return {'CANCELLED'}
        sel = context.selected_pose_bones or []
        exist = set(b.name for b in grp.flex_bones)
        added = 0
        for b in sel:
            if b.name not in exist:
                grp.flex_bones.add().name = b.name
                added += 1
        self.report({'INFO'}, f"柔性组添加 {added} 根")
        return {'FINISHED'}


class BONECHAIN_OT_remove_flex(Operator):
    bl_idname = "bonechain.remove_flex"
    bl_label = "移除当前柔性骨骼"
    bl_description = "只从当前柔性组移除列表中选中的骨骼，不删除骨架里的骨骼"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        grp = _get_active_group(context)
        if not grp or not grp.flex_bones:
            self.report({'WARNING'}, "柔性骨骼列表为空")
            return {'CANCELLED'}
        index = min(max(grp.flex_index, 0), len(grp.flex_bones) - 1)
        name = grp.flex_bones[index].name
        grp.flex_bones.remove(index)
        grp.flex_index = min(index, len(grp.flex_bones) - 1)
        if grp.collision_mode == 'OLD':
            _sync_group_collision(context, grp)
        self.report({'INFO'}, f"已从柔性组移除 {name}；Ruri/Wiggle 请重新应用配置")
        return {'FINISHED'}


class BONECHAIN_OT_clear_flex(Operator):
    bl_idname = "bonechain.clear_flex"
    bl_label = "清空柔性组"

    def execute(self, context):
        grp = _get_active_group(context)
        if grp:
            grp.flex_bones.clear()
            _sync_group_collision(context, grp)
        return {'FINISHED'}


# ===============================================================
# 操作符 - 碰撞组
# ===============================================================
class BONECHAIN_OT_add_coll(Operator):
    bl_idname = "bonechain.add_coll"
    bl_label = "添加选中到碰撞组"

    def execute(self, context):
        grp = _get_active_group(context)
        if not grp:
            self.report({'ERROR'}, "请先新建/选择分组")
            return {'CANCELLED'}
        sel = context.selected_pose_bones or []
        exist = set(b.name for b in grp.coll_bones)
        added = 0
        for b in sel:
            if b.name in exist:
                continue
            it = grp.coll_bones.add()
            it.name = b.name
            if grp.collision_mode == 'OLD':
                _rebuild_collision_item(context, it)
            added += 1

        if grp.collision_mode == 'OLD':
            _sync_group_collision(context, grp)

        self.report({'INFO'}, f"碰撞组添加 {added} 根")
        return {'FINISHED'}


import re as _re

def _extract_core_side(name):
    n = name
    side = ""
    m = _re.search(r'[._]([lrLR])$', n)
    if m:
        side = m.group(1).lower()
        n = n[:m.start()]
    if n.startswith("c_"):
        n = n[2:]
    for suf in ["_fk", "_ik", "_b", "_nostr", "_cns", "_twist",
                "_stretch", "_master", "_bend"]:
        while n.endswith(suf):
            n = n[:-len(suf)]
    core = n.split("_")[0].split(".")[0].lower()
    return core, side


def _find_deform_by_name(arm, ctrl_name):
    core, side = _extract_core_side(ctrl_name)
    if not core:
        return []
    found = []
    for pb in arm.pose.bones:
        if not arm.data.bones[pb.name].use_deform:
            continue
        c2, s2 = _extract_core_side(pb.name)
        if c2 == core and (side == "" or s2 == side):
            found.append(pb.name)
    return found


def _resolved_deform_names(arm, collision_items):
    """Resolve each remaining collision source using the same rules as the UI."""
    result = []
    for item in collision_items:
        bone = arm.data.bones.get(item.name)
        if bone is None:
            continue
        names = [bone.name] if bone.use_deform else _find_deform_by_name(arm, bone.name)
        for name in names:
            if name not in result:
                result.append(name)
    return result


def _prune_wiggle_deform_targets(context, grp):
    """Drop derived capsules no longer referenced by any collision source."""
    if grp.collision_mode != 'NEW' or not grp.deform_targets:
        return 0
    keep = set()
    if grp.coll_bones:
        owners = set()
        for item in grp.deform_targets:
            obj = bpy.data.objects.get(item.capsule_name)
            if obj and obj.parent and obj.parent.type == 'ARMATURE' and obj.parent_type == 'BONE':
                owners.add(obj.parent)
        # Prefer the capsules' owner over whichever object is currently selected.
        arm = next(iter(owners)) if len(owners) == 1 else None
        if not owners:
            active = context.active_object
            if active and active.type == 'ARMATURE':
                arm = active
            elif active and active.type == 'MESH':
                arm = active.find_armature()
        if arm is None:
            return 0
        keep.update(_resolved_deform_names(arm, grp.coll_bones))
    removed = 0
    for index in range(len(grp.deform_targets) - 1, -1, -1):
        item = grp.deform_targets[index]
        if item.name not in keep:
            _clear_item_objects(item)
            grp.deform_targets.remove(index)
            removed += 1
    grp.deform_index = max(0, min(grp.deform_index, len(grp.deform_targets) - 1))
    return removed


class BONECHAIN_OT_resolve_deform(Operator):
    bl_idname = "bonechain.resolve_deform"
    bl_label = "解析变形骨"
    bl_description = ("遍历碰撞组:控制骨→按命名规范找同名变形骨,变形骨→原样加入。"
                     "结果进'变形骨列表',圆柱和 Wiggle 只对该列表操作")

    def execute(self, context):
        grp = _get_active_group(context)
        arm = context.active_object
        if not grp:
            self.report({'ERROR'}, "请先新建/选择分组")
            return {'CANCELLED'}
        if not arm or arm.type != 'ARMATURE':
            self.report({'ERROR'}, "请先选择骨架")
            return {'CANCELLED'}
        if not grp.coll_bones:
            self.report({'ERROR'}, "碰撞组为空,先添加骨骼")
            return {'CANCELLED'}

        for d in grp.deform_targets:
            _clear_item_objects(d)
        grp.deform_targets.clear()

        result = _resolved_deform_names(arm, grp.coll_bones)

        if not result:
            self.report({'WARNING'},
                        "没解析到变形骨,请检查碰撞组骨骼")
            return {'CANCELLED'}

        for dn in result:
            it = grp.deform_targets.add()
            it.name = dn
            it.radius = grp.cyl_radius

        self.report({'INFO'},
                    f"解析出 {len(result)} 根变形骨,可'生成碰撞圆柱'了")
        return {'FINISHED'}


class BONECHAIN_OT_clear_deform(Operator):
    bl_idname = "bonechain.clear_deform"
    bl_label = "清空变形骨列表"

    def execute(self, context):
        grp = _get_active_group(context)
        if grp:
            for d in grp.deform_targets:
                _clear_item_objects(d)
            grp.deform_targets.clear()
        return {'FINISHED'}


class BONECHAIN_OT_remove_coll(Operator):
    bl_idname = "bonechain.remove_coll"
    bl_label = "移除碰撞项"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        grp = _get_active_group(context)
        if not grp:
            return {'CANCELLED'}
        idx = grp.coll_index
        if 0 <= idx < len(grp.coll_bones):
            shared = _clear_ruri_collision_items(context, grp, [grp.coll_bones[idx]])
            _clear_item_objects(grp.coll_bones[idx])
            grp.coll_bones.remove(idx)
            grp.coll_index = max(0, idx - 1)
            removed = _prune_wiggle_deform_targets(context, grp)

            if grp.collision_mode == 'OLD':
                _sync_group_collision(context, grp)
            message = "已移除当前组的碰撞项及其碰撞引用"
            if removed:
                message += f"；同步移除 {removed} 个变形骨碰撞圆柱"
            if shared:
                message += f"；该部位仍有 {shared} 个其他分组/全局碰撞体"
            self.report({'INFO'}, message)
        return {'FINISHED'}


class BONECHAIN_OT_clear_coll(Operator):
    bl_idname = "bonechain.clear_coll"
    bl_label = "清空碰撞组"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        grp = _get_active_group(context)
        if not grp:
            return {'CANCELLED'}
        shared = _clear_ruri_collision_items(context, grp, list(grp.coll_bones), clear_all=True)
        for citem in grp.coll_bones:
            _clear_item_objects(citem)
        grp.coll_bones.clear()
        grp.coll_index = 0
        removed = _prune_wiggle_deform_targets(context, grp)

        if grp.collision_mode == 'OLD':
            _sync_group_collision(context, grp)
        message = "已清空当前组的碰撞项及其碰撞引用"
        if removed:
            message += f"；同步移除 {removed} 个变形骨碰撞圆柱"
        if shared:
            message += f"；该部位仍有 {shared} 个其他分组/全局碰撞体"
        self.report({'INFO'}, message)
        return {'FINISHED'}


# ===============================================================
# 操作符 - 阻尼追踪(渐变) / 应用碰撞
# ===============================================================
class BONECHAIN_OT_reset_ruri_geometry(Operator):
    bl_idname = "bonechain.reset_ruri_geometry"
    bl_label = "重置形状与偏移"
    bl_description = "恢复选中碰撞体的默认胶囊形状，保留粗细和其他碰撞体"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        grp = _get_active_group(context)
        if not grp or grp.collision_mode != 'RURI' or not 0 <= grp.coll_index < len(grp.coll_bones):
            return {'CANCELLED'}
        item = grp.coll_bones[grp.coll_index]
        # Set the stored properties together, then perform a single geometry update.
        for key, value in {'ruri_shape': 0, 'ruri_offset': (0, 0, 0),
                           'ruri_rotation': (0, 0, 0), 'ruri_dimensions': (.1, .1, .1),
                           'ruri_length_scale': 1.0, 'ruri_end_offset': (0, 0, 0),
                           'ruri_split_radius': False, 'ruri_end_radius': item.radius}.items():
            item[key] = value
        _update_ruri_geometry(item, context)
        return {'FINISHED'}


class BONECHAIN_OT_clean_ruri_collisions(Operator):
    bl_idname = "bonechain.clean_ruri_collisions"
    bl_label = "清理无效碰撞骨"
    bl_description = "移除不存在或位于本组柔性链中的碰撞项；保留有效碰撞参数、柔性列表和实际骨骼"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        grp = _get_active_group(context)
        if not grp or grp.collision_mode != 'RURI':
            return {'CANCELLED'}
        arm = _ruri_armature_for_group(context, grp)
        if arm is None or not grp.flex_bones or any(
                item.name not in arm.pose.bones for item in grp.flex_bones):
            self.report({'ERROR'}, "请先选择包含本组柔性骨骼的正确骨架")
            return {'CANCELLED'}
        issues = _ruri_collision_issues(arm, grp)
        if not issues:
            self.report({'INFO'}, "没有无效碰撞项")
            return {'FINISHED'}
        items = [grp.coll_bones[issue['index']] for issue in issues]
        _clear_ruri_collision_items(context, grp, items)
        for item in items:
            _clear_item_objects(item)
        for issue in reversed(issues):
            grp.coll_bones.remove(issue['index'])
        grp.coll_index = max(0, min(grp.coll_index, len(grp.coll_bones) - 1))
        print('[BoneChain] 已清理无效 Ruri 碰撞项: ' +
              ', '.join(issue['name'] for issue in issues), flush=True)
        self.report({'INFO'}, f"已清理 {len(issues)} 项；保留 {len(grp.coll_bones)} 项有效碰撞，请添加头部/身体固定骨后配置 Ruri")
        return {'FINISHED'}


class BONECHAIN_OT_damped_track(Operator):
    bl_idname = "bonechain.damped_track"
    bl_label = "修复追踪循环"
    bl_description = "清除当前骨架中追踪自身或后代的阻尼追踪；此操作不会创建摆动物理"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        arm = context.active_object
        if not arm or arm.type != 'ARMATURE':
            self.report({'ERROR'}, "请先选择骨架")
            return {'CANCELLED'}
        count = _repair_tracking_cycles(arm)
        self.report({'INFO'}, f"已清除 {count} 条自我/后代追踪；保留其他约束")
        return {'FINISHED'}


class BONECHAIN_OT_apply_collision(Operator):
    bl_idname = "bonechain.apply_collision"
    bl_label = "应用碰撞防穿(老版本)"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        grp = _get_active_group(context)
        arm = context.active_object
        if not arm or arm.type != 'ARMATURE':
            self.report({'ERROR'}, "请先选择骨架")
            return {'CANCELLED'}
        if not grp:
            return {'CANCELLED'}
        if not grp.flex_bones or not grp.coll_bones:
            self.report({'ERROR'}, "柔性组或碰撞组为空")
            return {'CANCELLED'}
        mmd_disabled = _disable_mmd_physics(
            [arm.pose.bones[b.name] for b in grp.flex_bones if b.name in arm.pose.bones])
        _pause_ruri_group(arm, grp)
        _stop_wiggle_bones(context.scene,
                           [arm.pose.bones[b.name] for b in grp.flex_bones
                            if b.name in arm.pose.bones])

        repaired = _repair_tracking_cycles(arm)
        # 确保对象存在，参数更新复用原有对象。
        for citem in grp.coll_bones:
            _rebuild_collision_item(context, citem)

        skipped = _sync_group_collision(context, grp)
        self.report({'WARNING'} if skipped else {'INFO'},
                    f"已应用碰撞，停用 {mmd_disabled} 条 MMD 跟随，清理 {repaired} 条循环追踪，跳过 {skipped} 个自我/后代碰撞配对")
        return {'FINISHED'}


# ===============================================================
# 细分骨骼链(实验性)
# ===============================================================
def _find_skinned_meshes(arm):
    meshes = []
    for obj in bpy.data.objects:
        if obj.type != 'MESH':
            continue
        for m in obj.modifiers:
            if m.type == 'ARMATURE' and m.object == arm:
                meshes.append(obj)
                break
    return meshes


def _transfer_weights(mesh, arm, orig_name, seg_names, seg_edit_data):
    vg = mesh.vertex_groups.get(orig_name)
    if not vg:
        return
    seg_groups = []
    for sn in seg_names:
        g = mesh.vertex_groups.get(sn) or mesh.vertex_groups.new(name=sn)
        seg_groups.append(g)

    n = len(seg_names)
    root = seg_edit_data[0][0]
    tip = seg_edit_data[-1][1]
    axis = tip - root
    total_len2 = axis.length_squared
    mw = mesh.matrix_world

    vg_idx = vg.index
    for v in mesh.data.vertices:
        w = 0.0
        for g in v.groups:
            if g.group == vg_idx:
                w = g.weight
                break
        if w <= 0.0:
            continue
        co_world = mw @ v.co
        if total_len2 > 1e-9:
            t = (co_world - root).dot(axis) / total_len2
        else:
            t = 0.0
        t = max(0.0, min(1.0, t))
        seg_f = t * n
        idx = int(seg_f)
        if idx >= n:
            idx = n - 1
        seg_groups[idx].add([v.index], w, 'REPLACE')

    mesh.vertex_groups.remove(vg)


class BONECHAIN_OT_subdivide_bones(Operator):
    bl_idname = "bonechain.subdivide_bones"
    bl_label = "细分骨骼链(实验性)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.active_object
                and context.active_object.type == 'ARMATURE')

    def execute(self, context):
        props = context.scene.bonechain_props
        arm = context.active_object
        n = props.subdiv_count
        do_weights = props.subdiv_weights
        prev_mode = context.mode

        if context.mode == 'POSE':
            targets = [b.name for b in (context.selected_pose_bones or [])]
        elif context.mode == 'EDIT_ARMATURE':
            targets = [b.name for b in context.selected_editable_bones
                       if b.select]
        else:
            self.report({'ERROR'}, "请在姿态或编辑模式选中骨骼")
            return {'CANCELLED'}

        if not targets:
            self.report({'ERROR'}, "未选中骨骼")
            return {'CANCELLED'}

        meshes = _find_skinned_meshes(arm) if do_weights else []
        bpy.ops.object.mode_set(mode='EDIT')
        eb = arm.data.edit_bones
        weight_jobs = []

        for orig_name in targets:
            bone = eb.get(orig_name)
            if not bone:
                continue
            head = bone.head.copy()
            tail = bone.tail.copy()
            roll = bone.roll
            orig_children = [c for c in bone.children]

            seg_points = [head.lerp(tail, i / n) for i in range(n + 1)]
            seg_names = [orig_name]

            bone.tail = seg_points[1]
            prev = bone
            for i in range(1, n):
                seg = eb.new(f"{orig_name}_seg{i+1}")
                seg.head = seg_points[i]
                seg.tail = seg_points[i + 1]
                seg.roll = roll
                seg.parent = prev
                seg.use_connect = True
                prev = seg
                seg_names.append(seg.name)

            last_seg = prev
            for ch in orig_children:
                ch.parent = last_seg

            mw = arm.matrix_world
            endpoints = []
            for i in range(n):
                h = mw @ seg_points[i]
                t = mw @ seg_points[i + 1]
                endpoints.append((h, t))
            weight_jobs.append((orig_name, seg_names, endpoints))

        bpy.ops.object.mode_set(mode='OBJECT')

        if do_weights:
            for mesh in meshes:
                for (orig_name, seg_names, endpoints) in weight_jobs:
                    _transfer_weights(mesh, arm, orig_name, seg_names, endpoints)

        try:
            if prev_mode == 'POSE':
                bpy.ops.object.mode_set(mode='POSE')
            elif prev_mode.startswith('EDIT'):
                bpy.ops.object.mode_set(mode='EDIT')
        except Exception:
            pass

        self.report({'INFO'},
                    f"{len(weight_jobs)} 根骨骼各细分为 {n} 段"
                    + ("(已转权重)" if do_weights else ""))
        return {'FINISHED'}


# ===============================================================
# Wiggle 2 (RTX Edition) 物理防穿
# ===============================================================
def _wiggle_available(context):
    return (hasattr(context.scene, "wiggle_enable")
            and hasattr(bpy.types.PoseBone, "wiggle_tail")
            and hasattr(bpy.types.PoseBone, "wiggle_collider_collection"))


def _wiggle_runtime():
    import sys
    return next((m for name, m in sys.modules.items()
                 if name.endswith('.wiggle_2') and hasattr(m, 'build_list')
                 and hasattr(m, 'reset_bones_batch')), None)


def _seed_wiggle_state(runtime, bones):
    """Initialize from the evaluated animation without writing a second pose."""
    runtime.clear_parent_cache()
    for bone in sorted(bones, key=lambda b: len(b.parent_recursive)):
        world = bone.id_data.matrix_world
        state = bone.wiggle
        state.matrix = runtime.flatten(world @ bone.matrix)
        state.position = state.position_last = world @ bone.tail
        state.position_head = state.position_last_head = world @ bone.head
        state.velocity = state.velocity_head = (0, 0, 0)
        state.collision_normal = state.collision_normal_head = (0, 0, 0)
        state.collision_ob = state.collision_ob_head = None


def _reset_wiggle_pose(context, runtime, bones):
    scene = context.scene
    previous = scene.wiggle.reset
    scene.wiggle.reset = True
    try:
        # Frame evaluation restores animated channels after clearing feedback
        # from the previous simulation, including channels without base keys.
        for bone in bones:
            bone.matrix_basis = Matrix.Identity(4)
        scene.frame_set(scene.frame_current, subframe=scene.frame_subframe)
        context.view_layer.update()
        _seed_wiggle_state(runtime, bones)
        scene.wiggle.lastframe = scene.frame_current
        invalidate = getattr(runtime, 'invalidate_auto_cache', None)
        if invalidate:
            invalidate(scene)
    finally:
        scene.wiggle.reset = previous


_MMD_TRACK_STATE = '_bonechain_mmd_track_previous_mute'


def _disable_mmd_physics(bones):
    """只接管本组骨骼；保留 MMD 的目标引用、影响、缓存与组外约束。"""
    count = 0
    for bone in bones:
        con = bone.constraints.get('mmd_tools_rigid_track')
        if con is None or con.type not in {'COPY_TRANSFORMS', 'COPY_ROTATION'}:
            continue
        if _MMD_TRACK_STATE not in bone:
            bone[_MMD_TRACK_STATE] = bool(con.mute)
        if not con.mute:
            con.mute = True
            count += 1
    return count


def _restore_mmd_physics(bones):
    count = 0
    for bone in bones:
        if _MMD_TRACK_STATE not in bone:
            continue
        con = bone.constraints.get('mmd_tools_rigid_track')
        if con is not None and con.type in {'COPY_TRANSFORMS', 'COPY_ROTATION'}:
            previous = bool(bone[_MMD_TRACK_STATE])
            if con.mute != previous:
                con.mute = previous
                count += 1
        del bone[_MMD_TRACK_STATE]
    return count


class BONECHAIN_OT_restore_mmd(Operator):
    bl_idname = 'bonechain.restore_mmd'
    bl_label = '恢复本组 MMD 跟随'
    bl_description = '停用本组 Ruri/Wiggle/旧碰撞，并恢复应用前的 MMD 跟随状态；保留缓存'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        arm = context.active_object
        grp = _get_active_group(context)
        if not arm or arm.type != 'ARMATURE' or not grp:
            self.report({'ERROR'}, '请选择骨架和分组')
            return {'CANCELLED'}
        if _BONECHAIN_BAKE_OWNER is not None:
            self.report({'WARNING'}, '正在烘焙，请等待完成或按 Esc 中止')
            return {'CANCELLED'}
        if grp.collision_mode == 'RURI':
            arm = _ruri_armature_for_group(context, grp)
            if arm is None:
                self.report({'ERROR'}, '找不到本组绑定的正确骨架')
                return {'CANCELLED'}
        # A bake action belongs to the armature and can contain other groups.
        # Cancelling this group's live simulation must not restore that action
        # or require cancelling an unrelated group's bake first.
        bones = [arm.pose.bones[item.name] for item in grp.flex_bones
                 if item.name in arm.pose.bones]
        _pause_ruri_group(arm, grp)
        _stop_wiggle_bones(context.scene, bones)
        prefix = _group_collision_prefix(grp)
        for bone in bones:
            for con in bone.constraints:
                if con.type == 'LIMIT_DISTANCE' and con.name.startswith(prefix):
                    con.mute = True
        restored = _restore_mmd_physics(bones)
        self.report({'INFO'}, f'已停用分组「{grp.name}」的实时模拟，恢复 {restored} 条 MMD 跟随；烘焙动作保持原样')
        return {'FINISHED'}


class BONECHAIN_OT_cancel_physics(Operator):
    bl_idname = 'bonechain.cancel_physics'
    bl_label = '取消本组物理'
    bl_description = '仅停用当前分组的实时 Wiggle/Ruri/旧碰撞并恢复已记录的 MMD 跟随；不切换动作、不删除烘焙关键帧'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        return BONECHAIN_OT_restore_mmd.execute(self, context)


def _disable_wiggle_conflicts(bones):
    """删除约束会触发动画路径维护，大量旧约束时极慢；改为失活。"""
    count = 0
    for bone in bones:
        for con in bone.constraints:
            if con.type == 'DAMPED_TRACK' or (
                    con.type == 'LIMIT_DISTANCE' and con.name.startswith(('碰撞_', 'BC_'))):
                if not con.mute or con.target is not None:
                    con.mute = True
                    con.target = None
                    count += 1
    return count


def _pause_ruri_group(arm, grp):
    settings = getattr(arm, 'ruri_cloth_physics', None)
    if settings and grp.ruri_config_name:
        config = settings.configs.get(grp.ruri_config_name)
        if config:
            config.enabled = False


def _stop_wiggle_bones(scene, bones):
    if not hasattr(bpy.types.PoseBone, 'wiggle_tail'):
        return
    bones = [bone for bone in bones if bone.wiggle_tail or bone.wiggle_head]
    if not bones:
        return
    previous = scene.get('wiggle_updating', False)
    scene['wiggle_updating'] = True
    try:
        for bone in bones:
            bone.wiggle_tail = False
            bone.wiggle_head = False
    finally:
        scene['wiggle_updating'] = previous
    runtime = _wiggle_runtime()
    if runtime:
        runtime.build_list()


def _ensure_wiggle_base_keys(scene, arm, bones):
    """只给没有动画/驱动器的变换分量补基础关键帧。"""
    animated = set()
    def collect_action(action):
        if not action:
            return
        curves = list(getattr(action, 'fcurves', ()))
        for layer in getattr(action, 'layers', ()):
            for strip in layer.strips:
                for bag in getattr(strip, 'channelbags', ()):
                    curves.extend(bag.fcurves)
        animated.update((c.data_path, c.array_index) for c in curves)
    ad = arm.animation_data
    if ad:
        collect_action(ad.action)
        animated.update((c.data_path, c.array_index) for c in ad.drivers)
        def collect_strips(strips):
            for strip in strips:
                collect_action(getattr(strip, 'action', None))
                collect_strips(getattr(strip, 'strips', ()))
        for track in ad.nla_tracks:
            collect_strips(track.strips)
    added = 0
    for bone in bones:
        rotation = ('rotation_quaternion' if bone.rotation_mode == 'QUATERNION'
                    else 'rotation_axis_angle' if bone.rotation_mode == 'AXIS_ANGLE'
                    else 'rotation_euler')
        for prop in ('location', rotation, 'scale'):
            path = bone.path_from_id(prop)
            for index in range(len(getattr(bone, prop))):
                if (path, index) not in animated:
                    bone.keyframe_insert(prop, index=index, frame=scene.frame_start,
                                         group=bone.name)
                    added += 1
    return added


def _make_cylinder(context, arm, bone_name, radius):
    pb = arm.pose.bones.get(bone_name)
    if not pb:
        return None
    # 直接创建数据，在姿态模式下也能工作，且不改变活动对象/选择。
    name = f"碰撞柱_{bone_name}"
    mesh = _make_capsule_mesh(name, radius, pb.length)
    cyl = bpy.data.objects.new(name, mesh)
    context.collection.objects.link(cyl)
    cyl.display_type = 'WIRE'
    cyl.hide_render = True
    cyl.show_in_front = True # 强制透视显示

    cyl.parent = arm
    cyl.parent_type = 'BONE'
    cyl.parent_bone = bone_name
    cyl.location = (0, -pb.length / 2, 0)
    return cyl


def _get_group_collection(grp):
    name = grp.coll_collection_name or f"碰撞体_{grp.name}"
    coll = bpy.data.collections.get(name)
    if not coll:
        coll = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(coll)
    grp.coll_collection_name = coll.name
    return coll


class BONECHAIN_OT_gen_cylinders(Operator):
    bl_idname = "bonechain.gen_cylinders"
    bl_label = "生成碰撞圆柱"
    bl_options = {'REGISTER', 'UNDO'}
    bl_description = "为变形骨列表每根骨骼生成对齐圆柱,放进该组碰撞集合"

    def execute(self, context):
        grp = _get_active_group(context)
        arm = context.active_object
        if not arm or arm.type != 'ARMATURE':
            self.report({'ERROR'}, "请先选择骨架")
            return {'CANCELLED'}
        if not grp or not grp.deform_targets:
            self.report({'ERROR'}, "变形骨列表为空,先点'解析变形骨'")
            return {'CANCELLED'}

        coll = _get_group_collection(grp)
        made = 0
        for citem in grp.deform_targets:
            old = bpy.data.objects.get(citem.capsule_name)
            if old:
                _remove_collision_object(old)
            cyl = _make_cylinder(context, arm, citem.name, grp.cyl_radius)
            if cyl:
                citem.capsule_name = cyl.name
                for uc in list(cyl.users_collection):
                    uc.objects.unlink(cyl)
                coll.objects.link(cyl)
                made += 1

        context.view_layer.objects.active = arm
        self.report({'INFO'}, f"生成 {made} 个圆柱到集合「{coll.name}」")
        return {'FINISHED'}


def _wiggle_collision_issues(arm, grp):
    """Report the collider bone and simulated ancestor responsible for feedback."""
    collection = bpy.data.collections.get(grp.coll_collection_name)
    if collection is None:
        return []
    flexible = {item.name for item in grp.flex_bones}
    issues = []
    for obj in collection.all_objects:
        if obj.parent != arm or obj.parent_type != 'BONE':
            continue
        ancestor = arm.pose.bones.get(obj.parent_bone)
        while ancestor is not None:
            if ancestor.name in flexible:
                issues.append({'object': obj.name, 'bone': obj.parent_bone,
                               'ancestor': ancestor.name})
                break
            ancestor = ancestor.parent
    return issues


class BONECHAIN_OT_apply_wiggle(Operator):
    bl_idname = "bonechain.apply_wiggle"
    bl_label = "应用Wiggle防穿"
    bl_options = {'REGISTER', 'UNDO'}
    bl_description = "把柔性骨骼配成 Wiggle 摆动骨，启用骨尾与本组圆柱集合碰撞"

    def execute(self, context):
        grp = _get_active_group(context)
        arm = context.active_object
        scene = context.scene
        if not arm or arm.type != 'ARMATURE':
            self.report({'ERROR'}, "请先选择骨架")
            return {'CANCELLED'}
        if not _wiggle_available(context):
            self.report({'ERROR'}, "未检测到 Wiggle 2,请先安装并启用")
            return {'CANCELLED'}
        if not grp or not grp.flex_bones:
            self.report({'ERROR'}, "柔性组为空")
            return {'CANCELLED'}

        runtime = _wiggle_runtime()
        if runtime is None:
            self.report({'ERROR'}, "Wiggle 接口不兼容，请启用 Wiggle 2 RTX 2.2.9")
            return {'CANCELLED'}
        bones = [arm.pose.bones.get(item.name) for item in grp.flex_bones]
        if any(b is None for b in bones):
            self.report({'ERROR'}, "柔性组包含不存在的骨骼，请更新列表")
            return {'CANCELLED'}
        # 用户列表顺序不保证父骨在前（补齐中间骨时尤其如此）。
        # Wiggle 初始化子骨前必须先写好其父骨的模拟矩阵。
        bones.sort(key=lambda b: len(b.parent_recursive))

        coll = bpy.data.collections.get(grp.coll_collection_name)
        if not coll or not list(coll.objects):
            self.report({'ERROR'}, "没有碰撞圆柱,请先点'生成碰撞圆柱'")
            return {'CANCELLED'}

        issues = _wiggle_collision_issues(arm, grp)
        if issues:
            for issue in issues:
                print(f"[BoneChain] 自碰撞冲突：圆柱 {issue['object']} → "
                      f"碰撞骨 {issue['bone']} → 柔性祖先 {issue['ancestor']}", flush=True)
            first = issues[0]
            if first['bone'] == first['ancestor']:
                detail = f"柔性骨“{first['bone']}”同时用作碰撞骨"
            else:
                detail = f"碰撞骨“{first['bone']}”由柔性骨“{first['ancestor']}”驱动"
            self.report({'ERROR'}, detail + "；移出碰撞组并重新解析/生成圆柱")
            return {'CANCELLED'}

        _pause_ruri_group(arm, grp)
        mmd_disabled = _disable_mmd_physics(bones)
        print(f"[BoneChain] 开始应用 Wiggle: {len(bones)} 根骨骼", flush=True)
        cleaned = _disable_wiggle_conflicts(bones) if grp.wig_clean else 0
        print(f"[BoneChain] 已停用 {cleaned} 条冲突约束，准备基础关键帧", flush=True)
        base_keys = _ensure_wiggle_base_keys(scene, arm, bones) if grp.wig_base_keys else 0
        print(f"[BoneChain] 已补齐 {base_keys} 个通道，准备物理参数", flush=True)

        scene.wiggle_enable = True
        arm.wiggle_enable = True
        arm.wiggle_mute = False
        arm.wiggle_freeze = False
        if not scene.wiggle.is_property_set('iterations'):
            chains = get_all_chains([b.name for b in bones], arm)
            longest = max((len(chain) for chain in chains), default=1)
            scene.wiggle.iterations = max(4, min(12, (longest + 3) // 4))

        # RTX 的参数回调会传播到所有选中骨骼，并反复重建模拟列表。
        # 按插件自己的批量更新锁抑制回调，最后只重建/重置一次。
        previous_updating = scene.get('wiggle_updating', False)
        scene['wiggle_updating'] = True
        try:
            for pb in bones:
                pb.wiggle_head = False
                pb.wiggle_tail = True
                pb.wiggle_mute = False
                pb.wiggle_tail_mute = False
                pb.wiggle_collider_type = 'Collection'
                pb.wiggle_collider_collection = coll
                pb.wiggle_radius = grp.wig_radius
                pb.wiggle_chain = True
                pb.wiggle_mass = grp.wig_mass
                pb.wiggle_stiff = grp.wig_stiff
                pb.wiggle_damp = grp.wig_damp
                pb.wiggle_stretch = grp.wig_stretch
                pb.wiggle_bounce = 0.0
        finally:
            scene['wiggle_updating'] = previous_updating
        runtime.build_list()
        print("[BoneChain] 参数就绪，开始批量初始化", flush=True)
        _reset_wiggle_pose(context, runtime, bones)
        print("[BoneChain] Wiggle 应用完成", flush=True)
        ok = len(bones)

        self.report({'INFO'},
                    f"{ok} 根柔性骨骼已配 Wiggle,碰撞 {len(list(coll.objects))} 个圆柱"
                    + (f",停用 {mmd_disabled} 条 MMD 跟随" if mmd_disabled else "")
                    + (f",停用 {cleaned} 个冲突约束" if cleaned else "")
                    + (f",补齐 {base_keys} 个基础姿态通道" if base_keys else "")
                    + "。按'重置'再播放")
        return {'FINISHED'}


def _missing_chain_bones(arm, selected_names):
    """只补已选祖先与已选后代之间的路径，不扩展到其他支链或上级。"""
    selected = set(selected_names)
    missing = {}
    for name in selected:
        bone = arm.pose.bones.get(name)
        if bone is None:
            continue
        path = []
        parent = bone.parent
        while parent is not None and parent.name not in selected:
            path.append(parent)
            parent = parent.parent
        if parent is not None:
            for middle in reversed(path):
                missing[middle.name] = middle
    return list(missing.values())


class BONECHAIN_OT_fill_chain_gaps(Operator):
    bl_idname = "bonechain.fill_chain_gaps"
    bl_label = "补齐柔性链中间骨骼"
    bl_description = "将已选祖先和后代之间缺失的骨骼加入柔性组，不添加旁支；可撤销"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        arm = context.active_object
        grp = _get_active_group(context)
        if not arm or arm.type != 'ARMATURE' or not grp:
            return {'CANCELLED'}
        missing = _missing_chain_bones(arm, [b.name for b in grp.flex_bones])
        for bone in missing:
            grp.flex_bones.add().name = bone.name
        if missing:
            print('[BoneChain] 补齐柔性链: ' + ', '.join(b.name for b in missing), flush=True)
        self.report({'INFO'}, f"已补齐 {len(missing)} 根中间骨骼，请检查柔性组后再配置 Ruri")
        return {'FINISHED'}


class BONECHAIN_OT_apply_ruri(Operator):
    bl_idname = "bonechain.apply_ruri"
    bl_label = "配置 Ruri 骨骼布料"
    bl_options = {'REGISTER', 'UNDO'}
    bl_description = "按本组根骨选项配置 Ruri；可使用现有父骨固定支撑胸骨，自动排除未列入的支链"

    def execute(self, context):
        import importlib
        grp = _get_active_group(context)
        if not hasattr(context.scene, 'ruri_cloth_physics'):
            self.report({'ERROR'}, "Ruri 前置未启用，请先点击“启用 Ruri 前置”")
            return {'CANCELLED'}
        arm = _ruri_armature_for_group(context, grp) if grp else None
        if not arm or arm.type != 'ARMATURE' or not grp:
            self.report({'ERROR'}, "请选择骨架和分组")
            return {'CANCELLED'}
        if _BONECHAIN_BAKE_OWNER is not None:
            self.report({'WARNING'}, "正在烘焙，请先按 Esc 中止")
            return {'CANCELLED'}
        if _ruri_bake_active(arm):
            self.report({'WARNING'}, "请先取消 Ruri 烘焙，恢复原动作后再配置；修改参数后可直接重新烘焙")
            return {'CANCELLED'}
        if grp.collision_mode != 'RURI':
            self.report({'ERROR'}, "当前分组不是 Ruri 模式，请先点击“Ruri (GPU骨骼布料)”")
            return {'CANCELLED'}
        if not hasattr(arm, 'ruri_cloth_physics'):
            self.report({'ERROR'}, "请先启用 Ruri Cloth Physics")
            return {'CANCELLED'}
        flex_names = [item.name for item in grp.flex_bones if item.name]
        if not flex_names:
            self.report({'ERROR'}, "柔性骨骼列表为空，请在 Pose 模式选择骨骼后点“添加选中”")
            return {'CANCELLED'}
        invalid = [name for name in flex_names if arm.pose.bones.get(name) is None]
        if invalid:
            preview = ', '.join(invalid[:3])
            print(f'[BoneChain] 活动骨架「{arm.name}」缺少柔性骨骼: ' + ', '.join(invalid), flush=True)
            self.report({'ERROR'}, f"当前骨架「{arm.name}」缺少 {len(invalid)} 根柔性骨骼（{preview}），请选正确骨架或重新添加列表")
            return {'CANCELLED'}
        props = context.scene.bonechain_props
        overlaps = {}
        names_set = set(flex_names)
        for other in props.groups:
            if other == grp or other.collision_mode != 'RURI':
                continue
            shared = names_set.intersection(item.name for item in other.flex_bones)
            for name in shared:
                overlaps.setdefault(name, []).append(other.name)
        if overlaps:
            preview = ', '.join(
                f"{name}→{'/'.join(groups)}" for name, groups in list(overlaps.items())[:3])
            print('[BoneChain] Ruri 跨组重复骨骼: ' + preview, flush=True)
            self.report({'ERROR'}, f"有 {len(overlaps)} 根骨骼同时属于其他 Ruri 分组（{preview}），请先从其中一组移除")
            return {'CANCELLED'}
        bones = [arm.pose.bones[name] for name in flex_names]
        names = {b.name for b in bones}
        is_spring = grp.ruri_physics_type == 'BONE_SPRING'
        missing = [] if is_spring else _missing_chain_bones(arm, names)
        if missing:
            preview = ', '.join(b.name for b in missing[:3])
            print('[BoneChain] 缺少中间骨骼: ' + ', '.join(b.name for b in missing), flush=True)
            self.report({'ERROR'}, f"缺少 {len(missing)} 根中间骨骼（{preview}），请点“补齐柔性链中间骨骼”")
            return {'CANCELLED'}
        roots = [b for b in bones if b.parent is None or b.parent.name not in names]
        if is_spring:
            # Native spring mode makes FIXED particles oscillate around their
            # animated positions. Each selected bone is its own spring anchor.
            fixed_roots = bones
            included = names
            movable_count = len(bones)
        elif grp.ruri_simulate_roots:
            unattached = [b.name for b in roots if b.parent is None]
            if unattached:
                self.report({'ERROR'}, '这些骨骼没有现有父骨可作固定支撑：' + ', '.join(unattached[:3]))
                return {'CANCELLED'}
            anchors = {b.parent.name: b.parent for b in roots}
            fixed_roots = list(anchors.values())
            included = names | set(anchors)
            movable_count = len(bones)
        else:
            fixed_roots = roots
            included = names
            movable_count = len(bones) - len(roots)
        if not movable_count:
            self.report({'ERROR'}, "所选骨骼没有可动子骨；胸部请选择“骨骼弹簧（胸部）”类型")
            return {'CANCELLED'}
        settings = arm.ruri_cloth_physics
        anchor_names = {bone.name for bone in fixed_roots} if grp.ruri_simulate_roots and not is_spring else set()
        for other in props.groups:
            if other == grp or other.collision_mode != 'RURI':
                continue
            other_names = {item.name for item in other.flex_bones}
            # A support may be shared with fixed animation, but not with a
            # bone that another group is actively configured to simulate.
            other_movable = {name for name in other_names if name in arm.pose.bones
                             and (other.ruri_physics_type == 'BONE_SPRING' or other.ruri_simulate_roots
                                  or (arm.pose.bones[name].parent is not None
                                      and arm.pose.bones[name].parent.name in other_names))}
            conflict = anchor_names & other_movable
            if conflict:
                self.report({'ERROR'}, '固定支撑正在另一分组中参与模拟：' + ', '.join(sorted(conflict)[:3]))
                return {'CANCELLED'}
        for other in settings.configs:
            if other.name == grp.ruri_config_name:
                continue
            shared_roots = {item.bone for item in other.root_bones}.intersection(included)
            if shared_roots:
                self.report({'ERROR'}, '固定支撑或所选骨骼已是另一 Ruri 配置的根骨：' + ', '.join(sorted(shared_roots)[:3]) + '；共享支撑的左右胸骨请放在同一组')
                return {'CANCELLED'}
        items = list(grp.coll_bones)
        if not items:
            self.report({'ERROR'}, "请先添加头部或身体等固定碰撞骨骼")
            return {'CANCELLED'}
        issues = _ruri_collision_issues(arm, grp)
        if issues:
            details = []
            for reason, label in (('missing', '不存在'), ('flexible', '位于柔性链')):
                bad = [issue['name'] for issue in issues if issue['reason'] == reason]
                if bad:
                    details.append(f"{len(bad)} 项{label}（{', '.join(bad[:3])}）")
            print(f'[BoneChain] 骨架「{arm.name}」无效碰撞项: ' +
                  ', '.join(f"{issue['name']}:{issue['reason']}" for issue in issues), flush=True)
            self.report({'ERROR'}, '碰撞组有无效项：' + '；'.join(details) + '；请点“清理无效碰撞骨”')
            return {'CANCELLED'}
        runtime = importlib.import_module('RuriClothPhysics.blender_host.runtime')
        operators = importlib.import_module('RuriClothPhysics.blender_host.operators')
        world = importlib.import_module('RuriClothPhysics.blender_host.world')
        settings = arm.ruri_cloth_physics
        body = world.enroll_body(context.scene, arm)
        body.live = False
        grp.ruri_armature = arm
        mmd_disabled = _disable_mmd_physics(bones)
        _group_collision_prefix(grp)
        config = settings.configs.get(grp.ruri_config_name) if grp.ruri_config_name else None
        if config is None:
            config = settings.configs.add()
            config.name = f"BC_{grp.collision_id}"
            grp.ruri_config_name = config.name
        entering_spring = is_spring and config.cloth_type != 'BONE_SPRING'
        config.root_bones.clear()
        config.attribute_overrides.clear()
        for bone in fixed_roots:
            config.root_bones.add().bone = bone.name
        # Native Ruri includes all descendants of a declared root. Restrict
        # traversal at every boundary, including siblings under added anchors.
        for bone_name in sorted(included):
            bone = arm.pose.bones[bone_name]
            for child in bone.children:
                if child.name not in included:
                    override = config.attribute_overrides.add()
                    override.bone = child.name
                    override.attribute = 'IGNORE'
        config.cloth_type = grp.ruri_physics_type
        if entering_spring:
            config.spring.use_spring = True
            config.spring.limit_distance = 0.01
            config.spring.spring_power = 0.06
            config.blend_weight = 0.35
            config.inertia.world_inertia = 0.25
            config.inertia.local_inertia = 0.25
            config.root_rotation = 0.0
        config.collider_collision.mode = 'POINT' if is_spring else 'EDGE'
        config.collider_collision.collision_bones.clear()
        if is_spring:
            for bone in bones:
                config.collider_collision.collision_bones.add().bone = bone.name
        config.radius.value = grp.wig_radius
        config.enabled = True
        config.collider_collision.collider_references.clear()
        collection = operators._collider_collection(context, arm)
        context.view_layer.update()
        for item in items:
            start = _ensure_ruri_item_geometry(context, grp, item, arm, collection)
            operators._reference_collider(config, start)
        # 同一组只让一套引擎写入姿态。
        _stop_wiggle_bones(context.scene, bones)
        disabled = _disable_wiggle_conflicts(bones)
        settings.active_config_index = list(settings.configs.keys()).index(config.name)
        context.scene.ruri_cloth_physics.max_simulation_count = 5
        config.rebuild_pending = True
        body.live = True
        # Live frame handlers write pose data shared with the viewport.
        # Blender requires a locked interface during threaded rendering.
        context.scene.render.use_lock_interface = True
        kind = f'{len(bones)} 根原位弹簧骨' if is_spring else f'{len(fixed_roots)} 根固定支撑、{movable_count} 根可动骨'
        self.report({'INFO'}, f"Ruri 已配置分组「{grp.name}」：{kind}，停用 {mmd_disabled} 条 MMD 跟随、{disabled} 条旧约束；首次播放需要编译")
        return {'FINISHED'}


class BONECHAIN_OT_enable_ruri(Operator):
    bl_idname = "bonechain.enable_ruri"
    bl_label = "启用 Ruri 前置"
    bl_description = "启用已经安装的 Ruri Cloth Physics，并保存到用户偏好设置"

    def execute(self, context):
        try:
            bpy.ops.preferences.addon_enable(module='RuriClothPhysics')
            if not hasattr(context.scene, 'ruri_cloth_physics'):
                raise RuntimeError("Ruri 未完成注册")
            bpy.ops.wm.save_userpref()
        except Exception as error:
            self.report({'ERROR'}, f"Ruri 前置启用失败：{error}")
            return {'CANCELLED'}
        self.report({'INFO'}, "Ruri Cloth Physics 已启用")
        return {'FINISHED'}


class BONECHAIN_OT_wiggle_reset(Operator):
    bl_idname = "bonechain.wiggle_reset"
    bl_label = "重置Wiggle物理"
    bl_description = "调用 Wiggle 的重置(回到静止,消除抖动)"

    def execute(self, context):
        runtime = _wiggle_runtime()
        if runtime is None or not _wiggle_available(context):
            self.report({'ERROR'}, "请先启用 Wiggle 2 RTX")
            return {'CANCELLED'}
        try:
            bones = [bone for obj in context.scene.objects if obj.type == 'ARMATURE'
                     and not obj.wiggle_freeze for bone in obj.pose.bones
                     if bone.wiggle_head or bone.wiggle_tail]
            _reset_wiggle_pose(context, runtime, bones)
        except Exception as e:
            self.report({'WARNING'}, f"重置失败: {e}")
            return {'CANCELLED'}
        self.report({'INFO'}, "已重置 Wiggle 物理")
        return {'FINISHED'}


def _wiggle_bake_curves(action, arm, slot_handle):
    """Edit only the armature's action slot (Blender 4.4+/5.x)."""
    legacy = getattr(action, 'fcurves', None)
    if legacy is not None and not getattr(action, 'slots', ()):
        return legacy
    slots = list(action.slots)
    slot = next((s for s in slots if s.handle == slot_handle), None)
    if slot is None:
        if slots:
            raise RuntimeError("无法确定骨架对应的动作槽")
        slot = action.slots.new('OBJECT', arm.name)
    from bpy_extras.anim_utils import action_ensure_channelbag_for_slot
    return action_ensure_channelbag_for_slot(action, slot).fcurves


def _write_wiggle_bake(action, arm, slot_handle, channels, samples, start, end,
                       guards):
    """Replace sampled channels in range; preserve other bones and outside keys."""
    curves = _wiggle_bake_curves(action, arm, slot_handle)
    lookup = {(fc.data_path, fc.array_index): fc for fc in curves}
    for channel, values in zip(channels, samples):
        path, index, group = channel
        fc = lookup.get((path, index))
        if fc is None:
            fc = curves.new(path, index=index, group_name=group)
        outside = {}
        for key in fc.keyframe_points:
            if key.co.x < start or key.co.x > end:
                outside[float(key.co.x)] = (
                    tuple(key.co), key.interpolation, key.type,
                    key.handle_left_type, key.handle_right_type,
                    tuple(key.handle_left), tuple(key.handle_right))
        # Guard keys stop the first/last simulated pose extrapolating into
        # the unbaked part of a constant, single-key base channel.
        edge_values = {
            start - 1: fc.evaluate(start - 1) if len(fc.keyframe_points) else guards[0][(path, index)],
            end + 1: fc.evaluate(end + 1) if len(fc.keyframe_points) else guards[1][(path, index)],
        }
        for key_index in range(len(fc.keyframe_points) - 1, -1, -1):
            key = fc.keyframe_points[key_index]
            if start <= key.co.x <= end:
                fc.keyframe_points.remove(key, fast=True)
        pairs = [(start + offset, value) for offset, value in enumerate(values)]
        pairs.extend((frame, value) for frame, value in edge_values.items() if frame not in outside)
        old_coords = [v for key in fc.keyframe_points for v in key.co]
        fc.keyframe_points.add(len(pairs))
        fc.keyframe_points.foreach_set('co', old_coords + [v for pair in pairs for v in pair])
        # Existing keys keep their interpolation and handles. Baked keys are
        # linear so interpolation cannot overshoot the sampled simulation.
        for key in list(fc.keyframe_points)[len(outside):]:
            key.interpolation = 'LINEAR'
        fc.update()
        for key in fc.keyframe_points:
            saved = outside.get(float(key.co.x))
            if saved is not None:
                _, key.interpolation, key.type, key.handle_left_type, key.handle_right_type, left, right = saved
                key.handle_left, key.handle_right = left, right


def _wiggle_bake_range(context, arm, start, end):
    """Yield progress while reading the source, then write a separate action."""
    import importlib
    import math
    from array import array

    scene, view_layer = context.scene, context.view_layer
    runtime = _wiggle_runtime()
    layers = importlib.import_module(runtime.__package__ + '.wiggle_layers')
    ad = arm.animation_data_create()
    old_action, old_slot = ad.action, ad.action_slot
    old_handle = old_slot.handle if old_slot else 0
    old_influence, old_blend = ad.action_influence, ad.action_blend_type
    repeat = (arm.bonechain_wiggle_bake_saved and old_action == arm.bonechain_wiggle_bake_output)
    source = arm.bonechain_wiggle_bake_restore if repeat else old_action
    source_handle = arm.bonechain_wiggle_bake_slot if repeat else old_handle
    source_influence = arm.bonechain_wiggle_bake_influence if repeat else old_influence
    source_blend = arm.bonechain_wiggle_bake_blend if repeat else old_blend
    if source_blend != 'REPLACE' or abs(source_influence - 1.0) > 1e-6:
        raise RuntimeError("请将当前动作的混合方式设为替换、影响设为 1 后烘焙")
    bones = sorted((b for b in arm.pose.bones if (b.wiggle_head or b.wiggle_tail)
                    and not b.wiggle_mute), key=lambda b: len(b.parent_recursive))
    frame, subframe = scene.frame_current, scene.frame_subframe
    flags = (scene.wiggle_enable, arm.wiggle_mute, arm.wiggle_freeze,
             scene.wiggle.reset, scene.wiggle.is_preroll, scene.wiggle.is_rendering,
             scene.tool_settings.use_keyframe_insert_auto,
             getattr(runtime, '_BAKE_NO_CACHE', False), layers._baking_in_progress)
    other_freeze = [(obj, obj.wiggle_freeze) for obj in scene.objects
                    if obj.type == 'ARMATURE' and obj != arm]
    output, success = None, False
    specs, channels, samples = [], [], []
    for bone in bones:
        rot = ('rotation_quaternion' if bone.rotation_mode == 'QUATERNION' else
               'rotation_axis_angle' if bone.rotation_mode == 'AXIS_ANGLE' else 'rotation_euler')
        for prop in ('location', rot, 'scale'):
            path = bone.path_from_id(prop)
            indices = []
            for index in range(len(getattr(bone, prop))):
                indices.append(len(samples))
                channels.append((path, index, bone.name))
                samples.append(array('f'))
            specs.append((bone, prop, indices))
    try:
        layers._baking_in_progress = True
        runtime._BAKE_NO_CACHE = True
        scene.tool_settings.use_keyframe_insert_auto = False
        scene.wiggle.reset = True
        ad.action = source
        if source is not None and source_handle:
            ad.action_slot = next(s for s in source.slots if s.handle == source_handle)
        ad.action_influence, ad.action_blend_type = source_influence, source_blend
        scene['wiggle_updating'] = True
        try:
            scene.wiggle_enable = True
            arm.wiggle_mute, arm.wiggle_freeze = False, False
        finally:
            scene['wiggle_updating'] = False
        for obj, _ in other_freeze:
            obj.wiggle_freeze = True
        scene.wiggle.is_rendering = False
        # Capture un-simulated boundary values before any output action exists.
        guards = []
        for edge in (start - 1, end + 1):
            for bone in bones:
                bone.matrix_basis = Matrix.Identity(4)
            scene.frame_set(edge)
            view_layer.update()
            guards.append({(bone.path_from_id(prop), i): value
                           for bone, prop, _ in specs
                           for i, value in enumerate(getattr(bone, prop))})
        sim_start = min(scene.frame_start, start) - max(0, scene.wiggle.preroll)
        for bone in bones:
            bone.matrix_basis = Matrix.Identity(4)
        scene.frame_set(sim_start)
        view_layer.update()
        _seed_wiggle_state(runtime, bones)
        scene.wiggle.lastframe = sim_start
        scene.wiggle.reset = False
        scene.wiggle.is_preroll = True
        runtime.build_list()
        previous_rotations = {}
        total = end - sim_start + 1
        for f in range(sim_start, end + 1):
            if f != sim_start:
                scene.frame_set(f)
                view_layer.update()
            if f >= start:
                for bone, prop, indices in specs:
                    values = (tuple(bone.rotation_axis_angle) if prop == 'rotation_axis_angle'
                              else getattr(bone, prop).copy())
                    if prop == 'rotation_quaternion':
                        previous = previous_rotations.get(bone.name)
                        if previous is not None:
                            values.make_compatible(previous)
                        previous_rotations[bone.name] = values.copy()
                    elif prop == 'rotation_euler':
                        previous = previous_rotations.get(bone.name)
                        if previous is not None:
                            values = values.to_quaternion().to_euler(bone.rotation_mode, previous)
                        previous_rotations[bone.name] = values.copy()
                    for index, value in zip(indices, values):
                        if not math.isfinite(value):
                            raise RuntimeError(f"第 {f} 帧骨骼 {bone.name} 的模拟数值无效")
                        samples[index].append(value)
            if (f - sim_start) % 50 == 0 or f == end:
                print(f'[BoneChain] 烘焙采样 {f}/{end}', flush=True)
            yield (f - sim_start + 1) / total
        scene.wiggle.reset = True
        output = source.copy() if source else bpy.data.actions.new('Wiggle')
        output.name = (source.name if source else arm.name) + f'_Wiggle_{start}_{end}'
        output.use_fake_user = True
        _write_wiggle_bake(output, arm, source_handle, channels, samples, start, end, guards)
        ad.action = output
        if source_handle:
            ad.action_slot = next(s for s in output.slots if s.handle == source_handle)
        arm.bonechain_wiggle_bake_restore = source
        arm.bonechain_wiggle_bake_slot = source_handle
        arm.bonechain_wiggle_bake_influence = source_influence
        arm.bonechain_wiggle_bake_blend = source_blend
        arm.bonechain_wiggle_bake_output = output
        arm.bonechain_wiggle_bake_saved = True
        arm.wiggle_freeze = True
        success = True
    finally:
        # Closing this generator (Esc) also restores the input and all flags.
        scene.wiggle.reset = True
        if not success:
            ad.action = old_action
            if old_slot is not None:
                ad.action_slot = old_slot
            ad.action_influence, ad.action_blend_type = old_influence, old_blend
            arm.wiggle_mute, arm.wiggle_freeze = flags[1:3]
            if output is not None:
                output.use_fake_user = False
                if output.users == 0:
                    bpy.data.actions.remove(output)
        for obj, frozen in other_freeze:
            obj.wiggle_freeze = frozen
        scene.frame_set(frame, subframe=subframe)
        view_layer.update()
        scene.wiggle.lastframe = frame
        if not success:
            _seed_wiggle_state(runtime, bones)
        scene['wiggle_updating'] = True
        try:
            scene.wiggle_enable = flags[0]
        finally:
            scene['wiggle_updating'] = False
        scene.wiggle.reset, scene.wiggle.is_preroll, scene.wiggle.is_rendering = flags[3:6]
        scene.tool_settings.use_keyframe_insert_auto = flags[6]
        runtime._BAKE_NO_CACHE, layers._baking_in_progress = flags[7:9]
    return output.name, len(bones)


_BONECHAIN_BAKE_OWNER = None
_BONECHAIN_PROGRESS_CALLBACK = None


class _BoneChainBakeModal:
    def _start_bake(self, context, job, start, end, group, prefix):
        global _BONECHAIN_BAKE_OWNER
        self._job = job
        self._range = start, end
        self._bake_group = group
        self._bake_prefix = prefix
        self._timer = None
        self._finished = False
        self._ui_progress = 0.0
        self._ui_progress_text = '准备烘焙'
        self._wm = context.window_manager
        self._workspace = context.workspace
        if context.screen and context.screen.is_animation_playing:
            bpy.ops.screen.animation_cancel(restore_frame=False)
        self._wm.progress_begin(0, 100)
        _BONECHAIN_BAKE_OWNER = self
        if not bpy.app.background and context.window is not None:
            try:
                self._timer = self._wm.event_timer_add(0.02, window=context.window)
                self._wm.modal_handler_add(self)
            except Exception as error:
                return self._finish(context, error=error)
            return {'RUNNING_MODAL'}
        try:
            while True:
                self._show_progress(next(self._job))
        except StopIteration as done:
            return self._finish(context, result=done.value)
        except Exception as error:
            return self._finish(context, error=error)

    def _show_progress(self, progress):
        self._ui_progress = max(0.0, min(0.95, progress * 0.95))
        self._ui_progress_text = f'{getattr(self, "_progress_stage", self._bake_prefix.upper())} · 第 {bpy.context.scene.frame_current}/{self._range[1]} 帧'
        if _BONECHAIN_PROGRESS_CALLBACK is not None:
            _BONECHAIN_PROGRESS_CALLBACK(progress, bpy.context.scene.frame_current, self._range)
        self._wm.progress_update(self._ui_progress * 100)
        if self._workspace is not None:
            self._workspace.status_text_set(
                f'{self._ui_progress_text} · {self._ui_progress:.1%} · Esc 中止')

    def modal(self, context, event):
        if event.type == 'ESC':
            return self._finish(context, cancelled=True)
        if event.type == 'TIMER':
            try:
                self._show_progress(next(self._job))
            except StopIteration as done:
                return self._finish(context, result=done.value)
            except Exception as error:
                return self._finish(context, error=error)
            if context.area:
                context.area.tag_redraw()
        return {'RUNNING_MODAL'}

    def cancel(self, context):
        self._finish(context, cancelled=True)

    def _finish(self, context, result=None, error=None, cancelled=False):
        global _BONECHAIN_BAKE_OWNER
        if self._finished:
            return {'CANCELLED'}
        self._finished = True
        try:
            self._job.close()
        except Exception as cleanup_error:
            error = cleanup_error if error is None else error
        finally:
            _BONECHAIN_BAKE_OWNER = None
            if self._timer is not None:
                self._wm.event_timer_remove(self._timer)
                self._timer = None
            self._wm.progress_end()
            if self._workspace is not None:
                self._workspace.status_text_set(None)
        if error is not None:
            self.report({'ERROR'}, f"烘焙失败：{error}")
            return {'CANCELLED'}
        if cancelled:
            self.report({'INFO'}, "已中止烘焙并恢复原动作")
            return {'CANCELLED'}
        group = self._bake_group
        if group:
            setattr(group, self._bake_prefix + '_bake_start', self._range[0])
            setattr(group, self._bake_prefix + '_bake_end', self._range[1])
        name, count = result
        self.report({'INFO'}, f"已烘焙 {count} 根骨骼，区间 {self._range[0]}–{self._range[1]}，动作「{name}」")
        return {'FINISHED'}


class BONECHAIN_OT_wiggle_bake(_BoneChainBakeModal, Operator):
    bl_idname = "bonechain.wiggle_bake"
    bl_label = "烘焙Wiggle到关键帧"
    bl_description = "选择起止帧，仅烘焙这段 Wiggle 物理，保留区间外关键帧与工程时间轴范围"
    bl_options = {'REGISTER', 'UNDO'}

    frame_start: IntProperty(name="起始帧", default=1, min=-1048574, max=1048574)
    frame_end: IntProperty(name="结束帧", default=250, min=-1048574, max=1048574)

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == 'ARMATURE'

    def invoke(self, context, event):
        grp = _get_active_group(context)
        if not self.properties.is_property_set('frame_start'):
            self.frame_start = (grp.wig_bake_start if grp and grp.is_property_set('wig_bake_start')
                                else context.scene.frame_start)
        if not self.properties.is_property_set('frame_end'):
            self.frame_end = (grp.wig_bake_end if grp and grp.is_property_set('wig_bake_end')
                              else context.scene.frame_end)
        return context.window_manager.invoke_props_dialog(self, width=320)

    def draw(self, context):
        row = self.layout.row(align=True)
        row.prop(self, 'frame_start')
        row.prop(self, 'frame_end')
        if self.frame_end < self.frame_start:
            self.layout.label(text="结束帧不能早于起始帧", icon='ERROR')
        else:
            self.layout.label(text=f"烘焙 {self.frame_end - self.frame_start + 1} 帧，包含首尾帧")
        self.layout.label(text="保留区间外关键帧及时间轴范围", icon='INFO')
        self.layout.label(text="生成独立动作；烘焙时按 Esc 可中止")

    def execute(self, context):
        global _BONECHAIN_BAKE_OWNER
        grp = _get_active_group(context)
        start = (self.frame_start if self.properties.is_property_set('frame_start') else
                 grp.wig_bake_start if grp and grp.is_property_set('wig_bake_start') else
                 context.scene.frame_start)
        end = (self.frame_end if self.properties.is_property_set('frame_end') else
               grp.wig_bake_end if grp and grp.is_property_set('wig_bake_end') else
               context.scene.frame_end)
        if end < start:
            self.report({'ERROR'}, "结束帧不能早于起始帧")
            return {'CANCELLED'}
        arm = context.active_object
        if not _wiggle_available(context):
            self.report({'ERROR'}, "未检测到 Wiggle 2")
            return {'CANCELLED'}
        if not any((b.wiggle_head or b.wiggle_tail) and not b.wiggle_mute
                   for b in arm.pose.bones):
            self.report({'WARNING'}, "没有启用 Wiggle 的骨骼可烘焙")
            return {'CANCELLED'}
        if _wiggle_runtime() is None:
            self.report({'ERROR'}, "Wiggle 2 RTX 接口不可用")
            return {'CANCELLED'}
        if arm.animation_data and arm.animation_data.use_tweak_mode:
            self.report({'ERROR'}, "请先退出 NLA 动作编辑模式")
            return {'CANCELLED'}
        if _BONECHAIN_BAKE_OWNER is not None:
            self.report({'WARNING'}, "正在烘焙，请等待完成或按 Esc 中止")
            return {'CANCELLED'}
        return self._start_bake(context, _wiggle_bake_range(context, arm, start, end),
                                start, end, grp, 'wig')


def _ruri_bake_armature(context):
    group = _get_active_group(context)
    if group is not None:
        return _ruri_armature_for_group(context, group)
    obj = context.active_object
    return obj if obj is not None and obj.type == 'ARMATURE' else None


def _ruri_bake_active(arm):
    return (arm is not None and arm.bonechain_ruri_bake_saved
            and arm.bonechain_ruri_bake_output is not None
            and arm.animation_data is not None
            and arm.animation_data.action == arm.bonechain_ruri_bake_output)


def _assign_bake_action(animation, action, handle, influence, blend):
    slots = list(getattr(action, 'slots', ())) if action else []
    slot = next((s for s in slots if s.handle == handle), None)
    if slots and slot is None:
        compatible = [s for s in slots if s.target_id_type == 'OBJECT']
        if len(compatible) != 1:
            raise RuntimeError("无法确定骨架对应的动作槽，请先在动作编辑器中选择")
        slot = compatible[0]
    animation.action = action
    if slot is not None:
        animation.action_slot = slot
    animation.action_influence, animation.action_blend_type = influence, blend


def _ruri_bake_range(context, arm, start, end, preroll):
    import importlib
    import json
    import math
    from array import array

    runtime = importlib.import_module('RuriClothPhysics.blender_host.runtime')
    native_bake = importlib.import_module('RuriClothPhysics.blender_host.bake')
    world = importlib.import_module('RuriClothPhysics.blender_host.world')
    scene, view_layer = context.scene, context.view_layer
    body = world.body_entry(scene, arm)
    if body is None:
        raise RuntimeError("请先配置 Ruri，将骨架加入当前场景")
    animation = arm.animation_data_create()
    old_action, old_slot = animation.action, animation.action_slot
    old_handle = old_slot.handle if old_slot else 0
    old_influence, old_blend = animation.action_influence, animation.action_blend_type
    repeat = _ruri_bake_active(arm)
    if repeat and arm.bonechain_ruri_bake_scene not in (None, scene):
        raise RuntimeError("请回到上次烘焙的场景再重新烘焙")
    source = arm.bonechain_ruri_bake_restore if repeat else old_action
    source_handle = arm.bonechain_ruri_bake_slot if repeat else old_handle
    source_influence = arm.bonechain_ruri_bake_influence if repeat else old_influence
    source_blend = arm.bonechain_ruri_bake_blend if repeat else old_blend
    if source_blend != 'REPLACE' or abs(source_influence - 1.0) > 1e-6:
        raise RuntimeError("请将原动作的混合方式设为替换、影响设为 1 后烘焙")
    frame, subframe = scene.frame_current, scene.frame_subframe
    original_live = body.live
    original_auto_key = scene.tool_settings.use_keyframe_insert_auto
    original_freeze = getattr(arm, 'wiggle_freeze', None)
    record_before = {name: getattr(arm, 'bonechain_ruri_bake_' + name)
                     for name in ('restore', 'output', 'saved', 'slot', 'influence',
                                  'blend', 'bones', 'scene')}
    culling = [(config, config.culling.camera_culling_mode,
                config.culling.distance_culling_length.use)
               for config in arm.ruri_cloth_physics.configs if config.enabled]
    output, success = None, False
    bones, specs, channels, samples = [], [], [], []

    def restore_settings():
        for config, camera_mode, distance_use in culling:
            config.culling.camera_culling_mode = camera_mode
            config.culling.distance_culling_length.use = distance_use
        scene.tool_settings.use_keyframe_insert_auto = original_auto_key
        if original_freeze is not None:
            arm.wiggle_freeze = original_freeze

    try:
        body.live = False
        runtime.forget_body(arm)
        scene.tool_settings.use_keyframe_insert_auto = False
        if original_freeze is not None:
            arm.wiggle_freeze = True
        _assign_bake_action(animation, source, source_handle, source_influence, source_blend)
        targets = native_bake._collect_target_bones(arm)
        bones = sorted((arm.pose.bones[name] for name in targets if name in arm.pose.bones),
                       key=lambda bone: (len(bone.parent_recursive), bone.name))
        if not bones:
            raise RuntimeError("没有可烘焙的 Ruri 骨骼，请检查启用的配置与根骨骼")
        for bone in bones:
            rotation = ('rotation_quaternion' if bone.rotation_mode == 'QUATERNION' else
                        'rotation_axis_angle' if bone.rotation_mode == 'AXIS_ANGLE' else 'rotation_euler')
            for prop in ('location', rotation, 'scale'):
                indices = []
                for index in range(len(getattr(bone, prop))):
                    indices.append(len(samples))
                    channels.append((bone.path_from_id(prop), index, bone.name))
                    samples.append(array('f'))
                specs.append((bone, prop, indices))
        paths = {path for path, _, _ in channels}
        if any(not driver.mute and driver.data_path in paths for driver in animation.drivers):
            raise RuntimeError("Ruri 骨骼的变换通道有启用的驱动器，请先停用这些驱动器再烘焙")
        for config, _, _ in culling:
            config.culling.camera_culling_mode = 'OFF'
            config.culling.distance_culling_length.use = False
        guards = []
        for edge in (start - 1, end + 1):
            for bone in bones:
                bone.matrix_basis = Matrix.Identity(4)
            scene.frame_set(edge)
            view_layer.update()
            guards.append({(bone.path_from_id(prop), index): value
                           for bone, prop, _ in specs
                           for index, value in enumerate(getattr(bone, prop))})
        sim_start = max(-1048574, min(scene.frame_start, start) - preroll)
        scene.frame_set(sim_start)
        view_layer.update()
        runtime.request_reset(arm, 'FULL')
        body.live = True
        previous_rotations = {}
        total = end - sim_start + 1
        for current_frame in range(sim_start, end + 1):
            scene.frame_set(current_frame)
            failure = getattr(runtime, '_handler_failure', None)
            if failure is not None:
                raise RuntimeError(f"第 {current_frame} 帧 Ruri 求解失败：{failure}")
            # The frame-change handler has already written matrix_basis.
            # Sampling those channels needs no second geometry evaluation.
            runtime.flush_pose_writes(None)
            if current_frame >= start:
                for bone, prop, indices in specs:
                    values = (tuple(bone.rotation_axis_angle) if prop == 'rotation_axis_angle'
                              else getattr(bone, prop).copy())
                    if prop == 'rotation_quaternion':
                        previous = previous_rotations.get(bone.name)
                        if previous is not None:
                            values.make_compatible(previous)
                        previous_rotations[bone.name] = values.copy()
                    elif prop == 'rotation_euler':
                        previous = previous_rotations.get(bone.name)
                        if previous is not None:
                            values = values.to_quaternion().to_euler(bone.rotation_mode, previous)
                        previous_rotations[bone.name] = values.copy()
                    for index, value in zip(indices, values):
                        if not math.isfinite(value):
                            raise RuntimeError(f"第 {current_frame} 帧骨骼 {bone.name} 的模拟数值无效")
                        samples[index].append(value)
            if (current_frame - sim_start) % 50 == 0 or current_frame == end:
                print(f'[BoneChain] Ruri 烘焙采样 {current_frame}/{end}', flush=True)
            yield (current_frame - sim_start + 1) / total
        output = source.copy() if source else bpy.data.actions.new('Ruri')
        output.name = (source.name if source else arm.name) + f'_Ruri_{start}_{end}'
        output.use_fake_user = True
        _write_wiggle_bake(output, arm, source_handle, channels, samples, start, end, guards)
        for curve in _wiggle_bake_curves(output, arm, source_handle):
            if curve.data_path in paths:
                curve.mute = False
                for modifier in curve.modifiers:
                    modifier.mute = True
        body.live = False
        runtime.forget_body(arm)
        restore_settings()
        _assign_bake_action(animation, output, source_handle, source_influence, source_blend)
        scene.frame_set(frame, subframe=subframe)
        view_layer.update()
        arm.bonechain_ruri_bake_restore = source
        arm.bonechain_ruri_bake_output = output
        arm.bonechain_ruri_bake_slot = source_handle
        arm.bonechain_ruri_bake_influence = source_influence
        arm.bonechain_ruri_bake_blend = source_blend
        arm.bonechain_ruri_bake_bones = json.dumps([bone.name for bone in bones], ensure_ascii=False)
        arm.bonechain_ruri_bake_scene = scene
        arm.bonechain_ruri_bake_saved = True
        success = True
    finally:
        if not success:
            body.live = False
            runtime.forget_body(arm)
            restore_settings()
            _assign_bake_action(animation, old_action, old_handle, old_influence, old_blend)
            for name, value in record_before.items():
                setattr(arm, 'bonechain_ruri_bake_' + name, value)
            for bone in bones:
                bone.matrix_basis = Matrix.Identity(4)
            body.live = original_live
            try:
                scene.frame_set(frame, subframe=subframe)
                view_layer.update()
            finally:
                if output is not None:
                    output.use_fake_user = False
                    if output.users == 0:
                        bpy.data.actions.remove(output)
    return output.name, len(bones)


class BONECHAIN_OT_ruri_bake(_BoneChainBakeModal, Operator):
    bl_idname = 'bonechain.ruri_bake'
    bl_label = '烘焙 Ruri 到关键帧'
    bl_description = '连续模拟并烘焙本骨架全部启用的 Ruri 配置；生成独立动作，完成后关闭实时模拟'
    bl_options = {'REGISTER', 'UNDO'}

    frame_start: IntProperty(name='起始帧', default=1, min=-1048574, max=1048574)
    frame_end: IntProperty(name='结束帧', default=250, min=-1048574, max=1048574)
    preroll: IntProperty(name='预模拟帧数', default=30, min=0, max=10000)

    @classmethod
    def poll(cls, context):
        arm = _ruri_bake_armature(context)
        return (hasattr(context.scene, 'ruri_cloth_physics') and arm is not None
                and hasattr(arm, 'ruri_cloth_physics'))

    def invoke(self, context, event):
        group = _get_active_group(context)
        for field in ('start', 'end'):
            name = 'frame_' + field
            saved = 'ruri_bake_' + field
            if not self.properties.is_property_set(name):
                setattr(self, name, getattr(group, saved) if group
                        else getattr(context.scene, name))
        if group and not self.properties.is_property_set('preroll'):
            self.preroll = group.ruri_bake_preroll
        return context.window_manager.invoke_props_dialog(self, width=380)

    def draw(self, context):
        row = self.layout.row(align=True)
        row.prop(self, 'frame_start')
        row.prop(self, 'frame_end')
        self.layout.prop(self, 'preroll')
        if self.frame_end < self.frame_start:
            self.layout.label(text='结束帧不能早于起始帧', icon='ERROR')
        else:
            self.layout.label(text=f'烘焙 {self.frame_end - self.frame_start + 1} 帧，包含首尾帧')
        self.layout.label(text='烘焙此骨架全部启用的 Ruri 配置', icon='INFO')
        self.layout.label(text='生成独立动作；完成后关闭本骨架实时模拟')
        self.layout.label(text='保留原动作；烘焙时按 Esc 可中止')

    def execute(self, context):
        import importlib
        if _BONECHAIN_BAKE_OWNER is not None:
            self.report({'WARNING'}, '正在烘焙，请等待完成或按 Esc 中止')
            return {'CANCELLED'}
        group = _get_active_group(context)
        start = (self.frame_start if self.properties.is_property_set('frame_start') else
                 group.ruri_bake_start if group else
                 context.scene.frame_start)
        end = (self.frame_end if self.properties.is_property_set('frame_end') else
               group.ruri_bake_end if group else
               context.scene.frame_end)
        preroll = (self.preroll if self.properties.is_property_set('preroll') else
                   group.ruri_bake_preroll if group else self.preroll)
        if end < start:
            self.report({'ERROR'}, '结束帧不能早于起始帧')
            return {'CANCELLED'}
        arm = _ruri_bake_armature(context)
        if arm is None or not hasattr(arm, 'ruri_cloth_physics'):
            self.report({'ERROR'}, '请先选择骨架并启用 Ruri 前置')
            return {'CANCELLED'}
        if not any(config.enabled for config in arm.ruri_cloth_physics.configs):
            self.report({'ERROR'}, '没有启用的 Ruri 配置，请先配置 Ruri')
            return {'CANCELLED'}
        world = importlib.import_module('RuriClothPhysics.blender_host.world')
        if world.body_entry(context.scene, arm) is None:
            self.report({'ERROR'}, '请先配置 Ruri，将骨架加入当前场景')
            return {'CANCELLED'}
        if arm.animation_data and arm.animation_data.use_tweak_mode:
            self.report({'ERROR'}, '请先退出 NLA 动作编辑模式')
            return {'CANCELLED'}
        if group:
            group.ruri_bake_preroll = preroll
        return self._start_bake(context, _ruri_bake_range(context, arm, start, end, preroll),
                                start, end, group, 'ruri')


class BONECHAIN_OT_ruri_reset(Operator):
    bl_idname = 'bonechain.ruri_reset'
    bl_label = '复位 Ruri'
    bl_description = '回到场景起始帧并清空本骨架的 Ruri 模拟状态'

    @classmethod
    def poll(cls, context):
        return BONECHAIN_OT_ruri_bake.poll(context) and _BONECHAIN_BAKE_OWNER is None

    def execute(self, context):
        import importlib
        arm = _ruri_bake_armature(context)
        if _ruri_bake_active(arm):
            self.report({'WARNING'}, '请先取消 Ruri 烘焙，恢复原动作后再复位')
            return {'CANCELLED'}
        runtime = importlib.import_module('RuriClothPhysics.blender_host.runtime')
        world = importlib.import_module('RuriClothPhysics.blender_host.world')
        body = world.body_entry(context.scene, arm)
        if body is None or not any(c.enabled for c in arm.ruri_cloth_physics.configs):
            self.report({'ERROR'}, '请先配置 Ruri')
            return {'CANCELLED'}
        runtime.forget_body(arm)
        body.live = True
        context.scene.frame_set(context.scene.frame_start)
        self.report({'INFO'}, '已回到起始帧并复位 Ruri')
        return {'FINISHED'}


class _RuriRestoreBake:
    @classmethod
    def poll(cls, context):
        arm = _ruri_bake_armature(context)
        return (hasattr(context.scene, 'ruri_cloth_physics')
                and arm is not None and hasattr(arm, 'ruri_cloth_physics')
                and arm.bonechain_ruri_bake_saved
                and _BONECHAIN_BAKE_OWNER is None)

    def execute(self, context):
        import importlib
        import json
        arm, scene = _ruri_bake_armature(context), context.scene
        if _BONECHAIN_BAKE_OWNER is not None:
            self.report({'WARNING'}, '正在烘焙，请先按 Esc 中止')
            return {'CANCELLED'}
        if arm is None or not arm.bonechain_ruri_bake_saved:
            self.report({'WARNING'}, '没有本插件记录的 Ruri 烘焙可删除或恢复')
            return {'CANCELLED'}
        active = _ruri_bake_active(arm)
        if not active and not self._delete_bake:
            self.report({'WARNING'}, '请切回本轮 Ruri 烘焙动作后再取消')
            return {'CANCELLED'}
        if arm.bonechain_ruri_bake_scene not in (None, scene):
            self.report({'WARNING'}, '请回到烘焙时的场景再取消')
            return {'CANCELLED'}
        if arm.animation_data and arm.animation_data.use_tweak_mode:
            self.report({'WARNING'}, '请先退出 NLA 动作编辑模式')
            return {'CANCELLED'}
        runtime = importlib.import_module('RuriClothPhysics.blender_host.runtime')
        world = importlib.import_module('RuriClothPhysics.blender_host.world')
        body = world.body_entry(scene, arm)
        if body is None:
            self.report({'WARNING'}, '请先在 Ruri 物理面板将骨架加入当前场景')
            return {'CANCELLED'}
        output = arm.bonechain_ruri_bake_output
        restore = arm.bonechain_ruri_bake_restore
        if self._delete_bake and output is not None and output == restore:
            self.report({'ERROR'}, '烘焙恢复记录异常：烘焙动作与原动作相同，未删除')
            return {'CANCELLED'}
        animation = arm.animation_data_create()
        current, slot = animation.action, animation.action_slot
        handle = slot.handle if slot else 0
        influence, blend = animation.action_influence, animation.action_blend_type
        original_live = body.live
        frame, subframe = scene.frame_current, scene.frame_subframe
        names = json.loads(arm.bonechain_ruri_bake_bones or '[]') if active else []
        try:
            body.live = False
            runtime.forget_body(arm)
            if active:
                _assign_bake_action(animation, restore, arm.bonechain_ruri_bake_slot,
                                    arm.bonechain_ruri_bake_influence,
                                    arm.bonechain_ruri_bake_blend)
            for name in names:
                bone = arm.pose.bones.get(name)
                if bone is not None:
                    bone.matrix_basis = Matrix.Identity(4)
            body.live = True
            scene.frame_set(frame, subframe=subframe)
            context.view_layer.update()
        except Exception as error:
            body.live = False
            runtime.forget_body(arm)
            _assign_bake_action(animation, current, handle, influence, blend)
            body.live = original_live
            scene.frame_set(frame, subframe=subframe)
            self.report({'ERROR'}, f'恢复失败，已保留烘焙恢复记录：{error}')
            return {'CANCELLED'}
        if output is not None and not self._delete_bake:
            output.use_fake_user = True
        arm.bonechain_ruri_bake_saved = False
        arm.bonechain_ruri_bake_restore = None
        arm.bonechain_ruri_bake_output = None
        arm.bonechain_ruri_bake_bones = ''
        arm.bonechain_ruri_bake_scene = None
        if self._delete_bake and output is not None:
            was_fake = output.use_fake_user
            output.use_fake_user = False
            if output.users == 0:
                name = output.name
                try:
                    bpy.data.actions.remove(output)
                except Exception as error:
                    output.use_fake_user = True
                    self.report({'WARNING'}, f'已恢复实时 Ruri，但烘焙动作删除失败：{error}')
                    return {'FINISHED'}
                self.report({'INFO'}, f'已删除 Ruri 烘焙动作「{name}」并恢复实时模拟')
            else:
                output.use_fake_user = was_fake
                self.report({'INFO'}, '已解除本骨架烘焙并恢复实时 Ruri；动作仍被其他对象或 NLA 引用，已保留')
        elif self._delete_bake:
            self.report({'INFO'}, '已清除 Ruri 烘焙记录并恢复实时模拟')
        else:
            self.report({'INFO'}, '已恢复原动作并启用实时 Ruri；烘焙动作已保留')
        return {'FINISHED'}


class BONECHAIN_OT_ruri_cancel_bake(_RuriRestoreBake, Operator):
    bl_idname = 'bonechain.ruri_cancel_bake'
    bl_label = '取消 Ruri 烘焙'
    bl_description = '恢复烘焙前动作并启用本骨架实时 Ruri；烘焙动作保留在动作列表中'
    bl_options = {'REGISTER', 'UNDO'}
    _delete_bake = False


class BONECHAIN_OT_ruri_delete_bake(_RuriRestoreBake, Operator):
    bl_idname = 'bonechain.ruri_delete_bake'
    bl_label = 'Delete Bake / 删除 Ruri 烘焙'
    bl_description = '解除本轮烘焙并恢复实时 Ruri；当前使用烘焙动作时恢复原动作，删除未被其他对象引用的烘焙动作'
    bl_options = {'REGISTER', 'UNDO'}
    _delete_bake = True


class BONECHAIN_OT_ruri_scene_range(Operator):
    bl_idname = 'bonechain.ruri_scene_range'
    bl_label = '使用场景区间'
    bl_description = '将 Ruri 烘焙起止帧设为当前场景的起止帧'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _get_active_group(context) is not None and _BONECHAIN_BAKE_OWNER is None

    def execute(self, context):
        group = _get_active_group(context)
        group.ruri_bake_start = context.scene.frame_start
        group.ruri_bake_end = context.scene.frame_end
        return {'FINISHED'}


class BONECHAIN_OT_wiggle_cancel_bake(Operator):
    bl_idname = "bonechain.wiggle_cancel_bake"
    bl_label = "取消Wiggle烘焙"
    bl_description = "恢复本轮烘焙前的动作并恢复实时物理；保留烘焙动作，不清空身体动画"
    bl_options = {'REGISTER', 'UNDO'}

    restore_action_name: StringProperty(name="烘焙前的动作")

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == 'ARMATURE'

    def invoke(self, context, event):
        arm = context.active_object
        if arm.bonechain_wiggle_bake_saved:
            return self.execute(context)
        # Older bakes did not record their input. Offer a name-based
        # suggestion for native copy-mode bakes, but require the user to
        # choose/confirm the action rather than guessing and deleting keys.
        import re
        action = arm.animation_data.action if arm.animation_data else None
        if action:
            name = re.sub(r'_Wiggle_-?\d+_-?\d+(?:\.\d{3})?$', '', action.name)
            if name != action.name and bpy.data.actions.get(name):
                self.restore_action_name = name
        return context.window_manager.invoke_props_dialog(self, width=360)

    def draw(self, context):
        self.layout.label(text="旧烘焙没有恢复记录，请选择烘焙前的动作", icon='INFO')
        self.layout.prop_search(self, "restore_action_name", bpy.data, "actions")
        self.layout.label(text="保留当前烘焙动作，不删除关键帧")

    def execute(self, context):
        arm, scene = context.active_object, context.scene
        if _BONECHAIN_BAKE_OWNER is not None:
            self.report({'WARNING'}, "正在烘焙，请按 Esc 中止")
            return {'CANCELLED'}
        if not _wiggle_available(context) or _wiggle_runtime() is None:
            self.report({'ERROR'}, "未检测到 Wiggle 2")
            return {'CANCELLED'}
        if arm.animation_data and arm.animation_data.use_tweak_mode:
            self.report({'ERROR'}, "请先退出 NLA 动作编辑模式")
            return {'CANCELLED'}
        current = arm.animation_data.action if arm.animation_data else None
        if arm.bonechain_wiggle_bake_saved:
            if current != arm.bonechain_wiggle_bake_output:
                self.report({'WARNING'}, "当前动作已切换，请切回本轮 Wiggle 烘焙动作后再取消")
                return {'CANCELLED'}
            restore = arm.bonechain_wiggle_bake_restore
            handle = arm.bonechain_wiggle_bake_slot
            influence = arm.bonechain_wiggle_bake_influence
            blend = arm.bonechain_wiggle_bake_blend
        else:
            restore = bpy.data.actions.get(self.restore_action_name)
            if restore is None or restore == current:
                self.report({'WARNING'}, "没有烘焙前备份；请选择不同的原始动作，覆盖过的旧动画需要从工程备份恢复")
                return {'CANCELLED'}
            slot = getattr(arm.animation_data, 'action_slot', None)
            handle = slot.handle if slot else 0
            influence, blend = 1.0, 'REPLACE'
        slots = list(getattr(restore, 'slots', ())) if restore else []
        slot = next((s for s in slots if s.handle == handle), None)
        if slots and slot is None:
            compatible = [s for s in slots if s.target_id_type == 'OBJECT']
            if len(compatible) != 1:
                self.report({'WARNING'}, "原动作有多个动作槽，无法确定骨架对应的槽，请在动作编辑器中手动恢复")
                return {'CANCELLED'}
            slot = compatible[0]
        frame, subframe = scene.frame_current, scene.frame_subframe
        previous_reset = scene.wiggle.reset
        old_slot = getattr(arm.animation_data, 'action_slot', None)
        old_influence = getattr(arm.animation_data, 'action_influence', 1.0)
        old_blend = getattr(arm.animation_data, 'action_blend_type', 'REPLACE')
        old_freeze, old_mute, old_enable = arm.wiggle_freeze, arm.wiggle_mute, scene.wiggle_enable
        old_fake_user = current.use_fake_user if current else False
        completed = False
        scene.wiggle.reset = True
        try:
            arm.animation_data_create()
            if current and current != restore:
                current.use_fake_user = True
            arm.animation_data.action = restore
            if slot is not None:
                arm.animation_data.action_slot = slot
            arm.animation_data.action_influence = influence
            arm.animation_data.action_blend_type = blend
            scene.wiggle_enable = True
            arm.wiggle_mute = False
            arm.wiggle_freeze = False
            runtime = _wiggle_runtime()
            runtime.build_list()
            bones = [b for b in arm.pose.bones if b.wiggle_head or b.wiggle_tail]
            _reset_wiggle_pose(context, runtime, bones)
            completed = True
        except Exception as e:
            arm.animation_data.action = current
            if old_slot is not None:
                arm.animation_data.action_slot = old_slot
            arm.animation_data.action_influence = old_influence
            arm.animation_data.action_blend_type = old_blend
            arm.wiggle_freeze, arm.wiggle_mute = old_freeze, old_mute
            scene.wiggle_enable = old_enable
            if current:
                current.use_fake_user = old_fake_user
            self.report({'WARNING'}, f"取消烘焙失败，已保留恢复记录，可重试：{e}")
            return {'CANCELLED'}
        finally:
            scene.wiggle.reset = False if completed else previous_reset
        arm.bonechain_wiggle_bake_saved = False
        arm.bonechain_wiggle_bake_output = None
        arm.bonechain_wiggle_bake_restore = None
        self.report({'INFO'}, "已恢复烘焙前动作并启用实时 Wiggle；烘焙动作保留在动作列表中")
        return {'FINISHED'}


# ===============================================================
# 插件骨骼物理烘焙：只处理 Ruri / Wiggle。
# ===============================================================
def _physics_bake_jobs(context):
    import importlib
    scene = context.scene
    world = (importlib.import_module('RuriClothPhysics.blender_host.world')
             if hasattr(scene, 'ruri_cloth_physics') else None)
    ruri_jobs, wiggle_jobs = [], []
    for arm in sorted((o for o in scene.objects if o.type == 'ARMATURE'), key=lambda o: o.name):
        if arm.name not in context.view_layer.objects:
            continue
        body = world.body_entry(scene, arm) if world else None
        if body is not None and any(c.enabled for c in arm.ruri_cloth_physics.configs):
            ruri_jobs.append(('Ruri', arm))
        if (getattr(scene, 'wiggle_enable', False) and getattr(arm, 'wiggle_enable', False)
                and not getattr(arm, 'wiggle_mute', True)
                and any((getattr(b, 'wiggle_head', False) or getattr(b, 'wiggle_tail', False))
                        and not getattr(b, 'wiggle_mute', False) for b in arm.pose.bones)):
            wiggle_jobs.append(('Wiggle', arm))
    return ruri_jobs + wiggle_jobs


def _physics_bake_source(arm, kinds):
    ad = arm.animation_data_create()
    action, slot = ad.action, ad.action_slot
    handle = slot.handle if slot else 0
    influence, blend = ad.action_influence, ad.action_blend_type
    seen = set()
    while action is not None and action.as_pointer() not in seen:
        seen.add(action.as_pointer())
        found = False
        for kind in kinds:
            prefix = 'bonechain_' + kind.lower() + '_bake_'
            if getattr(arm, prefix + 'saved') and action == getattr(arm, prefix + 'output'):
                action = getattr(arm, prefix + 'restore')
                handle = getattr(arm, prefix + 'slot')
                influence = getattr(arm, prefix + 'influence')
                blend = getattr(arm, prefix + 'blend')
                found = True
                break
        if not found:
            break
    return action, handle, influence, blend


def _bake_input_visibility(context, jobs):
    """Suspend downstream mesh evaluation; retain all declared solver inputs.

    Use Blender's ID dependency map, including constraints, drivers, node trees
    and collection references. Never change modifier or cache configuration.
    Unknown scripted driver dependencies fall back to the complete scene.
    """
    import importlib
    roots = {arm for _kind, arm in jobs}
    scene = context.scene
    if scene.camera:
        roots.add(scene.camera)
    if hasattr(scene, 'ruri_cloth_physics'):
        world = importlib.import_module('RuriClothPhysics.blender_host.world')
        roots.update(obj for obj, _settings in world.scene_colliders(scene))
        roots.update(world.winds(scene))
        roots.update(world.bodies(scene))
    # Scene-level drivers can feed global solver parameters. Keep their
    # explicit targets without traversing scene -> every scene object.
    scene_animation = scene.animation_data
    if scene_animation:
        for fc in scene_animation.drivers:
            if fc.mute:
                continue
            driver = fc.driver
            if driver.type == 'SCRIPTED' and not driver.is_simple_expression:
                print('[BoneChain] 场景自定义驱动器依赖无法静态确认，保留完整场景求值', flush=True)
                return lambda: None
            roots.update(target.id for variable in driver.variables for target in variable.targets
                         if target.id is not None and not isinstance(target.id, bpy.types.Scene))
    # Wiggle guides are resolved by name rather than an ID pointer.
    for _kind, arm in jobs:
        for bone in arm.pose.bones:
            guide = bpy.data.objects.get('WGuide_' + bone.name)
            if guide:
                roots.add(guide)
    dependencies = {}
    for used, users in bpy.data.user_map().items():
        if isinstance(used, bpy.types.Scene):
            continue
        for user in users:
            dependencies.setdefault(user, set()).add(used)
    retained, pending = set(), list(roots)
    while pending:
        item = pending.pop()
        if item in retained:
            continue
        retained.add(item)
        animation = getattr(item, 'animation_data', None)
        if animation and any(fc.driver.type == 'SCRIPTED' and not fc.driver.is_simple_expression
                             for fc in animation.drivers if not fc.mute):
            print('[BoneChain] 自定义驱动器依赖无法静态确认，保留完整场景求值', flush=True)
            return lambda: None
        pending.extend(dependencies.get(item, set()) - retained)
    # Native simulators can read scene-wide effectors without explicit ID
    # links. If a solver input itself uses one, preserve full scene evaluation.
    native_simulators = {'CLOTH', 'SOFT_BODY', 'FLUID', 'DYNAMIC_PAINT', 'PARTICLE_SYSTEM'}
    if any(isinstance(item, bpy.types.Object) and item.type == 'MESH'
           and any(mod.type in native_simulators for mod in item.modifiers) for item in retained):
        print('[BoneChain] 物理输入含场景级模拟，保留完整场景求值', flush=True)
        return lambda: None
    suspended = [obj for obj in context.view_layer.objects
                 if obj.type == 'MESH' and obj not in retained and not obj.hide_viewport
                 and obj.is_editable]
    print(f'[BoneChain] 采样期间暂缓更新 {len(suspended)} 个下游网格；保留 {len(retained)} 个输入数据块', flush=True)
    changed = []
    try:
        for obj in suspended:
            selected = obj.select_get()
            obj.hide_viewport = True
            changed.append((obj, selected))
    except BaseException:
        for obj, selected in reversed(changed):
            obj.hide_viewport = False
            obj.select_set(selected)
        raise
    def restore():
        for obj, selected in reversed(changed):
            obj.hide_viewport = False
            obj.select_set(selected)
    return restore


def _all_physics_bake_range(context, start, end, preroll, armature_name=''):
    jobs = _physics_bake_jobs(context)
    if armature_name:
        jobs = [(kind, arm) for kind, arm in jobs if arm.name == armature_name]
    if not jobs:
        raise RuntimeError('场景中没有启用的插件物理配置')
    restore_visibility = _bake_input_visibility(context, jobs)
    try:
        return (yield from _all_physics_bake_range_impl(context, start, end, preroll, armature_name))
    finally:
        # Also runs for Esc, errors and worker cancellation before saving.
        restore_visibility()


def _all_physics_bake_range_impl(context, start, end, preroll, armature_name=''):
    import importlib
    jobs = _physics_bake_jobs(context)
    if armature_name:
        jobs = [(kind, arm) for kind, arm in jobs if arm.name == armature_name]
    if not jobs:
        raise RuntimeError('场景中没有启用的插件物理配置')
    arms = {arm.name: arm for _kind, arm in jobs}
    kinds = {name: [kind for kind, arm in jobs if arm.name == name] for name in arms}
    snapshots, roots = {}, {}
    world = (importlib.import_module('RuriClothPhysics.blender_host.world')
             if any(kind == 'Ruri' for kind, _arm in jobs) else None)
    active = context.view_layer.objects.active
    for name, arm in arms.items():
        if arm.animation_data and arm.animation_data.use_tweak_mode:
            raise RuntimeError(f'{name} 请先退出 NLA 动作编辑模式')
        ad = arm.animation_data_create()
        fields = {}
        for kind in ('ruri', 'wiggle'):
            for field in ('restore', 'output', 'saved', 'slot', 'influence', 'blend'):
                key = 'bonechain_' + kind + '_bake_' + field
                fields[key] = getattr(arm, key)
        fields['bonechain_ruri_bake_bones'] = arm.bonechain_ruri_bake_bones
        fields['bonechain_ruri_bake_scene'] = arm.bonechain_ruri_bake_scene
        body = world.body_entry(context.scene, arm) if world else None
        snapshots[name] = (ad.action, ad.action_slot.handle if ad.action_slot else 0,
                           ad.action_influence, ad.action_blend_type, fields,
                           getattr(arm, 'wiggle_freeze', None), body.live if body else None)
        roots[name] = _physics_bake_source(arm, kinds[name])
    begun, total_bones, success = set(), 0, False
    try:
        for index, (kind, arm) in enumerate(jobs):
            if arm.name not in begun:
                _assign_bake_action(arm.animation_data, *roots[arm.name])
                begun.add(arm.name)
            context.view_layer.objects.active = arm
            owner = _BONECHAIN_BAKE_OWNER
            if owner is not None:
                owner._progress_stage = f'{index + 1}/{len(jobs)} · {kind} · {arm.name}'
            original_preroll = None
            if kind == 'Wiggle':
                original_preroll = context.scene.wiggle.preroll
                context.scene.wiggle.preroll = preroll
            generator = (_ruri_bake_range(context, arm, start, end, preroll)
                         if kind == 'Ruri' else _wiggle_bake_range(context, arm, start, end))
            try:
                while True:
                    try:
                        progress = next(generator)
                    except StopIteration as completed:
                        total_bones += completed.value[1]
                        break
                    yield (index + progress) / len(jobs)
            finally:
                generator.close()
                if original_preroll is not None:
                    context.scene.wiggle.preroll = original_preroll
        # Each engine adds its channels to the previous result. Point all
        # participating records to the final action and the same original input.
        for name, arm in arms.items():
            action = arm.animation_data.action
            source, handle, influence, blend = roots[name]
            for kind in kinds[name]:
                prefix = 'bonechain_' + kind.lower() + '_bake_'
                setattr(arm, prefix + 'output', action)
                setattr(arm, prefix + 'restore', source)
                setattr(arm, prefix + 'slot', handle)
                setattr(arm, prefix + 'influence', influence)
                setattr(arm, prefix + 'blend', blend)
        success = True
    finally:
        if not success:
            for name in begun:
                arm = arms[name]
                action, handle, influence, blend, fields, freeze, live = snapshots[name]
                _assign_bake_action(arm.animation_data, action, handle, influence, blend)
                for key, value in fields.items():
                    setattr(arm, key, value)
                if freeze is not None:
                    arm.wiggle_freeze = freeze
                body = world.body_entry(context.scene, arm) if world else None
                if body is not None and live is not None:
                    body.live = live
                    importlib.import_module('RuriClothPhysics.blender_host.runtime').forget_body(arm)
        if active and active.name in context.view_layer.objects:
            context.view_layer.objects.active = active
    return ', '.join(arm.animation_data.action.name for arm in arms.values()), total_bones


class BONECHAIN_OT_physics_restore_all(Operator):
    bl_idname = 'bonechain.physics_restore_all'
    bl_label = '恢复全部插件实时物理'
    bl_options = {'REGISTER', 'UNDO'}
    delete_output: BoolProperty(default=False)

    def execute(self, context):
        import importlib
        import json
        if _BONECHAIN_BAKE_OWNER is not None:
            self.report({'WARNING'}, '请先等待烘焙结束或按 Esc 中止')
            return {'CANCELLED'}
        outputs, restored = set(), 0
        for arm in context.scene.objects:
            if arm.type != 'ARMATURE':
                continue
            ad = arm.animation_data
            participating = [kind for kind in ('Ruri', 'Wiggle')
                             if getattr(arm, 'bonechain_' + kind.lower() + '_bake_saved')]
            if not ad or not participating or not any(ad.action == getattr(
                    arm, 'bonechain_' + kind.lower() + '_bake_output') for kind in participating):
                continue
            source = _physics_bake_source(arm, participating)
            _assign_bake_action(ad, *source)
            for kind in participating:
                prefix = 'bonechain_' + kind.lower() + '_bake_'
                output = getattr(arm, prefix + 'output')
                if output is not None:
                    outputs.add(output)
                if kind == 'Ruri':
                    world = importlib.import_module('RuriClothPhysics.blender_host.world')
                    runtime = importlib.import_module('RuriClothPhysics.blender_host.runtime')
                    for name in json.loads(arm.bonechain_ruri_bake_bones or '[]'):
                        if name in arm.pose.bones:
                            arm.pose.bones[name].matrix_basis = Matrix.Identity(4)
                    body = world.body_entry(context.scene, arm)
                    if body:
                        runtime.forget_body(arm)
                        body.live = True
                        runtime.request_reset(arm, 'FULL')
                    arm.bonechain_ruri_bake_bones = ''
                    arm.bonechain_ruri_bake_scene = None
                else:
                    arm.wiggle_freeze = False
                    arm.wiggle_mute = False
                    context.scene.wiggle_enable = True
                    context.scene.wiggle.reset = True
                    for bone in arm.pose.bones:
                        if getattr(bone, 'wiggle_head', False) or getattr(bone, 'wiggle_tail', False):
                            bone.matrix_basis = Matrix.Identity(4)
                setattr(arm, prefix + 'saved', False)
                setattr(arm, prefix + 'output', None)
                setattr(arm, prefix + 'restore', None)
            restored += 1
        context.scene.frame_set(context.scene.frame_current, subframe=context.scene.frame_subframe)
        context.view_layer.update()
        removed = 0
        for action in outputs:
            if self.delete_output:
                action.use_fake_user = False
                if action.users == 0:
                    bpy.data.actions.remove(action)
                    removed += 1
                else:
                    action.use_fake_user = True
            else:
                action.use_fake_user = True
        self.report({'INFO'}, f'已恢复 {restored} 个骨架的实时物理，删除 {removed} 个未使用的输出动作')
        return {'FINISHED'}


def _native_modal_running(context):
    ids = {'OBJECT_OT_simulation_nodes_cache_bake', 'PTCACHE_OT_bake'}
    return any(getattr(op, 'bl_idname', '') in ids or op.bl_rna.identifier in ids
               for window in context.window_manager.windows for op in getattr(window, 'modal_operators', ()))


class BONECHAIN_OT_physics_bake_range(Operator):
    bl_idname = 'bonechain.physics_bake_range'
    bl_label = '使用场景区间'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.bonechain_props
        props.native_cloth_start = context.scene.frame_start
        props.native_cloth_end = context.scene.frame_end
        return {'FINISHED'}


class BONECHAIN_OT_physics_bake_isolated(Operator):
    bl_idname = 'bonechain.physics_bake_isolated'
    bl_label = '内存受限烘焙'
    bl_description = '独立进程烘焙场景全部已启用的插件物理，限制内存并另存副本；仅支持 Linux'

    def execute(self, context):
        global _BONECHAIN_BAKE_OWNER
        import os
        from pathlib import Path
        import shutil
        import subprocess
        import sys
        import tempfile
        if _BONECHAIN_BAKE_OWNER is not None or _native_modal_running(context):
            self.report({'ERROR'}, '已有烘焙任务在运行')
            return {'CANCELLED'}
        if sys.platform != 'linux' or not shutil.which('systemd-run'):
            self.report({'ERROR'}, '独立进程内存限制需要 Linux 用户 systemd')
            return {'CANCELLED'}
        if not bpy.data.filepath or bpy.data.is_dirty:
            self.report({'ERROR'}, '请先保存工程；独立进程读取磁盘上的工程和设置')
            return {'CANCELLED'}
        root = Path(__file__).resolve().parent
        script = next((p for p in (root / 'bonechain_tools' / 'bake_limited.py',
                                   root / 'tools' / 'bake_limited.py') if p.is_file()), None)
        if script is None:
            self.report({'ERROR'}, '缺少 bonechain_tools/bake_limited.py，请同时安装隔离烘焙工具目录')
            return {'CANCELLED'}
        props = context.scene.bonechain_props
        fd, self._log_path = tempfile.mkstemp(prefix='bonechain-bake-', suffix='.log')
        command = [shutil.which('python3') or '/usr/bin/python3', str(script), bpy.data.filepath,
                   '--blender', bpy.app.binary_path, '--start', str(props.native_cloth_start),
                   '--end', str(props.native_cloth_end), '--memory-limit', str(props.native_cloth_memory_gb) + 'G']
        self._log_offset = 0
        self._log_pending = ''
        self._ui_progress = 0.0
        self._ui_progress_text = '启动工作进程'
        self._process = None
        self._timer = None
        self._workspace = context.workspace
        self._wm = context.window_manager
        try:
            with os.fdopen(fd, 'wb') as log:
                self._process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                                 start_new_session=True)
            self._timer = self._wm.event_timer_add(0.5, window=context.window)
            self._wm.modal_handler_add(self)
        except Exception as error:
            if self._process and self._process.poll() is None:
                import signal
                os.killpg(self._process.pid, signal.SIGINT)
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}
        self._wm.progress_begin(0, 100)
        _BONECHAIN_BAKE_OWNER = self
        self._cancel_sent = False
        self.report({'INFO'}, '已启动内存受限烘焙；结果另存新副本。日志：' + self._log_path)
        return {'RUNNING_MODAL'}

    def _finish(self, context, cancelled=False):
        global _BONECHAIN_BAKE_OWNER
        if self._process and self._process.poll() is None:
            import os
            import signal
            try:
                os.killpg(self._process.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
        if self._timer:
            self._wm.event_timer_remove(self._timer)
            self._timer = None
        self._wm.progress_end()
        if self._workspace:
            self._workspace.status_text_set(None)
        _BONECHAIN_BAKE_OWNER = None
        return {'CANCELLED'}

    def cancel(self, context):
        self._finish(context, cancelled=True)

    def _read_progress(self):
        import json
        try:
            with open(self._log_path, 'rb') as log:
                log.seek(self._log_offset)
                data = log.read(65536)
                self._log_offset = log.tell()
        except OSError:
            return
        pending = self._log_pending + data.decode('utf-8', errors='replace')
        lines = pending.split('\n')
        self._log_pending = lines.pop()[-8192:]
        for line in lines:
            if not line.startswith('BONECHAIN_PROGRESS '):
                continue
            try:
                progress = json.loads(line[len('BONECHAIN_PROGRESS '):])
                self._ui_progress = max(self._ui_progress, min(1.0, max(0.0, float(progress['progress']))))
                self._ui_progress_text = str(progress['stage'])
                if 'frame' in progress:
                    self._ui_progress_text += f" · 第 {progress['frame']}/{progress['end']} 帧"
            except (ValueError, KeyError, TypeError):
                continue
        self._wm.progress_update(self._ui_progress * 100)

    def modal(self, context, event):
        global _BONECHAIN_BAKE_OWNER
        if event.type == 'ESC' and not self._cancel_sent and self._process.poll() is None:
            import os
            import signal
            try:
                os.killpg(self._process.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
            self._cancel_sent = True
        if event.type != 'TIMER':
            return {'PASS_THROUGH'}
        self._read_progress()
        if context.area:
            context.area.tag_redraw()
        code = self._process.poll()
        if code is None:
            if self._workspace:
                self._workspace.status_text_set(f'{self._ui_progress_text} · {self._ui_progress:.1%} · Esc 中止')
            return {'PASS_THROUGH'}
        if self._timer:
            self._wm.event_timer_remove(self._timer)
        self._wm.progress_end()
        if self._workspace:
            self._workspace.status_text_set(None)
        _BONECHAIN_BAKE_OWNER = None
        reason = ''
        if code != 0:
            try:
                import os
                with open(self._log_path, 'rb') as log:
                    log.seek(0, os.SEEK_END)
                    log.seek(max(0, log.tell() - 8192))
                    lines = log.read(8192).decode('utf-8', errors='replace').splitlines()
                reason = next((line for line in reversed(lines)
                               if line.startswith(('RuntimeError:', 'MemoryError:', 'ModuleNotFoundError:'))), '')
                if not reason and any('oom-kill' in line.lower() for line in lines):
                    reason = '工作进程达到内存上限'
            except OSError:
                pass
        self.report({'INFO'} if code == 0 else {'WARNING'},
                    ('隔离烘焙完成，结果在新工程副本；' if code == 0 else
                     '隔离烘焙已停止：' + (reason or f'退出码 {code}') + '；原工程保留；')
                    + '详情：' + self._log_path)
        return {'FINISHED'} if code == 0 else {'CANCELLED'}


class BONECHAIN_OT_physics_bake(_BoneChainBakeModal, Operator):
    bl_idname = 'bonechain.physics_bake'
    bl_label = '正常烘焙'
    bl_description = '自动烘焙场景中全部已启用的插件骨骼物理，包含 Ruri 所有类型和 Wiggle'
    armature_name: StringProperty(default='', options={'HIDDEN'})

    def execute(self, context):
        if _BONECHAIN_BAKE_OWNER is not None or _native_modal_running(context):
            self.report({'ERROR'}, '已有烘焙在运行')
            return {'CANCELLED'}
        props = context.scene.bonechain_props
        if props.native_cloth_end < props.native_cloth_start:
            self.report({'ERROR'}, '结束帧不能早于起始帧')
            return {'CANCELLED'}
        if not _physics_bake_jobs(context):
            self.report({'ERROR'}, '场景中没有启用的插件物理')
            return {'CANCELLED'}
        return self._start_bake(context, _all_physics_bake_range(context,
            props.native_cloth_start, props.native_cloth_end, props.native_cloth_preroll,
            self.armature_name), props.native_cloth_start, props.native_cloth_end, None, 'all')


# ===============================================================
# UIList
# ===============================================================
class BONECHAIN_UL_groups(UIList):
    def draw_item(self, ctx, layout, data, item, icon, active_data, prop):
        layout.prop(item, "name", text="", emboss=False, icon='GROUP_BONE')


class BONECHAIN_UL_flex(UIList):
    def draw_item(self, ctx, layout, data, item, icon, active_data, prop):
        layout.label(text=item.name, icon='BONE_DATA')


class BONECHAIN_UL_coll(UIList):
    def draw_item(self, ctx, layout, data, item, icon, active_data, prop):
        props = ctx.scene.bonechain_props
        if not props.groups or props.group_index < 0 or props.group_index >= len(props.groups):
            return

        grp = props.groups[props.group_index]

        if grp.collision_mode in {'OLD', 'RURI'}:
            col = layout.column(align=True)
            col.label(text=item.name, icon='META_CAPSULE')
            row = col.row(align=True)
            if grp.collision_mode == 'RURI' and item.ruri_shape in {'BOX', 'ELLIPSOID'}:
                row.label(text="盒体" if item.ruri_shape == 'BOX' else "椭球")
            else:
                row.prop(item, "radius", text="粗")
            if grp.collision_mode == 'OLD':
                row.prop(item, "subdiv", text="细分")
                row.prop(item, "influence", text="强度")
        else:
            arm = ctx.active_object
            tag = ""
            if arm and arm.type == 'ARMATURE':
                db = arm.data.bones.get(item.name)
                if db:
                    tag = "  [变形骨]" if db.use_deform else "  [控制骨]"
            layout.label(text=item.name + tag, icon='BONE_DATA')


class BONECHAIN_UL_deform(UIList):
    def draw_item(self, ctx, layout, data, item, icon, active_data, prop):
        layout.label(text=item.name, icon='MESH_CYLINDER')


# ===============================================================
# 面板
# ===============================================================

def _draw_physics_bake(layout, context):
    props = context.scene.bonechain_props
    box = layout.box()
    box.prop(props, 'native_cloth_open', text='插件物理烘焙', emboss=False,
             icon='TRIA_DOWN' if props.native_cloth_open else 'TRIA_RIGHT')
    if not props.native_cloth_open:
        return
    box.label(text='一键烘焙全部已启用的插件物理', icon='PHYSICS')
    row = box.row(align=True)
    row.prop(props, 'native_cloth_start')
    row.prop(props, 'native_cloth_end')
    box.prop(props, 'native_cloth_preroll')
    box.operator('bonechain.physics_bake_range', text='使用场景区间', icon='TIME')
    box.operator('bonechain.physics_bake', text='正常烘焙', icon='RENDER_ANIMATION')
    box.prop(props, 'native_cloth_memory_gb')
    box.operator('bonechain.physics_bake_isolated', text='内存受限烘焙', icon='RENDER_ANIMATION')
    box.label(text='受限：Linux · 保存后运行 · 另存副本', icon='INFO')
    owner = _BONECHAIN_BAKE_OWNER
    if owner is not None:
        progress = getattr(owner, '_ui_progress', 0.0)
        text = getattr(owner, '_ui_progress_text', '准备烘焙')
        if hasattr(box, 'progress'):
            box.progress(factor=progress, type='BAR', text=f'{progress:.1%} · {text}')
        else:
            box.label(text=f'{progress:.1%} · {text}')
        box.label(text='Esc 中止', icon='INFO')
    box.operator('bonechain.physics_restore_all', text='恢复全部实时物理（保留烘焙）', icon='LOOP_BACK')
    delete = box.operator('bonechain.physics_restore_all', text='删除插件物理烘焙', icon='TRASH')
    delete.delete_output = True


class BONECHAIN_PT_panel(Panel):
    bl_label = "骨骼链阻尼追踪"
    bl_idname = "BONECHAIN_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "骨骼链"

    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        props = context.scene.bonechain_props
        if not (obj and obj.type == 'ARMATURE' and context.mode == 'POSE'):
            layout.label(text="请在姿态模式下操作", icon='INFO')
            _draw_physics_bake(layout, context)
            return
        props = context.scene.bonechain_props

        # 分组列表
        box = layout.box()
        box.label(text="分组(头发/裙子/飘带…)", icon='GROUP_BONE')
        row = box.row()
        row.template_list("BONECHAIN_UL_groups", "", props, "groups",
                          props, "group_index", rows=3)
        col = row.column(align=True)
        col.operator("bonechain.group_add", text="", icon='ADD')
        col.operator("bonechain.group_remove", text="", icon='REMOVE')

        # 细分骨骼(实验性)
        box = layout.box()
        box.label(text="细分骨骼链(实验性)", icon='MOD_SUBSURF')
        box.label(text="⚠ 不保证稳定,先备份文件", icon='ERROR')
        row = box.row(align=True)
        row.prop(props, "subdiv_count", text="段数")
        box.prop(props, "subdiv_weights")
        box.operator("bonechain.subdivide_bones", text="细分选中骨骼", icon='MOD_SUBSURF')

        grp = _get_active_group(context)
        if not grp:
            layout.label(text="请新建一个分组开始", icon='INFO')
            _draw_physics_bake(layout, context)
            return

        # 柔性组
        box = layout.box()
        box.label(text="柔性骨骼组", icon='CON_TRACKTO')
        box.template_list("BONECHAIN_UL_flex", "", grp, "flex_bones",
                          grp, "flex_index", rows=3)
        row = box.row(align=True)
        row.operator("bonechain.add_flex", text="添加选中", icon='ADD')
        row.operator("bonechain.remove_flex", text="移除", icon='REMOVE')
        row.operator("bonechain.clear_flex", text="清空", icon='TRASH')
        box.label(text="旧版父骨追踪子骨会产生循环", icon='INFO')
        box.operator("bonechain.damped_track", text="① 修复追踪循环",
                     icon='CON_TRACKTO')
        box.label(text="应用时自动接管本组 MMD 跟随", icon='INFO')
        box.operator('bonechain.cancel_physics', icon='CANCEL')
        box.operator('bonechain.restore_mmd', icon='LOOP_BACK')

        # ============ 碰撞设置 (按组切换) ============
        box = layout.box()
        row = box.row()
        row.label(text="碰撞设置", icon='META_CAPSULE')
        # 一键显示隐藏按钮
        row.operator("bonechain.toggle_collision_vis", text="", icon='HIDE_OFF')

        box.prop(grp, "collision_mode", expand=True) # 切换按钮

        if grp.collision_mode == 'RURI':
            box.prop(grp, 'ruri_armature')
            box.label(text="根骨固定，其余已选子骨模拟", icon='INFO')
            box.label(text="形状、粗细与偏移可实时微调", icon='INFO')
            box.operator("bonechain.fill_chain_gaps")
            box.label(text="头部/身体固定骨做碰撞，不添加柔性链骨", icon='INFO')
            box.template_list("BONECHAIN_UL_coll", "ruri", grp, "coll_bones", grp, "coll_index", rows=4)
            box.operator("bonechain.add_coll", text="添加选中碰撞骨")
            row = box.row(align=True)
            row.operator("bonechain.remove_coll", text="移除碰撞骨")
            row.operator("bonechain.clear_coll", text="清空碰撞骨")
            if 0 <= grp.coll_index < len(grp.coll_bones):
                item = grp.coll_bones[grp.coll_index]
                details = box.box()
                details.prop(grp, "ruri_geometry_open", emboss=False,
                             icon='TRIA_DOWN' if grp.ruri_geometry_open else 'TRIA_RIGHT')
                if grp.ruri_geometry_open:
                    details.label(text=item.name, icon='META_CAPSULE')
                    details.prop(item, "ruri_shape")
                    details.label(text="偏移沿绑定骨骼局部 XYZ 轴")
                    details.prop(item, "ruri_offset")
                    details.prop(item, "ruri_rotation")
                    if item.ruri_shape in {'BOX', 'ELLIPSOID'}:
                        details.prop(item, "ruri_dimensions")
                    else:
                        details.prop(item, "radius", text="起点半径" if item.ruri_shape == 'CAPSULE' else "半径")
                        if item.ruri_shape == 'CAPSULE':
                            details.prop(item, "ruri_length_scale")
                            details.prop(item, "ruri_end_offset")
                            details.prop(item, "ruri_split_radius")
                            if item.ruri_split_radius:
                                details.prop(item, "ruri_end_radius")
                    details.operator("bonechain.reset_ruri_geometry", icon='LOOP_BACK')
            arm = _ruri_armature_for_group(context, grp)
            if arm and _ruri_collision_issues(arm, grp):
                box.label(text="碰撞列表含不存在或柔性链中的骨骼", icon='ERROR')
            box.operator("bonechain.clean_ruri_collisions", icon='TRASH')
            box.prop(grp, "wig_radius", text="粒子碰撞半径")
            if hasattr(context.scene, 'ruri_cloth_physics'):
                box.label(text="Ruri 前置已启用", icon='CHECKMARK')
                rs = context.scene.ruri_cloth_physics
                box.prop(rs, "compile_target")
                box.prop(rs, "simulation_frequency")
                box.prop(grp, 'ruri_physics_type')
                if grp.ruri_physics_type == 'BONE_CLOTH':
                    box.prop(grp, 'ruri_simulate_roots')
                    if grp.ruri_simulate_roots:
                        box.label(text='已有父骨固定，列表中的根骨参与模拟', icon='INFO')
                else:
                    box.label(text='所选胸骨原位回弹，不加入父骨', icon='INFO')
                box.operator("bonechain.apply_ruri")
                config = (arm.ruri_cloth_physics.configs.get(grp.ruri_config_name)
                          if arm and grp.ruri_config_name and hasattr(arm, 'ruri_cloth_physics') else None)
                if config and config.cloth_type == 'BONE_SPRING':
                    spring_box = box.box()
                    spring_box.label(text='本组弹簧参数')
                    spring_box.prop(config.spring, 'limit_distance')
                    spring_box.prop(config.spring, 'spring_power')
                    spring_box.prop(config, 'blend_weight')
                    spring_box.prop(config.inertia, 'world_inertia')
                    spring_box.prop(config.inertia, 'local_inertia')
                box.label(text="详细参数：属性编辑器 > 物理 > Ruri", icon='INFO')
            else:
                box.label(text="Ruri 前置未启用", icon='ERROR')
                box.operator('bonechain.enable_ruri')
        elif grp.collision_mode == 'NEW':
            # Wiggle 物理模式 UI
            cbox = box.box()
            cbox.label(text="碰撞骨骼组 (控制源)", icon='BONE_DATA')
            cbox.label(text="身体骨做碰撞，柔性链只做模拟", icon='INFO')
            cbox.template_list("BONECHAIN_UL_coll", "", grp, "coll_bones",
                              grp, "coll_index", rows=3)
            row = cbox.row(align=True)
            row.operator("bonechain.add_coll", text="添加选中", icon='ADD')
            row.operator("bonechain.remove_coll", text="移除", icon='REMOVE')
            row.operator("bonechain.clear_coll", text="清空", icon='TRASH')

            dbox = box.box()
            dbox.label(text="变形骨列表(圆柱目标)", icon='BONE_DATA')
            dbox.operator("bonechain.resolve_deform",
                          text="解析变形骨(控制骨→找变形骨)", icon='ZOOM_SELECTED')
            dbox.template_list("BONECHAIN_UL_deform", "", grp, "deform_targets",
                               grp, "deform_index", rows=3)
            dbox.operator("bonechain.clear_deform", text="清空变形骨", icon='TRASH')

            wbox = box.box()
            wbox.label(text="物理防穿 (Wiggle 2)", icon='PHYSICS')
            if not _wiggle_available(context):
                wbox.label(text="⚠ 未检测到 Wiggle 2,请先安装", icon='ERROR')
            else:
                wbox.prop(context.scene.wiggle, "iterations", text="求解质量")
            wbox.prop(grp, "cyl_radius")
            wbox.operator("bonechain.gen_cylinders", text="① 生成碰撞圆柱", icon='MESH_CYLINDER')

            col = wbox.column(align=True)
            col.prop(grp, "wig_radius")
            col.prop(grp, "wig_mass")
            col.prop(grp, "wig_stiff")
            col.prop(grp, "wig_damp")
            col.prop(grp, "wig_stretch")
            wbox.prop(grp, "wig_clean")
            wbox.prop(grp, "wig_base_keys")
            wbox.label(text="阻尼追踪与旧碰撞会被停用", icon='INFO')
            wbox.operator("bonechain.apply_wiggle", text="② 应用Wiggle防穿", icon='FORCE_TURBULENCE')

            wbox.label(text="配好后按播放键预览摆动+防穿", icon='INFO')

        else:
            # 限制距离约束 (老版本) UI
            cbox = box.box()
            cbox.label(text="碰撞骨骼组 (胶囊/球)", icon='META_CAPSULE')
            cbox.template_list("BONECHAIN_UL_coll", "", grp, "coll_bones",
                              grp, "coll_index", rows=4)
            row = cbox.row(align=True)
            row.operator("bonechain.add_coll", text="添加选中", icon='ADD')
            row.operator("bonechain.remove_coll", text="移除", icon='REMOVE')
            row.operator("bonechain.clear_coll", text="清空", icon='TRASH')

            lbox = box.box()
            lbox.label(text="限制距离防穿(非物理)", icon='CON_DISTLIMIT')
            lbox.label(text="粗=半径 细分=球数 强度=逐骨骼调", icon='INFO')
            if 0 <= grp.coll_index < len(grp.coll_bones):
                item = grp.coll_bones[grp.coll_index]
                lbox.prop(item, "fill_gaps")
                lbox.prop(item, "margin")
                arm = context.active_object
                pb = arm.pose.bones.get(item.name) if arm and arm.type == 'ARMATURE' else None
                if pb:
                    lbox.label(text=f"实际球半径: {_collision_radius(item, pb.length):.4f}")
            lbox.label(text="球为实际碰撞范围，圆柱仅作参考", icon='INFO')
            lbox.label(text="缩放骨架后请重新应用碰撞", icon='INFO')
            lbox.operator("bonechain.apply_collision", text="② 应用老版防穿", icon='CON_DISTLIMIT')



        _draw_physics_bake(layout, context)

# ===============================================================
classes = (
    BoneItem, CollisionItem, ChainGroup, BoneChainProps,
    BONECHAIN_OT_toggle_collision_vis,  # 新增的可见性开关算子
    BONECHAIN_OT_group_add, BONECHAIN_OT_group_remove,
    BONECHAIN_OT_add_flex, BONECHAIN_OT_remove_flex, BONECHAIN_OT_clear_flex,
    BONECHAIN_OT_add_coll, BONECHAIN_OT_resolve_deform,
    BONECHAIN_OT_clear_deform,
    BONECHAIN_OT_remove_coll, BONECHAIN_OT_clear_coll,
    BONECHAIN_OT_reset_ruri_geometry,
    BONECHAIN_OT_clean_ruri_collisions,
    BONECHAIN_OT_damped_track, BONECHAIN_OT_apply_collision,
    BONECHAIN_OT_subdivide_bones,
    BONECHAIN_OT_gen_cylinders, BONECHAIN_OT_apply_wiggle,
    BONECHAIN_OT_restore_mmd, BONECHAIN_OT_cancel_physics,
    BONECHAIN_OT_apply_ruri, BONECHAIN_OT_enable_ruri,
    BONECHAIN_OT_fill_chain_gaps,
    BONECHAIN_OT_wiggle_reset, BONECHAIN_OT_wiggle_bake, BONECHAIN_OT_wiggle_cancel_bake,
    BONECHAIN_OT_ruri_reset, BONECHAIN_OT_ruri_bake, BONECHAIN_OT_ruri_cancel_bake,
    BONECHAIN_OT_ruri_delete_bake, BONECHAIN_OT_ruri_scene_range,
    BONECHAIN_OT_physics_bake_range, BONECHAIN_OT_physics_bake,
    BONECHAIN_OT_physics_bake_isolated, BONECHAIN_OT_physics_restore_all,
    BONECHAIN_UL_groups, BONECHAIN_UL_flex, BONECHAIN_UL_coll,
    BONECHAIN_UL_deform,
    BONECHAIN_PT_panel,
)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.bonechain_props = PointerProperty(type=BoneChainProps)
    bpy.types.Object.bonechain_wiggle_bake_restore = PointerProperty(type=bpy.types.Action)
    bpy.types.Object.bonechain_wiggle_bake_output = PointerProperty(type=bpy.types.Action)
    bpy.types.Object.bonechain_wiggle_bake_saved = BoolProperty(default=False)
    bpy.types.Object.bonechain_wiggle_bake_slot = IntProperty(default=0)
    bpy.types.Object.bonechain_wiggle_bake_influence = FloatProperty(default=1.0)
    bpy.types.Object.bonechain_wiggle_bake_blend = StringProperty(default='REPLACE')
    bpy.types.Object.bonechain_ruri_bake_restore = PointerProperty(type=bpy.types.Action)
    bpy.types.Object.bonechain_ruri_bake_output = PointerProperty(type=bpy.types.Action)
    bpy.types.Object.bonechain_ruri_bake_saved = BoolProperty(default=False)
    bpy.types.Object.bonechain_ruri_bake_slot = IntProperty(default=0)
    bpy.types.Object.bonechain_ruri_bake_influence = FloatProperty(default=1.0)
    bpy.types.Object.bonechain_ruri_bake_blend = StringProperty(default='REPLACE')
    bpy.types.Object.bonechain_ruri_bake_bones = StringProperty(default='')
    bpy.types.Object.bonechain_ruri_bake_scene = PointerProperty(type=bpy.types.Scene)


def unregister():
    if _BONECHAIN_BAKE_OWNER is not None:
        _BONECHAIN_BAKE_OWNER._finish(bpy.context, cancelled=True)
    for name in ('restore', 'output', 'saved', 'slot', 'influence', 'blend'):
        delattr(bpy.types.Object, 'bonechain_wiggle_bake_' + name)
    for name in ('restore', 'output', 'saved', 'slot', 'influence', 'blend', 'bones', 'scene'):
        delattr(bpy.types.Object, 'bonechain_ruri_bake_' + name)
    del bpy.types.Scene.bonechain_props
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
