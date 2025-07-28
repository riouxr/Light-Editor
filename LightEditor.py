import bpy
import fnmatch
from bpy.props import (
    BoolProperty,
    IntProperty,
    FloatProperty,
    StringProperty,
    EnumProperty,
    PointerProperty
)
from bpy.app.handlers import persistent
from bpy.types import Panel
from bpy.app.translations import contexts as i18n_contexts
from bpy.types import Light
import re

# --- Global Variables ---
# Stores the currently active light for the "Turn Off Others" feature.
current_active_light = None

# Dictionary to track the state (True/False) of the group checkboxes (ON by default).
group_checkbox_1_state = {}

# Dictionary to store original light states for each group when toggled off.
group_lights_original_state = {}

# Dictionary to track whether each group (by kind/collection) is collapsed in the UI.
group_collapse_dict = {}

# Cache for found emissive material/object pairs to improve performance.
emissive_material_cache = {}

# Dictionary to backup emission states for the "Isolate Emissive" operator.
_emissive_state_backup = {}


# --- Helper Functions ---

def find_emissive_objects(context):
    """Finds mesh objects with emissive materials (EMISSION node or Principled BSDF with emission).
    Returns a list of tuples (object, material).
    """
    scene = context.scene
    emissive_objs = []
    seen = set() # To avoid processing the same material multiple times

    def is_emissive_output(node, visited):
        """Recursively checks if a node or its inputs lead to an emissive output."""
        if node in visited:
            return False
        visited.add(node)

        # Check for EMISSION node with strength > 0
        if node.type == 'EMISSION':
            strength_input = node.inputs.get("Strength")
            return strength_input and strength_input.default_value > 0

        # Check for BSDF_PRINCIPLED with emission (Blender 4.0+ names prioritized)
        if node.type == 'BSDF_PRINCIPLED':
            emission_input = node.inputs.get("Emission Strength") # 4.0+ name
            emission_color = node.inputs.get("Emission Color")   # 4.0+ name
            # Enabled if Emission Strength > 0 OR Emission Color RGB has any value > 0
            return (emission_input and emission_input.default_value > 0) or \
                   (emission_color and any(emission_color.default_value[:3]))

        # Recursively check inputs of nodes like MIX_SHADER, ADD_SHADER
        for input_socket in node.inputs:
            if input_socket.is_linked:
                for link in input_socket.links:
                    if is_emissive_output(link.from_node, visited):
                        return True
        return False

    for obj in context.view_layer.objects:
        if obj.type != 'MESH':
            continue
        for slot in obj.material_slots:
            mat = slot.material
            if not mat or not mat.use_nodes:
                continue
            if mat.name in seen:
                continue
            seen.add(mat.name)
            nt = mat.node_tree
            if not nt:
                continue

            # Find the main material output node
            output_node = next((n for n in nt.nodes if n.type == 'OUTPUT_MATERIAL'), None)
            if not output_node:
                continue

            # Check the 'Surface' input of the output node
            surf_input = output_node.inputs.get('Surface')
            if not surf_input or not surf_input.is_linked:
                continue

            # Start the recursive check from the connected node
            from_node = surf_input.links[0].from_node
            if is_emissive_output(from_node, set()):
                emissive_objs.append((obj, mat))

    return emissive_objs

def draw_emissive_row(box, obj, mat):
    """Draws a single row in the emissive materials list."""
    row = box.row(align=True)

    # Get the emission node or principled BSDF
    nt = mat.node_tree
    output_node = next((n for n in nt.nodes if n.type == 'OUTPUT_MATERIAL'), None)
    surf_input = output_node.inputs.get('Surface') if output_node else None
    from_node = surf_input.links[0].from_node if surf_input and surf_input.is_linked else None

    emission_node = None
    principled_node = None
    color_input = None
    strength_input = None
    enabled = False

    def traverse_inputs(node, visited):
        """Helper to find emission/principled nodes in the node tree."""
        nonlocal emission_node, principled_node
        if node in visited:
            return
        visited.add(node)
        if node.type == 'EMISSION':
            emission_node = node
        elif node.type == 'BSDF_PRINCIPLED':
            principled_node = node
        for input_socket in node.inputs:
            if input_socket.is_linked:
                for link in input_socket.links:
                    traverse_inputs(link.from_node, visited)

    if from_node:
        traverse_inputs(from_node, set())

    # Determine inputs and enabled state based on found node (4.0+ logic)
    if emission_node:
        color_input = emission_node.inputs.get("Color")
        strength_input = emission_node.inputs.get("Strength")
        if strength_input:
            enabled = strength_input.default_value > 0
    elif principled_node:
        color_input = principled_node.inputs.get("Emission Color") # 4.0+ name
        strength_input = principled_node.inputs.get("Emission Strength") # 4.0+ name
        if strength_input is not None:
            enabled = strength_input.default_value > 0
        elif color_input is not None:
            enabled = any(channel > 0.0 for channel in color_input.default_value[:3])

    # --- UI Elements for the Row ---
    # Toggle emission
    icon = 'OUTLINER_OB_LIGHT' if enabled else 'LIGHT_DATA'
    op = row.operator("le.toggle_emission", text="", icon=icon, depress=enabled)
    op.mat_name = mat.name

    # Isolate button
    iso_icon = 'RADIOBUT_ON' if _emissive_state_backup else 'RADIOBUT_OFF'
    row.operator("le.isolate_emissive", text="", icon=iso_icon).mat_name = mat.name

    # Select object
    row.operator("le.select_light", text="", icon="RESTRICT_SELECT_OFF").name = obj.name

    # Editable object name
    obj_col = row.column(align=True)
    obj_col.scale_x = 0.5
    obj_col.prop(obj, "name", text="")

    # Editable material name
    mat_col = row.column(align=True)
    mat_col.scale_x = 0.5
    mat_col.prop(mat, "name", text="")

    # --- Emission Color & Strength Display ---
    value_row = row.row(align=True)
    col_color = value_row.row(align=True)
    col_color.ui_units_x = 4
    col_strength = value_row.row(align=True)
    col_strength.ui_units_x = 6

    # COLOR COLUMN
    if color_input:
        if color_input.is_linked:
            color_row = col_color.row(align=True)
            color_row.alignment = 'EXPAND'
            color_row.label(icon='NODETREE')
            color_row.enabled = False
            color_row.prop(color_input, "default_value", text="")
        else:
            try:
                col_color.prop(color_input, "default_value", text="")
            except:
                col_color.label(text="Col?")
    else:
        col_color.label(text="")

    # STRENGTH COLUMN
    if strength_input:
        if strength_input.is_linked:
            strength_row = col_strength.row(align=True)
            strength_row.alignment = 'EXPAND'
            strength_row.label(icon='NODETREE')
            strength_row.enabled = False
            strength_row.prop(strength_input, "default_value", text="")
        else:
            try:
                col_strength.prop(strength_input, "default_value", text="")
            except:
                col_strength.label(text="Str?")
    else:
        col_strength.label(text="")

def draw_emissives_section(self, context, layout):
    """Draws the collapsible section for all emissive materials."""
    scene = context.scene
    box = layout.box() # Create a box for the emissive section

    # Collapsible Header for Emissives
    header_row = box.row(align=True)
    emissive_collapse_icon = 'RIGHTARROW' if scene.collapse_all_emissives else 'DOWNARROW_HLT'
    header_row.operator("light_editor.toggle_all_emissives", text="", icon=emissive_collapse_icon, emboss=False)
    header_row.label(text="All Emissive Materials (Alphabetical)", icon='SHADING_RENDERED')

    # Draw the emissive materials list only if not collapsed
    if not scene.collapse_all_emissives:
        emissive_box = box.box() # Optional: sub-box for emissives
        emissive_pairs = find_emissive_objects(context)
        sorted_emissive_pairs = sorted(emissive_pairs, key=lambda pair: f"{pair[0].name}_{pair[1].name}".lower())
        if not sorted_emissive_pairs:
             emissive_box.label(text="No emissive materials found.")
        else:
            for obj, mat in sorted_emissive_pairs:
                draw_emissive_row(emissive_box, obj, mat)

def draw_main_row(box, obj):
    """Draws a single row for a light object in the main list."""
    scene = bpy.context.scene
    light = obj.data
    row = box.row(align=True)

    # Auto-sync light.color and light.energy to a new emission node if nodes are enabled
    # and the light hasn't been synced before.
    if light.use_nodes and light.node_tree and not hasattr(light, "_synced_once"):
        output_node = next((n for n in light.node_tree.nodes if n.type == 'OUTPUT_LIGHT'), None)
        has_emission = any(n.type == 'EMISSION' for n in light.node_tree.nodes)
        if output_node and not has_emission:
            # Clear and setup a basic emission node setup
            nt = light.node_tree
            nt.nodes.clear()
            emission = nt.nodes.new("ShaderNodeEmission")
            output = nt.nodes.new("ShaderNodeOutputLight")
            emission.location = (0, 0)
            output.location = (200, 0)
            nt.links.new(emission.outputs["Emission"], output.inputs["Surface"])
            # Copy values from the light properties
            emission.inputs["Color"].default_value = list(light.color) + [1.0]
            emission.inputs["Strength"].default_value = light.energy
            # Prevent redoing this sync
            light["_synced_once"] = True

    # --- Controls: toggle, solo, select, expand ---
    controls_row = row.row(align=True)
    controls_row.prop(
        obj, "light_enabled", text="",
        icon="OUTLINER_OB_LIGHT" if obj.light_enabled else "LIGHT_DATA"
    )
    controls_row.active = obj.light_enabled
    controls_row.prop(
        obj, "light_turn_off_others", text="",
        icon="RADIOBUT_ON" if obj.light_turn_off_others else "RADIOBUT_OFF"
    )
    controls_row.operator(
        "le.select_light", text="",
        icon="RESTRICT_SELECT_ON" if obj.select_get() else "RESTRICT_SELECT_OFF"
    ).name = obj.name
    controls_row.prop(
        obj, "light_expanded", text="",
        emboss=True,
        icon='DOWNARROW_HLT' if obj.light_expanded else 'RIGHTARROW'
    )

    # --- Light Name ---
    col_name = row.row(align=True)
    col_name.ui_units_x = 8
    col_name.prop(obj, "name", text="")

    # --- Color and Energy Columns ---
    col_color = row.row(align=True)
    col_color.ui_units_x = 4
    col_energy = row.row(align=True)
    col_energy.ui_units_x = 6

    # --- Display Color/Energy from Node or Light Properties ---
    if light.use_nodes and light.node_tree:
        nt = light.node_tree
        output_node = next((n for n in nt.nodes if n.type == 'OUTPUT_LIGHT'), None)
        surface_input = output_node.inputs.get("Surface") if output_node else None
        if surface_input and surface_input.is_linked:
            from_node = surface_input.links[0].from_node
            if from_node.type == 'EMISSION':
                color_socket = from_node.inputs.get("Color")
                strength_socket = from_node.inputs.get("Strength")
                # Display color input (dimmed if linked)
                if color_socket:
                    if color_socket.is_linked:
                        color_row = col_color.row(align=True)
                        color_row.alignment = 'EXPAND'
                        color_row.label(icon='NODETREE')
                        color_row.enabled = False
                        color_row.prop(color_socket, "default_value", text="")
                    else:
                        col_color.prop(color_socket, "default_value", text="")
                else:
                    col_color.label(text="")
                # Display strength input (dimmed if linked)
                if strength_socket:
                    if strength_socket.is_linked:
                        strength_row = col_energy.row(align=True)
                        strength_row.alignment = 'EXPAND'
                        strength_row.label(icon='NODETREE')
                        strength_row.enabled = False
                        strength_row.prop(strength_socket, "default_value", text="")
                    else:
                         col_energy.prop(strength_socket, "default_value", text="")
                else:
                    col_energy.label(text="")
            else:
                # Not connected to emission
                col_color.label(icon='ERROR')
                col_energy.label(icon='ERROR')
        else:
            # Nodes exist but not connected to emission, show light props
            col_color.prop(light, "color", text="")
            col_energy.prop(light, "energy", text="")
    else:
        # No nodes, show light properties directly
        col_color.prop(light, "color", text="")
        col_energy.prop(light, "energy", text="")

def draw_extra_params(self, box, obj, light):
    """Draws the additional light settings based on the render engine."""
    col = box.column()
    col.prop(light, "color")
    col.prop(light, "energy")
    col.separator()

    # --- Cycles Specific Settings ---
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
        sub.prop(clamp, "cast_shadow")
        sub.prop(clamp, "use_multiple_importance_sampling", text="Multiple Importance")
        if use_mnee(bpy.context):
            sub.prop(clamp, "is_caustics_light", text="Shadow Caustics")
        if light.type == 'AREA':
            col.prop(clamp, "is_portal", text="Portal")

    # --- Spot Light Specific Settings ---
    if light.type == 'SPOT':
        col.separator()
        row = col.row(align=True)
        row.alignment = 'CENTER'
        row.label(text="Spot Shape")
        col.alignment = 'RIGHT'
        col.prop(light, "spot_size", text="Beam Size")
        col.prop(light, "spot_blend", text="Blend", slider=True)
        col.prop(light, "show_cone")

    # --- Area Light Specific Settings ---
    elif light.type == 'AREA':
        col.separator()
        row = col.row(align=True)
        row.alignment = 'CENTER'
        row.label(text="Beam Shape")
        col.prop(light, "spread", text="Spread")

    # --- EEVEE / EEVEE_NEXT Specific Settings ---
    if bpy.context.engine in {'BLENDER_EEVEE', 'BLENDER_EEVEE_NEXT'}:
        col.separator()
        col.prop(light, "diffuse_factor", text="Diffuse")
        col.prop(light, "specular_factor", text="Specular")
        col.prop(light, "volume_factor", text="Volume", text_ctxt=i18n_contexts.id_id)
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
            col.prop(light, "shadow_softness_factor", text="Shadow Softness")
            if light.type == 'SUN':
                col.prop(light, "shadow_trace_distance", text="Trace Distance")
        if light.type != 'SUN':
            col.separator()
            sub = col.column()
            sub.prop(light, "use_custom_distance", text="Custom Distance")
            sub.active = light.use_custom_distance
            sub.prop(light, "cutoff_distance", text="Distance")
        if light.type == 'SPOT':
            col.separator()
            row = col.row(align=True)
            row.alignment = 'CENTER'
            row.label(text="Spot Shape")
            col.prop(light, "spot_size", text="Size")
            col.prop(light, "spot_blend", text="Blend", slider=True)
            col.prop(light, "show_cone")
        if light.type in {'POINT', 'SUN', 'SPOT', 'AREA'}:
            col.separator()
            subb = col.column()
            subb.prop(light, "use_shadow", text="Shadow")
            if light.type != 'SUN':
                subb.prop(light, "shadow_buffer_clip_start", text="Clip Start")
            subb.prop(light, "shadow_buffer_bias", text="Bias")
            subb.active = light.use_shadow
            if light.type == 'SUN':
                col.separator()
                col.alignment = 'RIGHT'
                col.label(text="Cascaded Shadow Map")
                col.prop(light, "shadow_cascade_count", text="Count")
                col.prop(light, "shadow_cascade_fade", text="Fade")
                col.prop(light, "shadow_cascade_max_distance", text="Max Distance")
                col.prop(light, "shadow_cascade_exponent", text="Distribution")
            if light.type in {'POINT', 'SUN', 'SPOT', 'AREA'} and bpy.context.engine in {'BLENDER_EEVEE', 'BLENDER_EEVEE_NEXT'}:
                col.separator()
                subbb = col.column()
                subbb.active = light.use_shadow
                subbb.prop(light, "use_contact_shadow", text="Contact Shadows")
                col = subbb.column()
                col.active = light.use_shadow and light.use_contact_shadow
                col.prop(light, "contact_shadow_distance", text="Distance")
                col.prop(light, "contact_shadow_bias", text="Bias")
                col.prop(light, "contact_shadow_thickness", text="Thickness")

def get_render_layer_items(self, context):
    """Provides items for the render layer selection enum property."""
    items = []
    for view_layer in context.scene.view_layers:
        items.append((view_layer.name, view_layer.name, ""))
    return items

# --- Utility Functions for Cycles Settings ---
def get_device_type(context):
    """Get the compute device type from Cycles preferences."""
    return context.preferences.addons['cycles'].preferences.compute_device_type

def backend_has_active_gpu(context):
    """Check if Cycles has an active GPU device."""
    return context.preferences.addons['cycles'].preferences.has_active_device()

def use_metal(context):
    """Check if Metal backend is being used."""
    cscene = context.scene.cycles
    return (get_device_type(context) == 'METAL' and cscene.device == 'GPU' and backend_has_active_gpu(context))

def use_mnee(context):
    """Check if MNEE (Metal Native Embree) is available and applicable."""
    if use_metal(context):
        import platform
        version, _, _ = platform.mac_ver()
        major_version = version.split(".")[0]
        if int(major_version) < 13:
            return False
    return True


# --- Property Update Callbacks ---

def update_light_enabled(self, context):
    """Callback when the light_enabled property changes.
    Syncs the light's viewport and render visibility.
    """
    self.hide_viewport = not self.light_enabled
    self.hide_render = not self.light_enabled

def update_light_turn_off_others(self, context):
    """Callback when the light_turn_off_others property changes.
    Turns off all other lights if enabled, restores them if disabled.
    """
    scene = context.scene
    if self.light_turn_off_others:
        # If another light was previously 'soloed', turn it off
        if scene.current_active_light and scene.current_active_light != self:
            scene.current_active_light.light_turn_off_others = False
        # Set this light as the current active one
        scene.current_active_light = self
        # Turn off all other lights
        for obj in context.view_layer.objects:
            if obj.type == 'LIGHT' and obj.name != self.name:
                # Store original state if not already stored
                if 'prev_light_enabled' not in obj:
                    obj['prev_light_enabled'] = obj.light_enabled
                obj.light_enabled = False
    else:
        # If this light was the active one, clear it
        if scene.current_active_light == self:
            scene.current_active_light = None
        # Restore other lights to their previous state
        for obj in context.view_layer.objects:
            if obj.type == 'LIGHT' and obj.name != self.name:
                if 'prev_light_enabled' in obj:
                    obj.light_enabled = obj['prev_light_enabled']
                    del obj['prev_light_enabled']

def update_group_by_kind(self, context):
    """Callback to ensure only one grouping mode is active."""
    if self.light_editor_kind_alpha:
        self.light_editor_group_by_collection = False

def update_group_by_collection(self, context):
    """Callback to ensure only one grouping mode is active."""
    if self.light_editor_group_by_collection:
        self.light_editor_kind_alpha = False

def update_render_layer(self, context):
    """Callback when the selected render layer changes."""
    selected = self.selected_render_layer
    for vl in context.scene.view_layers:
        if vl.name == selected:
            context.window.view_layer = vl
            break

# --- Handler Functions ---

def light_editor_tag_redraw(scene, depsgraph):
    """Handler to redraw the 3D View when light data changes."""
    for update in depsgraph.updates:
        if isinstance(update.id, bpy.types.Light):
            for window in bpy.context.window_manager.windows:
                for area in window.screen.areas:
                    if area.type == 'VIEW_3D':
                        area.tag_redraw()
                        return

# --- Operator Classes ---

class LE_OT_ToggleEmission(bpy.types.Operator):
    """Operator to toggle the emission strength of a material."""
    bl_idname = "le.toggle_emission"
    bl_label = "Toggle Emission"
    mat_name: StringProperty()

    def execute(self, context):
        mat = bpy.data.materials.get(self.mat_name)
        if not mat or not mat.use_nodes:
            return {'CANCELLED'}
        nt = mat.node_tree

        emission_node = None
        principled_node = None

        def traverse_inputs(node, visited):
            nonlocal emission_node, principled_node
            if node in visited:
                return
            visited.add(node)
            if node.type == 'EMISSION':
                emission_node = node
            elif node.type == 'BSDF_PRINCIPLED':
                principled_node = node
            for input_socket in node.inputs:
                if input_socket.is_linked:
                    for link in input_socket.links:
                        traverse_inputs(link.from_node, visited)

        # Find the emission/principled node
        output_node = next((n for n in nt.nodes if n.type == 'OUTPUT_MATERIAL'), None)
        if output_node:
            surf_input = output_node.inputs.get('Surface')
            if surf_input and surf_input.is_linked:
                traverse_inputs(surf_input.links[0].from_node, set())

        # Toggle emission based on node type (4.0+ logic)
        if emission_node:
            strength_input = emission_node.inputs.get("Strength")
            if not strength_input:
                return {'CANCELLED'}
            current = strength_input.default_value
            if current > 0:
                mat["_original_emission_strength"] = current
                strength_input.default_value = 0.0
            else:
                restored = mat.get("_original_emission_strength", 10.0)
                strength_input.default_value = restored
        elif principled_node:
            strength_input = principled_node.inputs.get("Emission Strength") # 4.0+ name
            color_input = principled_node.inputs.get("Emission Color")       # 4.0+ name
            if strength_input:
                 current = strength_input.default_value
                 if current > 0:
                     mat["_original_emission_strength"] = current
                     strength_input.default_value = 0.0
                 else:
                     restored = mat.get("_original_emission_strength", 10.0)
                     strength_input.default_value = restored
            elif color_input:
                current = color_input.default_value[:3]
                if any(current):
                    mat["_original_emission_color"] = current
                    color_input.default_value = (0, 0, 0, 1)
                else:
                    restored = mat.get("_original_emission_color", (1, 1, 1))
                    color_input.default_value = restored + (1,)
        else:
            return {'CANCELLED'}
        return {'FINISHED'}

class LE_OT_isolate_emissive(bpy.types.Operator):
    """Operator to isolate a single emissive material."""
    bl_idname = "le.isolate_emissive"
    bl_label = "Isolate Emissive Material"
    mat_name: StringProperty()

    def execute(self, context):
        target = bpy.data.materials.get(self.mat_name)
        if not target:
            self.report({'WARNING'}, f"Material {self.mat_name} not found")
            return {'CANCELLED'}

        global _emissive_state_backup

        if not _emissive_state_backup:
            # Backup current states and turn off all emissives except target
            _emissive_state_backup = {}
            for obj, mat in find_emissive_objects(context):
                tree = mat.node_tree
                if not tree:
                    continue
                output = next((n for n in tree.nodes if n.type == 'OUTPUT_MATERIAL'), None)
                if not output:
                    continue
                input_socket = output.inputs.get("Surface")
                if input_socket and input_socket.is_linked:
                    emission_node = None
                    principled_node = None
                    def traverse_inputs(node, visited):
                        nonlocal emission_node, principled_node
                        if node in visited:
                            return
                        visited.add(node)
                        if node.type == 'EMISSION':
                            emission_node = node
                        elif node.type == 'BSDF_PRINCIPLED':
                            principled_node = node
                        for input_socket in node.inputs:
                            if input_socket.is_linked:
                                for link in input_socket.links:
                                    traverse_inputs(link.from_node, visited)
                    traverse_inputs(input_socket.links[0].from_node, set())

                    # Store backup based on node type (4.0+ names)
                    if emission_node and emission_node.inputs.get("Strength"):
                        _emissive_state_backup[mat.name] = ('EMISSION', emission_node.inputs["Strength"].default_value)
                    elif principled_node:
                        if principled_node.inputs.get("Emission Strength"):
                            _emissive_state_backup[mat.name] = ('PRINCIPLED', principled_node.inputs["Emission Strength"].default_value)
                        elif principled_node.inputs.get("Emission Color"): # 4.0+ name
                            _emissive_state_backup[mat.name] = ('PRINCIPLED_COLOR', principled_node.inputs["Emission Color"].default_value[:]) # 4.0+ name

            # Hide all light objects
            for obj in bpy.data.objects:
                if obj.type == 'LIGHT':
                    obj.hide_viewport = True
                    obj.hide_render = True

            # Turn off all other emissives
            for name, (node_type, value) in _emissive_state_backup.items():
                mat = bpy.data.materials.get(name)
                if not mat or name == self.mat_name:
                    continue
                nt = mat.node_tree
                output = next((n for n in nt.nodes if n.type == 'OUTPUT_MATERIAL'), None)
                if not output:
                    continue
                input_socket = output.inputs.get("Surface")
                if input_socket and input_socket.is_linked:
                    emission_node = None
                    principled_node = None
                    traverse_inputs(input_socket.links[0].from_node, set())
                    # Set emission to 0 based on backup type (4.0+ names)
                    if node_type == 'EMISSION' and emission_node:
                        emission_node.inputs["Strength"].default_value = 0
                    elif node_type == 'PRINCIPLED' and principled_node:
                        principled_node.inputs["Emission Strength"].default_value = 0
                    elif node_type == 'PRINCIPLED_COLOR' and principled_node:
                        principled_node.inputs["Emission Color"].default_value = (0, 0, 0, 1) # 4.0+ name

        else:
            # Restore states from backup
            for name, (node_type, val) in _emissive_state_backup.items():
                mat = bpy.data.materials.get(name)
                if not mat:
                    continue
                nt = mat.node_tree
                output = next((n for n in nt.nodes if n.type == 'OUTPUT_MATERIAL'), None)
                if not output:
                    continue
                input_socket = output.inputs.get("Surface")
                if input_socket and input_socket.is_linked:
                    emission_node = None
                    principled_node = None
                    traverse_inputs(input_socket.links[0].from_node, set())
                    # Restore emission value based on backup type (4.0+ names)
                    if node_type == 'EMISSION' and emission_node:
                        emission_node.inputs["Strength"].default_value = val
                    elif node_type == 'PRINCIPLED' and principled_node:
                        principled_node.inputs["Emission Strength"].default_value = val
                    elif node_type == 'PRINCIPLED_COLOR' and principled_node:
                        principled_node.inputs["Emission Color"].default_value = val + (1,) # 4.0+ name

            # Show all light objects
            for obj in bpy.data.objects:
                if obj.type == 'LIGHT':
                    obj.hide_viewport = False
                    obj.hide_render = False

            # Clear the backup
            _emissive_state_backup = {}
        return {'FINISHED'}

class LIGHT_OT_ToggleAllLights(bpy.types.Operator):
    """Operator to toggle the collapse state of the 'All Lights' section."""
    bl_idname = "light_editor.toggle_all_lights"
    bl_label = "Toggle All Lights Section"
    bl_description = "Collapse or expand the 'All Lights (Alphabetical)' section"

    def execute(self, context):
        context.scene.collapse_all_lights = not context.scene.collapse_all_lights
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
        return {'FINISHED'}

class LIGHT_OT_ToggleAllEmissives(bpy.types.Operator):
    """Operator to toggle the collapse state of the 'All Emissives' section."""
    bl_idname = "light_editor.toggle_all_emissives"
    bl_label = "Toggle All Emissive Materials Section"
    bl_description = "Collapse or expand the 'All Emissive Materials (Alphabetical)' section"

    def execute(self, context):
        context.scene.collapse_all_emissives = not context.scene.collapse_all_emissives
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
        return {'FINISHED'}

class LIGHT_OT_ToggleGroup(bpy.types.Operator):
    """Operator to toggle the collapse state of a light group (by kind/collection)."""
    bl_idname = "light_editor.toggle_group"
    bl_label = "Toggle Group"
    group_key: StringProperty()

    def execute(self, context):
        current = group_collapse_dict.get(self.group_key, False)
        group_collapse_dict[self.group_key] = not current
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
        return {'FINISHED'}

class LIGHT_OT_ToggleGroupAllOff(bpy.types.Operator):
    """Operator to toggle all lights within a group on/off."""
    bl_idname = "light_editor.toggle_group_all_off"
    bl_label = "Toggle Group All Off"
    group_key: StringProperty()

    def execute(self, context):
        global group_checkbox_1_state, group_lights_original_state
        is_on = group_checkbox_1_state.get(self.group_key, True)
        group_objs = self._get_group_objects(context, self.group_key)

        if is_on:
            # Turn all off, store original states
            original_states = {}
            for obj in group_objs:
                original_states[obj.name] = obj.light_enabled
            group_lights_original_state[self.group_key] = original_states
            for obj in group_objs:
                obj.light_enabled = False
            group_checkbox_1_state[self.group_key] = False
        else:
            # Restore original states
            original_states = group_lights_original_state.get(self.group_key, {})
            for obj in group_objs:
                obj.light_enabled = original_states.get(obj.name, True)
            if self.group_key in group_lights_original_state:
                del group_lights_original_state[self.group_key]
            group_checkbox_1_state[self.group_key] = True

        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
        return {'FINISHED'}

    def _get_group_objects(self, context, group_key):
        """Helper to get the list of light objects belonging to a specific group."""
        scene = context.scene
        filter_pattern = scene.light_editor_filter.lower()
        if filter_pattern:
            all_lights = [obj for obj in context.view_layer.objects
                          if obj.type == 'LIGHT' and re.search(filter_pattern, obj.name, re.I)]
        else:
            all_lights = [obj for obj in context.view_layer.objects if obj.type == 'LIGHT']

        if scene.filter_light_types == 'COLLECTION' and group_key.startswith("coll_"):
            coll_name = group_key[5:]
            return [obj for obj in all_lights
                    if (obj.users_collection and obj.users_collection[0].name == coll_name)
                    or (not obj.users_collection and coll_name == "No Collection")]

        if scene.filter_light_types == 'KIND' and group_key.startswith("kind_"):
            kind = group_key[5:]
            return [obj for obj in all_lights if obj.data.type == kind]

        return []

class LIGHT_OT_ToggleAllLightsAlpha(bpy.types.Operator):
    """Operator to toggle the collapse state of the 'All Lights (Alphabetical)' section in 'All' view."""
    bl_idname = "light_editor.toggle_all_lights_alpha"
    bl_label = "Toggle All Lights Alphabetical"
    bl_description = "Collapse or expand the 'All Lights (Alphabetical)' section"

    def execute(self, context):
        context.scene.collapse_all_lights_alpha = not context.scene.collapse_all_lights_alpha
        context.area.tag_redraw() # More direct redraw for the current area
        return {'FINISHED'}

class LIGHT_OT_ToggleAllEmissivesAlpha(bpy.types.Operator):
    """Operator to toggle the collapse state of the 'All Emissive Materials (Alphabetical)' section in 'All' view."""
    bl_idname = "light_editor.toggle_all_emissives_alpha"
    bl_label = "Toggle All Emissive Materials Alphabetical"
    bl_description = "Collapse or expand the 'All Emissive Materials (Alphabetical)' section"

    def execute(self, context):
        context.scene.collapse_all_emissives_alpha = not context.scene.collapse_all_emissives_alpha
        context.area.tag_redraw() # More direct redraw for the current area
        return {'FINISHED'}

class LIGHT_OT_ClearFilter(bpy.types.Operator):
    """Operator to clear the light filter text."""
    bl_idname = "le.clear_light_filter"
    bl_label = "Clear Filter"

    @classmethod
    def poll(cls, context):
        return context.scene.light_editor_filter

    def execute(self, context):
        if context.scene.light_editor_filter:
            context.scene.light_editor_filter = ""
        return {'FINISHED'}

class LIGHT_OT_SelectLight(bpy.types.Operator):
    """Operator to select/deselect a light object."""
    bl_idname = "le.select_light"
    bl_label = "Select Light"
    name: StringProperty()

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


# --- Panel Class ---

class LIGHT_PT_editor(bpy.types.Panel):
    """Main panel for the Light Editor UI."""
    bl_label = "Light Editor"
    bl_idname = "LIGHT_PT_editor"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Light Editor"

    @classmethod
    def poll(cls, context):
        # Force redraw by checking material node tree updates for emission/principled nodes
        # This helps the UI update when emission values are changed directly in the Shader Editor.
        for mat in bpy.data.materials:
            if mat.use_nodes and mat.node_tree:
                for node in mat.node_tree.nodes:
                    if node.type in {'EMISSION', 'BSDF_PRINCIPLED'}:
                        if node.type == 'EMISSION' and node.inputs.get("Color"):
                            _ = node.inputs["Color"].default_value # Access value
                        elif node.type == 'BSDF_PRINCIPLED':
                            emission_color_socket = node.inputs.get("Emission Color") # 4.0+ name
                            if emission_color_socket:
                                _ = emission_color_socket.default_value # Access value
                            emission_strength_socket = node.inputs.get("Emission Strength") # 4.0+ name
                            if emission_strength_socket:
                                _ = emission_strength_socket.default_value # Access value
        return True

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        # --- Filter and View Options ---
        layout.row().prop(scene, "filter_light_types", expand=True)
        layout.use_property_split = True
        layout.use_property_decorate = False

        # --- Filter Input ---
        row = layout.row(align=True)
        row.prop(scene, "light_editor_filter", text="", icon="VIEWZOOM")
        row.operator("le.clear_light_filter", text="", icon='PANEL_CLOSE')

        # --- Render Layer Selection (Collection View) ---
        if scene.filter_light_types == 'COLLECTION':
            row = layout.row()
            row.prop(scene, "selected_render_layer", text="Render Layer")

        # --- Filter Lights Based on Input ---
        filter_str = scene.light_editor_filter.lower()
        if filter_str:
            lights = [
                obj for obj in context.view_layer.objects
                if obj.type == 'LIGHT' and re.search(filter_str, obj.name, re.I)
            ]
        else:
            lights = [obj for obj in context.view_layer.objects if obj.type == 'LIGHT']

        # --- Draw UI Based on Selected View Mode ---
        if scene.filter_light_types == 'COLLECTION':
            self.draw_layer_collection(layout, scene, context.view_layer.layer_collection, lights)
        elif scene.filter_light_types == 'KIND':
            # Group lights by type
            groups = {'POINT': [], 'SPOT': [], 'SUN': [], 'AREA': []}
            for obj in lights:
                if obj.data.type in groups:
                    groups[obj.data.type].append(obj)

            # Draw Light Groups (POINT, SPOT, SUN, AREA)
            for kind in ('POINT', 'SPOT', 'SUN', 'AREA'):
                if groups[kind]:
                    group_key = f"kind_{kind}"
                    collapsed = group_collapse_dict.get(group_key, False)
                    header_box = layout.box()
                    header_row = header_box.row(align=True)
                    is_on_1 = group_checkbox_1_state.get(group_key, True)
                    icon_1 = 'CHECKBOX_HLT' if is_on_1 else 'CHECKBOX_DEHLT'
                    header_row.active = is_on_1
                    op_1 = header_row.operator("light_editor.toggle_group_all_off",
                                               text="",
                                               icon=icon_1,
                                               depress=is_on_1)
                    op_1.group_key = group_key
                    op_tri = header_row.operator("light_editor.toggle_group",
                                                 text="",
                                                 emboss=True,
                                                 icon='RIGHTARROW' if collapsed else 'DOWNARROW_HLT')
                    op_tri.group_key = group_key
                    header_row.label(text=f"{kind} Lights", icon=f"LIGHT_{kind}")
                    if not collapsed:
                        for obj in groups[kind]:
                            draw_main_row(header_box, obj)
                            if obj.light_expanded:
                                extra_box = header_box.box()
                                draw_extra_params(self, extra_box, obj, obj.data)

            # Draw Emissive Materials Group (Kind View)
            emissive_group_key = "kind_EMISSIVE"
            emissive_collapsed = group_collapse_dict.get(emissive_group_key, False)
            emissive_pairs = find_emissive_objects(context)
            if emissive_pairs:
                emissive_header_box = layout.box()
                emissive_header_row = emissive_header_box.row(align=True)
                emissive_is_on = group_checkbox_1_state.get(emissive_group_key, True)
                emissive_icon = 'CHECKBOX_HLT' if emissive_is_on else 'CHECKBOX_DEHLT'
                emissive_header_row.active = emissive_is_on
                emissive_op_1 = emissive_header_row.operator("light_editor.toggle_group_all_off",
                                                   text="",
                                                   icon=emissive_icon,
                                                   depress=emissive_is_on)
                emissive_op_1.group_key = emissive_group_key
                emissive_op_tri = emissive_header_row.operator("light_editor.toggle_group",
                                                     text="",
                                                     emboss=True,
                                                     icon='RIGHTARROW' if emissive_collapsed else 'DOWNARROW_HLT')
                emissive_op_tri.group_key = emissive_group_key
                emissive_header_row.label(text="Emissive Materials", icon='SHADING_RENDERED')
                if not emissive_collapsed:
                    emissive_content_box = emissive_header_box.box()
                    sorted_emissives = sorted(emissive_pairs, key=lambda pair: f"{pair[0].name}_{pair[1].name}".lower())
                    for obj, mat in sorted_emissives:
                        draw_emissive_row(emissive_content_box, obj, mat)
        else: # NO_FILTER - "All" view
            # "All Lights (Alphabetical)" Section
            lights_header_box = layout.box()
            lights_header_row = lights_header_box.row(align=True)
            all_lights_group_key = "all_lights_alpha"
            is_on_1 = group_checkbox_1_state.get(all_lights_group_key, True)
            icon_1 = 'CHECKBOX_HLT' if is_on_1 else 'CHECKBOX_DEHLT'
            lights_header_row.active = is_on_1
            op_1 = lights_header_row.operator("light_editor.toggle_group_all_off",
                                               text="",
                                               icon=icon_1,
                                               depress=is_on_1)
            op_1.group_key = all_lights_group_key
            is_lights_collapsed = group_collapse_dict.get(all_lights_group_key, False)
            op_tri = lights_header_row.operator("light_editor.toggle_group",
                                                 text="",
                                                 emboss=True,
                                                 icon='RIGHTARROW' if is_lights_collapsed else 'DOWNARROW_HLT')
            op_tri.group_key = all_lights_group_key
            lights_header_row.label(text="All Lights (Alphabetical)", icon='LIGHT_DATA')
            if not is_lights_collapsed:
                lights_content_box = lights_header_box.box()
                sorted_lights = sorted(lights, key=lambda o: o.name.lower())
                if not is_on_1:
                     lights_content_box.enabled = False
                for obj in sorted_lights:
                    draw_main_row(lights_content_box, obj)
                    if obj.light_expanded:
                        extra_box = lights_content_box.box()
                        draw_extra_params(self, extra_box, obj, obj.data)

            # "All Emissive Materials (Alphabetical)" Section
            emissive_header_box = layout.box()
            emissive_header_row = emissive_header_box.row(align=True)
            all_emissives_group_key = "all_emissives_alpha"
            is_on_2 = group_checkbox_1_state.get(all_emissives_group_key, True)
            icon_2 = 'CHECKBOX_HLT' if is_on_2 else 'CHECKBOX_DEHLT'
            emissive_header_row.active = is_on_2
            op_2 = emissive_header_row.operator("light_editor.toggle_group_all_off",
                                               text="",
                                               icon=icon_2,
                                               depress=is_on_2)
            op_2.group_key = all_emissives_group_key
            is_emissives_collapsed = group_collapse_dict.get(all_emissives_group_key, False)
            op_tri2 = emissive_header_row.operator("light_editor.toggle_group",
                                                 text="",
                                                 emboss=True,
                                                 icon='RIGHTARROW' if is_emissives_collapsed else 'DOWNARROW_HLT')
            op_tri2.group_key = all_emissives_group_key
            emissive_header_row.label(text="All Emissive Materials (Alphabetical)", icon='SHADING_RENDERED')
            if not is_emissives_collapsed:
                emissive_content_box = emissive_header_box.box()
                if not is_on_2:
                    emissive_content_box.enabled = False
                emissive_pairs = find_emissive_objects(context)
                sorted_emissive_pairs = sorted(emissive_pairs, key=lambda pair: f"{pair[0].name}_{pair[1].name}".lower())
                if not sorted_emissive_pairs:
                     emissive_content_box.label(text="No emissive materials found.")
                else:
                    for obj, mat in sorted_emissive_pairs:
                        draw_emissive_row(emissive_content_box, obj, mat)

    def draw_layer_collection(self, layout, scene, layer_coll, all_lights, collection_path="", level=0):
        """Recursively draws the layer collection tree for the 'Collection' view."""
        global group_checkbox_1_state, group_collapse_dict
        this_coll_name = layer_coll.collection.name
        if collection_path:
            full_path = collection_path + "/" + this_coll_name
        else:
            full_path = this_coll_name

        lights_in_this_coll = [obj for obj in layer_coll.collection.objects if obj in all_lights]
        children = layer_coll.children
        has_sub = (len(children) > 0)

        # Only draw if this collection or its children have lights
        if not lights_in_this_coll and not has_sub:
            return

        group_key = "coll_" + full_path
        collapsed = group_collapse_dict.get(group_key, False)
        header_box = layout.box()
        header_row = header_box.row(align=True)

        is_on_1 = group_checkbox_1_state.get(group_key, True)
        icon_1 = 'CHECKBOX_HLT' if is_on_1 else 'CHECKBOX_DEHLT'
        header_row.active = is_on_1
        op_1 = header_row.operator("light_editor.toggle_group_all_off",
                                   text="",
                                   icon=icon_1,
                                   depress=is_on_1)
        op_1.group_key = group_key

        op_tri = header_row.operator("light_editor.toggle_group",
                                     text="",
                                     emboss=True,
                                     icon='RIGHTARROW' if collapsed else 'DOWNARROW_HLT')
        op_tri.group_key = group_key

        header_row.label(text=this_coll_name, icon='OUTLINER_COLLECTION')

        if not collapsed:
            # Draw lights in this collection
            for obj in lights_in_this_coll:
                draw_main_row(header_box, obj)
                if obj.light_expanded:
                    extra_box = header_box.box()
                    draw_extra_params(self, extra_box, obj, obj.data)
            # Recursively draw child collections
            for child in children:
                self.draw_layer_collection(
                    layout=header_box,
                    scene=scene,
                    layer_coll=child,
                    all_lights=all_lights,
                    collection_path=full_path,
                    level=level+1
                )


# --- Persistent Handlers ---

@persistent
def LE_check_lights_enabled(dummy):
    """Handler on load to ensure light_enabled property matches visibility."""
    for obj in bpy.data.objects:
        if obj.type == 'LIGHT':
            if obj.hide_viewport and obj.hide_render:
                bpy.context.view_layer.objects[obj.name].light_enabled = False
            else:
                bpy.context.view_layer.objects[obj.name].light_enabled = True

@persistent
def LE_clear_handler(dummy):
    """Handler on load to initialize light_enabled property."""
    context = bpy.context
    for obj in bpy.data.objects:
        if obj.type == 'LIGHT':
            if obj.hide_viewport == False and obj.hide_render == False:
                context.view_layer.objects[obj.name].light_enabled = True
            else:
                context.view_layer.objects[obj.name].light_enabled = False


# --- Registration ---

# List of classes to register/unregister
classes = (
    # Group/Section Toggles
    LIGHT_OT_ToggleGroup,
    LIGHT_OT_ToggleGroupAllOff,
    LIGHT_OT_ToggleAllLights,
    LIGHT_OT_ToggleAllEmissives,
    LIGHT_OT_ToggleAllLightsAlpha,
    LIGHT_OT_ToggleAllEmissivesAlpha,

    # Utility Operators
    LIGHT_OT_ClearFilter,
    LIGHT_OT_SelectLight,

    # Emissive Material Operators
    LE_OT_ToggleEmission,
    LE_OT_isolate_emissive,

    # Main Panel
    LIGHT_PT_editor,
)

def register():
    """Registers properties, classes, and handlers."""
    # --- Scene Properties ---
    bpy.types.Scene.current_active_light = PointerProperty(type=bpy.types.Object)
    bpy.types.Scene.selected_render_layer = EnumProperty(
        name="Render Layer",
        description="Select the render layer",
        items=get_render_layer_items,
        update=update_render_layer
    )
    bpy.types.Scene.collapse_all_lights = BoolProperty(
        name="Collapse All Lights",
        default=False,
        description="Collapse the 'All Lights (Alphabetical)' section"
    )
    bpy.types.Scene.collapse_all_emissives = BoolProperty(
        name="Collapse All Emissive Materials",
        default=False,
        description="Collapse the 'All Emissive Materials (Alphabetical)' section"
    )
    bpy.types.Scene.light_editor_filter = StringProperty(
        name="Filter",
        default="",
        description="Filter lights by name (wildcards allowed)"
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
        items=(
            ('NO_FILTER', 'All', 'Show All no filter (Alphabetical)', 'NONE', 0),
            ('KIND', 'Kind', 'Filter lights by Kind', 'LIGHT_DATA', 1),
            ('COLLECTION', 'Collection', 'Filter lights by Collections', 'OUTLINER_COLLECTION', 2)
        )
    )
    bpy.types.Scene.collapse_all_lights_alpha = BoolProperty(
        name="Collapse All Lights Alphabetical",
        default=False,
        description="Collapse the 'All Lights (Alphabetical)' section in the 'All' view"
    )
    bpy.types.Scene.collapse_all_emissives_alpha = BoolProperty(
        name="Collapse All Emissive Materials Alphabetical",
        default=False,
        description="Collapse the 'All Emissive Materials (Alphabetical)' section in the 'All' view"
    )

    # --- Light Properties ---
    # These are used by the draw_extra_params function for Cycles/EEVEE settings.
    bpy.types.Light.soft_falloff = BoolProperty(default=False)
    bpy.types.Light.max_bounce = IntProperty(default=0, min=0, max=10)
    bpy.types.Light.multiple_instance = BoolProperty(default=False)
    bpy.types.Light.shadow_caustic = BoolProperty(default=False)
    bpy.types.Light.spread = FloatProperty(default=0.0, min=0.0, max=1.0)

    # --- Object Properties ---
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

    # --- Register Classes ---
    for cls in classes:
        bpy.utils.register_class(cls)

    # --- Register Handlers ---
    bpy.app.handlers.depsgraph_update_post.append(light_editor_tag_redraw)
    bpy.app.handlers.load_post.append(LE_clear_handler)
    bpy.app.handlers.load_post.append(LE_check_lights_enabled)

def unregister():
    """Unregisters properties, classes, and handlers."""
    # --- Unregister Handlers ---
    bpy.app.handlers.load_post.remove(LE_clear_handler)
    bpy.app.handlers.load_post.remove(LE_check_lights_enabled)
    if light_editor_tag_redraw in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(light_editor_tag_redraw)

    # --- Unregister Classes ---
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

    # --- Remove Scene Properties ---
    del bpy.types.Scene.current_active_light
    del bpy.types.Scene.selected_render_layer
    del bpy.types.Scene.collapse_all_lights
    del bpy.types.Scene.collapse_all_emissives
    del bpy.types.Scene.light_editor_filter
    del bpy.types.Scene.light_editor_kind_alpha
    del bpy.types.Scene.light_editor_group_by_collection
    del bpy.types.Scene.filter_light_types
    del bpy.types.Scene.collapse_all_lights_alpha
    del bpy.types.Scene.collapse_all_emissives_alpha

    # --- Remove Light Properties ---
    del bpy.types.Light.soft_falloff
    del bpy.types.Light.max_bounce
    del bpy.types.Light.multiple_instance
    del bpy.types.Light.shadow_caustic
    del bpy.types.Light.spread

    # --- Remove Object Properties ---
    del bpy.types.Object.light_enabled
    del bpy.types.Object.light_turn_off_others
    del bpy.types.Object.light_expanded

if __name__ == "__main__":
    register()
