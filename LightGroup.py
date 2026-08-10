bl_info = {
    "name": "Light Group Editor",
    "author": "Robert Rioux",
    "version": (1, 0, 1),  # Incremented version
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > Light Editor",
    "description": "Manage Cycles Light Groups with environment support",
    "category": "Lighting",
}

# LightGroup.py — Environment (World) visibility + selectable checkbox
# This file reflects the previous patch (show World in group lists) and adds a checkbox
# to select the Environment so it can be reassigned via Assign/Unassign like lights.

import bpy
from bpy.types import Operator, Panel
from bpy.props import StringProperty
from bpy.app.handlers import persistent

# Single source of truth for "does this node actually emit light", shared with
# the Light Editor panel so both lists agree. Safe at import time: __init__.py
# imports LightEditor first and LightEditor never imports this module.
from .LightEditor import is_emissive_node_active

# -------------------------------------------------------------------------
# Scene-scoped state
# -------------------------------------------------------------------------
# Dictionaries for collapse and exclusivity (persist across redraws).
# These are attached to bpy.types.Scene in register() rather than at module
# import time: unregister() deletes them, and on a re-enable the module is
# already imported so import-time assignments would never run again.
#
# Backup of per-light visibility taken when a group is soloed, so that
# un-soloing restores the user's original state instead of blanket-clearing it.
_exclusive_visibility_backup = {}

# -------------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------------
def _get_world_if_lightgroup_capable(context):
    """Return the scene World if it supports 'lightgroup' (Cycles), else None."""
    world = context.scene.world
    return world if (world and hasattr(world, "lightgroup")) else None

def _display_name(obj):
    """Nice label used in lists/filters."""
    if isinstance(obj, bpy.types.World):
        return f"{obj.name} (Environment)"
    return obj.name

# Emissive lookup is per-material and runs during panel draw, so the result is
# cached and invalidated from a depsgraph handler (see LG_clear_emissive_cache).
_emissive_material_cache = {}

def _material_is_emissive(mat):
    """True if the material's active output is fed by an Emission/Principled emission node."""
    # use_nodes is deprecated and slated for removal in Blender 6.0; default it
    # to True so this keeps working once the property is gone.
    if not mat or not getattr(mat, "use_nodes", True) or not mat.node_tree:
        return False

    cached = _emissive_material_cache.get(mat.name)
    if cached is not None:
        return cached

    result = False
    nt = mat.node_tree
    output_node = next((n for n in nt.nodes if n.type == 'OUTPUT_MATERIAL' and n.is_active_output), None)
    surface = output_node.inputs.get('Surface') if output_node else None
    if surface and surface.is_linked:
        visited = set()
        stack = [link.from_node for link in surface.links]
        while stack:
            node = stack.pop()
            if node in visited:
                continue
            visited.add(node)
            # Node type alone isn't enough: every Principled BSDF has an
            # emission socket, so an unfiltered check lists the whole scene.
            if is_emissive_node_active(node):
                result = True
                break
            for socket in node.inputs:
                if socket.is_linked:
                    stack.extend(link.from_node for link in socket.links)

    _emissive_material_cache[mat.name] = result
    return result

def _is_emissive_mesh(obj):
    """True for a mesh object carrying at least one emissive material."""
    if obj.type != 'MESH':
        return False
    return any(_material_is_emissive(slot.material) for slot in obj.material_slots)

def _is_lightgroup_object(obj):
    """Objects that Cycles can assign to a light group: lights and emissive meshes."""
    return obj.type == 'LIGHT' or _is_emissive_mesh(obj)

def _lightgroup_objects(scene):
    """All light-group-capable objects in the scene."""
    return [obj for obj in scene.objects if _is_lightgroup_object(obj)]

def _is_selected(obj):
    """Selection state of an object, safe to call from a draw function.

    select_get() raises for objects outside the current view layer (e.g. in an
    excluded collection), which would break the whole panel.
    """
    try:
        return obj.select_get()
    except RuntimeError:
        return False

def LG_clear_emissive_cache(scene, depsgraph=None):
    """Drop the emissive lookup when a material changes.

    Without this the panel would keep listing a mesh after its emission node
    was removed, and miss one that just gained it. Only Material updates matter
    — the cache is keyed by material, and which materials an object uses is
    re-read on every lookup — so object transforms leave it intact rather than
    forcing a full node-tree rescan on every viewport move.
    """
    if depsgraph is None:
        _emissive_material_cache.clear()
        return
    for update in depsgraph.updates:
        if isinstance(update.id, bpy.types.Material):
            _emissive_material_cache.clear()
            return

@persistent
def LG_clear_state_on_load(dummy):
    """Drop solo state on file load.

    The solo flag and its visibility backup live in memory, not in the .blend,
    so carrying them into a freshly loaded file would leave the UI claiming a
    group is soloed while the backup refers to objects from the old scene.
    """
    _emissive_material_cache.clear()
    _exclusive_visibility_backup.clear()
    if hasattr(bpy.types.Scene, "group_exclusive_dict"):
        bpy.types.Scene.group_exclusive_dict.clear()
    if hasattr(bpy.types.Scene, "group_collapse_dict"):
        bpy.types.Scene.group_collapse_dict.clear()

# -------------------------------------------------------------------------
# Render Layer Functions
# -------------------------------------------------------------------------
def get_render_layer_items(self, context):
    """Return a list of render layer items for the EnumProperty."""
    items = []
    for view_layer in context.scene.view_layers:
        items.append((view_layer.name, view_layer.name, ""))
    return items

def update_render_layer(self, context):
    selected = self.selected_render_layer
    for vl in context.scene.view_layers:
        if vl.name == selected:
            context.window.view_layer = vl
            break

# -------------------------------------------------------------------------
# Filter Functions
# -------------------------------------------------------------------------
class LG_ClearFilter(Operator):
    """Clear the light group filter."""
    bl_idname = "lg_editor.clear_filter"
    bl_label = "Clear Filter"

    @classmethod
    def poll(cls, context):
        return context.scene.render.engine == 'CYCLES'

    def execute(self, context):
        context.scene.light_group_filter = ""
        return {'FINISHED'}

# -------------------------------------------------------------------------
# Operators
# -------------------------------------------------------------------------
class LG_AssignLightGroup(Operator):
    """Assign the active light group to selected lights (and Environment if checked)."""
    bl_idname = "lg_editor.assign_light_group"
    bl_label = "Assign"

    def execute(self, context):
        view_layer = context.view_layer
        if (hasattr(view_layer, "lightgroups")
                and view_layer.active_lightgroup_index >= 0
                and view_layer.active_lightgroup_index < len(view_layer.lightgroups)):
            active_group = view_layer.lightgroups[view_layer.active_lightgroup_index]

            # Selected lights and emissive meshes (selection driven by
            # Object.is_selected -> select_set)
            selected_lights = [obj for obj in context.selected_objects if _is_lightgroup_object(obj)]
            for light in selected_lights:
                light.lightgroup = active_group.name

            # Environment (World) if user checked its checkbox
            world = _get_world_if_lightgroup_capable(context)
            if world and getattr(world, "le_is_selected", False):
                world.lightgroup = active_group.name

            bpy.ops.lg_editor.reset_light_selection()
        else:
            self.report({'WARNING'}, "No light group selected or available.")
        return {'FINISHED'}

class LG_UnassignLightGroup(Operator):
    """Unassign selected lights (and Environment if checked) from any group."""
    bl_idname = "lg_editor.unassign_light_group"
    bl_label = "Unassign"

    def execute(self, context):
        # Selected lights and emissive meshes
        selected_lights = [obj for obj in context.selected_objects if _is_lightgroup_object(obj)]
        for light in selected_lights:
            light.lightgroup = ""

        # Environment (World) if user checked its checkbox
        world = _get_world_if_lightgroup_capable(context)
        if world and getattr(world, "le_is_selected", False):
            world.lightgroup = ""

        bpy.ops.lg_editor.reset_light_selection()
        return {'FINISHED'}

class LG_ResetLightSelection(Operator):
    """Reset the selection of lights and Environment checkbox."""
    bl_idname = "lg_editor.reset_light_selection"
    bl_label = "Reset Light Selection"

    def execute(self, context):
        bpy.ops.object.select_all(action='DESELECT')
        for obj in _lightgroup_objects(context.scene):
            obj.is_selected = False

        world = _get_world_if_lightgroup_capable(context)
        if world and hasattr(world, "le_is_selected"):
            world.le_is_selected = False

        self.report({'INFO'}, "Deselected all lights and Environment checkbox")
        return {'FINISHED'}

class LG_ToggleLightSelection(Operator):
    """Toggle selection for an individual light object."""
    bl_idname = "lg_editor.toggle_light_selection"
    bl_label = "Toggle Light Selection"

    light_name: bpy.props.StringProperty()

    def execute(self, context):
        light_obj = context.scene.objects.get(self.light_name)
        if not light_obj:
            self.report({'WARNING'}, f"Light '{self.light_name}' not found.")
            return {'CANCELLED'}

        # Toggle from the real selection state, not the mirrored property —
        # the property goes stale when the user selects in the viewport.
        select = not _is_selected(light_obj)
        if light_obj.name in context.view_layer.objects:
            light_obj.select_set(select)
            if select:
                context.view_layer.objects.active = light_obj
        light_obj.is_selected = select
        return {'FINISHED'}

class LG_ToggleGroupExclusive(Operator):
    """Toggle exclusive activation of this group (LIGHT objects only)."""
    bl_idname = "lg_editor.toggle_group_exclusive"
    bl_label = "Toggle Group Exclusive"

    group_key: bpy.props.StringProperty()

    def execute(self, context):
        exclusive_dict = context.scene.group_exclusive_dict
        is_exclusive = not exclusive_dict.get(self.group_key, False)

        # Soloing is mutually exclusive: turning one group on clears the rest,
        # so we never stack two solos and lose track of the original state.
        was_soloing = any(exclusive_dict.values())
        exclusive_dict.clear()

        if is_exclusive:
            # Only snapshot when nothing was soloed yet, otherwise switching
            # straight from one solo to another would capture the soloed
            # (already hidden) state as if it were the user's own.
            if not was_soloing:
                _exclusive_visibility_backup.clear()
                for obj in _lightgroup_objects(context.scene):
                    _exclusive_visibility_backup[obj.name] = (
                        obj.hide_viewport, obj.hide_render
                    )

            exclusive_dict[self.group_key] = True
            exclusive_group_name = self.group_key.replace("group_", "")
            for obj in _lightgroup_objects(context.scene):
                hidden = getattr(obj, "lightgroup", "") != exclusive_group_name
                obj.hide_viewport = hidden
                obj.hide_render = hidden
            # World has no viewport toggle; leave it untouched.
        else:
            # Restore what the user had before soloing rather than forcing
            # everything visible (which wiped their own hidden lights).
            # Iterate the backup, not the scene: a mesh whose emission was
            # edited away while soloed still needs its visibility restored.
            for name, (vp, rp) in _exclusive_visibility_backup.items():
                obj = context.scene.objects.get(name)
                if obj:
                    obj.hide_viewport = vp
                    obj.hide_render = rp
            _exclusive_visibility_backup.clear()

        for area in context.screen.areas:
            if area.type in {'VIEW_3D', 'PROPERTIES'}:
                area.tag_redraw()
        return {'FINISHED'}

class LG_ToggleGroup(Operator):
    """Toggle the collapse state of a group in the UI list."""
    bl_idname = "lg_editor.toggle_group"
    bl_label = "Toggle Group"
    group_key: bpy.props.StringProperty()

    def execute(self, context):
        context.scene.group_collapse_dict[self.group_key] = not context.scene.group_collapse_dict.get(self.group_key, False)
        return {'FINISHED'}

class LG_AddLightGroup(Operator):
    """Add a new light group in the current view layer."""
    bl_idname = "lg_editor.add_light_group"
    bl_label = "Add Light Group"

    def execute(self, context):
        view_layer = context.view_layer
        if not hasattr(view_layer, "lightgroups"):
            self.report({'WARNING'}, "This Blender version doesn't support per-view-layer lightgroups.")
            return {'CANCELLED'}

        new_group = view_layer.lightgroups.add()
        new_group.name = "NewGroup"
        view_layer.active_lightgroup_index = len(view_layer.lightgroups) - 1
        return {'FINISHED'}

class LG_RemoveLightGroup(Operator):
    """Remove the selected light group and clear assignments on its lights."""
    bl_idname = "lg_editor.remove_light_group"
    bl_label = "Remove Light Group"

    def execute(self, context):
        view_layer = context.view_layer
        if hasattr(view_layer, "lightgroups"):
            if view_layer.active_lightgroup_index >= 0 and view_layer.active_lightgroup_index < len(view_layer.lightgroups):
                active_group_name = view_layer.lightgroups[view_layer.active_lightgroup_index].name

                # Unassign lights and emissive meshes from the group before removing
                for obj in _lightgroup_objects(context.scene):
                    if getattr(obj, "lightgroup", "") == active_group_name:
                        obj.lightgroup = ""

                # Note: We don't touch World.lightgroup here; Blender will handle invalid refs.
                bpy.ops.scene.view_layer_remove_lightgroup()

                if view_layer.active_lightgroup_index >= len(view_layer.lightgroups):
                    view_layer.active_lightgroup_index = max(0, len(view_layer.lightgroups) - 1)

                group_key = f"group_{active_group_name}"
                context.scene.group_collapse_dict.pop(group_key, None)
                context.scene.group_exclusive_dict.pop(group_key, None)

                for area in context.screen.areas:
                    if area.type == 'VIEW_3D':
                        area.tag_redraw()
            else:
                self.report({'WARNING'}, "No active light group to remove.")
        else:
            self.report({'WARNING'}, "Lightgroups not available in this Blender version.")
        return {'FINISHED'}

# -------------------------------------------------------------------------
# Drawing
# -------------------------------------------------------------------------
def draw_main_row(box, obj):
    """Draw a row for a LIGHT object, an emissive mesh, or the Environment (World).
    - LIGHT / emissive mesh: toggles viewport selection
    - WORLD: toggles World.le_is_selected (for Assign/Unassign)

    Both use the same select-cursor icon as the Light Editor panel — a
    checkbox reads as "on/off", which isn't what this control does.
    """
    row = box.row(align=True)

    if isinstance(obj, bpy.types.World):
        # Environment can't be selected in the viewport, so this stays a plain
        # property, but it's drawn with the same icon for a consistent panel.
        selected = getattr(obj, "le_is_selected", False)
        row.prop(obj, "le_is_selected", text="", emboss=True,
                 icon='RESTRICT_SELECT_ON' if selected else 'RESTRICT_SELECT_OFF')
        row.label(text=_display_name(obj), icon='WORLD')
    else:
        # Drive the icon from the real selection state so the panel stays
        # correct when the user selects lights in the viewport or outliner.
        selected = _is_selected(obj)
        op = row.operator("lg_editor.toggle_light_selection", text="",
                          icon='RESTRICT_SELECT_ON' if selected else 'RESTRICT_SELECT_OFF',
                          depress=selected)
        op.light_name = obj.name
        # Same icon the Light Editor panel uses for emissive meshes.
        row.label(text=obj.name, icon='LIGHT' if obj.type == 'LIGHT' else 'SHADING_RENDERED')

# -------------------------------------------------------------------------
# Main Panel
# -------------------------------------------------------------------------
class LG_PT_LightGroupPanel(Panel):
    bl_label = "Light Groups"
    bl_idname = "LG_PT_light_group_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Light Editor"

    @classmethod
    def poll(cls, context):
        return context.scene.render.engine == 'CYCLES'

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        view_layer = context.view_layer

        # Lightgroup list / add / remove
        row = layout.row(align=True)
        col = row.column()
        if hasattr(view_layer, "lightgroups"):
            col.template_list("UI_UL_list", "lightgroups", view_layer, "lightgroups",
                              view_layer, "active_lightgroup_index", rows=3)
            col = row.column(align=True)
            col.operator("lg_editor.add_light_group", icon='ADD', text="")
            col.operator("lg_editor.remove_light_group", icon='REMOVE', text="")
        else:
            col.label(text="No Lightgroups in this Blender version", icon='ERROR')

        # Assign / Unassign / Reset (lights + Environment checkbox)
        row = layout.row(align=True)
        row.operator("lg_editor.assign_light_group", text="Assign")
        row.operator("lg_editor.unassign_light_group", text="Unassign")
        row.operator("lg_editor.reset_light_selection", text="Deselect All")

        # Filter
        row = layout.row(align=True)
        row.prop(scene, "light_group_filter", text="", icon="VIEWZOOM")
        row.operator("lg_editor.clear_filter", text="", icon='PANEL_CLOSE')

        # Render layer dropdown
        row = layout.row()
        row.prop(scene, "selected_render_layer", text="Render Layer")

        # -----------------------------------------------------------------
        # Build grouped lists (include Environment/World where relevant)
        # -----------------------------------------------------------------
        groups = {}
        capable_world = _get_world_if_lightgroup_capable(context)

        # Resolved once per draw: emissive detection walks node trees, so
        # re-scanning the scene for every light group would cost real time.
        candidates = _lightgroup_objects(scene)

        if hasattr(view_layer, "lightgroups"):
            for lg in view_layer.lightgroups:
                # Membership is about light group assignment, not visibility.
                # Filtering on hide_render here made lights disappear from the
                # list whenever anything hid them (solo/exclusive, the Light
                # Editor's enable toggle, or a manual outliner click).
                lights_in_group = [
                    obj for obj in candidates
                    if getattr(obj, "lightgroup", "") == lg.name
                ]
                # Include the World if it's assigned to this group
                if capable_world and getattr(capable_world, "lightgroup", "") == lg.name:
                    lights_in_group.append(capable_world)
                groups[lg.name] = lights_in_group

        # Not Assigned
        not_assigned = [obj for obj in candidates if not getattr(obj, "lightgroup", "")]
        if capable_world and not getattr(capable_world, "lightgroup", ""):
            not_assigned.append(capable_world)
        if not_assigned:
            groups["Not Assigned"] = not_assigned

        # Filter groups
        filter_pattern = scene.light_group_filter.strip().lower()
        filtered_groups = {}
        for grp_name, group_objs in groups.items():
            if filter_pattern:
                filtered_objs = [obj for obj in group_objs if filter_pattern in _display_name(obj).lower()]
                if filtered_objs:
                    filtered_groups[grp_name] = filtered_objs
            else:
                filtered_groups[grp_name] = group_objs

        # Draw
        for grp_name, group_objs in filtered_groups.items():
            group_key = f"group_{grp_name}"
            collapsed = scene.group_collapse_dict.get(group_key, False)
            is_exclusive = scene.group_exclusive_dict.get(group_key, False)

            header_box = layout.box()
            header_row = header_box.row(align=True)

            icon_exclusive = "RADIOBUT_ON" if is_exclusive else "RADIOBUT_OFF"
            op_exclusive = header_row.operator("lg_editor.toggle_group_exclusive", text="",
                                               icon=icon_exclusive, emboss=True)
            op_exclusive.group_key = group_key

            icon_arrow = 'TRIA_DOWN' if not collapsed else 'TRIA_RIGHT'
            op = header_row.operator("lg_editor.toggle_group", text="", icon=icon_arrow)
            op.group_key = group_key

            header_row.label(text=grp_name, icon='GROUP')

            if not collapsed:
                for obj in group_objs:
                    draw_main_row(header_box, obj)

# -------------------------------------------------------------------------
# Classes and Registration
# -------------------------------------------------------------------------
classes = (
    LG_AssignLightGroup,
    LG_UnassignLightGroup,
    LG_ResetLightSelection,
    LG_ToggleLightSelection,
    LG_ToggleGroupExclusive,
    LG_ToggleGroup,
    LG_AddLightGroup,
    LG_RemoveLightGroup,
    LG_ClearFilter,
)

def register():
    # UI state dicts (plain Python attrs on the Scene type, shared scene-wide)
    bpy.types.Scene.group_collapse_dict = {}
    bpy.types.Scene.group_exclusive_dict = {}

    # Scene properties
    bpy.types.Scene.selected_render_layer = bpy.props.EnumProperty(
        name="Render Layer",
        description="Select the render layer",
        items=get_render_layer_items,
        update=update_render_layer
    )

    bpy.types.Scene.light_group_filter = StringProperty(
        name="Filter",
        default="",
        description="Filter light groups by name (wildcards allowed)"
    )

    # Object selection checkbox (mirrors viewport selection)
    bpy.types.Object.is_selected = bpy.props.BoolProperty(
        name="Is Selected",
        description="Indicates whether the light is selected",
        default=False,
        update=lambda self, context: self.select_set(self.is_selected)
    )

    # Environment selection checkbox for assignment/unassignment
    bpy.types.World.le_is_selected = bpy.props.BoolProperty(
        name="Selected (Light Groups)",
        description="Select the Environment to Assign/Unassign its light group",
        default=False
    )

    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.utils.register_class(LG_PT_LightGroupPanel)

    if LG_clear_state_on_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(LG_clear_state_on_load)
    if LG_clear_emissive_cache not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(LG_clear_emissive_cache)


def unregister():
    if LG_clear_state_on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(LG_clear_state_on_load)
    if LG_clear_emissive_cache in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(LG_clear_emissive_cache)

    _emissive_material_cache.clear()
    _exclusive_visibility_backup.clear()

    # Remove props. Each removal is guarded so that one failure can't abort the
    # rest of unregister() — leftover registered classes make a subsequent
    # enable fail with "already registered as a subclass".
    for owner, prop in (
        (bpy.types.Scene, "selected_render_layer"),
        (bpy.types.Scene, "light_group_filter"),
        (bpy.types.Scene, "group_collapse_dict"),
        (bpy.types.Scene, "group_exclusive_dict"),
        (bpy.types.Object, "is_selected"),
        (bpy.types.World, "le_is_selected"),
    ):
        if hasattr(owner, prop):
            try:
                delattr(owner, prop)
            except (AttributeError, TypeError):
                pass

    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except (RuntimeError, ValueError):
            pass

    try:
        bpy.utils.unregister_class(LG_PT_LightGroupPanel)
    except (RuntimeError, ValueError):
        pass


if __name__ == "__main__":
    register()
