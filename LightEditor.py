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
from bpy.app.translations import contexts as i18n_contexts
import re, os

class NullWriter:
    def write(self, text):
        pass
    def flush(self):
        pass

# --- Global State Tracking (UI visuals, operator states) ---
isolate_env_header_state = False
isolate_env_surface_state = False
isolate_env_volume_state = False
current_active_light = None
current_exclusive_group = None
group_checkbox_1_state = {}
group_lights_original_state = {}
group_collapse_dict = {}
collections_with_lights = {}
group_checkbox_2_state = {}
other_groups_original_state = {}
emissive_material_cache = {}
group_mat_checkbox_state = {}
environment_checkbox_state = {'environment': True}
_surface_link_backup = None
_volume_link_backup = None
_light_isolate_state_backup = {}
emissive_isolate_icon_state = {}
_emissive_link_backup = {}

def is_blender_4_5_or_higher():
    """Check if the Blender version is 4.5 or higher."""
    return bpy.app.version >= (4, 5, 0)

# --- New Unified Isolate System ---

class UnifiedOnOffManager:
    def __init__(self):
        # Backups for lights, emissive‐socket values, and environment links
        self._light_backup = {}
        self._material_backup = {}
        self._env_backup = {}

    def force_all_off(self, context, except_mode=None, except_identifier=None):
        """
        Turn off every light, every emissive socket (Emission nodes & Principled BSDF emission),
        and the world shader, except for the single item identified by (except_mode, except_identifier).
        """
        print(f"[UnifiedOnOffManager] force_all_off called with mode={except_mode}, identifier={except_identifier}")

        # --- Backup & disable all lights except the isolated one ---
        keep_lights = set()
        if except_mode in {UnifiedIsolateMode.LIGHT_ROW, UnifiedIsolateMode.LIGHT_GROUP} and except_identifier:
            keep_lights = except_identifier[0]

        for obj in bpy.data.objects:
            if obj.type == 'LIGHT':
                # backup
                self._light_backup[obj.name] = (
                    obj.hide_viewport, obj.hide_render, getattr(obj, "light_enabled", True)
                )
                if obj.name not in keep_lights:
                    print(f"[UnifiedOnOffManager]   Disabling light: {obj.name}")
                    obj.hide_viewport = True
                    obj.hide_render = True
                    obj.light_enabled = False
                else:
                    print(f"[UnifiedOnOffManager]   Keeping light: {obj.name}")

        # --- Disable all emissive sockets except the isolated one ---
        for mat in bpy.data.materials:
            if not mat.use_nodes:
                continue
            for node in mat.node_tree.nodes:
                # catch both pure Emission nodes and Principled BSDF emission sockets
                strength_socket = node.inputs.get("Strength") or node.inputs.get("Emission Strength")
                if not strength_socket:
                    continue

                ident = (mat.name, node.name)
                print(f"[UnifiedOnOffManager] Processing emissive socket on node: {ident}")

                if not (except_mode == UnifiedIsolateMode.MATERIAL and except_identifier == ident):
                    # backup & disable
                    self._material_backup[ident] = strength_socket.default_value
                    print(f"[UnifiedOnOffManager]   Disabling emissive socket on: {ident}")
                    strength_socket.default_value = 0.0
                else:
                    print(f"[UnifiedOnOffManager]   Keeping emissive socket on: {ident}")

        # --- Disconnect world Surface & Volume ---
        world = context.scene.world
        if world and world.use_nodes:
            nt = world.node_tree
            output = next((n for n in nt.nodes if n.type == 'OUTPUT_WORLD'), None)
            if output:
                for name in ("Surface", "Volume"):
                    sock = output.inputs.get(name)
                    if sock and sock.is_linked and sock.links:
                        link = sock.links[0]
                        self._env_backup[name] = (link.from_node.name, link.from_socket.name)
                        print(f"[UnifiedOnOffManager] Disconnecting world {name} link from {link.from_node.name}.{link.from_socket.name}")
                        nt.links.remove(link)

        # --- Redraw all areas ---
        for area in context.screen.areas:
            area.tag_redraw()

    def restore_all(self):
        """Restore lights, emissive‐socket values, and world links from backup."""
        print(f"[UnifiedOnOffManager] restore_all called")

        # Restore lights
        for obj in bpy.data.objects:
            if obj.type == 'LIGHT' and obj.name in self._light_backup:
                vp, rp, en = self._light_backup[obj.name]
                print(f"[UnifiedOnOffManager] Restoring light {obj.name}: vp={vp}, rp={rp}, enabled={en}")
                obj.hide_viewport = vp
                obj.hide_render = rp
                obj.light_enabled = en

        # Restore emissive sockets
        for ident, val in self._material_backup.items():
            mat_name, node_name = ident
            mat = bpy.data.materials.get(mat_name)
            if not mat or not mat.use_nodes:
                continue
            node = mat.node_tree.nodes.get(node_name)
            if not node:
                continue
            strength_socket = node.inputs.get("Strength") or node.inputs.get("Emission Strength")
            if strength_socket:
                print(f"[UnifiedOnOffManager] Restoring emissive socket on {ident} to {val}")
                strength_socket.default_value = val

        # Restore world links
        world = bpy.context.scene.world
        if world and world.use_nodes:
            nt = world.node_tree
            output = next((n for n in nt.nodes if n.type == 'OUTPUT_WORLD'), None)
            if output:
                for name, (from_n, from_s) in self._env_backup.items():
                    src = nt.nodes.get(from_n)
                    dst = output.inputs.get(name)
                    if src and dst and not dst.is_linked:
                        out_sock = src.outputs.get(from_s)
                        if out_sock:
                            print(f"[UnifiedOnOffManager] Restoring world {name} link from {from_n}.{from_s}")
                            nt.links.new(out_sock, dst)

        # Clear backups
        self._light_backup.clear()
        self._material_backup.clear()
        self._env_backup.clear()


class UnifiedIsolateMode:
    """Enum for different isolation modes."""
    LIGHT_GROUP = "LIGHT_GROUP"
    LIGHT_ROW = "LIGHT_ROW"
    MATERIAL = "MATERIAL"
    ENVIRONMENT = "ENVIRONMENT"
    MATERIAL_GROUP = "MATERIAL_GROUP"
    ENVIRONMENT_SURFACE = "ENVIRONMENT_SURFACE"
    ENVIRONMENT_VOLUME = "ENVIRONMENT_VOLUME"

class UnifiedIsolateManager:
    def __init__(self):
        self._backup = {}
        self._active_mode = None
        self._active_identifier = None

    def _redraw_areas(self, context):
        for area in context.screen.areas:
            if area.type in {'VIEW_3D', 'NODE_EDITOR', 'PROPERTIES'}:
                area.tag_redraw()

    def is_active(self, mode=None, identifier=None):
        if mode is None:
            return self._active_mode is not None
        if identifier is None:
            return self._active_mode == mode
        return self._active_mode == mode and self._active_identifier == identifier

    def get_active_info(self):
        return self._active_mode, self._active_identifier

    def activate(self, context, mode, identifier=None):
        self._backup.clear()
        self._active_mode = mode
        self._active_identifier = identifier

        # Initialize backup for all relevant states
        for obj in context.view_layer.objects:
            if obj.type == 'LIGHT':
                self._backup[obj.name] = (obj.hide_viewport, obj.hide_render)
        for obj, mat, node in find_emissive_objects(context):
            key = (mat.name, node.name)
            s = node.inputs.get("Strength") if node.type == 'EMISSION' else node.inputs.get("Emission Strength")
            if s:
                self._backup[key] = s.default_value
        world = context.scene.world
        if world and world.use_nodes:
            nt = world.node_tree
            output = next((n for n in nt.nodes if n.type == 'OUTPUT_WORLD'), None)
            if output:
                for name in ("Surface", "Volume"):
                    sock = output.inputs.get(name)
                    if sock and sock.is_linked and sock.links:
                        link = sock.links[0]
                        self._backup[f"env_link_{name}"] = (link.from_node.name, link.from_socket.name)

        # Turn everything off except the one we're isolating
        _unified_on_off_manager.force_all_off(context, except_mode=mode, except_identifier=identifier)

        # Specific mode handling
        if mode == UnifiedIsolateMode.LIGHT_GROUP or mode == UnifiedIsolateMode.LIGHT_ROW:
            to_keep_enabled, _ = identifier if identifier else (set(), set())
            for obj in context.view_layer.objects:
                if obj.type == 'LIGHT' and obj.name in to_keep_enabled:
                    obj.hide_viewport = self._backup[obj.name][0]
                    obj.hide_render = self._backup[obj.name][1]
        elif mode == UnifiedIsolateMode.MATERIAL:
            _, node_name = identifier if identifier else (None, None)
            for obj, mat, node in find_emissive_objects(context):
                if (mat.name, node.name) == identifier:
                    s = node.inputs.get("Strength") if node.type == 'EMISSION' else node.inputs.get("Emission Strength")
                    if s:
                        s.default_value = self._backup[(mat.name, node.name)]
        elif mode == UnifiedIsolateMode.ENVIRONMENT:
            world = context.scene.world
            if world and world.use_nodes:
                nt = world.node_tree
                output = next((n for n in nt.nodes if n.type == 'OUTPUT_WORLD'), None)
                if output:
                    for name in ("Surface", "Volume"):
                        if f"env_link_{name}" in self._backup:
                            from_node_name, from_socket_name = self._backup[f"env_link_{name}"]
                            from_node = nt.nodes.get(from_node_name)
                            from_socket = from_node.outputs.get(from_socket_name) if from_node else None
                            to_socket = output.inputs.get(name)
                            if from_socket and to_socket and not to_socket.is_linked:
                                nt.links.new(from_socket, to_socket)

        self._redraw_areas(context)

    def deactivate(self, context):
        # Restore everything from backup
        for key, val in self._backup.items():
            if isinstance(key, tuple):  # Emissive nodes
                mat_name, node_name = key
                mat = bpy.data.materials.get(mat_name)
                if mat and mat.use_nodes:
                    node = mat.node_tree.nodes.get(node_name)
                    if node:
                        s = node.inputs.get("Strength") if node.type == 'EMISSION' else node.inputs.get("Emission Strength")
                        if s:
                            s.default_value = val
            elif key.startswith("env_link_"):  # Environment links
                world = context.scene.world
                if world and world.use_nodes:
                    nt = world.node_tree
                    output = next((n for n in nt.nodes if n.type == 'OUTPUT_WORLD'), None)
                    if output:
                        socket_name = key.replace("env_link_", "")
                        from_node_name, from_socket_name = val
                        from_node = nt.nodes.get(from_node_name)
                        from_socket = from_node.outputs.get(from_socket_name) if from_node else None
                        to_socket = output.inputs.get(socket_name)
                        if from_socket and to_socket and not to_socket.is_linked:
                            nt.links.new(from_socket, to_socket)
            else:  # Lights
                obj = bpy.data.objects.get(key)
                if obj and obj.type == 'LIGHT':
                    obj.hide_viewport, obj.hide_render = val
                    obj.light_enabled = not (val[0] and val[1])

        self._backup.clear()
        self._active_mode = None
        self._active_identifier = None
        self._redraw_areas(context)

# --- Global instance of the manager ---
_unified_isolate_manager = UnifiedIsolateManager()
_unified_on_off_manager = UnifiedOnOffManager()


def update_render_layer(self, context):
    """Update the context to the selected render layer."""
    selected_layer_name = self.light_editor_selected_render_layer
    view_layer = context.scene.view_layers.get(selected_layer_name)
    if view_layer and context.window.view_layer != view_layer:
        context.window.view_layer = view_layer

def get_render_layer_items(self, context):
    """Generate items for the render layer enum property."""
    items = []
    for view_layer in context.scene.view_layers:
        items.append((view_layer.name, view_layer.name, f"Switch to {view_layer.name} render layer"))
    # Ensure the current view layer is always an option, even if list is somehow empty
    if not items:
        current_name = context.view_layer.name if context.view_layer else "Default"
        items.append((current_name, current_name, "Current render layer"))
    return items

def update_render_layer(self, context):
    """Update the current render layer."""
    selected = self.selected_render_layer
    for vl in context.scene.view_layers:
        if vl.name == selected:
            context.window.view_layer = vl
            break

def get_render_layer_items(self, context):
    """Get the list of render layers for the enum property."""
    items = []
    for view_layer in context.scene.view_layers:
        items.append((view_layer.name, view_layer.name, ""))
    return items

def gather_layer_collections(parent_lc, result):
    """Recursively gather all layer collections."""
    result.append(parent_lc)
    for child in parent_lc.children:
        gather_layer_collections(child, result)

def get_layer_collection_by_name(layer_collection, coll_name):
    """Find a layer collection by its name."""
    if layer_collection.collection.name == coll_name:
        return layer_collection
    for child in layer_collection.children:
        found = get_layer_collection_by_name(child, coll_name)
        if found:
            return found
    return None

def update_light_enabled(self, context):
    """Update light visibility based on the light_enabled property."""
    self.hide_viewport = not self.light_enabled
    self.hide_render = not self.light_enabled

def update_light_turn_off_others(self, context):
    global group_checkbox_2_state, emissive_isolate_icon_state
    scene = context.scene
    world = scene.world
    nt = world.node_tree if world and world.use_nodes else None
    output_node = next((n for n in nt.nodes if n.type == 'OUTPUT_WORLD'), None) if nt else None

    if self.light_turn_off_others:
        # --- Activate Isolation ---
        # 1. Manage mutual exclusivity
        if scene.current_active_light and scene.current_active_light != self:
            scene.current_active_light.light_turn_off_others = False
        scene.current_active_light = self

        # 2. Prepare identifier for UnifiedIsolateManager
        to_keep_enabled = {self.name}
        to_keep_emissive = set()  # No emissive nodes kept active for light isolation

        # 3. Activate isolation using UnifiedIsolateManager
        _unified_isolate_manager.activate(context, UnifiedIsolateMode.LIGHT_ROW, identifier=(to_keep_enabled, to_keep_emissive))

        # 4. Update UI states
        group_key = f"light_{self.name}"
        group_checkbox_2_state[group_key] = True

    else:
        # --- Deactivate Isolation ---
        # 1. Clear active light tracking
        if scene.current_active_light == self:
            scene.current_active_light = None

        # 2. Deactivate isolation using UnifiedIsolateManager
        if _unified_isolate_manager.is_active(UnifiedIsolateMode.LIGHT_ROW):
            _unified_isolate_manager.deactivate(context)

        # 3. Update UI states
        group_key = f"light_{self.name}"
        group_checkbox_2_state[group_key] = False
        for key in list(emissive_isolate_icon_state.keys()):
            emissive_isolate_icon_state[key] = False  # Reset emissive isolate icons

    # --- Redraw UI ---
    for area in context.screen.areas:
        if area.type in {'VIEW_3D', 'NODE_EDITOR', 'PROPERTIES'}:
            area.tag_redraw()

def get_all_collections(obj):
    """Get all collections an object belongs to, including nested paths."""
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

def is_emissive_node_active(node):
    if node.type == 'EMISSION':
        strength_socket = node.inputs.get("Strength")
        color_socket = node.inputs.get("Color")
    elif node.type == 'BSDF_PRINCIPLED':
        strength_socket = node.inputs.get("Emission Strength")
        color_socket = node.inputs.get("Emission Color")
    else:
        return False

    if not strength_socket or not color_socket:
        return False

    # If either socket is linked, assume potentially active
    if strength_socket.is_linked or color_socket.is_linked:
        return True

    # If not linked, check values
    strength = strength_socket.default_value
    color = color_socket.default_value[:3]  # RGB
    return strength > 0 and any(c > 0 for c in color)

def find_emissive_objects(context, search_objects=None):
    """Find all objects with emissive materials, including all reachable emissive nodes."""
    global emissive_material_cache

    objects_to_search = search_objects if search_objects is not None else context.view_layer.objects
    use_cache = (search_objects is None)
    cache_key = f"{context.view_layer.name}_{len(bpy.data.materials)}_{len(bpy.data.objects)}" if use_cache else None

    if use_cache and cache_key in emissive_material_cache:
        return emissive_material_cache[cache_key]

    emissive_objs = []
    seen = set()

    for obj in objects_to_search:
        if obj.type != 'MESH':
            continue
        for slot in obj.material_slots:
            mat = slot.material
            if not mat or not mat.use_nodes or mat.name in seen:
                continue
            seen.add(mat.name)
            nt = mat.node_tree
            output_node = next((n for n in nt.nodes if n.type == 'OUTPUT_MATERIAL' and n.is_active_output), None)
            if not output_node or not output_node.inputs.get('Surface') or not output_node.inputs['Surface'].is_linked:
                continue

            def find_emission_nodes(node, visited, found_nodes):
                if node in visited:
                    return
                visited.add(node)
                if node.type == 'EMISSION':
                    found_nodes.append(node)
                elif node.type == 'BSDF_PRINCIPLED' and node.inputs.get("Emission Strength"):
                    found_nodes.append(node)
                for input_socket in node.inputs:
                    if input_socket.is_linked:
                        for link in input_socket.links:
                            find_emission_nodes(link.from_node, visited, found_nodes)

            found_nodes = []
            for link in output_node.inputs['Surface'].links:
                find_emission_nodes(link.from_node, set(), found_nodes)
            for node in found_nodes:
                if is_emissive_node_active(node):
                    emissive_objs.append((obj, mat, node))

    if use_cache:
        if not emissive_objs:
            if cache_key in emissive_material_cache:
                del emissive_material_cache[cache_key]
        else:
            emissive_material_cache[cache_key] = emissive_objs

    return emissive_objs

def draw_environment_row(box, context):
    """Draw the environment row in the UI."""
    world = context.scene.world
    if not world or not world.use_nodes:
        return
    row = box.row(align=True)
    nt = world.node_tree
    background_node = next((n for n in nt.nodes if n.type == 'BACKGROUND'), None)
    if not background_node:
        return
    color_input = background_node.inputs.get("Color")
    strength_input = background_node.inputs.get("Strength")
    enabled = strength_input.default_value > 0 if strength_input else False
    icon = 'OUTLINER_OB_LIGHT' if enabled else 'LIGHT_DATA'
    row.operator("le.toggle_environment", text="", icon=icon, depress=enabled)
    iso_icon = 'RADIOBUT_ON' if _unified_isolate_manager.is_active(UnifiedIsolateMode.ENVIRONMENT) else 'RADIOBUT_OFF'
    row.operator("le.isolate_environment", text="", icon=iso_icon)
    row.operator("le.select_environment", text="", icon='WORLD')
    world_col = row.column(align=True)
    world_col.scale_x = 0.5
    world_col.prop(world, "name", text="")
    value_row = row.row(align=True)
    col_color = value_row.row(align=True)
    col_color.ui_units_x = 4
    col_strength = value_row.row(align=True)
    col_strength.ui_units_x = 6
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

def draw_emissive_row(box, obj, mat, emissive_nodes):
    """
    Draw a row for a material, with a collapsible sub-list for emissive nodes.
    - If multiple_nodes OR single-node with linked socket, split into four equal columns.
    - Otherwise, use the regular layout.
    """
    row = box.row(align=True)
    multiple_nodes = len(emissive_nodes) > 1
    first_node = emissive_nodes[0]
    group_key = f"mat_{mat.name}_{obj.name}"
    collapsed = group_collapse_dict.get(group_key, False)

    # --- Toggle & Isolate (header) ---
    enabled = any(
        (n.inputs.get("Strength") or n.inputs.get("Emission Strength")).default_value > 0 or
        (n.inputs.get("Strength") or n.inputs.get("Emission Strength")).is_linked
        for n in emissive_nodes
    )
    icon = 'OUTLINER_OB_LIGHT' if enabled else 'LIGHT_DATA'
    op_toggle = row.operator("le.toggle_emission", text="", icon=icon, depress=enabled)
    op_toggle.mat_name = mat.name
    op_toggle.node_name = ""

    iso_active = emissive_isolate_icon_state.get((mat.name, ""), False)
    iso_icon   = 'RADIOBUT_ON' if iso_active else 'RADIOBUT_OFF'
    op_iso     = row.operator("le.isolate_emissive", text="", icon=iso_icon)
    op_iso.mat_name  = mat.name
    op_iso.node_name = ""

    # --- Select & Expand ---
    row.operator("le.select_light", text="",
                 icon="RESTRICT_SELECT_ON" if obj.select_get() else "RESTRICT_SELECT_OFF"
    ).name = obj.name

    if multiple_nodes:
        exp_icon = 'DOWNARROW_HLT' if not collapsed else 'RIGHTARROW'
        row.operator("light_editor.toggle_group", text="", emboss=True, icon=exp_icon).group_key = group_key
    else:
        row.label(text="", icon='BLANK1')

    # Determine if single-node linked case
    color_input    = first_node.inputs.get("Color") if first_node.type == 'EMISSION' else first_node.inputs.get("Emission Color")
    strength_input = first_node.inputs.get("Strength") if first_node.type == 'EMISSION' else first_node.inputs.get("Emission Strength")
    linked_case = (not multiple_nodes) and ((color_input and color_input.is_linked) or (strength_input and strength_input.is_linked))

    # --- Header columns: equal for multi-node or linked single-node ---
    if multiple_nodes or linked_case:
        col_width = 12
        # Object name
        col_obj = row.column(align=True)
        col_obj.ui_units_x = col_width
        col_obj.prop(obj, "name", text="")
        # Material name
        col_mat = row.column(align=True)
        col_mat.ui_units_x = col_width
        col_mat.prop(mat, "name", text="")
        # Color placeholder
        col_color = row.column(align=True)
        col_color.ui_units_x = col_width
        rc = col_color.row(align=True)
        rc.alignment = 'EXPAND'
        rc.label(icon='NODETREE', text="See Nodes")
        rc.enabled = False
        # Strength placeholder
        col_strength = row.column(align=True)
        col_strength.ui_units_x = col_width
        rs = col_strength.row(align=True)
        rs.alignment = 'EXPAND'
        rs.label(icon='NODETREE', text="See Nodes")
        rs.enabled = False
    else:
        # --- Regular layout for single-node without links ---
        # Object name
        col_obj = row.column(align=True)
        col_obj.scale_x = 0.5
        col_obj.prop(obj, "name", text="")
        # Material name
        col_mat = row.column(align=True)
        col_mat.scale_x = 0.5
        col_mat.prop(mat, "name", text="")
        # Color socket
        col_color = row.column(align=True)
        col_color.ui_units_x = 4
        if color_input:
            draw_socket_with_icon(col_color, color_input, text="")
        else:
            col_color.label(text="")
        # Strength socket
        col_strength = row.column(align=True)
        col_strength.ui_units_x = 6
        if strength_input:
            draw_socket_with_icon(col_strength, strength_input, text="")
        else:
            col_strength.label(text="")

    # --- Sub-rows for each emissive node ---
    if multiple_nodes and not collapsed:
        sub_box = box.box()
        for subnode in sorted(emissive_nodes, key=lambda x: x.name.lower()):
            sub_row = sub_box.row(align=True)
            sub_row.label(text="", icon='BLANK1')

            s_in = subnode.inputs.get("Strength") if subnode.type == 'EMISSION' else subnode.inputs.get("Emission Strength")
            val = s_in.default_value if s_in else 0.0
            ico = 'OUTLINER_OB_LIGHT' if (s_in and (s_in.is_linked or val > 0)) else 'LIGHT_DATA'
            op_n = sub_row.operator("le.toggle_emission", text="", icon=ico, depress=(val > 0))
            op_n.mat_name  = mat.name
            op_n.node_name = subnode.name

            iso_n = emissive_isolate_icon_state.get((mat.name, subnode.name), False)
            ico_ni = 'RADIOBUT_ON' if iso_n else 'RADIOBUT_OFF'
            op_ni = sub_row.operator("le.isolate_emissive", text="", icon=ico_ni)
            op_ni.mat_name  = mat.name
            op_ni.node_name = subnode.name

            # Node name only
            col_node = sub_row.column(align=True)
            col_node.scale_x = 0.5
            col_node.prop(subnode, "name", text="")

            # Color socket
            c_col = sub_row.column(align=True)
            c_col.ui_units_x = 4
            color_in = subnode.inputs.get("Color") if subnode.type == 'EMISSION' else subnode.inputs.get("Emission Color")
            if color_in:
                if color_in.is_linked:
                    rc = c_col.row(align=True)
                    rc.alignment = 'EXPAND'
                    rc.label(icon='NODETREE', text="See Nodes")
                    rc.enabled = False
                else:
                    draw_socket_with_icon(c_col, color_in, text="")
            else:
                c_col.label(text="")

            # Strength socket
            c_str = sub_row.column(align=True)
            c_str.ui_units_x = 6
            if s_in:
                if s_in.is_linked:
                    rs = c_str.row(align=True)
                    rs.alignment = 'EXPAND'
                    rs.label(icon='NODETREE', text="See Nodes")
                    rs.enabled = False
                else:
                    draw_socket_with_icon(c_str, s_in, text="")
            else:
                c_str.label(text="")
                
def update_group_by_kind(self, context):
    """Ensure 'By Kind' and 'By Collection' are mutually exclusive."""
    if self.light_editor_kind_alpha:
        self.light_editor_group_by_collection = False

def update_group_by_collection(self, context):
    """Ensure 'By Kind' and 'By Collection' are mutually exclusive."""
    if self.light_editor_group_by_collection:
        self.light_editor_kind_alpha = False

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
    """Check if MNEE is available (Metal-specific check)."""
    if use_metal(context):
        import platform
        version, _, _ = platform.mac_ver()
        major_version = version.split(".")[0]
        if int(major_version) < 13:
            return False
    return True

def draw_extra_params(self, box, obj, light):
    """Draw extra light parameters based on the light type and render engine."""
    if light and isinstance(light, bpy.types.Light) and not light.use_nodes:
        layout = box
        row = layout.row()
        row.prop(light, "type", expand=True)
        col = layout.column()
        col.separator()
        if is_blender_4_5_or_higher():
            col.prop(light, "use_temperature", text="Use Temperature")
            if light.use_temperature:
                col.prop(light, "temperature", text="Temperature")
            col.prop(light, "normalize", text="Normalize")
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

# --- Operators (refactored to use UnifiedIsolateManager) ---



class LE_OT_ToggleEnvironment(bpy.types.Operator):
    """Toggle the environment lighting on/off."""
    bl_idname = "le.toggle_environment"
    bl_label = "Toggle Environment Lighting"

    def execute(self, context):
        global environment_checkbox_state, _surface_link_backup, _volume_link_backup
        world = context.scene.world
        if not world or not world.use_nodes:
            self.report({'WARNING'}, "No world or world shader found")
            return {'CANCELLED'}
        nt = world.node_tree
        background_node = next((n for n in nt.nodes if n.type == 'BACKGROUND'), None)
        if not background_node:
            self.report({'WARNING'}, "No Background node found in world shader")
            return {'CANCELLED'}
        strength_input = background_node.inputs.get("Strength")
        if not strength_input:
            self.report({'WARNING'}, "Background node has no Strength input")
            return {'CANCELLED'}
        output_node = next((n for n in nt.nodes if n.type == 'OUTPUT_WORLD'), None)
        if not output_node:
            self.report({'WARNING'}, "No World Output node found")
            return {'CANCELLED'}
        is_on = environment_checkbox_state.get('environment', True)
        if is_on:
            # Store current state and disable
            world['original_environment_strength'] = strength_input.default_value
            strength_input.default_value = 0.0
            # Disconnect Surface and Volume inputs
            for socket_name in ("Surface", "Volume"):
                socket = output_node.inputs.get(socket_name)
                if socket and socket.is_linked and socket.links:
                    try:
                        link = socket.links[0]
                        if link.is_valid:
                            if socket_name == "Surface":
                                _surface_link_backup = (link.from_node.name, link.from_socket.name)
                            else:
                                _volume_link_backup = (link.from_node.name, link.from_socket.name)
                            nt.links.remove(link)
                    except Exception as e:
                        self.report({'WARNING'}, f"Failed to remove link for {socket_name}: {e}")
        else:
            # Restore state
            restored_strength = world.get('original_environment_strength', 1.0)
            strength_input.default_value = restored_strength
            # Reconnect Surface and Volume inputs
            for socket_name, backup in [("Surface", _surface_link_backup), ("Volume", _volume_link_backup)]:
                if backup:
                    node_name, socket_name_from = backup
                    from_node = nt.nodes.get(node_name)
                    from_socket = from_node.outputs.get(socket_name_from) if from_node else None
                    to_socket = output_node.inputs.get(socket_name)
                    if from_socket and to_socket and not to_socket.is_linked:
                        try:
                            nt.links.new(from_socket, to_socket)
                        except Exception as e:
                            self.report({'WARNING'}, f"Failed to restore link for {socket_name}: {e}")
                    # Clear backup after restoration (optional, keeps it clean)
                    if socket_name == "Surface":
                        _surface_link_backup = None
                    else:
                        _volume_link_backup = None
        environment_checkbox_state['environment'] = not is_on
        # Redraw relevant areas
        for area in context.screen.areas:
            if area.type in ('VIEW_3D', 'NODE_EDITOR'):
                area.tag_redraw()
        return {'FINISHED'}

def execute(self, context):
    global isolate_env_header_state, isolate_env_surface_state, isolate_env_volume_state
    global env_isolated_ui_state  # ← ADD THIS

    flag_map = {
        "HEADER": "isolate_env_header_state",
        "SURFACE": "isolate_env_surface_state",
        "VOLUME": "isolate_env_volume_state",
    }
    mode_map = {
        "HEADER": UnifiedIsolateMode.ENVIRONMENT,
        "SURFACE": UnifiedIsolateMode.ENVIRONMENT_SURFACE,
        "VOLUME": UnifiedIsolateMode.ENVIRONMENT_VOLUME,
    }

    unified_mode = mode_map.get(self.mode, UnifiedIsolateMode.ENVIRONMENT)
    is_currently_active = _unified_isolate_manager.is_active(unified_mode)

    if not is_currently_active:
        globals()[flag_map[self.mode]] = True
        if self.mode == "HEADER":
            env_isolated_ui_state = True  # ← SET TRUE when activated
        _unified_isolate_manager.activate(context, unified_mode)
    else:
        globals()[flag_map[self.mode]] = False
        if self.mode == "HEADER":
            env_isolated_ui_state = False  # ← SET FALSE when deactivated
        _unified_isolate_manager.deactivate(context)

    return {'FINISHED'}

class LE_OT_SelectEnvironment(bpy.types.Operator):
    """Select the environment world in the Shader Editor."""
    bl_idname = "le.select_environment"
    bl_label = "Select Environment"

    def execute(self, context):
        world = context.scene.world
        if not world:
            self.report({'WARNING'}, "No world found")
            return {'CANCELLED'}
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                for space in area.spaces:
                    if space.type == 'VIEW_3D':
                        space.shading.type = 'MATERIAL'
                        break
                break
        for area in context.screen.areas:
            if area.type == 'NODE_EDITOR':
                area.spaces.active.node_tree = world.node_tree
                break
        else:
            self.report({'INFO'}, "No Shader Editor found; open one to edit world shader")
        self.report({'INFO'}, f"Selected world: {world.name}")
        return {'FINISHED'}

class LE_OT_SelectGroup(bpy.types.Operator):
    """Select all objects in the specified group."""
    bl_idname = "le.select_group"
    bl_label = "Select Group"
    group_key: bpy.props.StringProperty()

    def execute(self, context):
        objects_to_select = []
        objects_in_group = []
        deselect_all_flag = False
        filter_str = context.scene.light_editor_filter.lower()

        # Handle different group types
        if self.group_key.startswith("coll_"):
            coll_name = self.group_key[5:]
            if coll_name == "No Collection":
                for obj in context.view_layer.objects:
                    if obj.type == 'LIGHT' or (obj.type == 'MESH' and any(mat in [m for o, m, n in find_emissive_objects(context)] for mat in obj.material_slots)):
                        objects_in_group.append(obj)
                        if len(obj.users_collection) == 1 and obj.users_collection[0].name == "Scene Collection":
                            if (not filter_str or re.search(filter_str, obj.name, re.I)) and (obj.type != 'LIGHT' or obj.light_enabled):
                                objects_to_select.append(obj)
            else:
                collection = bpy.data.collections.get(coll_name)
                if collection:
                    for obj in collection.all_objects:
                        if obj.type == 'LIGHT' or (obj.type == 'MESH' and any(mat in [m for o, m, n in find_emissive_objects(context)] for mat in obj.material_slots)):
                            objects_in_group.append(obj)
                            if (not filter_str or re.search(filter_str, obj.name, re.I)) and (obj.type != 'LIGHT' or obj.light_enabled):
                                objects_to_select.append(obj)
        elif self.group_key.startswith("kind_"):
            kind = self.group_key[5:]
            if kind == "EMISSIVE":
                for obj, mat, node in find_emissive_objects(context):
                    if not filter_str or re.search(filter_str, obj.name, re.I) or re.search(filter_str, mat.name, re.I):
                        objects_in_group.append(obj)
                        objects_to_select.append(obj)
            else:
                for obj in context.view_layer.objects:
                    if obj.type == 'LIGHT' and obj.data.type == kind:
                        if obj.light_enabled:
                            objects_in_group.append(obj)
                        if (not filter_str or re.search(filter_str, obj.name, re.I)) and obj.light_enabled:
                            objects_to_select.append(obj)
        elif self.group_key == "all_lights_alpha":
            for obj in context.view_layer.objects:
                if obj.type == 'LIGHT' and obj.light_enabled:
                    objects_in_group.append(obj)
                    if not filter_str or re.search(filter_str, obj.name, re.I):
                        objects_to_select.append(obj)
        elif self.group_key == "all_emissives_alpha":
            for obj, mat, node in find_emissive_objects(context):
                if not filter_str or re.search(filter_str, obj.name, re.I) or re.search(filter_str, mat.name, re.I):
                    objects_in_group.append(obj)
                    objects_to_select.append(obj)
        elif self.group_key == "selected_lights":
            for obj in context.view_layer.objects:
                if obj.type == 'LIGHT' and obj.select_get() and obj.light_enabled:
                    objects_in_group.append(obj)
                    if not filter_str or re.search(filter_str, obj.name, re.I):
                        objects_to_select.append(obj)
        elif self.group_key == "selected_emissives":
            for obj, mat, node in find_emissive_objects(context):
                if obj.select_get():
                    if not filter_str or re.search(filter_str, obj.name, re.I) or re.search(filter_str, mat.name, re.I):
                        objects_in_group.append(obj)
                        objects_to_select.append(obj)
        elif self.group_key == "not_selected_lights":
            for obj in context.view_layer.objects:
                if obj.type == 'LIGHT' and not obj.select_get() and obj.light_enabled:
                    objects_in_group.append(obj)
                    if not filter_str or re.search(filter_str, obj.name, re.I):
                        objects_to_select.append(obj)
        elif self.group_key == "not_selected_emissives":
            for obj, mat, node in find_emissive_objects(context):
                if not obj.select_get():
                    if not filter_str or re.search(filter_str, obj.name, re.I) or re.search(filter_str, mat.name, re.I):
                        objects_in_group.append(obj)
                        objects_to_select.append(obj)
        elif self.group_key == "env_header":
            self.report({'INFO'}, "Selected world: {}".format(context.scene.world.name))
            for area in context.screen.areas:
                if area.type == 'NODE_EDITOR':
                    area.spaces.active.node_tree = context.scene.world.node_tree
            return {'FINISHED'}

        # --- Determine Action: Select or Deselect All ---
        selected_objects = [obj for obj in objects_in_group if obj.name in context.view_layer.objects and obj.select_get()]
        if objects_in_group and all(obj.select_get() for obj in objects_in_group if obj.name in context.view_layer.objects):
            deselect_all_flag = True

        # --- Perform Action ---
        any_selected = False
        if deselect_all_flag:
            bpy.ops.object.select_all(action='DESELECT')
            self.report({'INFO'}, f"Deselected all objects in group: {self.group_key}")
        else:
            bpy.ops.object.select_all(action='DESELECT')
            for obj in objects_to_select:
                if obj.name in context.view_layer.objects:
                    obj.select_set(True)
                    any_selected = True
                    if not context.view_layer.objects.active:
                        context.view_layer.objects.active = obj
            if any_selected:
                self.report({'INFO'}, f"Selected {len(objects_to_select)} objects in group: {self.group_key}")
            else:
                self.report({'INFO'}, f"No selectable objects found in group: {self.group_key}")

        # Redraw the UI to update icons
        for area in context.screen.areas:
            if area.type in ('VIEW_3D', 'NODE_EDITOR', 'PROPERTIES'):
                area.tag_redraw()

        return {'FINISHED'}
                        
class LE_OT_ToggleEmission(bpy.types.Operator):
    bl_idname = "le.toggle_emission"
    bl_label = "Toggle Emission"
    mat_name: StringProperty()
    node_name: StringProperty()

    def execute(self, context):
        global _emissive_link_backup
        mat = bpy.data.materials.get(self.mat_name)
        if not mat or not mat.use_nodes:
            self.report({'WARNING'}, f"Material {self.mat_name} not found or has no nodes")
            return {'CANCELLED'}
        nt = mat.node_tree

        # Debug: log call context
        print(f"LE_OT_ToggleEmission called for material {self.mat_name}, node_name='{self.node_name}'")

        # Determine nodes to toggle
        if self.node_name.strip():
            # Sub-list: toggle specific node
            node = nt.nodes.get(self.node_name)
            if not node:
                self.report({'WARNING'}, f"Node {self.node_name} not found in material {self.mat_name}")
                return {'CANCELLED'}
            nodes_to_toggle = [node]
        else:
            # Main row: toggle all emissive nodes
            nodes_to_toggle = [
                n for n in nt.nodes
                if n.type in {'EMISSION', 'BSDF_PRINCIPLED'}
                and (n.inputs.get("Strength") or n.inputs.get("Emission Strength"))
            ]
            if not nodes_to_toggle:
                self.report({'WARNING'}, f"No emissive nodes found in material {self.mat_name}")
                return {'CANCELLED'}

        # Debug: log nodes and their initial states
        print(f"Toggling {len(nodes_to_toggle)} nodes: {[(n.name, n.type) for n in nodes_to_toggle]}")
        for node in nodes_to_toggle:
            strength_socket = node.inputs.get("Strength") if node.type == 'EMISSION' else node.inputs.get("Emission Strength")
            print(f"Node {node.name}: linked={strength_socket.is_linked}, value={strength_socket.default_value if not strength_socket.is_linked else 'N/A'}")

        # Initialize backup storage
        if mat.name not in _emissive_link_backup:
            _emissive_link_backup[mat.name] = {}
        store = _emissive_link_backup[mat.name]

        # Check if material is on
        is_on = any(
            (n.inputs.get("Strength") or n.inputs.get("Emission Strength")).is_linked or
            (n.inputs.get("Strength") or n.inputs.get("Emission Strength")).default_value > 0
            for n in nodes_to_toggle
        )
        print(f"Material {self.mat_name} is_on={is_on}")

        # Toggle nodes
        for node in nodes_to_toggle:
            strength_socket = node.inputs.get("Strength") if node.type == 'EMISSION' else node.inputs.get("Emission Strength")
            if not strength_socket:
                print(f"Skipping node {node.name}: no Strength/Emission Strength input")
                continue
            key = f"{mat.name}:{node.name}:Strength"
            if is_on:
                # Turn off: store state and set to 0
                if strength_socket.is_linked and strength_socket.links:
                    link = strength_socket.links[0]
                    store[key] = ('LINK', link.from_node.name, link.from_socket.name)
                    nt.links.remove(link)
                    print(f"Stored link for {node.name}: {link.from_node.name}.{link.from_socket.name}")
                else:
                    store[key] = ('VALUE', strength_socket.default_value)
                    strength_socket.default_value = 0
                    print(f"Stored value for {node.name}: {strength_socket.default_value}")
            else:
                # Turn on: restore state or set to 1.0
                if key in store:
                    typ, *data = store[key]
                    if typ == 'LINK':
                        from_node = nt.nodes.get(data[0])
                        from_socket = from_node.outputs.get(data[1]) if from_node else None
                        if from_socket:
                            nt.links.new(from_socket, strength_socket)
                            print(f"Restored link for {node.name}: {data[0]}.{data[1]}")
                        else:
                            print(f"Failed to restore link for {node.name}: node/socket not found")
                    else:
                        strength_socket.default_value = data[0]
                        print(f"Restored value for {node.name}: {data[0]}")
                    del store[key]
                else:
                    strength_socket.default_value = 1.0
                    print(f"No backup for {node.name}, set to 1.0")

        # Clean up empty backup
        if not store:
            _emissive_link_backup.pop(mat.name, None)

        # Debug: log final backup state
        print(f"Backup for {mat.name}: {store}")

        # Redraw UI
        for area in context.screen.areas:
            if area.type in {'VIEW_3D', 'NODE_EDITOR', 'PROPERTIES'}:
                area.tag_redraw()
        return {'FINISHED'}
    
def _disable_material_node(self, mat, node):
    global _emissive_link_backup
    if not node or not mat.use_nodes:
        return
    nt = mat.node_tree

    strength = node.inputs.get("Strength") if node.type == 'EMISSION' else node.inputs.get("Emission Strength")
    color = node.inputs.get("Color") if node.type == 'EMISSION' else node.inputs.get("Emission Color")
    socket = strength or color
    if not socket:
        return

    key = f"{mat.name}:{node.name}:{socket.name}"

    if socket.is_linked and socket.links:
        link = socket.links[0]
        _emissive_link_backup[key] = ('LINK', node.name, socket.name, link.from_node.name, link.from_socket.name)
        nt.links.remove(link)
    else:
        if socket.name == "Color":
            _emissive_link_backup[key] = ('VALUE', node.name, socket.name, tuple(socket.default_value[:]))
            socket.default_value = (0, 0, 0, 1)
        else:
            _emissive_link_backup[key] = ('VALUE', node.name, socket.name, socket.default_value)
            socket.default_value = 0
    
class LE_OT_isolate_emissive(bpy.types.Operator):
    """Toggle isolation of emissive nodes—or entire material if node_name == ""."""
    bl_idname = "le.isolate_emissive"
    bl_label  = "Isolate Emissive"
    mat_name:  bpy.props.StringProperty()
    node_name: bpy.props.StringProperty(default="")  # empty = header/group

    def execute(self, context):
        key = (self.mat_name, self.node_name or "")
        is_active = _unified_isolate_manager.is_active(
            UnifiedIsolateMode.MATERIAL, identifier=key
        )

        if not is_active:
            _unified_isolate_manager.activate(
                context, UnifiedIsolateMode.MATERIAL, identifier=key
            )
            emissive_isolate_icon_state[key] = True
        else:
            _unified_isolate_manager.deactivate(context)
            emissive_isolate_icon_state[key] = False

        return {'FINISHED'}


class EMISSIVE_OT_ToggleGroupAllOff(bpy.types.Operator):
    bl_idname = "light_editor.toggle_group_emissive_all_off"
    bl_label = "Toggle Emissive Group On/Off"

    group_key: StringProperty()

    def execute(self, context):
        global group_mat_checkbox_state, _emissive_link_backup

        is_on = group_mat_checkbox_state.get(self.group_key, True)
        emissive_pairs = find_emissive_objects(context)
        filtered_pairs = []
        seen_materials = set()

        # Filter based on group_key
        if self.group_key.startswith("emissive_"):
            coll_name = self.group_key[9:]
            for obj, mat, node in emissive_pairs:
                if obj.users_collection and obj.users_collection[0].name == coll_name:
                    if mat.name not in seen_materials:
                        filtered_pairs.append((obj, mat, node))
                        seen_materials.add(mat.name)
        elif self.group_key in ("kind_EMISSIVE", "all_emissives_alpha"):
            for obj, mat, node in emissive_pairs:
                if mat.name not in seen_materials:
                    filtered_pairs.append((obj, mat, node))
                    seen_materials.add(mat.name)

        # Toggle materials
        for obj, mat, node in filtered_pairs:
            if not mat or not mat.use_nodes:
                continue
            nt = mat.node_tree

            # Group nodes by material for this specific pair
            material_nodes = [n for n in nt.nodes if n.type in {'EMISSION', 'BSDF_PRINCIPLED'}]
            emissive_nodes = []
            for n in material_nodes:
                strength_socket = n.inputs.get("Strength") if n.type == 'EMISSION' else n.inputs.get("Emission Strength")
                if strength_socket:
                    emissive_nodes.append(n)

            if not emissive_nodes:
                continue

            key = mat.name
            if is_on:
                # --- Turn OFF ---
                if mat.name not in _emissive_link_backup:
                    _emissive_link_backup[mat.name] = {}
                store = _emissive_link_backup[mat.name]

                for node in emissive_nodes:
                    strength_socket = node.inputs.get("Strength") if node.type == 'EMISSION' else node.inputs.get("Emission Strength")

                    # Handle Strength only
                    if strength_socket:
                        s_key = f"{node.name}:Strength"
                        if strength_socket.is_linked:
                            link = strength_socket.links[0]
                            store[s_key] = ('LINK', link.from_node.name, link.from_socket.name)
                            nt.links.remove(link)
                        else:
                            store[s_key] = ('VALUE', strength_socket.default_value)
                            strength_socket.default_value = 0

            else:
                # --- Turn ON ---
                if mat.name in _emissive_link_backup:
                    store = _emissive_link_backup[mat.name]
                    for node in emissive_nodes:
                        strength_socket = node.inputs.get("Strength") if node.type == 'EMISSION' else node.inputs.get("Emission Strength")

                        # Restore Strength only
                        if strength_socket:
                            s_key = f"{node.name}:Strength"
                            if s_key in store:
                                data = store[s_key]
                                if data[0] == 'LINK':
                                    from_node_name, from_socket_name = data[1], data[2]
                                    from_node = nt.nodes.get(from_node_name)
                                    if from_node:
                                        from_socket = from_node.outputs.get(from_socket_name)
                                        if from_socket:
                                            nt.links.new(from_socket, strength_socket)
                                elif data[0] == 'VALUE':
                                    strength_socket.default_value = data[1]
                                # Remove the key from the store after restoration
                                del store[s_key]

                    # If the store for this material is empty, remove the material entry
                    if not store:
                        del _emissive_link_backup[mat.name]

        group_mat_checkbox_state[self.group_key] = not is_on

        # Request UI Redraw
        for area in context.screen.areas:
            if area.type in ('VIEW_3D', 'NODE_EDITOR', 'PROPERTIES'):
                area.tag_redraw()

        return {'FINISHED'}
    
class LIGHT_OT_ToggleGroup(bpy.types.Operator):
    """Toggle the collapse state of a group."""
    bl_idname = "light_editor.toggle_group"
    bl_label = "Toggle Group"
    group_key: bpy.props.StringProperty()

    def execute(self, context):
        group_collapse_dict[self.group_key] = not group_collapse_dict.get(self.group_key, False)
        for area in context.screen.areas:
            if area.type in {'VIEW_3D', 'NODE_EDITOR', 'PROPERTIES'}:
                area.tag_redraw()
        return {'FINISHED'}

class LIGHT_OT_ToggleCollection(bpy.types.Operator):
    """Exclude collection or turn off its lights."""
    bl_idname = "light_editor.toggle_collection"
    bl_label = "Collection Control"
    # Use bl_property to link the enum property for invoke_props_dialog
    bl_property = "action"

    group_key: bpy.props.StringProperty()

    # Define the action as an EnumProperty
    action: bpy.props.EnumProperty(
        items=[
            ('EXCLUDE', "Turn Collection OFF", "Exclude the collection"),
            ('TURN_OFF_LIGHTS', "Turn Lights OFF", "Turn off lights within the collection"),
        ],
        default='EXCLUDE',
        options={'SKIP_SAVE'} # Prevents saving to blend file
    )

    def invoke(self, context, event):
        coll_name = self.group_key[5:]
        collection = bpy.data.collections.get(coll_name)
        if not collection:
            return {'CANCELLED'}

        layer_collection = get_layer_collection_by_name(context.view_layer.layer_collection, coll_name)
        has_meshes = any(obj.type == 'MESH' for obj in collection.all_objects)

        # If it has meshes and is not excluded, show the dialog
        if has_meshes and layer_collection and not layer_collection.exclude:
            # invoke_props_dialog will show the enum property 'action' and a confirmation button
            return context.window_manager.invoke_props_dialog(self, width=400)
        # Skip dialog if no mesh objects – just exclude
        return self.execute(context) # Defaults to EXCLUDE action

    def draw(self, context):
        layout = self.layout
        layout.label(text="Collection also contains objects.")
        layout.label(text="What would you like to do?")
        layout.separator()
        # This will draw the 'action' enum property
        layout.prop(self, "action", expand=True)

    def execute(self, context):
        coll_name = self.group_key[5:]
        collection = bpy.data.collections.get(coll_name)
        if not collection:
            return {'CANCELLED'}

        layer_collection = get_layer_collection_by_name(context.view_layer.layer_collection, coll_name)
        if not layer_collection:
             return {'CANCELLED'}

        # Perform action based on the 'action' property
        if self.action == 'EXCLUDE':
            def toggle_exclusion_recursive(lc, exclude):
                lc.exclude = exclude
                for child in lc.children:
                    toggle_exclusion_recursive(child, exclude)

            # Toggle the exclusion state
            new_exclude_state = not layer_collection.exclude
            toggle_exclusion_recursive(layer_collection, new_exclude_state)

        elif self.action == 'TURN_OFF_LIGHTS':
            for obj in collection.all_objects:
                if obj.type == 'LIGHT':
                    obj.light_enabled = False
                    obj.hide_viewport = True
                    obj.hide_render = True

        # Redraw
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
        # Returning FINISHED here will close the dialog invoked by invoke_props_dialog
        return {'FINISHED'}

    def exclude_collection(self, context, collection, layer_collection):
        def toggle_exclusion_recursive(lc, exclude):
            lc.exclude = exclude
            for child in lc.children:
                toggle_exclusion_recursive(child, exclude)
        toggle_exclusion_recursive(layer_collection, not layer_collection.exclude)

        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
        return {'FINISHED'}

class EMISSIVE_OT_IsolateGroup(bpy.types.Operator):
    """Isolate all emissive materials within a group."""
    bl_idname = "light_editor.isolate_group_emissive"
    bl_label = "Isolate Emissive Group"
    group_key: StringProperty()

    def execute(self, context):
        global group_checkbox_2_state
        is_currently_active = group_checkbox_2_state.get(self.group_key, False)
        to_keep_emissive = set()
        emissive_pairs = find_emissive_objects(context)
        if self.group_key.startswith("emissive_"):
            coll_name = self.group_key[9:]
            for obj, mat, node in emissive_pairs:
                if obj.users_collection and obj.users_collection[0].name == coll_name:
                    to_keep_emissive.add(mat.name)
        elif self.group_key in ("kind_EMISSIVE", "all_emissives_alpha"):
            for obj, mat, node in emissive_pairs:
                to_keep_emissive.add(mat.name)
        if not is_currently_active:
            group_checkbox_2_state[self.group_key] = True
            _unified_isolate_manager.activate(context, UnifiedIsolateMode.MATERIAL_GROUP, identifier=(set(), to_keep_emissive))
        else:
            group_checkbox_2_state[self.group_key] = False
            if _unified_isolate_manager.is_active(UnifiedIsolateMode.MATERIAL_GROUP):
                _unified_isolate_manager.deactivate(context)
        return {'FINISHED'}

class LIGHT_OT_ToggleKind(bpy.types.Operator):
    """Toggle the visibility of lights of a specific kind."""
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
        if group_key == "all_lights_alpha":
            return all_lights
        if group_key.startswith("kind_"):
            kind = group_key[5:]
            return [obj for obj in all_lights if obj.data.type == kind]
        return []

class LE_OT_toggle_env_socket(bpy.types.Operator):
    """Toggle the connection of an environment input socket (Surface/Volume)."""
    bl_idname = "le.toggle_env_socket"
    bl_label = "Toggle Environment Input"
    socket_name: bpy.props.StringProperty()

    def execute(self, context):
        global _surface_link_backup, _volume_link_backup
        world = context.scene.world
        if not world or not world.use_nodes:
            self.report({'WARNING'}, "No world with nodes found")
            return {'CANCELLED'}
        nt = world.node_tree
        output_node = next((n for n in nt.nodes if n.type == 'OUTPUT_WORLD'), None)
        if not output_node:
            self.report({'WARNING'}, "No World Output node")
            return {'CANCELLED'}
        socket = output_node.inputs.get(self.socket_name)
        if not socket:
            self.report({'WARNING'}, f"No input socket named '{self.socket_name}'")
            return {'CANCELLED'}
        if socket.is_linked:
            link = socket.links[0]
            from_socket = link.from_socket
            from_node = link.from_node
            nt.links.remove(link)
            if self.socket_name == "Surface":
                _surface_link_backup = (from_node.name, from_socket.name)
            else:
                _volume_link_backup = (from_node.name, from_socket.name)
        else:
            if self.socket_name == "Surface" and _surface_link_backup:
                node_name, socket_name = _surface_link_backup
            elif self.socket_name == "Volume" and _volume_link_backup:
                node_name, socket_name = _volume_link_backup
            else:
                self.report({'INFO'}, f"No stored connection for {self.socket_name}")
                return {'CANCELLED'}
            from_node = nt.nodes.get(node_name)
            from_socket = from_node.outputs.get(socket_name) if from_node else None
            if from_node and from_socket:
                nt.links.new(from_socket, socket)
            else:
                self.report({'WARNING'}, f"Stored node/socket not found for {self.socket_name}")
        for area in context.screen.areas:
            if area.type == 'NODE_EDITOR':
                area.tag_redraw()
        return {'FINISHED'}

class LIGHT_OT_ToggleGroupExclusive(bpy.types.Operator):
    """Toggle exclusive isolation for a group of lights and emissives."""
    bl_idname = "light_editor.toggle_group_exclusive"
    bl_label = "Toggle Exclusive Light Group"
    group_key: bpy.props.StringProperty()

    def execute(self, context):
        global group_checkbox_2_state # Access global state
        # --- 1. Determine New State ---
        is_currently_active = group_checkbox_2_state.get(self.group_key, False)
        new_state = not is_currently_active
        # --- 2. Handle Activation/Deactivation using UnifiedIsolateManager ---
        if new_state:
            # --- Activate Isolation ---
            # a. Determine members of the group
            to_keep_enabled = set() # For light object names
            to_keep_emissive = set() # For material names
            lights = [obj for obj in context.view_layer.objects if obj.type == 'LIGHT']
            # find_emissive_objects must be defined before this point
            emissive_pairs = find_emissive_objects(context)
            if self.group_key.startswith("coll_"):
                coll_name = self.group_key[5:]
                if coll_name == "No Collection":
                    for obj in lights:
                        if len(obj.users_collection) == 1 and obj.users_collection[0].name == "Scene Collection":
                            to_keep_enabled.add(obj.name)
                    for obj, mat in emissive_pairs:
                         if len(obj.users_collection) == 1 and obj.users_collection[0].name == "Scene Collection":
                            to_keep_emissive.add(mat.name)
                else:
                    for obj in lights:
                        if any(coll.name == coll_name for coll in obj.users_collection):
                            to_keep_enabled.add(obj.name)
                    for obj, mat in emissive_pairs:
                        if any(coll.name == coll_name for coll in obj.users_collection):
                            to_keep_emissive.add(mat.name)
            elif self.group_key.startswith("kind_"):
                kind = self.group_key[5:]
                if kind == "EMISSIVE":
                    # This case is for the Emissive Materials Kind group header
                    for obj, mat in emissive_pairs:
                        to_keep_emissive.add(mat.name)
                else:
                    # This case is for standard Light Kind group headers (POINT, SUN, etc.)
                    for obj in lights:
                        if obj.data.type == kind:
                            to_keep_enabled.add(obj.name)
            elif self.group_key == "all_lights_alpha":
                # All Lights group
                for obj in lights:
                    to_keep_enabled.add(obj.name)
            elif self.group_key == "all_emissives_alpha":
                 # All Emissive Materials group
                 for obj, mat in emissive_pairs:
                    to_keep_emissive.add(mat.name)
            # b. Activate the unified isolate manager for LIGHT_GROUP mode
            # Pass the sets of lights and emissives to keep enabled
            _unified_isolate_manager.activate(
                context,
                UnifiedIsolateMode.LIGHT_GROUP,
                identifier=(to_keep_enabled, to_keep_emissive)
            )
        else:
            # --- Deactivate Isolation ---
            # Check if the currently active isolation is indeed this specific LIGHT_GROUP
            # This prevents conflicts if another isolation mode was activated manually
            current_mode, current_id = _unified_isolate_manager.get_active_info()
            if current_mode == UnifiedIsolateMode.LIGHT_GROUP:
                 _unified_isolate_manager.deactivate(context)
            # If it wasn't active or was a different mode, deactivating is a safe no-op for the manager.
            # We still proceed to update the UI state flag.
        # --- 3. Update Global UI State Flag ---
        group_checkbox_2_state[self.group_key] = new_state
        # --- 4. Request UI Redraw ---
        for area in context.screen.areas:
            if area.type in ('VIEW_3D', 'NODE_EDITOR'):
                area.tag_redraw()
        return {'FINISHED'}

class LIGHT_OT_ClearFilter(bpy.types.Operator):
    """Clear the filter text field."""
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
    """Select or deselect a specific light object."""
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

def draw_socket_with_icon(layout, socket, label=""):
    row = layout.row(align=True)
    if socket.is_linked:
        row.enabled = False
    row.template_node_socket(color=socket.draw_color)
    row.prop(socket, "default_value", text=label)

def draw_socket_with_icon(layout, socket, text="", linked_only=False, icon='NODETREE'):
    """Draws a socket's value with an icon if linked, or just the value if not."""
    if socket.is_linked:
        row = layout.row(align=True)
        row.alignment = 'EXPAND'
        row.label(icon=icon)
        if not linked_only:
            row.enabled = False
            row.prop(socket, "default_value", text=text)
        else:
            row.label(text=text)
    else:
        if hasattr(socket, 'default_value'):
            try:
                layout.prop(socket, "default_value", text=text)
            except:
                layout.label(text=f"{text[:3]}?" if text else "Val?")
        else:
            layout.label(text=text if text else "")

def node_tree_has_linked_emission_inputs(ntree):
    """Returns True if any emission-related inputs are linked in this node tree."""
    for node in ntree.nodes:
        if node.type == 'EMISSION':
            if node.inputs.get("Strength") and node.inputs["Strength"].is_linked:
                return True
            if node.inputs.get("Color") and node.inputs["Color"].is_linked:
                return True
        elif node.type == 'BSDF_PRINCIPLED':
            if node.inputs.get("Emission Strength") and node.inputs["Emission Strength"].is_linked:
                return True
            if node.inputs.get("Emission Color") and node.inputs["Emission Color"].is_linked:
                return True
    return False


def group_emissive_by_material(pairs):
    """Group emissive objects by material and object, preserving node information."""
    from collections import defaultdict
    grouped = defaultdict(list)
    for obj, mat, node in pairs:
        key = (obj.name, mat.name)
        grouped[key].append(node)
    result = [(bpy.data.objects[obj_name], bpy.data.materials[mat_name], nodes)
              for (obj_name, mat_name), nodes in grouped.items()]
    return result

def draw_emissive_grouped_by_ntree(scene, container_box, emissive_pairs):
    """Group and display emissive materials by shared node trees with collapsible sections."""
    emissive_by_ntree = defaultdict(list)
    for obj, mat in emissive_pairs:
        if mat.use_nodes and mat.node_tree:
            emissive_by_ntree[mat.node_tree].append((obj, mat))

    for ntree in sorted(emissive_by_ntree, key=lambda nt: emissive_by_ntree[nt][0][1].name.lower()):
        entries = emissive_by_ntree[ntree]
        group_key = f"ntree_{ntree.as_pointer()}"
        if group_key not in group_collapse_dict:
            group_collapse_dict[group_key] = False
        collapsed = group_collapse_dict[group_key]

        first_obj, first_mat = entries[0]

        # Draw main row
        header = container_box.box()
        row = header.row(align=True)
        row.operator("le.toggle_emission", text="", icon='OUTLINER_OB_LIGHT', depress=True).mat_name = first_mat.name
        row.operator("le.isolate_emissive", text="", icon='RADIOBUT_ON' if emissive_isolate_icon_state.get(first_mat.name, False) else 'RADIOBUT_OFF').mat_name = first_mat.name
        row.operator("le.select_light", text="", icon='RESTRICT_SELECT_ON' if first_obj.select_get() else 'RESTRICT_SELECT_OFF').name = first_obj.name

        expand_col = row.column(align=True)
        expand_op = expand_col.operator("light_editor.toggle_group", text="", icon='RIGHTARROW' if collapsed else 'DOWNARROW_HLT')
        expand_op.group_key = group_key
        expand_col.enabled = len(entries) > 1

        row.column(align=True).prop(first_obj, "name", text="")
        row.column(align=True).prop(first_mat, "name", text="")

        # Always show sockets, possibly with NODETREE icons
        output_node = next((n for n in ntree.nodes if n.type == 'OUTPUT_MATERIAL'), None)
        surf_input = output_node.inputs.get('Surface') if output_node else None
        from_node = surf_input.links[0].from_node if surf_input and surf_input.is_linked else None

        emission_node = None
        principled_node = None
        def find_nodes(node, visited):
            nonlocal emission_node, principled_node
            if not node or node in visited:
                return
            visited.add(node)
            if node.type == 'EMISSION':
                emission_node = node
            elif node.type == 'BSDF_PRINCIPLED':
                principled_node = node
            for inp in node.inputs:
                if inp.is_linked:
                    for link in inp.links:
                        find_nodes(link.from_node, visited)

        find_nodes(from_node, set())

        color_input = None
        strength_input = None
        if emission_node:
            color_input = emission_node.inputs.get("Color")
            strength_input = emission_node.inputs.get("Strength")
        elif principled_node:
            color_input = principled_node.inputs.get("Emission Color")
            strength_input = principled_node.inputs.get("Emission Strength")

        col_color = row.column(align=True)
        col_strength = row.column(align=True)

        def draw_socket_with_icon(col, socket):
            if socket:
                if socket.is_linked:
                    r = col.row(align=True)
                    r.alignment = 'EXPAND'
                    r.label(icon='NODETREE')
                    r.enabled = False
                    r.prop(socket, "default_value", text="")
                else:
                    try:
                        col.prop(socket, "default_value", text="")
                    except:
                        col.label(text="?")
            else:
                col.label(text="")

        draw_socket_with_icon(col_color, color_input)
        draw_socket_with_icon(col_strength, strength_input)

        if not collapsed:
            obj, mat = entries[0]
            nt = mat.node_tree
            for node in nt.nodes:
                if node.type in {'EMISSION', 'BSDF_PRINCIPLED'}:
                    draw_emissive_node_row(container_box, obj, mat, node)


def draw_emissive_node_row(box, obj, mat, node):
    """Draws a row for a specific emissive node inside a material."""
    row = box.row(align=True)

    if node.type == 'EMISSION':
        color_input = node.inputs.get("Color")
        strength_input = node.inputs.get("Strength")
    elif node.type == 'BSDF_PRINCIPLED':
        color_input = node.inputs.get("Emission Color")
        strength_input = node.inputs.get("Emission Strength")
    else:
        return

    enabled = False
    if strength_input:
        if strength_input.is_linked:
            enabled = True
        else:
            try:
                enabled = strength_input.default_value > 0
            except:
                pass
    if not enabled and color_input:
        try:
            enabled = any(c > 0.0 for c in color_input.default_value[:3])
        except:
            pass

    icon = 'OUTLINER_OB_LIGHT' if enabled else 'LIGHT_DATA'

    # Toggle emission
    op = row.operator("le.toggle_emission", text="", icon=icon, depress=enabled)
    op.mat_name = mat.name

    # Per-node isolate button
    iso_key = (mat.name, node.name)
    iso_icon = 'RADIOBUT_ON' if emissive_isolate_icon_state.get(iso_key, False) else 'RADIOBUT_OFF'
    op = row.operator("le.isolate_emissive", text="", icon=iso_icon)
    op.mat_name = mat.name
    op.node_name = node.name

    # Label
    row.label(text=f"{mat.name} / {node.name}")

    col_color = row.column(align=True)
    col_strength = row.column(align=True)

    if color_input:
        draw_socket_with_icon(col_color, color_input)
    else:
        col_color.label(text="")

    if strength_input:
        draw_socket_with_icon(col_strength, strength_input)
    else:
        col_strength.label(text="")


def draw_main_row(box, obj):
    """Draw a single light object row in the UI, with equal-width color/strength/exposure fields."""
    light = obj.data
    row = box.row(align=True)

    # --- On/Off, Exclusive, Select, Expand ---
    controls = row.row(align=True)
    controls.prop(obj, "light_enabled", text="",
                  icon="OUTLINER_OB_LIGHT" if obj.light_enabled else "LIGHT_DATA")
    controls.prop(obj, "light_turn_off_others", text="",
                  icon="RADIOBUT_ON" if obj.light_turn_off_others else "RADIOBUT_OFF")
    controls.operator("le.select_light", text="",
                      icon="RESTRICT_SELECT_ON" if obj.select_get() else "RESTRICT_SELECT_OFF").name = obj.name
    exp = controls.row(align=True)
    exp.enabled = not light.use_nodes
    exp.prop(obj, "light_expanded", text="",
             emboss=True,
             icon='DOWNARROW_HLT' if obj.light_expanded else 'RIGHTARROW')

    # --- Name column ---
    col_name = row.column(align=True)
    col_name.ui_units_x = 6
    col_name.prop(obj, "name", text="")

    # --- Value columns (equal width) ---
    col_color    = row.column(align=True)
    col_strength = row.column(align=True)
    col_exposure = row.column(align=True)

    # Make all three the same width
    uniform_width = 5.0
    col_color.ui_units_x    = uniform_width
    col_strength.ui_units_x = uniform_width
    col_exposure.ui_units_x = uniform_width

    if light.use_nodes:
        nt = light.node_tree
        output_node = next((n for n in nt.nodes if n.type == 'OUTPUT_LIGHT'), None)
        surface_in = output_node.inputs.get("Surface") if output_node else None

        if surface_in and surface_in.is_linked:
            from_node = surface_in.links[0].from_node
            if from_node.type == 'EMISSION':
                color_input = from_node.inputs.get("Color")
                strength_input = from_node.inputs.get("Strength")

                # — Color —
                if color_input:
                    if color_input.is_linked:
                        rc = col_color.row(align=True)
                        rc.alignment = 'EXPAND'
                        rc.label(icon='NODETREE', text="See Nodes")
                        rc.enabled = False
                    else:
                        col_color.prop(color_input, "default_value", text="")
                else:
                    col_color.label(text="", icon='ERROR')

                # — Strength —
                if strength_input:
                    if strength_input.is_linked:
                        rs = col_strength.row(align=True)
                        rs.alignment = 'EXPAND'
                        rs.label(icon='NODETREE', text="See Nodes")
                        rs.enabled = False
                    else:
                        draw_socket_with_icon(col_strength, strength_input, text="")
                else:
                    col_strength.label(text="", icon='ERROR')
            else:
                # Not an Emission node: fallback to direct properties
                col_color.prop(light, "color", text="")
                col_strength.prop(light, "energy", text="")
        else:
            # No node-linked surface: direct properties
            col_color.prop(light, "color", text="")
            col_strength.prop(light, "energy", text="")
    else:
        # Non-node lights: direct properties
        col_color.prop(light, "color", text="")
        col_strength.prop(light, "energy", text="")

    # --- Exposure / dummy field ---
    if hasattr(light, "exposure"):
        col_exposure.prop(light, "exposure", text="Exp.")
    else:
        d = col_exposure.row(align=True)
        d.enabled = False
        d.label(text="")


class LE_OT_IsolateEnvironment(bpy.types.Operator):
    """Isolate the environment lighting."""
    bl_idname = "le.isolate_environment"
    bl_label = "Isolate Environment Lighting"
    mode: bpy.props.StringProperty(default="HEADER")

    def execute(self, context):
        global isolate_env_header_state, isolate_env_surface_state, isolate_env_volume_state
        global env_isolated_ui_state  # Declare global at the start

        flag_map = {
            "HEADER": "isolate_env_header_state",
            "SURFACE": "isolate_env_surface_state",
            "VOLUME": "isolate_env_volume_state",
        }
        mode_map = {
            "HEADER": UnifiedIsolateMode.ENVIRONMENT,
            "SURFACE": UnifiedIsolateMode.ENVIRONMENT_SURFACE,
            "VOLUME": UnifiedIsolateMode.ENVIRONMENT_VOLUME,
        }

        unified_mode = mode_map.get(self.mode, UnifiedIsolateMode.ENVIRONMENT)
        is_currently_active = _unified_isolate_manager.is_active(unified_mode)

        if not is_currently_active:
            globals()[flag_map[self.mode]] = True
            if self.mode == "HEADER":
                env_isolated_ui_state = True  # Assign after global declaration
            _unified_isolate_manager.activate(context, unified_mode)
        else:
            globals()[flag_map[self.mode]] = False
            if self.mode == "HEADER":
                env_isolated_ui_state = False  # Assign after global declaration
            _unified_isolate_manager.deactivate(context)

        for area in context.screen.areas:
            if area.type in {'VIEW_3D', 'NODE_EDITOR'}:
                area.tag_redraw()
        return {'FINISHED'}

class LIGHT_PT_editor(bpy.types.Panel):
    """Main Light Editor panel in the 3D View sidebar."""
    bl_label = "Light Editor"
    bl_idname = "LIGHT_PT_editor"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Light Editor"

    @classmethod
    def poll(cls, context):
        # Ensure any material with emission inputs is preloaded
        for mat in bpy.data.materials:
            if mat.use_nodes and mat.node_tree:
                for node in mat.node_tree.nodes:
                    if node.type in {'EMISSION', 'BSDF_PRINCIPLED'}:
                        if node.type == 'EMISSION' and node.inputs.get("Color"):
                            _ = node.inputs["Color"].default_value
                        elif node.type == 'BSDF_PRINCIPLED':
                            color_sock = node.inputs.get("Emission Color")
                            strength_sock = node.inputs.get("Emission Strength")
                            if color_sock:
                                _ = color_sock.default_value
                            if strength_sock:
                                _ = strength_sock.default_value
        return True

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        # --- 1. Filter Type Buttons ---
        layout.row().prop(scene, "filter_light_types", expand=True)

        # --- 2. Render Layer Selector ---
        if len(context.scene.view_layers) > 1 and scene.filter_light_types == 'COLLECTION':
            layout.prop(scene, "light_editor_selected_render_layer", text="Render Layer")

        # --- 3. Search Bar ---
        layout.use_property_split = True
        layout.use_property_decorate = False
        row = layout.row(align=True)
        row.prop(scene, "light_editor_filter", text="", icon="VIEWZOOM")
        row.operator("le.clear_light_filter", text="", icon='PANEL_CLOSE')
        filter_str = scene.light_editor_filter.lower()

        # --- 4. Gather Lights and Emissive Nodes ---
        try:
            lights = [o for o in context.view_layer.objects if o.type == 'LIGHT' and (not filter_str or re.search(filter_str, o.name, re.I))]
        except Exception as e:
            layout.box().label(text=f"Error filtering lights: {e}", icon='ERROR')
            lights = []
        try:
            emissive_pairs = find_emissive_objects(context)
            filtered_emissive_pairs = [(o, m, n) for o, m, n in emissive_pairs
                                     if not filter_str or re.search(filter_str, o.name, re.I) or re.search(filter_str, m.name, re.I)]
            if not emissive_pairs:
                layout.box().label(text="No emissive materials detected", icon='INFO')
            elif not filtered_emissive_pairs:
                layout.box().label(text="No emissive materials match filter", icon='INFO')
        except Exception as e:
            layout.box().label(text=f"Error detecting emissive materials: {e}", icon='ERROR')
            filtered_emissive_pairs = []

        def is_group_selected(group_key, objects):
            if not objects:
                return False
            return all(obj.select_get() for obj in objects if obj.name in context.view_layer.objects)

        # --- 5. Draw UI Based on Filter Type ---
        if scene.filter_light_types == 'NO_FILTER':
            ab = layout.box()
            ar = ab.row(align=True)
            key_a = "all_lights_alpha"
            iA1 = 'CHECKBOX_HLT' if group_checkbox_1_state.get(key_a, True) else 'CHECKBOX_DEHLT'
            oA1 = ar.operator("light_editor.toggle_kind", text="", icon=iA1, depress=group_checkbox_1_state.get(key_a, True))
            oA1.group_key = key_a
            oA2 = ar.operator("light_editor.toggle_group_exclusive", text="",
                              icon=('RADIOBUT_ON' if group_checkbox_2_state.get(key_a, False) else 'RADIOBUT_OFF'),
                              depress=group_checkbox_2_state.get(key_a, False))
            oA2.group_key = key_a
            select_icon = 'RESTRICT_SELECT_ON' if is_group_selected(key_a, lights) else 'RESTRICT_SELECT_OFF'
            op_select = ar.operator("le.select_group", text="", icon=select_icon)
            op_select.group_key = key_a
            oA3 = ar.operator("light_editor.toggle_group", text="",
                              emboss=True,
                              icon=('DOWNARROW_HLT' if not group_collapse_dict.get(key_a, False) else 'RIGHTARROW'))
            oA3.group_key = key_a
            ar.label(text="All Lights (Alphabetical)", icon='LIGHT_DATA')
            if not group_collapse_dict.get(key_a, False):
                lb6 = ab.box()
                for o in sorted(lights, key=lambda x: x.name.lower()):
                    draw_main_row(lb6, o)
                    if o.light_expanded and not o.data.use_nodes:
                        eb6 = lb6.box()
                        draw_extra_params(self, eb6, o, o.data)
            eb7 = layout.box()
            er7 = eb7.row(align=True)
            key_e = "all_emissives_alpha"
            iem = 'CHECKBOX_HLT' if group_mat_checkbox_state.get(key_e, True) else 'CHECKBOX_DEHLT'
            oE1 = er7.operator("light_editor.toggle_group_emissive_all_off", text="", icon=iem, depress=group_mat_checkbox_state.get(key_e, True))
            oE1.group_key = key_e
            oE2 = er7.operator("light_editor.isolate_group_emissive", text="",
                               icon=('RADIOBUT_ON' if group_checkbox_2_state.get(key_e, False) else 'RADIOBUT_OFF'))
            oE2.group_key = key_e
            select_icon = 'RESTRICT_SELECT_ON' if is_group_selected(key_e, [o for o, _, _ in filtered_emissive_pairs]) else 'RESTRICT_SELECT_OFF'
            op_select = er7.operator("le.select_group", text="", icon=select_icon)
            op_select.group_key = key_e
            oE3 = er7.operator("light_editor.toggle_group", text="",
                               emboss=True,
                               icon=('DOWNARROW_HLT' if not group_collapse_dict.get(key_e, False) else 'RIGHTARROW'))
            oE3.group_key = key_e
            er7.label(text="All Emissive Materials (Alphabetical)", icon='SHADING_RENDERED')
            if not group_collapse_dict.get(key_e, False):
                cb7 = eb7.box()
                grouped_emissives = group_emissive_by_material(filtered_emissive_pairs)
                if not grouped_emissives:
                    cb7.label(text="No emissive materials match filter", icon='INFO')
                for obj, mat, nodes in sorted(grouped_emissives, key=lambda x: f"{x[0].name}_{x[1].name}".lower()):
                    draw_emissive_row(cb7, obj, mat, nodes)
            if scene.world:
                draw_environment_single_row(layout.box(), context, filter_str)
        elif scene.filter_light_types == 'KIND':
            kinds = ['AREA', 'POINT', 'SPOT', 'SUN', 'EMISSIVE']
            for kind in kinds:
                if kind == 'EMISSIVE':
                    key_k = f"kind_{kind}"
                    collapsed = group_collapse_dict.get(key_k, False)
                    emissives = [(o, m, n) for o, m, n in filtered_emissive_pairs]
                    if emissives:
                        eb = layout.box()
                        er = eb.row(align=True)
                        i_e = 'CHECKBOX_HLT' if group_mat_checkbox_state.get(key_k, True) else 'CHECKBOX_DEHLT'
                        o_e1 = er.operator("light_editor.toggle_group_emissive_all_off", text="", icon=i_e, depress=group_mat_checkbox_state.get(key_k, True))
                        o_e1.group_key = key_k
                        o_e2 = er.operator("light_editor.isolate_group_emissive", text="",
                                           icon=('RADIOBUT_ON' if group_checkbox_2_state.get(key_k, False) else 'RADIOBUT_OFF'))
                        o_e2.group_key = key_k
                        select_icon = 'RESTRICT_SELECT_ON' if is_group_selected(key_k, [o for o, _, _ in emissives]) else 'RESTRICT_SELECT_OFF'
                        op_select = er.operator("le.select_group", text="", icon=select_icon)
                        op_select.group_key = key_k
                        o_e3 = er.operator("light_editor.toggle_group", text="",
                                           emboss=True,
                                           icon=('DOWNARROW_HLT' if not collapsed else 'RIGHTARROW'))
                        o_e3.group_key = key_k
                        er.label(text="Emissive Materials", icon='SHADING_RENDERED')
                        if not collapsed:
                            cb = eb.box()
                            grouped_emissives = group_emissive_by_material(emissives)
                            if not grouped_emissives:
                                cb.label(text="No emissive materials match filter", icon='INFO')
                            for obj, mat, nodes in sorted(grouped_emissives, key=lambda x: f"{x[0].name}_{x[1].name}".lower()):
                                draw_emissive_row(cb, obj, mat, nodes)
                else:
                    lights_in = [o for o in lights if o.data.type == kind]
                    if lights_in:
                        key_k = f"kind_{kind}"
                        collapsed = group_collapse_dict.get(key_k, False)
                        kb = layout.box()
                        kr = kb.row(align=True)
                        i_k = 'CHECKBOX_HLT' if group_checkbox_1_state.get(key_k, True) else 'CHECKBOX_DEHLT'
                        o_k1 = kr.operator("light_editor.toggle_kind", text="", icon=i_k, depress=group_checkbox_1_state.get(key_k, True))
                        o_k1.group_key = key_k
                        o_k2 = kr.operator("light_editor.toggle_group_exclusive", text="",
                                           icon=('RADIOBUT_ON' if group_checkbox_2_state.get(key_k, False) else 'RADIOBUT_OFF'),
                                           depress=group_checkbox_2_state.get(key_k, False))
                        o_k2.group_key = key_k
                        select_icon = 'RESTRICT_SELECT_ON' if is_group_selected(key_k, lights_in) else 'RESTRICT_SELECT_OFF'
                        op_select = kr.operator("le.select_group", text="", icon=select_icon)
                        op_select.group_key = key_k
                        o_k3 = kr.operator("light_editor.toggle_group", text="",
                                           emboss=True,
                                           icon=('DOWNARROW_HLT' if not collapsed else 'RIGHTARROW'))
                        o_k3.group_key = key_k
                        kr.label(text=f"{kind.title()} Lights", icon='LIGHT_{}'.format(kind))
                        if not collapsed:
                            lb = kb.box()
                            for o in sorted(lights_in, key=lambda x: x.name.lower()):
                                draw_main_row(lb, o)
                                if o.light_expanded and not o.data.use_nodes:
                                    eb = lb.box()
                                    draw_extra_params(self, eb, o, o.data)
        elif scene.filter_light_types == 'COLLECTION':
            all_colls = []
            try:
                gather_layer_collections(context.view_layer.layer_collection, all_colls)
            except Exception:
                all_colls = []
            relevant = [lc for lc in all_colls if lc.collection.name != "Scene Collection" and
                        any(o.type == 'LIGHT' or any(m in [mat for _, mat, _ in emissive_pairs] for m in o.material_slots) for o in lc.collection.all_objects)]
            no_lights = [o for o in lights if len(o.users_collection) == 1 and o.users_collection[0].name == "Scene Collection"]
            no_emissives = [o for o, _, _ in filtered_emissive_pairs if len(o.users_collection) == 1 and o.users_collection[0].name == "Scene Collection"]
            if not relevant and not no_lights and not no_emissives:
                box = layout.box()
                box.label(text="No Collections or Unassigned Lights/Emissives Found", icon='INFO')
            else:
                for lc in relevant:
                    coll = lc.collection
                    group_key = f"coll_{coll.name}"
                    collapsed = group_collapse_dict.get(group_key, False)
                    group_objects = [o for o in context.view_layer.objects if
                                    (o.type == 'LIGHT' or any(m in [mat for _, mat, _ in emissive_pairs] for m in o.material_slots)) and
                                    any(c == coll for c in o.users_collection)]
                    header_box = layout.box()
                    hr = header_box.row(align=True)
                    icon_chk = 'CHECKBOX_HLT' if not lc.exclude else 'CHECKBOX_DEHLT'
                    op_inc = hr.operator("light_editor.toggle_collection", text="", icon=icon_chk, depress=not lc.exclude)
                    op_inc.group_key = group_key
                    op_iso = hr.operator("light_editor.toggle_group_exclusive", text="",
                                         icon=('RADIOBUT_ON' if group_checkbox_2_state.get(group_key, False) else 'RADIOBUT_OFF'),
                                         depress=group_checkbox_2_state.get(group_key, False))
                    op_iso.group_key = group_key
                    select_icon = 'RESTRICT_SELECT_ON' if is_group_selected(group_key, group_objects) else 'RESTRICT_SELECT_OFF'
                    op_select = hr.operator("le.select_group", text="", icon=select_icon)
                    op_select.group_key = group_key
                    op_tri = hr.operator("light_editor.toggle_group", text="",
                                         emboss=True,
                                         icon=('DOWNARROW_HLT' if not collapsed else 'RIGHTARROW'))
                    op_tri.group_key = group_key
                    hr.label(text=coll.name, icon='OUTLINER_COLLECTION')
                    if not collapsed:
                        lights_in_collection = [o for o in coll.all_objects if o.type == 'LIGHT']
                        lights_in = [o for o in lights_in_collection if (not filter_str or re.search(filter_str, o.name, re.I))]
                        if lights_in:
                            lb = header_box.box()
                            for o in sorted(lights_in, key=lambda x: x.name.lower()):
                                draw_main_row(lb, o)
                                if o.light_expanded and not o.data.use_nodes:
                                    eb = lb.box()
                                    draw_extra_params(self, eb, o, o.data)
                        emissives_in_collection = [(o, m, n) for o, m, n in filtered_emissive_pairs if any(c == coll for c in o.users_collection)]
                        if emissives_in_collection:
                            cb = header_box.box()
                            grouped_emissives = group_emissive_by_material(emissives_in_collection)
                            for obj, mat, nodes in sorted(grouped_emissives, key=lambda x: f"{x[0].name}_{x[1].name}".lower()):
                                draw_emissive_row(cb, obj, mat, nodes)
                if no_lights or no_emissives:
                    key_nc = "coll_No Collection"
                    collapsed_nc = group_collapse_dict.get(key_nc, False)
                    group_objects = no_lights + no_emissives
                    nb = layout.box()
                    nr = nb.row(align=True)
                    col_disabled = nr.column(align=True)
                    col_disabled.enabled = False
                    op1 = col_disabled.operator("light_editor.toggle_collection", text="", icon='CHECKBOX_HLT', depress=True)
                    op1.group_key = key_nc
                    op2 = nr.operator("light_editor.toggle_group_exclusive", text="",
                                      icon=('RADIOBUT_ON' if group_checkbox_2_state.get(key_nc, False) else 'RADIOBUT_OFF'),
                                      depress=group_checkbox_2_state.get(key_nc, False))
                    op2.group_key = key_nc
                    select_icon = 'RESTRICT_SELECT_ON' if is_group_selected(key_nc, group_objects) else 'RESTRICT_SELECT_OFF'
                    op_select = nr.operator("le.select_group", text="", icon=select_icon)
                    op_select.group_key = key_nc
                    op3 = nr.operator("light_editor.toggle_group", text="",
                                      emboss=True,
                                      icon=('DOWNARROW_HLT' if not collapsed_nc else 'RIGHTARROW'))
                    op3.group_key = key_nc
                    nr.label(text="Not In Any Collections", icon='OUTLINER_COLLECTION')
                    if not collapsed_nc:
                        lb2 = nb.box()
                        for o in sorted(no_lights, key=lambda x: x.name.lower()):
                            draw_main_row(lb2, o)
                            if o.light_expanded and not o.data.use_nodes:
                                eb2 = lb2.box()
                                draw_extra_params(self, eb2, o, o.data)
                        if no_emissives:
                            cb2 = lb2.box()
                            grouped_emissives = group_emissive_by_material(no_emissives)
                            for obj, mat, nodes in sorted(grouped_emissives, key=lambda x: f"{x[0].name}_{x[1].name}".lower()):
                                draw_emissive_row(cb2, obj, mat, nodes)
        elif scene.filter_light_types == 'SELECTED':
            # Selected Lights
            selected_lights = [o for o in lights if o.select_get()]
            if selected_lights:
                key_sl = "selected_lights"
                collapsed_sl = group_collapse_dict.get(key_sl, False)
                sb = layout.box()
                sr = sb.row(align=True)
                i_sl = 'CHECKBOX_HLT' if group_checkbox_1_state.get(key_sl, True) else 'CHECKBOX_DEHLT'
                op_sl1 = sr.operator("light_editor.toggle_kind", text="", icon=i_sl, depress=group_checkbox_1_state.get(key_sl, True))
                op_sl1.group_key = key_sl
                op_sl2 = sr.operator("light_editor.toggle_group_exclusive", text="",
                                     icon=('RADIOBUT_ON' if group_checkbox_2_state.get(key_sl, False) else 'RADIOBUT_OFF'),
                                     depress=group_checkbox_2_state.get(key_sl, False))
                op_sl2.group_key = key_sl
                select_icon = 'RESTRICT_SELECT_ON' if is_group_selected(key_sl, selected_lights) else 'RESTRICT_SELECT_OFF'
                op_select = sr.operator("le.select_group", text="", icon=select_icon)
                op_select.group_key = key_sl
                op_sl3 = sr.operator("light_editor.toggle_group", text="",
                                     emboss=True,
                                     icon=('DOWNARROW_HLT' if not collapsed_sl else 'RIGHTARROW'))
                op_sl3.group_key = key_sl
                sr.label(text="Selected Lights", icon='LIGHT_DATA')
                if not collapsed_sl:
                    sb = sb.box()
                    for o in sorted(selected_lights, key=lambda x: x.name.lower()):
                        draw_main_row(sb, o)
                        if o.light_expanded and not o.data.use_nodes:
                            eb = sb.box()
                            draw_extra_params(self, eb, o, o.data)
            # Selected Emissive Meshes
            selected_emissives = [(o, m, n) for o, m, n in filtered_emissive_pairs if o.select_get()]
            if selected_emissives:
                key_se = "selected_emissives"
                collapsed_se = group_collapse_dict.get(key_se, False)
                se_box = layout.box()
                se_row = se_box.row(align=True)
                i_se = 'CHECKBOX_HLT' if group_mat_checkbox_state.get(key_se, True) else 'CHECKBOX_DEHLT'
                op_se1 = se_row.operator("light_editor.toggle_group_emissive_all_off", text="", icon=i_se, depress=group_mat_checkbox_state.get(key_se, True))
                op_se1.group_key = key_se
                op_se2 = se_row.operator("light_editor.isolate_group_emissive", text="",
                                         icon=('RADIOBUT_ON' if group_checkbox_2_state.get(key_se, False) else 'RADIOBUT_OFF'),
                                         depress=group_checkbox_2_state.get(key_se, False))
                op_se2.group_key = key_se
                select_icon = 'RESTRICT_SELECT_ON' if is_group_selected(key_se, [o for o, _, _ in selected_emissives]) else 'RESTRICT_SELECT_OFF'
                op_select = se_row.operator("le.select_group", text="", icon=select_icon)
                op_select.group_key = key_se
                op_se3 = se_row.operator("light_editor.toggle_group", text="",
                                         emboss=True,
                                         icon=('DOWNARROW_HLT' if not collapsed_se else 'RIGHTARROW'))
                op_se3.group_key = key_se
                se_row.label(text="Selected Emissive Meshes", icon='SHADING_RENDERED')
                if not collapsed_se:
                    se_cb = se_box.box()
                    grouped_emissives = group_emissive_by_material(selected_emissives)
                    if not grouped_emissives:
                        se_cb.label(text="No selected emissive materials match filter", icon='INFO')
                    for obj, mat, nodes in sorted(grouped_emissives, key=lambda x: f"{x[0].name}_{x[1].name}".lower()):
                        draw_emissive_row(se_cb, obj, mat, nodes)
            # Not Selected Lights
            not_selected_lights = [o for o in lights if not o.select_get()]
            if not_selected_lights:
                key_nsl = "not_selected_lights"
                collapsed_nsl = group_collapse_dict.get(key_nsl, False)
                nsl_box = layout.box()
                nsl_row = nsl_box.row(align=True)
                i_nsl = 'CHECKBOX_HLT' if group_checkbox_1_state.get(key_nsl, True) else 'CHECKBOX_DEHLT'
                op_nsl1 = nsl_row.operator("light_editor.toggle_kind", text="", icon=i_nsl, depress=group_checkbox_1_state.get(key_nsl, True))
                op_nsl1.group_key = key_nsl
                op_nsl2 = nsl_row.operator("light_editor.toggle_group_exclusive", text="",
                                           icon=('RADIOBUT_ON' if group_checkbox_2_state.get(key_nsl, False) else 'RADIOBUT_OFF'),
                                           depress=group_checkbox_2_state.get(key_nsl, False))
                op_nsl2.group_key = key_nsl
                select_icon = 'RESTRICT_SELECT_ON' if is_group_selected(key_nsl, not_selected_lights) else 'RESTRICT_SELECT_OFF'
                op_select = nsl_row.operator("le.select_group", text="", icon=select_icon)
                op_select.group_key = key_nsl
                op_nsl3 = nsl_row.operator("light_editor.toggle_group", text="",
                                           emboss=True,
                                           icon=('DOWNARROW_HLT' if not collapsed_nsl else 'RIGHTARROW'))
                op_nsl3.group_key = key_nsl
                nsl_row.label(text="Not Selected Lights", icon='LIGHT_DATA')
                if not collapsed_nsl:
                    nslb = nsl_box.box()
                    for o in sorted(not_selected_lights, key=lambda x: x.name.lower()):
                        draw_main_row(nslb, o)
                        if o.light_expanded and not o.data.use_nodes:
                            eb = nslb.box()
                            draw_extra_params(self, eb, o, o.data)
            # Not Selected Emissive Meshes
            not_selected_emissives = [(o, m, n) for o, m, n in filtered_emissive_pairs if not o.select_get()]
            if not_selected_emissives:
                key_nse = "not_selected_emissives"
                collapsed_nse = group_collapse_dict.get(key_nse, False)
                nse_box = layout.box()
                nse_row = nse_box.row(align=True)
                i_nse = 'CHECKBOX_HLT' if group_mat_checkbox_state.get(key_nse, True) else 'CHECKBOX_DEHLT'
                op_nse1 = nse_row.operator("light_editor.toggle_group_emissive_all_off", text="", icon=i_nse, depress=group_mat_checkbox_state.get(key_nse, True))
                op_nse1.group_key = key_nse
                op_nse2 = nse_row.operator("light_editor.isolate_group_emissive", text="",
                                           icon=('RADIOBUT_ON' if group_checkbox_2_state.get(key_nse, False) else 'RADIOBUT_OFF'),
                                           depress=group_checkbox_2_state.get(key_nse, False))
                op_nse2.group_key = key_nse
                select_icon = 'RESTRICT_SELECT_ON' if is_group_selected(key_nse, [o for o, _, _ in not_selected_emissives]) else 'RESTRICT_SELECT_OFF'
                op_select = nse_row.operator("le.select_group", text="", icon=select_icon)
                op_select.group_key = key_nse
                op_nse3 = nse_row.operator("light_editor.toggle_group", text="",
                                           emboss=True,
                                           icon=('DOWNARROW_HLT' if not collapsed_nse else 'RIGHTARROW'))
                op_nse3.group_key = key_nse
                nse_row.label(text="Not Selected Emissive Meshes", icon='SHADING_RENDERED')
                if not collapsed_nse:
                    nse_cb = nse_box.box()
                    grouped_emissives = group_emissive_by_material(not_selected_emissives)
                    if not grouped_emissives:
                        nse_cb.label(text="No not selected emissive materials match filter", icon='INFO')
                    for obj, mat, nodes in sorted(grouped_emissives, key=lambda x: f"{x[0].name}_{x[1].name}".lower()):
                        draw_emissive_row(nse_cb, obj, mat, nodes)
            # Environment
            if scene.world:
                draw_environment_single_row(layout.box(), context, filter_str)
            
# Global UI flag for visual toggle (updated in isolate operator)
env_isolated_ui_state = False

def draw_environment_single_row(box, context, filter_str=""):
    scene = context.scene
    world = scene.world
    nt = world.node_tree if world and world.use_nodes else None
    output_node = next((n for n in nt.nodes if n.type == 'OUTPUT_WORLD'), None) if nt else None
    surf_input = output_node.inputs.get("Surface") if output_node else None
    vol_input = output_node.inputs.get("Volume") if output_node else None
    is_on = environment_checkbox_state.get('environment', True)
    icon = 'CHECKBOX_HLT' if is_on else 'CHECKBOX_DEHLT'

    # ✅ Use global UI toggle instead of .is_active()
    iso_icon = 'RADIOBUT_ON' if env_isolated_ui_state else 'RADIOBUT_OFF'

    # Search filtering
    f = filter_str.lower()
    show_surface = not f or f in "surface"
    show_volume = not f or f in "volume"
    if not (show_surface or show_volume):
        return

    # Header row
    header_row = box.row(align=True)
    header_row.operator("le.toggle_environment", text="", icon=icon, depress=is_on)
    op = header_row.operator("le.isolate_environment", text="", icon=iso_icon)
    op.mode = "HEADER"
    group_key = "env_header"
    collapsed = group_collapse_dict.get(group_key, False)
    header_row.operator("light_editor.toggle_group", text="", emboss=True,
                        icon='RIGHTARROW' if collapsed else 'DOWNARROW_HLT').group_key = group_key
    header_row.label(text="Environment", icon='WORLD')

    # Content (Surface/Volume)
    if not collapsed:
        content_box = box.box()
        if show_surface:
            row = content_box.row(align=True)
            row.operator("le.toggle_env_socket",
                         text="", icon='OUTLINER_OB_LIGHT' if surf_input and surf_input.is_linked else 'LIGHT_DATA',
                         depress=surf_input and surf_input.is_linked).socket_name = "Surface"
            op = row.operator("le.isolate_environment", text="", icon='RADIOBUT_ON' if isolate_env_surface_state else 'RADIOBUT_OFF')
            op.mode = "SURFACE"
            row.prop(scene, "env_surface_label", text="")
        if show_volume:
            row = content_box.row(align=True)
            row.operator("le.toggle_env_socket",
                         text="", icon='OUTLINER_OB_LIGHT' if vol_input and vol_input.is_linked else 'LIGHT_DATA',
                         depress=vol_input and vol_input.is_linked).socket_name = "Volume"
            op = row.operator("le.isolate_environment", text="", icon='RADIOBUT_ON' if isolate_env_volume_state else 'RADIOBUT_OFF')
            op.mode = "VOLUME"
            row.prop(scene, "env_volume_label", text="")

@persistent
def LE_update_light_enabled_on_visibility_change(scene):
    """Update light_enabled property when hide_viewport or hide_render changes."""
    try:
        context = bpy.context
        for obj in context.scene.objects:
            if obj.type == 'LIGHT' and obj.name in context.view_layer.objects:
                # Consider the light disabled if either viewport or render is hidden
                new_enabled = not (obj.hide_viewport or obj.hide_render)
                if obj.light_enabled != new_enabled:
                    obj.light_enabled = new_enabled
                    # Redraw relevant UI areas
                    for area in context.screen.areas:
                        if area.type in {'VIEW_3D', 'PROPERTIES', 'NODE_EDITOR'}:
                            area.tag_redraw()
    except Exception as e:
        pass
        
@persistent
def LE_clear_emission_links(dummy):
    """Clear stale _emission_links data on scene load."""
    for mat in bpy.data.materials:
        if "_emission_links" in mat:
            del mat["_emission_links"]
@persistent
def LE_force_redraw_on_use_nodes_change(scene):
    """Force redraw when use_nodes changes."""
    try:
        wm = bpy.context.window_manager
        for window in wm.windows:
            for area in window.screen.areas:
                if area.type in {'VIEW_3D', 'PROPERTIES', 'NODE_EDITOR'}:
                    area.tag_redraw()
    except Exception:
        pass  # Silently ignore any errors


@persistent
def LE_check_lights_enabled(dummy):
    """Ensure light_enabled property matches visibility state."""
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
    """Clear light states on file load."""
    context = bpy.context
    for obj in bpy.data.objects:
        if obj.type == 'LIGHT':
            if not (obj.hide_viewport or obj.hide_render):
                if obj.name in context.view_layer.objects:
                    context.view_layer.objects[obj.name].light_enabled = True
            else:
                if obj.name in context.view_layer.objects:
                    context.view_layer.objects[obj.name].light_enabled = False

@persistent
def LE_clear_emissive_cache(dummy):
    """Clear the emissive material cache."""
    global emissive_material_cache
    emissive_material_cache.clear()

classes = (
    LIGHT_OT_ToggleGroup,
    LIGHT_OT_ToggleCollection,
    LIGHT_OT_ToggleKind,
    LIGHT_OT_ToggleGroupExclusive,
    LIGHT_OT_ClearFilter,
    LIGHT_OT_SelectLight,
    LE_OT_ToggleEmission,
    LE_OT_isolate_emissive,
    EMISSIVE_OT_ToggleGroupAllOff,
    EMISSIVE_OT_IsolateGroup,
    LE_OT_ToggleEnvironment,
    LE_OT_IsolateEnvironment,
    LE_OT_SelectEnvironment,
    LIGHT_PT_editor,
    LE_OT_SelectGroup,
    LE_OT_toggle_env_socket,
)

def register():
    """Register all classes and properties."""
    # Register handlers
    bpy.app.handlers.depsgraph_update_post.append(LE_force_redraw_on_use_nodes_change)
    bpy.app.handlers.load_post.append(LE_clear_handler)
    bpy.app.handlers.load_post.append(LE_check_lights_enabled)
    bpy.app.handlers.depsgraph_update_post.append(LE_clear_emissive_cache)
    bpy.app.handlers.depsgraph_update_post.append(LE_update_light_enabled_on_visibility_change)

    # Register the new render layer property
    bpy.types.Scene.light_editor_selected_render_layer = bpy.props.EnumProperty(
        name="Render Layer",
        description="Select the active render layer for the Light Editor",
        items=get_render_layer_items,
        update=update_render_layer,
    )
    # Set initial render layer
    def set_initial_render_layer(dummy):
        if hasattr(bpy.types.Scene, 'light_editor_selected_render_layer'):
            try:
                current_vl_name = bpy.context.view_layer.name
                if bpy.context.scene.view_layers.get(current_vl_name):
                    bpy.context.scene.light_editor_selected_render_layer = current_vl_name
            except:
                pass

    bpy.app.handlers.load_post.append(set_initial_render_layer)
    try:
        set_initial_render_layer(None)
    except:
        pass

    # Register properties and classes
    bpy.types.Scene.env_surface_label = bpy.props.StringProperty(default="Surface")
    bpy.types.Scene.env_volume_label = bpy.props.StringProperty(default="Volume")
    bpy.types.Scene.current_active_light = bpy.props.PointerProperty(type=bpy.types.Object)
    bpy.types.Scene.current_exclusive_group = bpy.props.StringProperty()
    
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Scene.light_editor_filter = StringProperty(
        name="Filter",
        default="",
        description="Filter lights by name (regex allowed)"
    )
    bpy.types.Scene.collapse_all_emissives = BoolProperty(
        name="Collapse All Emissive Materials",
        default=False,
        description="Collapse the 'All Emissive Materials (Alphabetical)' section"
    )
    bpy.types.Scene.collapse_all_emissives_alpha = BoolProperty(
        name="Collapse All Emissive Materials Alphabetical",
        default=False,
        description="Collapse the 'All Emissive Materials (Alphabetical)' section in the 'All' view"
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
               ('SELECTED', 'Selected', 'Filter by selection status', 'RESTRICT_SELECT_OFF', 1),
               ('KIND', 'Kind', 'Filter lights by Kind', 'LIGHT_DATA', 2),
               ('COLLECTION', 'Collection', 'Filter lights by Collections', 'OUTLINER_COLLECTION', 3)),
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

def unregister():
    """Unregister all classes and properties."""
    # Remove handlers
    if LE_update_light_enabled_on_visibility_change in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(LE_update_light_enabled_on_visibility_change)
    if LE_force_redraw_on_use_nodes_change in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(LE_force_redraw_on_use_nodes_change)
    if LE_clear_handler in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(LE_clear_handler)
    if LE_check_lights_enabled in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(LE_check_lights_enabled)
    if LE_clear_emissive_cache in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(LE_clear_emissive_cache)
    
    # Remove set_initial_render_layer handler
    def set_initial_render_layer(dummy):
        if hasattr(bpy.types.Scene, 'light_editor_selected_render_layer'):
            try:
                current_vl_name = bpy.context.view_layer.name
                if bpy.context.scene.view_layers.get(current_vl_name):
                    bpy.context.scene.light_editor_selected_render_layer = current_vl_name
            except:
                pass
    if set_initial_render_layer in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(set_initial_render_layer)

    # Unregister properties
    if hasattr(bpy.types.Scene, 'light_editor_selected_render_layer'):
        del bpy.types.Scene.light_editor_selected_render_layer
    if hasattr(bpy.types.Scene, 'env_surface_label'):
        del bpy.types.Scene.env_surface_label
    if hasattr(bpy.types.Scene, 'env_volume_label'):
        del bpy.types.Scene.env_volume_label
    if hasattr(bpy.types.Scene, 'current_active_light'):
        del bpy.types.Scene.current_active_light
    if hasattr(bpy.types.Scene, 'current_exclusive_group'):
        del bpy.types.Scene.current_exclusive_group
    if hasattr(bpy.types.Scene, 'light_editor_filter'):
        del bpy.types.Scene.light_editor_filter
    if hasattr(bpy.types.Scene, 'light_editor_kind_alpha'):
        del bpy.types.Scene.light_editor_kind_alpha
    if hasattr(bpy.types.Scene, 'light_editor_group_by_collection'):
        del bpy.types.Scene.light_editor_group_by_collection
    if hasattr(bpy.types.Scene, 'filter_light_types'):
        del bpy.types.Scene.filter_light_types
    if hasattr(bpy.types.Scene, 'collapse_all_emissives'):
        del bpy.types.Scene.collapse_all_emissives
    if hasattr(bpy.types.Scene, 'collapse_all_emissives_alpha'):
        del bpy.types.Scene.collapse_all_emissives_alpha
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

    # Unregister classes
    for cls in reversed(classes):
        if hasattr(bpy.types, cls.__name__):
            bpy.utils.unregister_class(cls)

if __name__ == "__main__":
    try:
        unregister()
    except Exception as e:
        print(f"⚠ Unregister failed (probably first run): {e}")
    register()
    print("✅ Registered updated LightEditor")

