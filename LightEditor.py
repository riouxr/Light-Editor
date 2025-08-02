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

def activate(self, context, mode, identifier=None):
    print(f"📦 UnifiedIsolateManager.activate() — mode={mode}, identifier={identifier}")

def is_blender_4_5_or_higher():
    """Check if the Blender version is 4.5 or higher."""
    return bpy.app.version >= (4, 5, 0)

# --- New Unified Isolate System ---
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
    """Manages saving, applying, and restoring states for all isolate operations."""
    def __init__(self):
        self._backup = {}
        self._active_mode = None
        self._active_identifier = None

    def is_active(self, mode=None, identifier=None):
        """Check if isolation is active, optionally for a specific mode/identifier."""
        if self._active_mode is None:
            return False
        if mode is None:
            return True
        return self._active_mode == mode and self._active_identifier == identifier

    def get_active_info(self):
        """Get the currently active mode and identifier."""
        return self._active_mode, self._active_identifier

    def activate(self, context, mode, identifier=None):
        print(f"📦 UnifiedIsolateManager.activate() — mode={mode}, identifier={identifier}")
        """Activate isolation for a given mode and identifier."""
        if self.is_active():
            self.deactivate(context)
        scene = context.scene
        world = scene.world
        world_nt = world.node_tree if world and world.use_nodes else None
        world_output_node = next((n for n in world_nt.nodes if n.type == 'OUTPUT_WORLD'), None) if world_nt else None
        saved = {}
        # --- 1. Save State of All Relevant Items ---
        # Save all lights
        for obj in context.view_layer.objects:
            if obj.type == 'LIGHT':
                saved[obj.name] = ('LIGHT', obj.light_enabled, obj.hide_viewport, obj.hide_render)
        # Save all emissive materials
        for obj, mat in find_emissive_objects(context):
            if not mat or not mat.use_nodes:
                continue
            nt_mat = mat.node_tree
            output = next((n for n in nt_mat.nodes if n.type == 'OUTPUT_MATERIAL'), None)
            if not output:
                continue
            input_socket = output.inputs.get("Surface")
            if not input_socket or not input_socket.is_linked:
                continue
            emission_node, principled_node = self._find_emission_nodes(input_socket.links[0].from_node)
            if emission_node:
                strength = emission_node.inputs.get("Strength")
                if strength:
                    saved[mat.name] = ('EMISSION', strength.default_value)
            elif principled_node:
                strength = principled_node.inputs.get("Emission Strength")
                if strength:
                    saved[mat.name] = ('PRINCIPLED', strength.default_value)
                else:
                    color = principled_node.inputs.get("Emission Color")
                    if color:
                        saved[mat.name] = ('PRINCIPLED_COLOR', tuple(color.default_value[:]))
        # Save environment
        if world and world.use_nodes:
            background_node = next((n for n in world_nt.nodes if n.type == 'BACKGROUND'), None)
            if background_node:
                strength_input = background_node.inputs.get("Strength")
                if strength_input:
                    saved['environment'] = ('ENVIRONMENT', strength_input.default_value)
        # Save world output links
        if world_nt and world_output_node:
            for socket_name in ("Surface", "Volume"):
                socket = world_output_node.inputs.get(socket_name)
                if socket and socket.is_linked and socket.links:
                    try:
                        link = socket.links[0]
                        if link.is_valid:
                            saved[f"SOCKET_{socket_name}"] = ('SOCKET', link.from_node.name, link.from_socket.name)
                    except Exception:
                        continue
        # --- 2. Determine what to keep enabled based on mode/identifier ---
        to_keep_enabled = set()
        to_keep_emissive = set()
        if mode == UnifiedIsolateMode.LIGHT_ROW:
            if identifier:
                to_keep_enabled.add(identifier)
        elif mode == UnifiedIsolateMode.MATERIAL:
            if identifier:
                to_keep_emissive.add(identifier)
        # For group modes, the logic to determine members is handled by the caller
        # before calling activate/deactivate. The manager just applies the lists.
        elif mode in (UnifiedIsolateMode.LIGHT_GROUP, UnifiedIsolateMode.MATERIAL_GROUP):
            # The identifier can be a tuple (to_keep_enabled_set, to_keep_emissive_set)
            if isinstance(identifier, tuple) and len(identifier) == 2:
                to_keep_enabled.update(identifier[0])
                to_keep_emissive.update(identifier[1])
        # --- 3. Apply Isolation ---
        # Disable lights not in to_keep_enabled
        for obj in context.view_layer.objects:
            if obj.type == 'LIGHT' and obj.name not in to_keep_enabled:
                obj.light_enabled = False
                obj.hide_viewport = True
                obj.hide_render = True
        # Disable emissive materials not in to_keep_emissive
        print("🧮 Checking emissive materials to disable...")
        emissive_list = find_emissive_objects(context)
        print(f"🔍 Found {len(emissive_list)} emissive material(s)")
        for obj, mat in emissive_list:
            print(f"   - {mat.name} on object {obj.name}", end="")
            if mat.name not in to_keep_emissive:
                print(" → will disable")
                self._disable_material_emission(mat)
            else:
                print(" → KEEP enabled")

        # Disable environment
        if world and world.use_nodes:
            background_node = next((n for n in world_nt.nodes if n.type == 'BACKGROUND'), None)
            if background_node:
                strength_input = background_node.inputs.get("Strength")
                if strength_input:
                    strength_input.default_value = 0.0
        # Disconnect world output links
        sockets_to_disconnect = []
        if mode == UnifiedIsolateMode.ENVIRONMENT_SURFACE:
            sockets_to_disconnect = ["Volume"]
        elif mode == UnifiedIsolateMode.ENVIRONMENT_VOLUME:
            sockets_to_disconnect = ["Surface"]
        elif mode != UnifiedIsolateMode.ENVIRONMENT:  # Skip disconnection for ENVIRONMENT mode
            sockets_to_disconnect = ["Surface", "Volume"]
        if world_nt and world_output_node:
            for socket_name in sockets_to_disconnect:
                socket = world_output_node.inputs.get(socket_name)
                if socket and socket.is_linked and socket.links:
                    try:
                        link = socket.links[0]
                        if link.is_valid:
                            world_nt.links.remove(link)
                    except Exception:
                        continue
        # --- 4. Store state and update active mode ---
        self._backup = saved
        self._active_mode = mode
        self._active_identifier = identifier
        self._redraw_areas(context)

    def deactivate(self, context):
        """Deactivate the current isolation."""
        if not self.is_active():
            return
        scene = context.scene
        world = scene.world
        world_nt = world.node_tree if world and world.use_nodes else None
        world_output_node = next((n for n in world_nt.nodes if n.type == 'OUTPUT_WORLD'), None) if world_nt else None
        # Restore everything from the backup
        for name, entry in self._backup.items():
            typ = entry[0]
            val = entry[1] if len(entry) == 2 else entry[1:]
            if typ == 'LIGHT':
                obj = bpy.data.objects.get(name)
                if obj:
                    obj.light_enabled = val[0]
                    obj.hide_viewport = val[1]
                    obj.hide_render = val[2]
            elif typ in ('EMISSION', 'PRINCIPLED', 'PRINCIPLED_COLOR'):
                if name.startswith("SOCKET_") or name == 'environment':
                    continue
                mat = bpy.data.materials.get(name)
                if not mat or not mat.use_nodes:
                    continue
                self._restore_material_emission(mat, typ, val, world_nt, world_output_node)
            elif typ == 'ENVIRONMENT':
                if world and world.use_nodes:
                    background_node = next((n for n in world_nt.nodes if n.type == 'BACKGROUND'), None)
                    if background_node:
                        strength_input = background_node.inputs.get("Strength")
                        if strength_input:
                            strength_input.default_value = val
            elif typ == 'SOCKET':
                try:
                    if len(entry) < 3:
                        continue
                    _, node_name, socket_name = entry
                    from_node = world_nt.nodes.get(node_name) if world_nt else None
                    from_socket = from_node.outputs.get(socket_name) if from_node else None
                    to_socket = world_output_node.inputs.get(name.replace("SOCKET_", "")) if world_output_node else None
                    if from_socket and to_socket and not to_socket.is_linked:
                        try:
                            world_nt.links.new(from_socket, to_socket)
                        except Exception:
                            pass
                except ValueError:
                    continue
        # Clear the backup and active mode
        self._backup.clear()
        self._active_mode = None
        self._active_identifier = None
        self._redraw_areas(context)

    # --- Helper Methods ---
    def _find_emission_nodes(self, start_node):
        """Find emission-related node and its active input socket (Strength or Color)."""
        visited = set()
        stack = [start_node]
        while stack:
            node = stack.pop()
            if node in visited:
                continue
            visited.add(node)

            if node.type == 'EMISSION':
                strength = node.inputs.get("Strength")
                return node, strength  # can be None
            elif node.type == 'BSDF_PRINCIPLED':
                strength = node.inputs.get("Emission Strength")
                if strength:
                    return node, strength
                color = node.inputs.get("Emission Color")
                return node, color

            # add all linked input sources to stack
            for inp in node.inputs:
                if inp.is_linked:
                    for link in inp.links:
                        stack.append(link.from_node)
        return None, None

    def _disable_material_emission(self, mat):
        print(f"⚡️ CALLED _disable_material_emission for {mat.name}")
        global _emissive_link_backup
        if not mat or not mat.use_nodes:
            print(f"🛑 Skip: Material {mat.name} has no nodes")
            return
        nt = mat.node_tree
        output = next((n for n in nt.nodes if n.type == 'OUTPUT_MATERIAL'), None)
        if not output:
            print(f"🛑 No output node in {mat.name}")
            return
        surf = output.inputs.get("Surface")
        if not surf or not surf.is_linked:
            print(f"🛑 Surface input not linked in {mat.name}")
            return

        entry_node = surf.links[0].from_node
        node, socket = self._find_emission_nodes(entry_node)
        if not node or not socket:
            print(f"❌ Emission control not found in {mat.name}")
            return

        key = mat.name
        print(f"🔧 Disabling emission for {mat.name} → Node: {node.name}, Socket: {socket.name}")
        if socket.is_linked and socket.links:
            link = socket.links[0]
            _emissive_link_backup[key] = ('LINK', node.name, socket.name, link.from_node.name, link.from_socket.name)
            nt.links.remove(link)
            print(f"⛓ Disconnected link from {link.from_node.name}.{link.from_socket.name} → {node.name}.{socket.name}")
        else:
            if socket.name == "Color":
                _emissive_link_backup[key] = ('VALUE', node.name, socket.name, tuple(socket.default_value[:]))
                socket.default_value = (0, 0, 0, 1)
                print(f"🎨 Set {node.name}.{socket.name} color to black")
            else:
                _emissive_link_backup[key] = ('VALUE', node.name, socket.name, socket.default_value)
                socket.default_value = 0
                print(f"💡 Set {node.name}.{socket.name} value to 0")

    def _restore_material_emission(self, mat, typ, val, world_nt, world_output_node):
        global _emissive_link_backup
        if not mat or not mat.use_nodes:
            print(f"🛑 Cannot restore: {mat.name} has no nodes")
            return
        nt = mat.node_tree
        key = mat.name
        if key not in _emissive_link_backup:
            print(f"❌ No backup for {mat.name}")
            return

        kind, node_name, socket_name, *data = _emissive_link_backup[key]
        node = nt.nodes.get(node_name)
        socket = node.inputs.get(socket_name) if node else None
        if not socket:
            print(f"❌ Cannot restore {mat.name}: socket {socket_name} not found on node {node_name}")
            return

        if kind == 'LINK':
            from_node = nt.nodes.get(data[0])
            from_socket = from_node.outputs.get(data[1]) if from_node else None
            if from_socket:
                nt.links.new(from_socket, socket)
                print(f"🔗 Restored link: {from_node.name}.{from_socket.name} → {node.name}.{socket.name}")
            else:
                print(f"⚠ Failed to restore link for {mat.name}")
        elif kind == 'VALUE':
            value = data[0]
            if isinstance(value, tuple) and len(value) == 3:
                value = value + (1.0,)
            socket.default_value = value
            print(f"🔄 Restored {node.name}.{socket.name} to {value}")

        del _emissive_link_backup[key]


    def _redraw_areas(self, context):
        """Force redraw of relevant areas."""
        for area in context.screen.areas:
            if area.type in ('VIEW_3D', 'NODE_EDITOR'):
                area.tag_redraw()

# --- Global instance of the manager ---
_unified_isolate_manager = UnifiedIsolateManager()

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
    """Updated to also disable emissive materials and environment when isolating a light."""
    global _light_isolate_state_backup # Access the global backup dict
    scene = context.scene
    world = scene.world
    nt = world.node_tree if world and world.use_nodes else None
    output_node = next((n for n in nt.nodes if n.type == 'OUTPUT_WORLD'), None) if nt else None
    if self.light_turn_off_others:
        # --- Existing Logic: Manage mutual exclusivity and turn off other lights ---
        if scene.current_active_light and scene.current_active_light != self:
            scene.current_active_light.light_turn_off_others = False
        scene.current_active_light = self
        for obj in context.view_layer.objects:
            if obj.type == 'LIGHT' and obj.name != self.name: # Turn OFF other lights
                if 'prev_light_enabled' not in obj:
                    obj['prev_light_enabled'] = obj.light_enabled
                obj.light_enabled = False
        # --- End Existing Logic ---
        # --- New Logic: Isolate this light (turn off emissives, environment) ---
        _light_isolate_state_backup = {} # Initialize backup dict for this isolation
        saved = {}
        # 1. Disable all emissive materials (adapted from LE_OT_isolate_emissive)
        # Use the same traversal logic as UnifiedIsolateManager for consistency
        for obj, mat in find_emissive_objects(context):
            if not mat or not mat.use_nodes:
                continue
            nt_mat = mat.node_tree
            output = next((n for n in nt_mat.nodes if n.type == 'OUTPUT_MATERIAL'), None)
            if not output:
                continue
            input_socket = output.inputs.get("Surface")
            if not input_socket or not input_socket.is_linked:
                continue
            # --- Find Emission Node using UnifiedIsolateManager's method ---
            # Create a temporary instance to access the method or make it static/classmethod
            # Simpler: inline the core logic here, reusing the traverse function structure
            emission_node = None
            principled_node = None
            def traverse_find(node, visited):
                nonlocal emission_node, principled_node
                if node in visited:
                    return
                visited.add(node)
                if node.type == 'EMISSION':
                    emission_node = node
                    return # Found it, stop traversing this branch
                elif node.type == 'BSDF_PRINCIPLED':
                    principled_node = node
                    # Don't return here, might still find an EMISSION node upstream
                # Traverse inputs recursively
                for inp_socket in node.inputs:
                    if inp_socket.is_linked:
                        for link in inp_socket.links:
                            traverse_find(link.from_node, visited)
            # Start traversal from the node connected to the Surface output
            from_node = input_socket.links[0].from_node
            traverse_find(from_node, set())
            # --- Save and Disable based on found node ---
            if emission_node:
                strength = emission_node.inputs.get("Strength")
                if strength:
                    saved[mat.name] = ('EMISSION', strength.default_value)
                    strength.default_value = 0
            elif principled_node:
                strength = principled_node.inputs.get("Emission Strength")
                if strength:
                    saved[mat.name] = ('PRINCIPLED', strength.default_value)
                    strength.default_value = 0
                else:
                    color = principled_node.inputs.get("Emission Color")
                    if color:
                        # Store as tuple to avoid reference issues
                        saved[mat.name] = ('PRINCIPLED_COLOR', tuple(color.default_value[:]))
                        color.default_value = (0, 0, 0, 1) # Set alpha to 1
        # 2. Disable environment (world background strength) (adapted from LE_OT_IsolateEnvironment)
        if world and world.use_nodes:
            background_node = next((n for n in nt.nodes if n.type == 'BACKGROUND'), None)
            if background_node:
                strength_input = background_node.inputs.get("Strength")
                if strength_input:
                    saved['environment'] = ('ENVIRONMENT', strength_input.default_value)
                    strength_input.default_value = 0.0
        # 3. Disconnect world output Surface and Volume (adapted from LE_OT_IsolateEnvironment)
        if nt and output_node:
            for socket_name in ("Surface", "Volume"):
                socket = output_node.inputs.get(socket_name)
                if socket and socket.is_linked and socket.links:
                    try:
                        link = socket.links[0]
                        if link.is_valid:
                            # Store node/socket names, not the objects themselves
                            saved[f"SOCKET_{socket_name}"] = ('SOCKET', link.from_node.name, link.from_socket.name)
                            nt.links.remove(link)
                    except Exception as e:
                        # print(f"Warning: Failed to remove link for {socket_name}: {e}") # Removed as per instruction
                        continue
        # Store the saved states
        _light_isolate_state_backup.update(saved)
        # --- End New Logic ---
    else:
        # --- Existing Logic: Clear active light tracking and restore other lights ---
        if scene.current_active_light == self:
            scene.current_active_light = None
        for obj in context.view_layer.objects:
            if obj.type == 'LIGHT' and obj.name != self.name: # Restore other lights
                if 'prev_light_enabled' in obj:
                    obj.light_enabled = obj['prev_light_enabled']
                    del obj['prev_light_enabled']
        # --- End Existing Logic ---
        # --- New Logic: Restore emissives and environment ---
        # Restore state from _light_isolate_state_backup (adapted from isolate operators)
        for name, entry in _light_isolate_state_backup.items():
            typ = entry[0]
            val = entry[1] if len(entry) == 2 else entry[1:]
            if typ in ('EMISSION', 'PRINCIPLED', 'PRINCIPLED_COLOR'):
                # Skip non-material entries for now
                if name.startswith("SOCKET_") or name == 'environment':
                     continue
                mat = bpy.data.materials.get(name)
                if not mat or not mat.use_nodes:
                    continue
                nt_mat = mat.node_tree
                output = next((n for n in nt_mat.nodes if n.type == 'OUTPUT_MATERIAL'), None)
                if not output:
                    continue
                input_socket = output.inputs.get("Surface")
                if not input_socket or not input_socket.is_linked:
                    continue
                # --- Find Emission Node for restoration using UnifiedIsolateManager's method ---
                 # Use the same traversal logic as UnifiedIsolateManager for consistency
                emission_node = None
                principled_node = None
                def traverse_restore(node, visited):
                    nonlocal emission_node, principled_node
                    if node in visited:
                        return
                    visited.add(node)
                    if node.type == 'EMISSION':
                        emission_node = node
                        return # Found it, stop traversing this branch
                    elif node.type == 'BSDF_PRINCIPLED':
                        principled_node = node
                        # Don't return here, might still find an EMISSION node upstream
                    # Traverse inputs recursively
                    for inp_socket in node.inputs:
                        if inp_socket.is_linked:
                            for link in inp_socket.links:
                                traverse_restore(link.from_node, visited)
                # Start traversal from the node connected to the Surface output
                from_node = input_socket.links[0].from_node
                traverse_restore(from_node, set())
                # --- Restore based on found node ---
                if typ == 'EMISSION' and emission_node:
                    emission_node.inputs["Strength"].default_value = val
                elif typ == 'PRINCIPLED' and principled_node:
                    principled_node.inputs["Emission Strength"].default_value = val
                elif typ == 'PRINCIPLED_COLOR' and principled_node:
                    # Ensure correct length for color restoration
                    restored_color = list(val)
                    if len(restored_color) == 3:
                        restored_color.append(1.0) # Ensure alpha is 1
                    principled_node.inputs["Emission Color"].default_value = restored_color
            elif typ == 'ENVIRONMENT':
                 if world and world.use_nodes:
                    background_node = next((n for n in nt.nodes if n.type == 'BACKGROUND'), None)
                    if background_node:
                        strength_input = background_node.inputs.get("Strength")
                        if strength_input:
                            strength_input.default_value = val
            elif typ == 'SOCKET':
                try:
                    # Ensure correct unpacking based on stored format
                    if len(entry) < 3: # Need at least ('SOCKET', node_name, socket_name)
                         # print(f"Warning: Invalid socket data for {name}: {entry}") # Removed as per instruction
                         continue
                    _, node_name, socket_name = entry # Unpack correctly
                    from_node = nt.nodes.get(node_name) if nt else None
                    from_socket = from_node.outputs.get(socket_name) if from_node else None
                    to_socket = output_node.inputs.get(name.replace("SOCKET_", "")) if output_node else None
                    if from_socket and to_socket and not to_socket.is_linked:
                        try:
                            nt.links.new(from_socket, to_socket)
                        except Exception as e:
                            # print(f"Warning: Failed to restore link for {name.replace('SOCKET_', '')}: {e}") # Removed as per instruction
                            pass
                    # else: Handle cases where restoration isn't possible/needed
                except ValueError as e:
                    # print(f"Warning: Error unpacking socket data for {name}: {e}") # Removed as per instruction
                    continue
        # Clear the backup after restoration
        _light_isolate_state_backup.clear()
        # --- End New Logic ---
    # Force redraw if needed (optional, might be handled by property updates)
    for area in context.screen.areas:
        if area.type == 'VIEW_3D':
            area.tag_redraw()
        elif area.type == 'NODE_EDITOR': # Redraw node editor for environment changes
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

def find_emissive_objects(context, search_objects=None):
    """Find all objects with emissive materials in the current view layer or a given list."""
    global emissive_material_cache
    
    # Use a specific list of objects if provided, otherwise use view layer objects
    objects_to_search = search_objects if search_objects is not None else context.view_layer.objects
    
    # Simple cache invalidation: Disable cache if searching a specific list
    use_cache = (search_objects is None)
    cache_key = f"{context.view_layer.name}_{len(bpy.data.materials)}_{len(bpy.data.objects)}" if use_cache else None
    
    if use_cache and cache_key in emissive_material_cache:
        return emissive_material_cache[cache_key]

    emissive_objs = []
    seen = set()

    def is_emissive_output(node, visited):
        if node in visited:
            return False
        visited.add(node)
        if node.type == 'EMISSION':
            strength_input = node.inputs.get("Strength")
            if strength_input and strength_input.default_value > 0:
                return True
            return False
        if node.type == 'BSDF_PRINCIPLED':
            emission_input = node.inputs.get("Emission Strength")
            emission_color = node.inputs.get("Emission Color")
            if emission_input and emission_input.default_value > 0:
                return True
            if emission_color and any(emission_color.default_value[:3]):
                return True
            return False
        for input_socket in node.inputs:
            if input_socket.is_linked:
                for link in input_socket.links:
                    if is_emissive_output(link.from_node, visited):
                        return True
        return False

    for obj in objects_to_search: # Use the potentially specific list
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
            output_node = next((n for n in nt.nodes if n.type == 'OUTPUT_MATERIAL'), None)
            if not output_node:
                continue
            surf_input = output_node.inputs.get('Surface')
            if not surf_input or not surf_input.is_linked:
                continue
            from_node = surf_input.links[0].from_node
            if is_emissive_output(from_node, set()):
                emissive_objs.append((obj, mat))
                
    # Only cache results from the default view layer search
    if use_cache:
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

def draw_emissive_row(box, obj, mat):
    """Draw a single emissive material row in the UI."""
    row = box.row(align=True)
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
    if emission_node:
        color_input = emission_node.inputs.get("Color")
        strength_input = emission_node.inputs.get("Strength")
        if strength_input:
            enabled = (strength_input and (strength_input.is_linked or strength_input.default_value > 0))
    elif principled_node:
        color_input = principled_node.inputs.get("Emission Color")
        strength_input = principled_node.inputs.get("Emission Strength")
        if strength_input is not None:
            enabled = strength_input.default_value > 0
        elif color_input is not None:
            enabled = any(channel > 0.0 for channel in color_input.default_value[:3])
    icon = 'OUTLINER_OB_LIGHT' if enabled else 'LIGHT_DATA'
    op = row.operator("le.toggle_emission", text="", icon=icon, depress=enabled)
    op.mat_name = mat.name
    iso_icon = 'RADIOBUT_ON' if emissive_isolate_icon_state.get(mat.name, False) else 'RADIOBUT_OFF'
    row.operator("le.isolate_emissive", text="", icon=iso_icon).mat_name = mat.name
    row.operator("le.select_light", text="",
    icon="RESTRICT_SELECT_ON" if obj.select_get() else "RESTRICT_SELECT_OFF").name = obj.name
    obj_col = row.column(align=True)
    obj_col.scale_x = 0.5
    obj_col.prop(obj, "name", text="")
    mat_col = row.column(align=True)
    mat_col.scale_x = 0.5
    mat_col.prop(mat, "name", text="")
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

class LE_OT_IsolateEnvironment(bpy.types.Operator):
    """Isolate the environment lighting."""
    bl_idname = "le.isolate_environment"
    bl_label = "Isolate Environment Lighting"
    mode: bpy.props.StringProperty(default="HEADER")

    def execute(self, context):
        global isolate_env_header_state, isolate_env_surface_state, isolate_env_volume_state
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
            _unified_isolate_manager.activate(context, unified_mode)
        else:
            globals()[flag_map[self.mode]] = False
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
    """Select objects within a group."""
    bl_idname = "le.select_group"
    bl_label = "Select Group"
    group_key: bpy.props.StringProperty()

    def execute(self, context):
        objects_to_select = []
        objects_in_group = []  # Store all objects belonging to the group for selection check
        deselect_all_flag = False  # Flag to indicate if we should deselect everything
        # Get the filter string
        filter_str = context.scene.light_editor_filter.lower()
        # Handle different group types - Determine objects belonging to the group
        if self.group_key.startswith("coll_"):
            coll_name = self.group_key[5:]
            if coll_name == "No Collection":
                # Objects not in any collection (only in Scene Collection)
                for obj in context.view_layer.objects:
                    if obj.type == 'LIGHT' or (obj.type == 'MESH' and any(mat in [m for o, m in find_emissive_objects(context)] for mat in obj.material_slots)):
                        objects_in_group.append(obj)
                        # Check for specific group membership and apply filter
                        if len(obj.users_collection) == 1 and obj.users_collection[0].name == "Scene Collection":
                            if (not filter_str or re.search(filter_str, obj.name, re.I)) and (obj.type != 'LIGHT' or obj.light_enabled):
                                objects_to_select.append(obj)
            else:
                collection = bpy.data.collections.get(coll_name)
                if collection:
                    for obj in collection.all_objects:
                        if obj.type == 'LIGHT' or (obj.type == 'MESH' and any(mat in [m for o, m in find_emissive_objects(context)] for mat in obj.material_slots)):
                            objects_in_group.append(obj)
                            # Apply filter and check light_enabled for lights
                            if (not filter_str or re.search(filter_str, obj.name, re.I)) and (obj.type != 'LIGHT' or obj.light_enabled):
                                objects_to_select.append(obj)
        elif self.group_key.startswith("kind_"):
            kind = self.group_key[5:]
            if kind == "EMISSIVE":
                # Emissive materials
                for obj, mat in find_emissive_objects(context):
                    if not filter_str or re.search(filter_str, obj.name, re.I) or re.search(filter_str, mat.name, re.I):
                        objects_in_group.append(obj)
                        objects_to_select.append(obj)
            else:
                # Lights of a specific kind
                for obj in context.view_layer.objects:
                    if obj.type == 'LIGHT' and obj.data.type == kind:
                        if obj.light_enabled:  # Only include enabled lights in group
                            objects_in_group.append(obj)
                        if (not filter_str or re.search(filter_str, obj.name, re.I)) and obj.light_enabled:
                            objects_to_select.append(obj)
        elif self.group_key == "all_lights_alpha":
            # All lights, respecting filter and light_enabled
            for obj in context.view_layer.objects:
                if obj.type == 'LIGHT' and obj.light_enabled:  # Only include enabled lights
                    objects_in_group.append(obj)
                    if not filter_str or re.search(filter_str, obj.name, re.I):
                        objects_to_select.append(obj)
        elif self.group_key == "all_emissives_alpha":
            for obj, mat in find_emissive_objects(context):
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
            # --- Deselect All Objects in the View Layer ---
            bpy.ops.object.select_all(action='DESELECT')
            self.report({'INFO'}, f"Deselected all objects.")
        else:
            # --- Select Objects in Group (and deselect others) ---
            bpy.ops.object.select_all(action='DESELECT')
            # Select the objects in the group
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
            if area.type in ('VIEW_3D', 'NODE_EDITOR'):
                area.tag_redraw()
        return {'FINISHED'}

class LE_OT_ToggleEmission(bpy.types.Operator):
    bl_idname = "le.toggle_emission"
    bl_label = "Toggle Emission"
    mat_name: StringProperty()

    def execute(self, context):
        print(f"🔘 ToggleEmission: {self.mat_name}")
        mat = bpy.data.materials.get(self.mat_name)
        if not mat or not mat.use_nodes:
            print(f"  ⚠ Material '{self.mat_name}' not found or has no nodes")
            return {'CANCELLED'}

        nt = mat.node_tree
        if "_emission_links" not in mat:
            mat["_emission_links"] = {}
        link_store = mat["_emission_links"]

        output_node = next((n for n in nt.nodes if n.type == 'OUTPUT_MATERIAL'), None)
        if not output_node:
            print(f"  ⚠ No output node found in material '{self.mat_name}'")
            return {'CANCELLED'}

        surf_input = output_node.inputs.get("Surface")
        if not surf_input or not surf_input.is_linked:
            print(f"  ⚠ Surface input not linked in material '{self.mat_name}'")
            return {'CANCELLED'}

        entry_node = surf_input.links[0].from_node
        visited = set()
        target_node = None
        strength_socket = None
        color_socket = None

        def find_emission_or_principled(node):
            nonlocal target_node, strength_socket, color_socket
            if node in visited:
                return
            visited.add(node)
            print(f"  🔎 Visiting node: {node.name} (type: {node.type})")
            if node.type == 'EMISSION':
                target_node = node
                strength_socket = node.inputs.get("Strength")
                color_socket = node.inputs.get("Color")
                print(f"  ✅ Found EMISSION node: {node.name}, Strength: {strength_socket is not None}, Color: {color_socket is not None}")
                return
            elif node.type == 'BSDF_PRINCIPLED':
                target_node = node
                strength_socket = node.inputs.get("Emission Strength") or node.inputs.get("Emission")
                color_socket = node.inputs.get("Emission Color")
                print(f"  ✅ Found BSDF_PRINCIPLED node: {node.name}, Strength: {strength_socket is not None}, Color: {color_socket is not None}")
                return
            for inp in node.inputs:
                if inp.is_linked:
                    for link in inp.links:
                        print(f"  ➡️ Following link from {link.from_node.name} to {node.name}")
                        find_emission_or_principled(link.from_node)

        find_emission_or_principled(entry_node)
        if not target_node:
            print(f"  ❌ No valid EMISSION or BSDF_PRINCIPLED node found in material '{self.mat_name}'")
            return {'CANCELLED'}

        if strength_socket:
            socket = strength_socket
            socket_type = 'STRENGTH'
        elif color_socket:
            socket = color_socket
            socket_type = 'COLOR'
        else:
            print(f"  ❌ No Strength or Color socket found for node '{target_node.name}' in material '{self.mat_name}'")
            return {'CANCELLED'}

        key = f"{target_node.name}:{socket.name}"
        print(f"  🔍 Processing socket: {key} (type: {socket_type}, linked: {socket.is_linked}, value: {socket.default_value})")

        # Validate link_store data
        if key in link_store and not socket.is_linked:
            print(f"  ⚠ Clearing stale link data for {key}")
            del link_store[key]

        if f"{key}_val" in link_store:
            # Restore value
            restored = link_store.pop(f"{key}_val")
            socket.default_value = restored
            print(f"  🔄 Restored {socket_type.lower()}: {key} → {restored}")
        elif key in link_store:
            # Restore link
            from_node_name, from_socket_name = link_store[key]
            from_node = nt.nodes.get(from_node_name)
            from_socket = from_node.outputs.get(from_socket_name) if from_node else None
            if from_socket and not socket.is_linked:
                try:
                    nt.links.new(from_socket, socket)
                    print(f"  🔗 Restored link: {key} from {from_node_name}.{from_socket_name}")
                except Exception as e:
                    print(f"  ⚠ Failed to restore link for {key}: {e}")
            else:
                print(f"  ⚠ Link restore failed for {key}: node or socket not found or already linked")
            del link_store[key]
        else:
            # Store and disable
            if socket.is_linked:
                for link in list(nt.links):
                    if link.to_socket == socket and link.is_valid:
                        link_store[key] = (link.from_node.name, link.from_socket.name)
                        try:
                            nt.links.remove(link)
                            print(f"  ⛓ Disconnected link from {link.from_node.name}.{link.from_socket.name} for {key}")
                        except Exception as e:
                            print(f"  ⚠ Failed to remove link for {key}: {e}")
                            # Continue to set default value
                            if socket_type == 'COLOR':
                                link_store[f"{key}_val"] = tuple(socket.default_value[:])
                                socket.default_value = (0.0, 0.0, 0.0, 1.0)
                                print(f"  🎨 Set {socket_type.lower()}: {key} to {socket.default_value} (stored: {link_store[f'{key}_val']})")
                            else:
                                link_store[f"{key}_val"] = socket.default_value if socket.default_value > 0.0 else 1.0
                                socket.default_value = 0.0
                                print(f"  💡 Set {socket_type.lower()}: {key} to {socket.default_value} (stored: {link_store[f'{key}_val']})")
                        break
                else:
                    print(f"  ⚠ No valid link found for {key}, treating as unlinked")
                    # Treat as unlinked if no valid link is found
                    if socket_type == 'COLOR':
                        link_store[f"{key}_val"] = tuple(socket.default_value[:])
                        socket.default_value = (0.0, 0.0, 0.0, 1.0)
                        print(f"  🎨 Set {socket_type.lower()}: {key} to {socket.default_value} (stored: {link_store[f'{key}_val']})")
                    else:
                        link_store[f"{key}_val"] = socket.default_value if socket.default_value > 0.0 else 1.0
                        socket.default_value = 0.0
                        print(f"  💡 Set {socket_type.lower()}: {key} to {socket.default_value} (stored: {link_store[f'{key}_val']})")
            else:
                # Handle unlinked socket
                if socket_type == 'COLOR':
                    link_store[f"{key}_val"] = tuple(socket.default_value[:])
                    socket.default_value = (0.0, 0.0, 0.0, 1.0)
                    print(f"  🎨 Set {socket_type.lower()}: {key} to {socket.default_value} (stored: {link_store[f'{key}_val']})")
                else:
                    link_store[f"{key}_val"] = socket.default_value if socket.default_value > 0.0 else 1.0
                    socket.default_value = 0.0
                    print(f"  💡 Set {socket_type.lower()}: {key} to {socket.default_value} (stored: {link_store[f'{key}_val']})")

        if not link_store:
            del mat["_emission_links"]

        for area in context.screen.areas:
            if area.type in {'VIEW_3D', 'NODE_EDITOR', 'PROPERTIES'}:
                area.tag_redraw()

        return {'FINISHED'}
    
class LE_OT_isolate_emissive(bpy.types.Operator):
    """Isolate a single emissive material."""
    bl_idname = "le.isolate_emissive"
    bl_label = "Isolate Emissive"
    mat_name: bpy.props.StringProperty()

    def execute(self, context):
        global emissive_isolate_icon_state
        print(f"🎯 IsolateEmissive: {self.mat_name}")
        print(f"🎯 IsolateEmissive: {self.mat_name}")
        mat = bpy.data.materials.get(self.mat_name)
        if not mat or not mat.use_nodes:
            print("  ⚠ Material not found or has no nodes")
            return {'CANCELLED'}
        is_currently_active = _unified_isolate_manager.is_active(UnifiedIsolateMode.MATERIAL, self.mat_name)
        if not is_currently_active:
            print("  ✅ Activating isolation via manager")
            emissive_isolate_icon_state[self.mat_name] = True
            _unified_isolate_manager.activate(context, UnifiedIsolateMode.MATERIAL, identifier=self.mat_name)
        else:
            print("  🔄 Deactivating isolation via manager")
            emissive_isolate_icon_state[self.mat_name] = False
            _unified_isolate_manager.deactivate(context)
        return {'FINISHED'}


class EMISSIVE_OT_ToggleGroupAllOff(bpy.types.Operator):
    bl_idname = "light_editor.toggle_group_emissive_all_off"
    bl_label = "Toggle Emissive Group On/Off"
    group_key: StringProperty()

    def execute(self, context):
        global group_mat_checkbox_state
        is_on = group_mat_checkbox_state.get(self.group_key, True)
        emissive_pairs = find_emissive_objects(context)
        # Filter unique emissive pairs based on group_key
        filtered_pairs = []
        seen_materials = set()
        if self.group_key.startswith("emissive_"):  # Collection-based group
            coll_name = self.group_key[9:]
            if coll_name == "No Collection":
                for obj, mat in emissive_pairs:
                    if len(obj.users_collection) == 1 and obj.users_collection[0].name == "Scene Collection" and mat.name not in seen_materials:
                        filtered_pairs.append((obj, mat))
                        seen_materials.add(mat.name)
            else:
                for obj, mat in emissive_pairs:
                    if any(coll.name == coll_name for coll in obj.users_collection) and mat.name not in seen_materials:
                        filtered_pairs.append((obj, mat))
                        seen_materials.add(mat.name)
        elif self.group_key in ("kind_EMISSIVE", "all_emissives_alpha"):  # All emissive materials
            for obj, mat in emissive_pairs:
                if mat.name not in seen_materials:
                    filtered_pairs.append((obj, mat))
                    seen_materials.add(mat.name)

        # Toggle filtered materials
        for obj, mat in filtered_pairs:
            if mat:
                print(f"  📌 Toggling material: {mat.name} for group {self.group_key}")
                bpy.ops.le.toggle_emission(mat_name=mat.name)

        # Update toggle state
        group_mat_checkbox_state[self.group_key] = not is_on
        for area in context.screen.areas:
            if area.type in {'VIEW_3D', 'NODE_EDITOR', 'PROPERTIES'}:
                area.tag_redraw()
        return {'FINISHED'}
    
class LIGHT_OT_ToggleGroup(bpy.types.Operator):
    """Toggle the collapse state of a group in the UI."""
    bl_idname = "light_editor.toggle_group"
    bl_label = ""
    group_key: bpy.props.StringProperty()

    def execute(self, context):
        current = group_collapse_dict.get(self.group_key, False)
        group_collapse_dict[self.group_key] = not current
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
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
    group_key: bpy.props.StringProperty()

    def execute(self, context):
        global group_mat_checkbox_state, group_checkbox_2_state
        is_currently_active = group_checkbox_2_state.get(self.group_key, False)
        if not is_currently_active:
            # Determine members to keep enabled for this group
            to_keep_emissive = set()
            if self.group_key.startswith("emissive_"):  # Collection group
                coll_name = self.group_key[9:]
                for obj, mat in find_emissive_objects(context):
                    if obj.users_collection and obj.users_collection[0].name == coll_name:
                        to_keep_emissive.add(mat.name)
            elif self.group_key == "kind_EMISSIVE":  # Kind group
                for obj, mat in find_emissive_objects(context):
                    to_keep_emissive.add(mat.name)
            elif self.group_key == "all_emissives_alpha":  # All Alphabetical group
                for obj, mat in find_emissive_objects(context):
                    to_keep_emissive.add(mat.name)
            group_checkbox_2_state[self.group_key] = True
            _unified_isolate_manager.activate(
                context,
                UnifiedIsolateMode.MATERIAL_GROUP,
                identifier=(set(), to_keep_emissive)  # No lights to keep, only emissives
            )
        else:
            group_checkbox_2_state[self.group_key] = False
            # Check if MATERIAL_GROUP is active for this group_key
            current_mode, current_id = _unified_isolate_manager.get_active_info()
            if current_mode == UnifiedIsolateMode.MATERIAL_GROUP:
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

def draw_main_row(box, obj):
    """Draw a single light object row in the UI (Adapted for v37 logic)."""
    light = obj.data
    row = box.row(align=True)

    controls_row = row.row(align=True)
    controls_row.prop(obj, "light_enabled", text="", icon="OUTLINER_OB_LIGHT" if obj.light_enabled else "LIGHT_DATA")
    controls_row.prop(obj, "light_turn_off_others", text="", icon="RADIOBUT_ON" if obj.light_turn_off_others else "RADIOBUT_OFF")
    controls_row.operator("le.select_light", text="", icon="RESTRICT_SELECT_ON" if obj.select_get() else "RESTRICT_SELECT_OFF").name = obj.name

    expand_button = controls_row.row(align=True)
    expand_button.enabled = not light.use_nodes
    expand_button.prop(obj, "light_expanded", text="", emboss=True, icon='DOWNARROW_HLT' if obj.light_expanded else 'RIGHTARROW')

    col_name = row.column(align=True)
    col_name.ui_units_x = 6
    col_name.prop(obj, "name", text="")

    col_color = row.column(align=True)
    col_strength = row.column(align=True)
    col_exposure = row.column(align=True)

    col_color.ui_units_x = 2.5
    col_strength.ui_units_x = 5.0
    col_exposure.ui_units_x = 5.0

    if light.use_nodes:
        nt = light.node_tree
        output_node = next((n for n in nt.nodes if n.type == 'OUTPUT_LIGHT'), None)
        surface_input = output_node.inputs.get("Surface") if output_node else None

        if surface_input and surface_input.is_linked:
            from_node = surface_input.links[0].from_node
            if from_node.type == 'EMISSION':
                color_input = from_node.inputs.get("Color")
                strength_input = from_node.inputs.get("Strength")

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
                    col_color.label(text="", icon='ERROR')

                if strength_input:
                    draw_socket_with_icon(col_strength, strength_input, text="", linked_only=False)
                else:
                    col_strength.label(text="", icon='ERROR')
            else:
                col_color.prop(light, "color", text="")
                col_strength.prop(light, "energy", text="")
        else:
            col_color.prop(light, "color", text="")
            col_strength.prop(light, "energy", text="")

        # Dummy field to preserve layout when exposure is hidden
        dummy = col_exposure.row()
        dummy.enabled = False
        dummy.label(text="")

    else:
        col_color.prop(light, "color", text="")
        col_strength.prop(light, "energy", text="")
        col_exposure.prop(light, "exposure", text="Exp.")

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

        # --- 2. Render Layer Selector (using existing property) ---
        # Add the render layer enum menu below the filter buttons if there are multiple view layers
        if len(context.scene.view_layers) > 1 and scene.filter_light_types == 'COLLECTION':
            layout.prop(scene, "selected_render_layer", text="Render Layer")

        # --- 3. Search Bar ---
        layout.use_property_split = True
        layout.use_property_decorate = False
        row = layout.row(align=True)
        row.prop(scene, "light_editor_filter", text="", icon="VIEWZOOM")
        row.operator("le.clear_light_filter", text="", icon='PANEL_CLOSE')
        filter_str = scene.light_editor_filter.lower()

        try:
            lights = [o for o in context.view_layer.objects if o.type == 'LIGHT' and (not filter_str or re.search(filter_str, o.name, re.I))]
        except Exception as e:
            layout.box().label(text=f"Error filtering lights: {e}", icon='ERROR')
            lights = []
        try:
             # Keep the general emissive list for non-collection modes and overall counts
            emissive_pairs = [(o, m) for o, m in find_emissive_objects(context)
                              if not filter_str or re.search(filter_str, o.name, re.I) or re.search(filter_str, m.name, re.I)]
        except Exception as e:
            layout.box().label(text=f"Error filtering emissive materials: {e}", icon='ERROR')
            emissive_pairs = []

        def is_group_selected(group_key, objects):
            if not objects:
                return False
            return all(obj.select_get() for obj in objects if obj.name in context.view_layer.objects)

        if scene.filter_light_types == 'COLLECTION':
            all_colls = []
            try:
                gather_layer_collections(context.view_layer.layer_collection, all_colls)
            except Exception:
                all_colls = []
            # Filter relevant collections: Only those with lights (emissives removed from display logic)
            relevant = [lc for lc in all_colls if lc.collection.name != "Scene Collection" and
                        (any(o.type == 'LIGHT' for o in lc.collection.all_objects))]

            # Find unassigned lights (only in Scene Collection)
            no_lights = [o for o in lights if len(o.users_collection) == 1 and o.users_collection[0].name == "Scene Collection"]
            # no_emiss list is calculated but not used/displayed in COLLECTION mode

            if not relevant and not no_lights: # Check only for lights now
                box = layout.box()
                box.label(text="No Collections or Unassigned Lights Found", icon='ERROR')
            else:
                for lc in relevant:
                    coll = lc.collection
                    group_key = f"coll_{coll.name}"
                    collapsed = group_collapse_dict.get(group_key, False)

                    # Determine objects for selection check (lights only now)
                    group_objects = [o for o in context.view_layer.objects if
                                     (o.type == 'LIGHT') and # Only lights
                                     any(c == coll for c in o.users_collection)]

                    header_box = layout.box()
                    hr = header_box.row(align=True)
                    icon_chk = 'CHECKBOX_HLT' if not lc.exclude else 'CHECKBOX_DEHLT'
                    op_inc = hr.operator("light_editor.toggle_collection", text="", icon=icon_chk, depress=not lc.exclude)
                    op_inc.group_key = group_key
                    op_iso = hr.operator("light_editor.toggle_group_exclusive", text="", icon=('RADIOBUT_ON' if group_checkbox_2_state.get(group_key, False) else 'RADIOBUT_OFF'),
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
                        # --- ONLY SHOW LIGHTS IN COLLECTION ---
                        # --- KEY CHANGE 1: Get lights from collection.all_objects ---
                        lights_in_collection = [o for o in coll.all_objects if o.type == 'LIGHT']
                        lights_in = [o for o in lights_in_collection if (not filter_str or re.search(filter_str, o.name, re.I))]

                        if lights_in:
                            lb = header_box.box()
                            # Sort lights alphabetically within the group
                            for o in sorted(lights_in, key=lambda x: x.name.lower()):
                                # Note: Properties like light_enabled might not behave as expected if 'o' is not in context.view_layer.objects
                                # UI drawing itself should work.
                                draw_main_row(lb, o)
                                if o.light_expanded and not o.data.use_nodes:
                                    eb = lb.box(); draw_extra_params(self, eb, o, o.data)

                # --- Handle "Not In Any Collections" section (Lights only) ---
                if no_lights: # Only check for lights
                    key_nc = "coll_No Collection"
                    collapsed_nc = group_collapse_dict.get(key_nc, False)
                    group_objects = no_lights # Only lights
                    nb = layout.box()
                    nr = nb.row(align=True)
                    # "Not In Any Collections" cannot be excluded like a real collection, so always show as 'on'
                    col_disabled = nr.column(align=True)
                    col_disabled.enabled = False
                    op1 = col_disabled.operator("light_editor.toggle_collection", text="", icon='CHECKBOX_HLT', depress=True)
                    op1.group_key = key_nc

                    op2 = nr.operator("light_editor.toggle_group_exclusive",
                                      text="",
                                      icon=('RADIOBUT_ON' if group_checkbox_2_state.get(key_nc, False) else 'RADIOBUT_OFF'),
                                      depress=group_checkbox_2_state.get(key_nc, False))
                    op2.group_key = key_nc
                    select_icon = 'RESTRICT_SELECT_ON' if is_group_selected(key_nc, group_objects) else 'RESTRICT_SELECT_OFF'
                    op_select = nr.operator("le.select_group", text="", icon=select_icon)
                    op_select.group_key = key_nc
                    op3 = nr.operator("light_editor.toggle_group",
                                      text="",
                                      emboss=True,
                                      icon=('DOWNARROW_HLT' if not collapsed_nc else 'RIGHTARROW'))
                    op3.group_key = key_nc
                    nr.label(text="Not In Any Collections", icon='OUTLINER_COLLECTION')
                    if not collapsed_nc:
                        if no_lights:
                            lb2 = nb.box()
                            for o in sorted(no_lights, key=lambda x: x.name.lower()): # Sort here too
                                draw_main_row(lb2, o)
                                if o.light_expanded and not o.data.use_nodes:
                                    eb2 = lb2.box(); draw_extra_params(self, eb2, o, o.data)

        elif scene.filter_light_types == 'KIND':
            # ... (KIND mode logic remains unchanged) ...
            groups = {'POINT': [], 'SPOT': [], 'SUN': [], 'AREA': []}
            for o in lights:
                if o.data.type in groups:
                    groups[o.data.type].append(o)
            if any(groups.values()) or emissive_pairs:
                for kind, objs in groups.items():
                    if objs:
                        key = f"kind_{kind}"
                        collapsed_k = group_collapse_dict.get(key, False)
                        kb = layout.box()
                        kr = kb.row(align=True)
                        i1 = 'CHECKBOX_HLT' if group_checkbox_1_state.get(key, True) else 'CHECKBOX_DEHLT'
                        op_k1 = kr.operator("light_editor.toggle_kind", text="", icon=i1, depress=group_checkbox_1_state.get(key, True))
                        op_k1.group_key = key
                        op_k2 = kr.operator("light_editor.toggle_group_exclusive",
                                            text="",
                                            icon=('RADIOBUT_ON' if group_checkbox_2_state.get(key, False) else 'RADIOBUT_OFF'),
                                            depress=group_checkbox_2_state.get(key, False))
                        op_k2.group_key = key
                        select_icon = 'RESTRICT_SELECT_ON' if is_group_selected(key, objs) else 'RESTRICT_SELECT_OFF'
                        op_select = kr.operator("le.select_group", text="", icon=select_icon)
                        op_select.group_key = key
                        op_k3 = kr.operator("light_editor.toggle_group",
                                            emboss=True,
                                            icon=('DOWNARROW_HLT' if not collapsed_k else 'RIGHTARROW'))
                        op_k3.group_key = key
                        kr.label(text=f"{kind} Lights", icon=f"LIGHT_{kind}")
                        if not collapsed_k:
                            lb4 = kb.box()
                            for o in objs:
                                draw_main_row(lb4, o)
                                if o.light_expanded and not o.data.use_nodes:
                                    eb4 = lb4.box(); draw_extra_params(self, eb4, o, o.data)
                ek3 = "kind_EMISSIVE"
                collapsed_em = group_collapse_dict.get(ek3, False)
                eb5 = layout.box()
                er5 = eb5.row(align=True)
                iem = 'CHECKBOX_HLT' if group_mat_checkbox_state.get(ek3, True) else 'CHECKBOX_DEHLT'
                ot5 = er5.operator("light_editor.toggle_group_emissive_all_off", text="", icon=iem, depress=group_mat_checkbox_state.get(ek3, True))
                ot5.group_key = ek3
                oi5 = er5.operator("light_editor.isolate_group_emissive", text="",
                                    icon=('RADIOBUT_ON' if group_checkbox_2_state.get(ek3, False) else 'RADIOBUT_OFF'))
                oi5.group_key = ek3
                select_icon = 'RESTRICT_SELECT_ON' if is_group_selected(ek3, [o for o, m in emissive_pairs]) else 'RESTRICT_SELECT_OFF'
                op_select = er5.operator("le.select_group", text="", icon=select_icon)
                op_select.group_key = ek3
                oc5 = er5.operator("light_editor.toggle_group",
                                    emboss=True,
                                    icon=('DOWNARROW_HLT' if not collapsed_em else 'RIGHTARROW'))
                oc5.group_key = ek3
                er5.label(text="Emissive Materials", icon='SHADING_RENDERED')
                if not collapsed_em:
                    cb5 = eb5.box()
                    for o, m in sorted(emissive_pairs, key=lambda x: f"{x[0].name}_{x[1].name}".lower()):
                        draw_emissive_row(cb5, o, m)
            if scene.world:
                draw_environment_single_row(layout.box(), context, filter_str)
        else: # NO_FILTER mode
            # ... (NO_FILTER mode logic remains unchanged) ...
            ab = layout.box()
            ar = ab.row(align=True)
            key_a = "all_lights_alpha"
            iA1 = 'CHECKBOX_HLT' if group_checkbox_1_state.get(key_a, True) else 'CHECKBOX_DEHLT'
            oA1 = ar.operator("light_editor.toggle_kind", text="", icon=iA1, depress=group_checkbox_1_state.get(key_a, True))
            oA1.group_key = key_a
            oA2 = ar.operator("light_editor.toggle_group_exclusive",
                               text="",
                               icon=('RADIOBUT_ON' if group_checkbox_2_state.get(key_a, False) else 'RADIOBUT_OFF'),
                               depress=group_checkbox_2_state.get(key_a, False))
            oA2.group_key = key_a
            select_icon = 'RESTRICT_SELECT_ON' if is_group_selected(key_a, lights) else 'RESTRICT_SELECT_OFF'
            op_select = ar.operator("le.select_group", text="", icon=select_icon)
            op_select.group_key = key_a
            oA3 = ar.operator("light_editor.toggle_group",
                               emboss=True,
                               icon=('DOWNARROW_HLT' if not group_collapse_dict.get(key_a, False) else 'RIGHTARROW'))
            oA3.group_key = key_a
            ar.label(text="All Lights (Alphabetical)", icon='LIGHT_DATA')
            if not group_collapse_dict.get(key_a, False):
                lb6 = ab.box()
                for o in sorted(lights, key=lambda x: x.name.lower()):
                    draw_main_row(lb6, o)
                    if o.light_expanded and not o.data.use_nodes:
                        eb6 = lb6.box(); draw_extra_params(self, eb6, o, o.data)
            eb7 = layout.box()
            er7 = eb7.row(align=True)
            key_e = "all_emissives_alpha"
            iE1 = 'CHECKBOX_HLT' if group_mat_checkbox_state.get(key_e, True) else 'CHECKBOX_DEHLT'
            oE1 = er7.operator("light_editor.toggle_group_emissive_all_off", text="", icon=iE1, depress=group_mat_checkbox_state.get(key_e, True))
            oE1.group_key = key_e
            oE2 = er7.operator("light_editor.isolate_group_emissive", text="",
                               icon=('RADIOBUT_ON' if group_checkbox_2_state.get(key_e, False) else 'RADIOBUT_OFF'))
            oE2.group_key = key_e
            select_icon = 'RESTRICT_SELECT_ON' if is_group_selected(key_e, [o for o, m in emissive_pairs]) else 'RESTRICT_SELECT_OFF'
            op_select = er7.operator("le.select_group", text="", icon=select_icon)
            op_select.group_key = key_e
            oE3 = er7.operator("light_editor.toggle_group",
                               emboss=True,
                               icon=('DOWNARROW_HLT' if not group_collapse_dict.get(key_e, False) else 'RIGHTARROW'))
            oE3.group_key = key_e
            er7.label(text="All Emissive Materials (Alphabetical)", icon='SHADING_RENDERED')
            if not group_collapse_dict.get(key_e, False):
                cb7 = eb7.box()
                for o, m in sorted(emissive_pairs, key=lambda x: f"{x[0].name}_{x[1].name}".lower()):
                    draw_emissive_row(cb7, o, m)
            if scene.world:
                draw_environment_single_row(layout.box(), context, filter_str)

def draw_environment_single_row(box, context, filter_str=""):
    """Draw the environment section as a single collapsible row."""
    scene = context.scene
    world = scene.world
    nt = world.node_tree if world and world.use_nodes else None
    output_node = next((n for n in nt.nodes if n.type == 'OUTPUT_WORLD'), None) if nt else None
    surf_input = output_node.inputs.get("Surface") if output_node else None
    vol_input = output_node.inputs.get("Volume") if output_node else None
    is_on = environment_checkbox_state.get('environment', True)
    icon = 'CHECKBOX_HLT' if is_on else 'CHECKBOX_DEHLT'
    iso_icon = 'RADIOBUT_ON' if _unified_isolate_manager.is_active(UnifiedIsolateMode.ENVIRONMENT) else 'RADIOBUT_OFF'
    # Search filtering logic
    f = filter_str.lower()
    show_surface = not f or f in "surface"
    show_volume = not f or f in "volume"
    if not (show_surface or show_volume):
        return  # Don't draw anything if neither matches
    # Header row
    header_row = box.row(align=True)
    header_row.operator("le.toggle_environment", text="", icon=icon, depress=is_on)
    header_row.operator("le.isolate_environment", text="", icon=iso_icon).mode = "HEADER"
    # Collapse toggle
    group_key = "env_header"
    collapsed = group_collapse_dict.get(group_key, False)
    header_row.operator("light_editor.toggle_group",
                        text="",
                        emboss=True,
                        icon='RIGHTARROW' if collapsed else 'DOWNARROW_HLT').group_key = group_key
    header_row.label(text="Environment", icon='WORLD')
    # Content (Surface/Volume rows), only if not collapsed
    if not collapsed:
        content_box = box.box()
        if show_surface:
            row = content_box.row(align=True)
            row.operator("le.toggle_env_socket",
                text="",
                icon='OUTLINER_OB_LIGHT' if surf_input and surf_input.is_linked else 'LIGHT_DATA',
                depress=surf_input and surf_input.is_linked).socket_name = "Surface"
            row.operator("le.isolate_environment",
                text="",
                icon='RADIOBUT_ON' if isolate_env_surface_state else 'RADIOBUT_OFF').mode = "SURFACE"
            row.prop(scene, "env_surface_label", text="")
        if show_volume:
            row = content_box.row(align=True)
            row.operator("le.toggle_env_socket",
                text="",
                icon='OUTLINER_OB_LIGHT' if vol_input and vol_input.is_linked else 'LIGHT_DATA',
                depress=vol_input and vol_input.is_linked).socket_name = "Volume"
            row.operator("le.isolate_environment",
                text="",
                icon='RADIOBUT_ON' if isolate_env_volume_state else 'RADIOBUT_OFF').mode = "VOLUME"
            row.prop(scene, "env_volume_label", text="")

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
                    print(f"Redrawing area: {area.type}")
                    area.tag_redraw()
    except Exception as e:
        print(f"Error in redraw handler: {e}")


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
    bpy.app.handlers.depsgraph_update_post.append(LE_force_redraw_on_use_nodes_change)
    # --- Register the new render layer property ---
    bpy.types.Scene.light_editor_selected_render_layer = bpy.props.EnumProperty(
        name="Render Layer",
        description="Select the active render layer for the Light Editor",
        items=get_render_layer_items,
        update=update_render_layer,
    )
    # After defining the property, set its initial value to the current view layer
    def set_initial_render_layer(dummy):
        if hasattr(bpy.types.Scene, 'light_editor_selected_render_layer'):
            try:
                current_vl_name = bpy.context.view_layer.name
                if bpy.context.scene.view_layers.get(current_vl_name):
                    bpy.context.scene.light_editor_selected_render_layer = current_vl_name
            except:
                pass

    bpy.app.handlers.load_post.append(set_initial_render_layer)
    # Attempt to set initial value during registration if context is suitable
    try:
        set_initial_render_layer(None)
    except:
        pass

    bpy.types.Scene.env_surface_label = bpy.props.StringProperty(default="Surface")
    bpy.types.Scene.env_volume_label = bpy.props.StringProperty(default="Volume")
    bpy.types.Scene.env_surface_label = bpy.props.StringProperty(name="Surface", default="Surface")
    bpy.types.Scene.env_volume_label = bpy.props.StringProperty(name="Volume", default="Volume")
    bpy.types.Scene.current_active_light = bpy.props.PointerProperty(type=bpy.types.Object)
    bpy.types.Scene.current_exclusive_group = bpy.props.StringProperty()
    # The old selected_render_layer property is replaced by light_editor_selected_render_layer
    # bpy.types.Scene.selected_render_layer = EnumProperty(...) # Removed/Replaced

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
    bpy.app.handlers.depsgraph_update_post.append(LE_clear_emissive_cache)


def unregister():
    """Unregister all classes and properties."""
    # --- Unregister the new render layer property ---
    if hasattr(bpy.types.Scene, 'light_editor_selected_render_layer'):
        del bpy.types.Scene.light_editor_selected_render_layer

    # Remove the load handler if it was added
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
    # --- End unregister new property ---

    if LE_force_redraw_on_use_nodes_change in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(LE_force_redraw_on_use_nodes_change)
    if hasattr(bpy.types.Scene, "env_surface_label"):
        del bpy.types.Scene.env_surface_label
    if hasattr(bpy.types.Scene, "env_volume_label"):
        del bpy.types.Scene.env_volume_label
    if hasattr(bpy.types.Scene, 'env_surface_label'):
        del bpy.types.Scene.env_surface_label
    if hasattr(bpy.types.Scene, 'env_volume_label'):
        del bpy.types.Scene.env_volume_label
    if LE_clear_emissive_cache in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(LE_clear_emissive_cache)
    if LE_clear_handler in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(LE_clear_handler)
    if LE_check_lights_enabled in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(LE_check_lights_enabled)
    # The old selected_render_layer property removal
    # if hasattr(bpy.types.Scene, 'selected_render_layer'): # Removed/Replaced
    #     del bpy.types.Scene.selected_render_layer # Removed/Replaced
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
    if hasattr(bpy.types.Scene, 'collapse_all_emissives'):
        del bpy.types.Scene.collapse_all_emissives
    if hasattr(bpy.types.Scene, 'collapse_all_emissives_alpha'):
        del bpy.types.Scene.collapse_all_emissives_alpha
    for cls in reversed(classes):
        if hasattr(bpy.types, cls.__name__):
            bpy.utils.unregister_class(cls)

if __name__ == "__main__":
    try:
        unregister()
        print("🔄 Unregistered previous version")
    except Exception as e:
        print(f"⚠ Unregister failed (probably first run): {e}")
    register()
    print("✅ Registered updated LightEditor")

