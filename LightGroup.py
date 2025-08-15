# LightGroup.py — Environment (World) visibility + selectable checkbox
# This file reflects the previous patch (show World in group lists) and adds a checkbox
# to select the Environment so it can be reassigned via Assign/Unassign like lights.

import bpy
from bpy.types import Operator, Panel, Menu
from bpy.props import StringProperty

# -------------------------------------------------------------------------
# Scene-scoped state
# -------------------------------------------------------------------------
# Dictionaries for collapse and exclusivity (persist across redraws)
bpy.types.Scene.group_collapse_dict = {}
bpy.types.Scene.group_exclusive_dict = {}

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


class LG_MT_lightgroup_context_menu(Menu):
    bl_label = "LighGroup Specials"

    def draw(self, _context):
        layout = self.layout

        layout.operator("scene.view_layer_add_used_lightgroups", icon='ADD')
        layout.operator("scene.view_layer_remove_unused_lightgroups", icon='REMOVE')

        layout.separator()
        layout.operator(
            "lg.reset_all_lightgroups",
            icon='LOOP_BACK',
            text="Reset All LighGroups",
        )
        layout.operator(
            "lg.remove_all_lightgroups",
            icon='NONE',
            text="Delete All LighGroups",
        )

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

            # Selected LIGHT objects (selection driven by Object.is_selected -> select_set)
            selected_lights = [obj for obj in context.selected_objects if obj.type == 'LIGHT']
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
        # Selected LIGHT objects
        selected_lights = [obj for obj in context.selected_objects if obj.type == 'LIGHT']
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
        for obj in context.scene.objects:
            if obj.type == 'LIGHT':
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
        if light_obj:
            light_obj.is_selected = not light_obj.is_selected
        else:
            self.report({'WARNING'}, f"Light '{self.light_name}' not found.")
        return {'FINISHED'}

class LG_ToggleGroupExclusive(Operator):
    """Toggle exclusive activation of this group (LIGHT objects only)."""
    bl_idname = "lg_editor.toggle_group_exclusive"
    bl_label = "Toggle Group Exclusive"

    group_key: bpy.props.StringProperty()

    def execute(self, context):
        is_exclusive = not context.scene.group_exclusive_dict.get(self.group_key, False)
        context.scene.group_exclusive_dict[self.group_key] = is_exclusive

        if is_exclusive:
            exclusive_group_name = self.group_key.replace("group_", "")
            for obj in context.scene.objects:
                if obj.type == 'LIGHT':
                    obj.hide_viewport = getattr(obj, "lightgroup", "") != exclusive_group_name
            # World has no viewport toggle; leave it untouched.
        else:
            for obj in context.scene.objects:
                if obj.type == 'LIGHT':
                    obj.hide_viewport = False
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

                # Unassign lights from the group before removing
                for obj in context.scene.objects:
                    if obj.type == 'LIGHT' and getattr(obj, "lightgroup", "") == active_group_name:
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



class LG_ResetAllLighgrGroups(Operator):
    """Remove all lights from any LightGroups."""
    bl_idname = "lg.reset_all_lightgroups"
    bl_label = "Reset All LightGroups"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        view_layer = context.view_layer
        # Unassign lights from the group before removing the group
        for obj in context.scene.objects :
            print(obj.type)
            if obj.type == 'LIGHT' and (obj.lightgroup != ""): 
                obj.lightgroup = ""

                # Force redraw panel so lightgroup gets updated
                for area in context.screen.areas:
                    if area.type == 'PROPERTIES':
                        area.tag_redraw()

        for obj in bpy.data.worlds:
            if obj.lightgroup != "": 
                obj.lightgroup = ""

                # Force redraw panel so lightgroup gets updated
                for area in context.screen.areas:
                    if area.type == 'PROPERTIES':
                        area.tag_redraw()

        else:
            self.report({'INFO'}, "All Lightgroups cleared.")
        return {'FINISHED'}


class LG_RemoveAllLighgrGroups(Operator):
    """Remove all LightGroups."""
    bl_idname = "lg.remove_all_lightgroups"
    bl_label = "Remove All the LightGroups in the scene. Also removes them from the lights."
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        view_layer = context.view_layer
        # Unassign lights from the group before removing the group
        if hasattr(view_layer, "lightgroups"):
            for index, item in reversed(list(enumerate(view_layer.lightgroups))):
                view_layer.active_lightgroup_index = index

                # Use the Blender operator to remove the lightgroup
                bpy.ops.scene.view_layer_remove_lightgroup()

                # Force redraw panel so lightgroup gets updated
                for area in context.screen.areas:
                    if area.type == 'PROPERTIES':
                        area.tag_redraw()

        else:
            self.report({'INFO'}, "All Lightgroups Removed.")
        return {'FINISHED'}
    

# -------------------------------------------------------------------------
# Drawing
# -------------------------------------------------------------------------
def draw_main_row(box, obj):
    """Draw a row for either a LIGHT object or the Environment (World).
    - LIGHT: checkbox toggles Object.is_selected (also viewport selection)
    - WORLD: checkbox toggles World.le_is_selected (for Assign/Unassign)
    """
    row = box.row(align=True)

    if isinstance(obj, bpy.types.World):
        # Selectable Environment checkbox (doesn't affect viewport selection)
        sel = row.row(align=True)
        sel.prop(obj, "le_is_selected", text="", emboss=True, icon='NONE')
        row.label(text=_display_name(obj), icon='WORLD')
    else:
        # LIGHT object row (kept minimal per previous version)
        row.prop(obj, "is_selected", text="", emboss=True, icon='NONE')
        row.label(text=obj.name, icon='LIGHT')

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

            col.menu("LG_MT_lightgroup_context_menu", icon='DOWNARROW_HLT', text="")
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

        if hasattr(view_layer, "lightgroups"):
            for lg in view_layer.lightgroups:
                lights_in_group = [
                    obj for obj in scene.objects
                    if obj.type == 'LIGHT'
                    and not obj.hide_render
                    and getattr(obj, "lightgroup", "") == lg.name
                ]
                # Include the World if it's assigned to this group
                if capable_world and getattr(capable_world, "lightgroup", "") == lg.name:
                    lights_in_group.append(capable_world)
                groups[lg.name] = lights_in_group

        # Not Assigned
        not_assigned = [
            obj for obj in scene.objects
            if obj.type == 'LIGHT'
            and not obj.hide_render
            and not getattr(obj, "lightgroup", "")
        ]
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
    LG_MT_lightgroup_context_menu,
    LG_ResetAllLighgrGroups,
    LG_RemoveAllLighgrGroups,
)

def register():
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


def unregister():
    # Remove props
    del bpy.types.Scene.selected_render_layer
    del bpy.types.Scene.light_group_filter
    del bpy.types.Object.is_selected
    if hasattr(bpy.types.World, "le_is_selected"):
        del bpy.types.World.le_is_selected
    del bpy.types.Scene.group_collapse_dict
    del bpy.types.Scene.group_exclusive_dict

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

    bpy.utils.unregister_class(LG_PT_LightGroupPanel)


if __name__ == "__main__":
    register()
