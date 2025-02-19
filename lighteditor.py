import bpy
import fnmatch
from bpy.props import (
    BoolProperty,
    IntProperty,
    FloatProperty,
    StringProperty,
    EnumProperty,
)
from bpy.app.handlers import persistent
from bpy.app.translations import contexts as i18n_contexts
import re, os
from . icons import get_icon_id

# Global variables to track active checkboxes
current_active_light = None
current_exclusive_group = None

# Global dictionaries to track states
group_checkbox_1_state = {}      # Used for first button (toggle group on/off)
group_lights_original_state = {} # Stores original light states for each group
group_collapse_dict = {}         # Whether each group is collapsed
collections_with_lights = {}     # Tracks collections that contain lights

# New globals for the exclusive (second) button
group_checkbox_2_state = {}      # Exclusive button state per group (False by default)
other_groups_original_state = {} # Stores the other groups' light states

# -------------------------------------------------------------------------
# Render layer functions
# -------------------------------------------------------------------------
def update_render_layer(self, context):
    selected = self.selected_render_layer
    for vl in context.scene.view_layers:
        if vl.name == selected:
            context.window.view_layer = vl
            break

def get_render_layer_items(self, context):
    items = []
    for view_layer in context.scene.view_layers:
        items.append((view_layer.name, view_layer.name, ""))
    return items

# -------------------------------------------------------------------------
# Collection functions
# -------------------------------------------------------------------------
def gather_layer_collections(parent_lc, result):
    """Recursively gather all LayerCollections in the parent_lc hierarchy."""
    result.append(parent_lc)
    for child in parent_lc.children:
        gather_layer_collections(child, result)

def get_layer_collection_by_name(layer_collection, coll_name):
    """Recursively search for a LayerCollection with the given name."""
    if layer_collection.collection.name == coll_name:
        return layer_collection
    for child in layer_collection.children:
        found = get_layer_collection_by_name(child, coll_name)
        if found:
            return found
    return None

# -------------------------------------------------------------------------
# Light update functions
# -------------------------------------------------------------------------
def update_light_enabled(self, context):
    self.hide_viewport = not self.light_enabled
    self.hide_render = not self.light_enabled

def update_light_turn_off_others(self, context):
    scene = context.scene
    if self.light_turn_off_others:
        if scene.current_active_light and scene.current_active_light != self:
            scene.current_active_light.light_turn_off_others = False
        scene.current_active_light = self
        for obj in context.view_layer.objects:
            if obj.type == 'LIGHT' and obj.name != self.name:
                if 'prev_light_enabled' not in obj:
                    obj['prev_light_enabled'] = obj.light_enabled
                obj.light_enabled = False
    else:
        if scene.current_active_light == self:
            scene.current_active_light = None
        for obj in context.view_layer.objects:
            if obj.type == 'LIGHT' and obj.name != self.name:
                if 'prev_light_enabled' in obj:
                    obj.light_enabled = obj['prev_light_enabled']
                    del obj['prev_light_enabled']

def get_all_collections(obj):
    def _get_collections_recursive(collection, path=None):
        if path is None:
            path = []
        path.append(collection.name)
        yield path[:]
        for child in collection.children:
            yield from _get_collections_recursive(child, path)
        path.pop()
    all_collections = set()
    for collection in obj.users_collection:
        for path in _get_collections_recursive(collection):
            all_collections.add(" > ".join(path))
    return sorted(all_collections)

# -------------------------------------------------------------------------
# Mutually exclusive grouping toggles
# -------------------------------------------------------------------------
def update_group_by_kind(self, context):
    if self.light_editor_kind_alpha:
        self.light_editor_group_by_collection = False

def update_group_by_collection(self, context):
    if self.light_editor_group_by_collection:
        self.light_editor_kind_alpha = False

# -------------------------------------------------------------------------
# Device functions (for cycles)
# -------------------------------------------------------------------------
def get_device_type(context):
    return context.preferences.addons['cycles'].preferences.compute_device_type

def backend_has_active_gpu(context):
    return context.preferences.addons['cycles'].preferences.has_active_device()

def use_metal(context):
    cscene = context.scene.cycles
    return (get_device_type(context) == 'METAL' and cscene.device == 'GPU' and backend_has_active_gpu(context))

def use_mnee(context):
    if use_metal(context):
        import platform
        version, _, _ = platform.mac_ver()
        major_version = version.split(".")[0]
        if int(major_version) < 13:
            return False
    return True

# -------------------------------------------------------------------------
# Extra parameters drawing function (unchanged)
# -------------------------------------------------------------------------
def draw_extra_params(self, box, obj, light):
    if light and isinstance(light, bpy.types.Light):
        layout = box
        row = layout.row()
        row.prop(light, "type", expand=True)
        col = layout.column()
        col.separator()
        if bpy.context.engine == 'CYCLES':
            clamp = light.cycles
            if light.type in {'POINT', 'SPOT'}:
                col.prop(light, "use_soft_falloff")
                col.prop(light, "shadow_soft_size", text="Radius")
            elif light.type == 'SUN':
                col.prop(light, "angle")
            elif light.type == 'AREA':
                col.prop(light, "shape", text="Shape")
                sub = col.column(align=True)
                if light.shape in {'SQUARE', 'DISK'}:
                    sub.prop(light, "size")
                elif light.shape in {'RECTANGLE', 'ELLIPSE'}:
                    sub.prop(light, "size", text="Size X")
                    sub.prop(light, "size_y", text="Y")
            if not (light.type == 'AREA' and clamp.is_portal):
                col.separator()
                sub = col.column()
                sub.prop(clamp, "max_bounces")
            sub = col.column(align=True)
            sub.active = not (light.type == 'AREA' and clamp.is_portal)
            sub.prop(light, "use_shadow", text="Cast Shadow")
            sub.prop(clamp, "use_multiple_importance_sampling", text="Multiple Importance")
            if use_mnee(bpy.context):
                sub.prop(clamp, "is_caustics_light", text="Shadow Caustics")
            if light.type == 'AREA':
                col.prop(clamp, "is_portal", text="Portal")
            if light.type == 'SPOT':
                col.separator()
                row = col.row(align=True)
                row.alignment = 'CENTER'
                row.label(text="Spot Shape")
                col.prop(light, "spot_size", text="Spot Size")
                col.prop(light, "spot_blend", text="Blend", slider=True)
                col.prop(light, "show_cone")
            elif light.type == 'AREA':
                col.separator()
                row = col.row(align=True)
                row.alignment = 'CENTER'
                row.label(text="Beam Shape")
                col.prop(light, "spread", text="Spread")
        if ((bpy.context.engine == 'BLENDER_EEVEE') or (bpy.context.engine == 'BLENDER_EEVEE_NEXT')):
            col.separator()
            if light.type in {'POINT', 'SPOT'}:
                col.prop(light, "use_soft_falloff")
                col.prop(light, "shadow_soft_size", text="Radius")
            elif light.type == 'SUN':
                col.prop(light, "angle")
            elif light.type == 'AREA':
                col.prop(light, "shape")
                sub = col.column(align=True)
                if light.shape in {'SQUARE', 'DISK'}:
                    sub.prop(light, "size")
                elif light.shape in {'RECTANGLE', 'ELLIPSE'}:
                    sub.prop(light, "size", text="Size X")
                    sub.prop(light, "size_y", text="Y")
            if bpy.context.engine == 'BLENDER_EEVEE_NEXT':
                col.separator()
                col.prop(light, "use_shadow", text="Cast Shadow")
                col.prop(light, "use_shadow_jitter")
                col.prop(light, "shadow_jitter_overblur", text="Overblur")
                col.prop(light, "shadow_filter_radius", text="Radius")
                col.prop(light, "shadow_maximum_resolution", text="Resolution Limit")
            if light and light.type == 'SPOT':
                col.separator()
                row = col.row(align=True)
                row.alignment = 'CENTER'
                row.label(text="Spot Shape")
                col.prop(light, "spot_size", text="Size")
                col.prop(light, "spot_blend", text="Blend", slider=True)
                col.prop(light, "show_cone")
            col.separator()
            col.prop(light, "diffuse_factor", text="Diffuse")
            col.prop(light, "specular_factor", text="Specular")
            col.prop(light, "volume_factor", text="Volume", text_ctxt=i18n_contexts.id_id)
            if light.type != 'SUN':
                col.separator()
                sub = col.column()
                sub.prop(light, "use_custom_distance", text="Custom Distance")
                sub.active = light.use_custom_distance
                sub.prop(light, "cutoff_distance", text="Distance")

# -------------------------------------------------------------------------
# Operators
# -------------------------------------------------------------------------
class LIGHT_OT_ToggleGroup(bpy.types.Operator):
    """Collapse or expand a group header."""
    bl_idname = "light_editor.toggle_group"
    bl_label = "Toggle Group"
    group_key: bpy.props.StringProperty()
    def execute(self, context):
        current = group_collapse_dict.get(self.group_key, False)
        group_collapse_dict[self.group_key] = not current
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
        return {'FINISHED'}

class LIGHT_OT_ToggleCollection(bpy.types.Operator):
    """Toggle the exclusion state of a collection (and its sub-collections)."""
    bl_idname = "light_editor.toggle_collection"
    bl_label = "Toggle Collection Exclusion"
    group_key: bpy.props.StringProperty()
    def execute(self, context):
        global collections_with_lights
        if self.group_key.startswith("coll_"):
            coll_name = self.group_key[5:]
            collection = bpy.data.collections.get(coll_name)
            if not collection:
                return {'CANCELLED'}
            def toggle_exclusion_recursive(layer_coll, exclude):
                layer_coll.exclude = exclude
                for child in layer_coll.children:
                    toggle_exclusion_recursive(child, exclude)
            layer_collection = get_layer_collection_by_name(context.view_layer.layer_collection, coll_name)
            if layer_collection:
                current_excluded = layer_collection.exclude
                toggle_exclusion_recursive(layer_collection, not current_excluded)
                def has_lights(coll):
                    return any(obj.type == 'LIGHT' for obj in coll.all_objects)
                if has_lights(collection):
                    collections_with_lights[coll_name] = True
                else:
                    collections_with_lights.pop(coll_name, None)
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
        return {'FINISHED'}

class LIGHT_OT_ToggleKind(bpy.types.Operator):
    """Toggle all lights of a specific kind."""
    bl_idname = "light_editor.toggle_kind"
    bl_label = "Toggle Kind Visibility"
    group_key: bpy.props.StringProperty()
    def execute(self, context):
        global group_checkbox_1_state, group_lights_original_state
        is_on = group_checkbox_1_state.get(self.group_key, True)
        group_objs = self._get_group_objects(context, self.group_key)
        if is_on:
            original_states = {}
            for obj in group_objs:
                if obj.type == 'LIGHT':
                    original_states[obj.name] = obj.light_enabled
                    obj.light_enabled = False
            group_lights_original_state[self.group_key] = original_states
        else:
            original_states = group_lights_original_state.get(self.group_key, {})
            for obj in group_objs:
                if obj.type == 'LIGHT':
                    obj.light_enabled = original_states.get(obj.name, True)
            if self.group_key in group_lights_original_state:
                del group_lights_original_state[self.group_key]
        group_checkbox_1_state[self.group_key] = not is_on
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
        return {'FINISHED'}
    def _get_group_objects(self, context, group_key):
        filter_pattern = context.scene.light_editor_filter.lower()
        all_lights = [obj for obj in context.view_layer.objects if obj.type == 'LIGHT']
        if filter_pattern:
            all_lights = [obj for obj in all_lights if re.search(filter_pattern, obj.name, re.I)]
        kind = group_key[5:]
        return [obj for obj in all_lights if obj.data.type == kind]

class LIGHT_OT_ToggleGroupExclusive(bpy.types.Operator):
    """
    Toggle exclusive mode for a group.
    When turned on, lights in all other groups are turned off.
    When turned off, the previous states of the other groups are restored.
    """
    bl_idname = "light_editor.toggle_group_exclusive"
    bl_label = "Toggle Group Exclusive"
    group_key: bpy.props.StringProperty()
    def execute(self, context):
        global current_exclusive_group, group_checkbox_2_state, other_groups_original_state
        is_on = group_checkbox_2_state.get(self.group_key, False)
        if not is_on:
            if current_exclusive_group and current_exclusive_group != self.group_key:
                saved = other_groups_original_state.get(current_exclusive_group, {})
                for gk, states in saved.items():
                    objs = self._get_group_objects(context, gk)
                    for obj in objs:
                        obj.light_enabled = states.get(obj.name, True)
                group_checkbox_2_state[current_exclusive_group] = False
                if current_exclusive_group in other_groups_original_state:
                    del other_groups_original_state[current_exclusive_group]
            others = self._get_all_other_groups(context, self.group_key)
            saved = {}
            for gk in others:
                objs = self._get_group_objects(context, gk)
                saved[gk] = {obj.name: obj.light_enabled for obj in objs}
                for obj in objs:
                    obj.light_enabled = False
            other_groups_original_state[self.group_key] = saved
            group_checkbox_2_state[self.group_key] = True
            current_exclusive_group = self.group_key
        else:
            saved = other_groups_original_state.get(self.group_key, {})
            for gk, states in saved.items():
                objs = self._get_group_objects(context, gk)
                for obj in objs:
                    obj.light_enabled = states.get(obj.name, True)
            if self.group_key in other_groups_original_state:
                del other_groups_original_state[self.group_key]
            group_checkbox_2_state[self.group_key] = False
            current_exclusive_group = None
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
        return {'FINISHED'}
    def _get_all_other_groups(self, context, current_key):
        all_groups = self._get_all_group_keys(context)
        return [k for k in all_groups if k != current_key]
    def _get_all_group_keys(self, context):
        scene = context.scene
        filter_type = scene.filter_light_types
        keys = set()
        if filter_type == 'COLLECTION':
            for obj in context.view_layer.objects:
                if obj.type == 'LIGHT':
                    if obj.users_collection:
                        keys.add("coll_" + obj.users_collection[0].name)
                    else:
                        keys.add("coll_No Collection")
        elif filter_type == 'KIND':
            for obj in context.view_layer.objects:
                if obj.type == 'LIGHT':
                    keys.add("kind_" + obj.data.type)
        elif filter_type == 'GROUP':
            for obj in context.view_layer.objects:
                if obj.type == 'LIGHT':
                    grp = getattr(obj, "lightgroup", "")
                    if not grp:
                        grp = "Not Assigned"
                    keys.add("group_" + grp)
        return list(keys)
    def _get_group_objects(self, context, group_key):
        scene = context.scene
        filter_pattern = scene.light_editor_filter.lower()
        all_lights = [obj for obj in context.view_layer.objects if obj.type == 'LIGHT']
        if filter_pattern:
            all_lights = [obj for obj in all_lights if re.search(filter_pattern, obj.name, re.I)]
        if scene.filter_light_types == 'COLLECTION' and group_key.startswith("coll_"):
            coll_name = group_key[5:]
            return [obj for obj in all_lights if (obj.users_collection and obj.users_collection[0].name == coll_name) or (not obj.users_collection and coll_name == "No Collection")]
        elif scene.filter_light_types == 'KIND' and group_key.startswith("kind_"):
            kind = group_key[5:]
            return [obj for obj in all_lights if obj.data.type == kind]
        elif scene.filter_light_types == 'GROUP' and group_key.startswith("group_"):
            grp_name = group_key[6:]
            if grp_name == "Not Assigned":
                return [obj for obj in all_lights if not getattr(obj, "lightgroup", "")]
            else:
                return [obj for obj in all_lights if getattr(obj, "lightgroup", "") == grp_name]
        return []

class LIGHT_OT_ClearFilter(bpy.types.Operator):
    """Clear Filter Types"""
    bl_idname = "le.clear_light_filter"
    bl_label = "Clear Filter"
    @classmethod
    def description(cls, context, properties):
        scene = context.scene
        if scene.filter_light_types == 'COLLECTION':
            return "Turn ON/OFF Collection"
        elif scene.filter_light_types == 'KIND':
            return "Turn ON/OFF All Lights of This Kind"
        return "Toggle all lights in the group off or restore them"
    def execute(self, context):
        context.scene.light_editor_filter = ""
        return {'FINISHED'}

class LIGHT_OT_SelectLight(bpy.types.Operator):
    """Selects light from UI and deselects everything else"""
    bl_idname = "le.select_light"
    bl_label = "Select Light"
    name : StringProperty()
    def execute(self, context):
        vob = context.view_layer.objects
        if self.name in vob:
            light = vob[self.name]
            if light.select_get():
                bpy.ops.object.select_all(action='DESELECT')
                self.report({'INFO'}, f"Deselected all objects")
            else:
                bpy.ops.object.select_all(action='DESELECT')
                light.select_set(True)
                vob.active = light
                self.report({'INFO'}, f"Selected light: {self.name}")
        else:
            self.report({'ERROR'}, f"Light '{self.name}' not found")
        return {'FINISHED'}

# -------------------------------------------------------------------------
# Main row UI drawing function
# -------------------------------------------------------------------------
def draw_main_row(box, obj):
    light = obj.data
    context = bpy.context
    scene = bpy.context.scene
    row = box.row(align=True)
    controls_row = row.row(align=True)
    controls_row.prop(obj, "light_enabled", text="",
            icon="OUTLINER_OB_LIGHT" if obj.light_enabled else "LIGHT_DATA")
    controls_row.active = (obj.light_enabled == True)
    controls_row.prop(obj, "light_turn_off_others", text="",
            icon="RADIOBUT_ON" if obj.light_turn_off_others else "RADIOBUT_OFF")
    if not scene.filter_light_types == 'GROUP':
        selected_true = get_icon_id("select_true")
        selected_false = get_icon_id("select_false")
        controls_row.operator("le.select_light", text="",
                icon_value=selected_true if obj.select_get() else selected_false).name = obj.name
    if not scene.filter_light_types == 'GROUP':
        controls_row.prop(obj, "light_expanded", text="",
                          emboss=True,
                          icon='DOWNARROW_HLT' if obj.light_expanded else 'RIGHTARROW')
    col_name = row.column(align=True)
    col_name.scale_x = 0.4
    col_name.prop(obj, "name", text="")
    col_color = row.column(align=True)
    col_color.scale_x = 0.25
    col_color.prop(light, "color", text="")
    col_energy = row.column(align=True)
    col_energy.scale_x = 0.35
    col_energy.prop(light, "energy", text="")

# -------------------------------------------------------------------------
# Main panel
# -------------------------------------------------------------------------
class LIGHT_PT_editor(bpy.types.Panel):
    """Panel to view/edit lights with grouping, filtering, and per‐group toggles."""
    bl_label = "Light Editor"
    bl_idname = "LIGHT_PT_editor"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Light Editor"
    def draw(self, context):
        layout = self.layout
        scene = context.scene
        layout.row().prop(scene, "filter_light_types", expand=True)
        layout.use_property_split = True
        layout.use_property_decorate = False
        row = layout.row(align=True)
        row.prop(scene, "light_editor_filter", text="", icon="VIEWZOOM")
        row.operator("le.clear_light_filter", text="", icon='PANEL_CLOSE')
        
        # Render layer dropdown in COLLECTION mode:
        if scene.filter_light_types == 'COLLECTION':
            row = layout.row()
            row.prop(scene, "selected_render_layer", text="Render Layer")
        
        # COLLECTION mode: group by collection
        if scene.filter_light_types == 'COLLECTION':
            all_layer_colls = []
            gather_layer_collections(context.view_layer.layer_collection, all_layer_colls)
            relevant_layer_colls = []
            for lc in all_layer_colls:
                coll = lc.collection
                if coll.name == "Scene Collection":
                    continue
                if any(obj.type == 'LIGHT' for obj in coll.all_objects):
                    relevant_layer_colls.append(lc)
            if not relevant_layer_colls:
                box = layout.box()
                box.label(text="No Collections Found", icon='ERROR')
            else:
                for lc in relevant_layer_colls:
                    coll = lc.collection
                    coll_name = coll.name
                    group_key = f"coll_{coll_name}"
                    collapsed = group_collapse_dict.get(group_key, False)
                    header_box = layout.box()
                    header_row = header_box.row(align=True)
                    is_included = not lc.exclude
                    icon_1 = 'CHECKBOX_HLT' if is_included else 'CHECKBOX_DEHLT'
                    header_row.active = True
                    op_1 = header_row.operator("light_editor.toggle_collection",
                                               text="",
                                               icon=icon_1,
                                               depress=is_included)
                    op_1.group_key = group_key
                    # Exclusive toggle button: icon is RADIOBUT_ON when active, RECORD_OFF when off.
                    is_on_2 = group_checkbox_2_state.get(group_key, False)
                    icon_2 = 'RADIOBUT_ON' if is_on_2 else 'RECORD_OFF'
                    op_excl = header_row.operator("light_editor.toggle_group_exclusive",
                                                  text="",
                                                  icon=icon_2,
                                                  depress=is_on_2)
                    op_excl.group_key = group_key
                    op_tri = header_row.operator("light_editor.toggle_group",
                                                 text="",
                                                 emboss=True,
                                                 icon='RIGHTARROW' if collapsed else 'DOWNARROW_HLT')
                    op_tri.group_key = group_key
                    header_row.label(text=coll_name, icon='OUTLINER_COLLECTION')
                    if not collapsed:
                        lights_in_coll = [o for o in coll.all_objects if o.type == 'LIGHT']
                        filter_str = scene.light_editor_filter.strip()
                        if filter_str:
                            lights_in_coll = [o for o in lights_in_coll if re.search(filter_str.lower(), o.name.lower())]
                        for obj in lights_in_coll:
                            draw_main_row(header_box, obj)
                            if obj.light_expanded:
                                extra_box = header_box.box()
                                draw_extra_params(self, extra_box, obj, obj.data)
        
        # KIND mode: group by kind
        elif scene.filter_light_types == 'KIND':
            if scene.light_editor_filter:
                lights = [obj for obj in context.view_layer.objects if obj.type == 'LIGHT' and re.search(scene.light_editor_filter.lower(), obj.name.lower(), re.I)]
            else:
                lights = [obj for obj in context.view_layer.objects if obj.type == 'LIGHT']
            groups = {'POINT': [], 'SPOT': [], 'SUN': [], 'AREA': []}
            for obj in lights:
                if obj.data.type in groups:
                    groups[obj.data.type].append(obj)
            if any(groups.values()):
                for kind in ('POINT', 'SPOT', 'SUN', 'AREA'):
                    group_objs = groups[kind]
                    if group_objs:
                        group_key = f"kind_{kind}"
                        collapsed = group_collapse_dict.get(group_key, False)
                        header_box = layout.box()
                        header_row = header_box.row(align=True)
                        is_on_1 = group_checkbox_1_state.get(group_key, True)
                        icon_1 = 'CHECKBOX_HLT' if is_on_1 else 'CHECKBOX_DEHLT'
                        header_row.active = is_on_1
                        op_1 = header_row.operator("light_editor.toggle_kind",
                                                   text="",
                                                   icon=icon_1,
                                                   depress=is_on_1)
                        op_1.group_key = group_key
                        # Exclusive toggle button for kind: icon is RADIOBUT_ON when active, RECORD_OFF when off.
                        is_on_2 = group_checkbox_2_state.get(group_key, False)
                        icon_2 = 'RADIOBUT_ON' if is_on_2 else 'RECORD_OFF'
                        op_excl_kind = header_row.operator("light_editor.toggle_group_exclusive",
                                                            text="",
                                                            icon=icon_2,
                                                            depress=is_on_2)
                        op_excl_kind.group_key = group_key
                        op_tri = header_row.operator("light_editor.toggle_group",
                                                     text="",
                                                     emboss=True,
                                                     icon='RIGHTARROW' if collapsed else 'DOWNARROW_HLT')
                        op_tri.group_key = group_key
                        header_row.label(text=f"{kind} Lights", icon=f"LIGHT_{kind}")
                        if not collapsed:
                            for obj in group_objs:
                                draw_main_row(header_box, obj)
                                if obj.light_expanded:
                                    extra_box = header_box.box()
                                    draw_extra_params(self, extra_box, obj, obj.data)
            else:
                box = layout.box()
                box.label(text="No Lights Found", icon='ERROR')
        
        # ALPHABETICAL mode: list all lights sorted by name
        else:
            if scene.light_editor_filter:
                lights = [obj for obj in context.view_layer.objects if obj.type == 'LIGHT' and re.search(scene.light_editor_filter.lower(), obj.name.lower(), re.I)]
            else:
                lights = [obj for obj in context.view_layer.objects if obj.type == 'LIGHT']
            if lights:
                box = layout.box()
                box.label(text="All Lights (Alphabetical)", icon='LIGHT_DATA')
                sorted_lights = sorted(lights, key=lambda o: o.name.lower())
                for obj in sorted_lights:
                    draw_main_row(box, obj)
                    if obj.light_expanded:
                        extra_box = box.box()
                        draw_extra_params(self, extra_box, obj, obj.data)
            else:
                box = layout.box()
                box.label(text="No Lights Found", icon='ERROR')

# -------------------------------------------------------------------------
# Handlers
# -------------------------------------------------------------------------
@persistent
def LE_check_lights_enabled(dummy):
    for obj in bpy.data.objects:
        if obj.type == 'LIGHT':
            if (obj.hide_viewport and obj.hide_render):
                if obj.name in bpy.context.view_layer.objects:
                    bpy.context.view_layer.objects[obj.name].light_enabled = False
            else:
                if obj.name in bpy.context.view_layer.objects:
                    bpy.context.view_layer.objects[obj.name].light_enabled = True

@persistent
def LE_clear_handler(dummy):
    context = bpy.context
    for obj in bpy.data.objects:
        if obj.type == 'LIGHT':
            if not (obj.hide_viewport or obj.hide_render):
                if obj.name in context.view_layer.objects:
                    context.view_layer.objects[obj.name].light_enabled = True
            else:
                if obj.name in context.view_layer.objects:
                    context.view_layer.objects[obj.name].light_enabled = False

# -------------------------------------------------------------------------
# Registration
# -------------------------------------------------------------------------
classes = (
    LIGHT_OT_ToggleGroup,
    LIGHT_OT_ToggleCollection,
    LIGHT_OT_ToggleKind,
    LIGHT_OT_ToggleGroupExclusive,
    LIGHT_OT_ClearFilter,
    LIGHT_OT_SelectLight,
    LIGHT_PT_editor,
)

def register():
    print("Light Editor add-on registered successfully.")
    bpy.types.Scene.current_active_light = bpy.props.PointerProperty(type=bpy.types.Object)
    bpy.types.Scene.current_exclusive_group = bpy.props.StringProperty()
    bpy.types.Scene.selected_render_layer = EnumProperty(
        name="Render Layer",
        description="Select the render layer",
        items=get_render_layer_items,
        update=update_render_layer
    )
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.light_editor_filter = StringProperty(
        name="Filter",
        default="",
        description="Filter lights by name (regex allowed)"
    )
    bpy.types.Scene.light_editor_kind_alpha = BoolProperty(
        name="By Kind",
        description="Group lights by kind",
        default=False,
        update=update_group_by_kind
    )
    bpy.types.Scene.light_editor_group_by_collection = BoolProperty(
        name="By Collections",
        description="Group lights by collection",
        default=False,
        update=update_group_by_collection
    )
    bpy.types.Scene.filter_light_types = EnumProperty(
        name="Type",
        description="Filter light by type",
        items=(('NO_FILTER', 'All', 'Show All (Alphabetical)', 'NONE', 0), 
               ('KIND', 'Kind', 'Filter lights by Kind', 'LIGHT_DATA', 1),
               ('COLLECTION', 'Collection', 'Filter lights by Collections', 'OUTLINER_COLLECTION', 2)),
        default='NO_FILTER'
    )
    bpy.types.Light.soft_falloff = BoolProperty(default=False)
    bpy.types.Light.max_bounce = IntProperty(default=0, min=0, max=10)
    bpy.types.Light.multiple_instance = BoolProperty(default=False)
    bpy.types.Light.shadow_caustic = BoolProperty(default=False)
    bpy.types.Light.spread = FloatProperty(default=0.0, min=0.0, max=1.0)
    bpy.types.Object.light_enabled = BoolProperty(
        name="Enabled",
        default=True,
        update=update_light_enabled
    )
    bpy.types.Object.light_turn_off_others = BoolProperty(
        name="Turn Off Others",
        default=False,
        update=update_light_turn_off_others
    )
    bpy.types.Object.light_expanded = BoolProperty(
        name="Expanded",
        default=False
    )
    bpy.app.handlers.load_post.append(LE_clear_handler)
    bpy.app.handlers.load_post.append(LE_check_lights_enabled)

def unregister():
    global custom_icons
    if custom_icons:
        bpy.utils.previews.remove(custom_icons)
        custom_icons = None
    if LE_clear_handler in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(LE_clear_handler)
    if LE_check_lights_enabled in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(LE_check_lights_enabled)
    if hasattr(bpy.types.Scene, 'current_active_light'):
        del bpy.types.Scene.current_active_light
    if hasattr(bpy.types.Scene, 'current_exclusive_group'):
        del bpy.types.Scene.current_exclusive_group
    if hasattr(bpy.types.Scene, 'selected_render_layer'):
        del bpy.types.Scene.selected_render_layer
    if hasattr(bpy.types.Scene, 'light_editor_filter'):
        del bpy.types.Scene.light_editor_filter
    if hasattr(bpy.types.Scene, 'light_editor_kind_alpha'):
        del bpy.types.Scene.light_editor_kind_alpha
    if hasattr(bpy.types.Scene, 'light_editor_group_by_collection'):
        del bpy.types.Scene.light_editor_group_by_collection
    if hasattr(bpy.types.Scene, 'filter_light_types'):
        del bpy.types.Scene.filter_light_types
    if hasattr(bpy.types.Light, 'soft_falloff'):
        del bpy.types.Light.soft_falloff
    if hasattr(bpy.types.Light, 'max_bounce'):
        del bpy.types.Light.max_bounce
    if hasattr(bpy.types.Light, 'multiple_instance'):
        del bpy.types.Light.multiple_instance
    if hasattr(bpy.types.Light, 'shadow_caustic'):
        del bpy.types.Light.shadow_caustic
    if hasattr(bpy.types.Light, 'spread'):
        del bpy.types.Light.spread
    if hasattr(bpy.types.Object, 'light_enabled'):
        del bpy.types.Object.light_enabled
    if hasattr(bpy.types.Object, 'light_turn_off_others'):
        del bpy.types.Object.light_turn_off_others
    if hasattr(bpy.types.Object, 'light_expanded'):
        del bpy.types.Object.light_expanded
    for cls in reversed(classes):
        if hasattr(bpy.types, cls.__name__):
            bpy.utils.unregister_class(cls)

if __name__ == "__main__":
    register()
