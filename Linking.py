import bpy
from bpy.app.handlers import persistent

# -------------------------------------------------------------------
#   Helper: Get Selected Collections from the Outliner
# -------------------------------------------------------------------
def get_selected_collections(context):
    """
    Uses a temporary override to obtain selected collections from the Outliner.
    Returns a list of bpy.types.Collection.
    """
    selected = []
    outliner_area = next((area for area in context.window.screen.areas if area.type == 'OUTLINER'), None)
    if outliner_area:
        region = next((r for r in outliner_area.regions if r.type == 'WINDOW'), None)
        with context.temp_override(window=context.window, screen=context.screen, area=outliner_area, region=region):
            if hasattr(bpy.context, "selected_ids"):
                for id_item in bpy.context.selected_ids:
                    if isinstance(id_item, bpy.types.Collection):
                        selected.append(id_item)
    return selected

# -------------------------------------------------------------------
#   Helper Functions: Ensure BB_ Collections
# -------------------------------------------------------------------
def ensure_bb_collection(light):
    """
    Ensures that the BB_Light Linking collection exists for the given light.
    The collection will not be linked to the scene hierarchy and will remain hidden in the Outliner.
    """
    prop_name = "light_linking_receiver_collection"
    expected_name = f"BB_Light Linking for {light.name}"
    
    bb_collection = bpy.data.collections.get(expected_name)
    if bb_collection:
        light[prop_name] = expected_name
        return bb_collection

    # Use the operator to create the light linking collection
    try:
        bpy.ops.object.select_all(action='DESELECT')
        light.select_set(True)
        bpy.context.view_layer.objects.active = light
        bpy.ops.object.light_linking_receiver_collection_new()
        
        if hasattr(light, "light_linking") and hasattr(light.light_linking, "receiver_collection"):
            new_collection = light.light_linking.receiver_collection
            new_collection.name = expected_name
            light[prop_name] = expected_name
            return new_collection
    except Exception as e:
        print(f"Operator failed: {e}. Falling back to manual collection creation.")

    new_collection = bpy.data.collections.new(expected_name)
    light[prop_name] = expected_name
    return new_collection

def ensure_shadow_collection(light):
    """
    For shadow linking, ensures that the BB_Shadow Linking collection exists.
    Expected name: "BB_Shadow Linking for <light_name>".
    The collection will not be linked to the scene hierarchy and will remain hidden in the Outliner.
    """
    prop_name = "shadow_linking_blocker_collection"
    expected_name = f"BB_Shadow Linking for {light.name}"
    
    shadow_collection = bpy.data.collections.get(expected_name)
    if shadow_collection:
        if hasattr(light, "light_linking") and hasattr(light.light_linking, "blocker_collection"):
            light.light_linking.blocker_collection = shadow_collection
            light[prop_name] = expected_name
        return shadow_collection

    new_collection = bpy.data.collections.new(expected_name)
    if hasattr(light, "light_linking") and hasattr(light.light_linking, "blocker_collection"):
        light.light_linking.blocker_collection = new_collection
        light[prop_name] = expected_name
    return new_collection

# -------------------------------------------------------------------
#   Property Groups for List Items
# -------------------------------------------------------------------
class LL_LightItem(bpy.types.PropertyGroup):
    name:     bpy.props.StringProperty()
    obj:      bpy.props.PointerProperty(type=bpy.types.Object)
    selected: bpy.props.BoolProperty(default=False)

class LL_MeshItem(bpy.types.PropertyGroup):
    name:     bpy.props.StringProperty()
    obj:      bpy.props.PointerProperty(type=bpy.types.Object)
    selected: bpy.props.BoolProperty(default=False)

class LL_CollectionItem(bpy.types.PropertyGroup):
    name:     bpy.props.StringProperty()
    coll:     bpy.props.PointerProperty(type=bpy.types.Collection)
    selected: bpy.props.BoolProperty(default=False)

# -------------------------------------------------------------------
#   Update Functions for Full List Population
# -------------------------------------------------------------------
def update_light_items(scene, context):
    prev_sel = {item.name: item.selected for item in scene.ll_light_items}
    scene.ll_light_items.clear()
    for obj in scene.objects:
        if obj.type == 'LIGHT':
            item = scene.ll_light_items.add()
            item.name = obj.name
            item.obj = obj
            item.selected = prev_sel.get(obj.name, False)
    scene.ll_light_index = 0 if scene.ll_light_items else -1

def update_mesh_items(scene, context):
    prev_sel = {item.name: item.selected for item in scene.ll_mesh_items}
    scene.ll_mesh_items.clear()
    for obj in scene.objects:
        if obj.type == 'MESH':
            item = scene.ll_mesh_items.add()
            item.name = obj.name
            item.obj = obj
            item.selected = prev_sel.get(obj.name, False)
    scene.ll_mesh_index = 0 if scene.ll_mesh_items else -1

def update_collection_items(scene, context):
    prev_sel = {item.name: item.selected for item in scene.ll_collection_items}
    scene.ll_collection_items.clear()
    for coll in bpy.data.collections:
        if "Light Linking for" in coll.name:
            continue
        item = scene.ll_collection_items.add()
        item.name = coll.name
        item.coll = coll
        item.selected = prev_sel.get(coll.name, False)
    scene.ll_collection_index = 0 if scene.ll_collection_items else -1

def force_redraw(context):
    for area in context.screen.areas:
        if area.type == 'VIEW_3D':
            area.tag_redraw()

# -------------------------------------------------------------------
#   Operator to Toggle an Item’s Selection
# -------------------------------------------------------------------
class LL_OT_ToggleSelection(bpy.types.Operator):
    bl_idname = "ll_editor.toggle_selection"
    bl_label = "Toggle Selection"
    bl_description = "Toggle the selection state for this item"
    
    item_name: bpy.props.StringProperty()
    item_type: bpy.props.EnumProperty(
        items=[
            ('LIGHT', "Light", ""),
            ('MESH',  "Mesh", ""),
            ('COLLECTION', "Collection", ""),
        ]
    )
    
    def execute(self, context):
        scene = context.scene
        if self.item_type == 'LIGHT':
            for item in scene.ll_light_items:
                if item.name == self.item_name:
                    item.selected = not item.selected
                    break
        elif self.item_type == 'MESH':
            for item in scene.ll_mesh_items:
                if item.name == self.item_name:
                    item.selected = not item.selected
                    break
        elif self.item_type == 'COLLECTION':
            for item in scene.ll_collection_items:
                if item.name == self.item_name:
                    item.selected = not item.selected
                    break
        else:
            self.report({'WARNING'}, "Unknown item type")
            return {'CANCELLED'}
        return {'FINISHED'}

# -------------------------------------------------------------------
#   Operators for Refreshing/Resetting Lists
# -------------------------------------------------------------------
class LL_OT_RefreshSelectedLights(bpy.types.Operator):
    bl_idname = "ll_editor.refresh_selected_lights"
    bl_label = "Refresh Selected Lights"
    bl_description = "Filter the lights list to show only lights selected in the viewport. If none are selected, use the active light."
    
    def execute(self, context):
        scene = context.scene
        selected_lights = [obj for obj in context.selected_objects if obj.type == 'LIGHT']
        if not selected_lights:
            active_obj = context.view_layer.objects.active
            if active_obj and active_obj.type == 'LIGHT':
                selected_lights.append(active_obj)
        if not selected_lights:
            self.report({'WARNING'}, "No lights selected in the viewport")
            return {'CANCELLED'}

        scene.ll_light_items.clear()
        for obj in selected_lights:
            item = scene.ll_light_items.add()
            item.name = obj.name
            item.obj = obj
            item.selected = True

        scene.ll_light_index = 0 if scene.ll_light_items else -1
        force_redraw(context)
        self.report({'INFO'}, f"Filtered lights to {len(selected_lights)} item(s)")
        return {'FINISHED'}

class LL_OT_RefreshSelectedMeshes(bpy.types.Operator):
    bl_idname = "ll_editor.refresh_selected_meshes"
    bl_label = "Refresh Selected Meshes"
    bl_description = "Filter the mesh list to show only meshes selected in the viewport. If none are selected, use the active mesh."
    
    def execute(self, context):
        scene = context.scene
        selected_meshes = [obj for obj in context.selected_objects if obj.type == 'MESH']
        if not selected_meshes:
            active_obj = context.view_layer.objects.active
            if active_obj and active_obj.type == 'MESH':
                selected_meshes.append(active_obj)
        if not selected_meshes:
            self.report({'WARNING'}, "No meshes selected in the viewport")
            return {'CANCELLED'}

        scene.ll_mesh_items.clear()
        for obj in selected_meshes:
            item = scene.ll_mesh_items.add()
            item.name = obj.name
            item.obj = obj
            item.selected = True

        scene.ll_mesh_index = 0 if scene.ll_mesh_items else -1
        force_redraw(context)
        self.report({'INFO'}, f"Filtered meshes to {len(selected_meshes)} item(s)")
        return {'FINISHED'}

class LL_OT_RefreshSelectedCollections(bpy.types.Operator):
    bl_idname = "ll_editor.refresh_selected_collections"
    bl_label = "Refresh Selected Collections"
    bl_description = (
        "Filter the collection list to show only collections selected in the Outliner. "
        "If none are selected, fall back to the UI list selection."
    )
    
    def execute(self, context):
        scene = context.scene
        selected_collections = get_selected_collections(context)
        if not selected_collections:
            selected_collections = [item.coll for item in scene.ll_collection_items if item.selected and item.coll]
        if not selected_collections and scene.ll_collection_index >= 0:
            active_item = scene.ll_collection_items[scene.ll_collection_index]
            if active_item.coll:
                selected_collections.append(active_item.coll)

        if not selected_collections:
            self.report({'WARNING'}, "No collections selected")
            return {'CANCELLED'}

        scene.ll_collection_items.clear()
        for coll in selected_collections:
            item = scene.ll_collection_items.add()
            item.name = coll.name
            item.coll = coll
            item.selected = True

        scene.ll_collection_index = 0 if scene.ll_collection_items else -1
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()

        self.report({'INFO'}, f"Filtered collections to {len(selected_collections)} item(s)")
        return {'FINISHED'}

class LL_OT_RefreshAllLights(bpy.types.Operator):
    bl_idname = "ll_editor.refresh_all_lights"
    bl_label = "Refresh All Lights"
    bl_description = "Display all lights in the scene that are turned on and renderable"

    def execute(self, context):
        scene = context.scene
        prev_sel = {item.name: item.selected for item in scene.ll_light_items}
        scene.ll_light_items.clear()

        for obj in scene.objects:
            if obj.type == 'LIGHT':
                if not obj.hide_render and not obj.hide_viewport:
                    item = scene.ll_light_items.add()
                    item.name = obj.name
                    item.obj = obj
                    item.selected = prev_sel.get(obj.name, False)

        scene.ll_light_index = 0 if scene.ll_light_items else -1
        force_redraw(context)
        self.report({'INFO'}, f"Listed {len(scene.ll_light_items)} visible and renderable lights")
        return {'FINISHED'}

class LL_OT_ResetLights(bpy.types.Operator):
    bl_idname = "ll_editor.reset_lights"
    bl_label = "Reset Lights"
    bl_description = "Deselect all lights in the list"
    
    def execute(self, context):
        for item in context.scene.ll_light_items:
            item.selected = False
        force_redraw(context)
        self.report({'INFO'}, "Light selections reset")
        return {'FINISHED'}

class LL_OT_RefreshAllMeshes(bpy.types.Operator):
    bl_idname = "ll_editor.refresh_all_meshes"
    bl_label = "Refresh All Meshes"
    bl_description = "Display all meshes in the scene"
    
    def execute(self, context):
        update_mesh_items(context.scene, context)
        force_redraw(context)
        self.report({'INFO'}, f"Listed all {len(context.scene.ll_mesh_items)} meshes")
        return {'FINISHED'}

class LL_OT_ResetMeshes(bpy.types.Operator):
    bl_idname = "ll_editor.reset_meshes"
    bl_label = "Reset Meshes"
    bl_description = "Deselect all meshes in the list"
    
    def execute(self, context):
        for item in context.scene.ll_mesh_items:
            item.selected = False
        force_redraw(context)
        self.report({'INFO'}, "Mesh selections reset")
        return {'FINISHED'}

class LL_OT_RefreshAllCollections(bpy.types.Operator):
    bl_idname = "ll_editor.refresh_all_collections"
    bl_label = "Refresh All Collections"
    bl_description = "Display all collections in the scene"
    
    def execute(self, context):
        update_collection_items(context.scene, context)
        force_redraw(context)
        self.report({'INFO'}, f"Listed all {len(context.scene.ll_collection_items)} collections")
        return {'FINISHED'}

class LL_OT_ResetCollections(bpy.types.Operator):
    bl_idname = "ll_editor.reset_collections"
    bl_label = "Reset Collections"
    bl_description = "Deselect all collections in the list"
    
    def execute(self, context):
        for item in context.scene.ll_collection_items:
            item.selected = False
        force_redraw(context)
        self.report({'INFO'}, "Collection selections reset")
        return {'FINISHED'}

# -------------------------------------------------------------------
#   Operators for Linking/Unlinking (Here's the "Assign" operator)
# -------------------------------------------------------------------
class LL_OT_Link(bpy.types.Operator):
    bl_idname = "ll_editor.link"
    bl_label = "Link Lights to Objects"
    bl_description = (
        "For each selected light, use the UI-selected BB_ light linking collection (or create it) and add "
        "the selected meshes (from the Mesh and Collection lists) to it."
    )

    def execute(self, context):
        scene = context.scene
        selected_lights = [item.obj for item in scene.ll_light_items if item.selected and item.obj]
        selected_meshes = [item.obj for item in scene.ll_mesh_items if item.selected and item.obj]
        collection_meshes = []
        for item in scene.ll_collection_items:
            if item.selected and item.coll:
                for obj in item.coll.all_objects:
                    if obj.type == 'MESH':
                        collection_meshes.append(obj)

        # If no groups exist at all (i.e. "lightgroups" in the view layer is empty),
        # you could check that here if needed, e.g.:
        # view_layer = context.view_layer
        # if not hasattr(view_layer, "lightgroups") or not view_layer.lightgroups:
        #     self.report({'WARNING'}, "Please create a light group first.")
        #     return {'CANCELLED'}

        if not selected_lights:
            self.report({'WARNING'}, "No lights selected")
            return {'CANCELLED'}

        all_meshes = {obj.name: obj for obj in (selected_meshes + collection_meshes)}.values()
        if not list(all_meshes):
            self.report({'WARNING'}, "No mesh objects selected")
            return {'CANCELLED'}

        total_linked_meshes = 0
        for light in selected_lights:
            if not light.visible_get():
                self.report({'ERROR'}, f"Light must be visible for linking: {light.name}")
                continue

            bpy.ops.object.select_all(action='DESELECT')
            light.select_set(True)
            context.view_layer.objects.active = light

            new_group = ensure_bb_collection(light)
            # Here is where we can show "Please create a light group first" if we fail:
            if not new_group:
                # Instead of the old message:
                # self.report({'WARNING'}, f"Failed to create or retrieve linking group for {light.name}")
                self.report({'WARNING'}, "Please create a light group first.")
                continue

            linked_meshes = 0
            for obj in all_meshes:
                if not new_group.objects.get(obj.name):
                    new_group.objects.link(obj)
                    linked_meshes += 1

            total_linked_meshes += linked_meshes

        self.report({'INFO'}, f"Linked {len(selected_lights)} light(s) to {total_linked_meshes} mesh(es)")
        return {'FINISHED'}

class LL_OT_Unlink(bpy.types.Operator):
    bl_idname = "ll_editor.unlink"
    bl_label = "Unlink Lights from Objects"
    bl_description = (
        "For each selected light, remove objects (from the Mesh and Collection lists) that are linked "
        "in the BB_ light linking collection."
    )
    
    def execute(self, context):
        scene = context.scene
        selected_lights = [item.obj for item in scene.ll_light_items if item.selected and item.obj]
        if not selected_lights:
            self.report({'WARNING'}, "No lights selected")
            return {'CANCELLED'}

        selected_meshes = [item.obj for item in scene.ll_mesh_items if item.selected and item.obj]
        collection_meshes = []
        for item in scene.ll_collection_items:
            if item.selected and item.coll:
                for obj in item.coll.all_objects:
                    if obj.type == 'MESH':
                        collection_meshes.append(obj)
        
        all_meshes = {obj.name: obj for obj in (selected_meshes + collection_meshes)}.values()

        total_removed = 0
        for light in selected_lights:
            group_name = f"BB_Light Linking for {light.name}"
            linking_group = bpy.data.collections.get(group_name)
            if not linking_group:
                self.report({'WARNING'}, f"No BB_ linking group found for {light.name}")
                continue

            removed = 0
            for obj in list(linking_group.objects):
                if obj.name in [m_obj.name for m_obj in all_meshes]:
                    linking_group.objects.unlink(obj)
                    removed += 1

            total_removed += removed
            if "light_linking_receiver_collection" in light:
                del light["light_linking_receiver_collection"]

        self.report({'INFO'}, f"Unlinked objects from {len(selected_lights)} light(s); removed {total_removed} object(s)")
        return {'FINISHED'}

class LL_OT_ShadowLink(bpy.types.Operator):
    bl_idname = "ll_editor.shadow_link"
    bl_label = "Shadow Link Lights to Objects"
    bl_description = (
        "For each selected light, use the UI-selected BB_ shadow linking collection (or create it) and add "
        "the selected meshes (from the Mesh and Collection lists) to it."
    )

    def execute(self, context):
        scene = context.scene
        selected_lights = [item.obj for item in scene.ll_light_items if item.selected and item.obj]
        selected_meshes = [item.obj for item in scene.ll_mesh_items if item.selected and item.obj]
        collection_meshes = []
        for item in scene.ll_collection_items:
            if item.selected and item.coll:
                for obj in item.coll.all_objects:
                    if obj.type == 'MESH':
                        collection_meshes.append(obj)

        all_meshes = {obj.name: obj for obj in (selected_meshes + collection_meshes)}.values()

        if not selected_lights:
            self.report({'WARNING'}, "No lights selected for shadow linking.")
            return {'CANCELLED'}
        if not list(all_meshes):
            self.report({'WARNING'}, "No mesh objects selected for shadow linking.")
            return {'CANCELLED'}

        total_linked_meshes = 0
        for light in selected_lights:
            if not light.visible_get():
                self.report({'ERROR'}, f"Light must be visible for linking: {light.name}")
                continue

            bpy.ops.object.select_all(action='DESELECT')
            light.select_set(True)
            context.view_layer.objects.active = light

            new_group = ensure_shadow_collection(light)
            if not new_group:
                self.report({'WARNING'}, "Failed to create or retrieve shadow linking group for {light.name}")
                continue

            linked_meshes = 0
            for obj in all_meshes:
                if not new_group.objects.get(obj.name):
                    new_group.objects.link(obj)
                    linked_meshes += 1

            light["shadow_linking_blocker_collection"] = new_group.name
            total_linked_meshes += linked_meshes

        self.report({'INFO'}, f"Shadow Linked {len(selected_lights)} light(s) to {total_linked_meshes} mesh(es)")
        return {'FINISHED'}

class LL_OT_ShadowUnlink(bpy.types.Operator):
    bl_idname = "ll_editor.shadow_unlink"
    bl_label = "Shadow Unlink Lights from Objects"
    bl_description = (
        "For each selected light, remove objects (from the Mesh and Collection lists) that are linked "
        "in the BB_ shadow linking collection."
    )

    def execute(self, context):
        scene = context.scene
        selected_lights = [item.obj for item in scene.ll_light_items if item.selected and item.obj]
        if not selected_lights:
            self.report({'WARNING'}, "No lights selected")
            return {'CANCELLED'}

        selected_meshes = [item.obj for item in scene.ll_mesh_items if item.selected and item.obj]
        collection_meshes = []
        for item in scene.ll_collection_items:
            if item.selected and item.coll:
                for obj in item.coll.all_objects:
                    if obj.type == 'MESH':
                        collection_meshes.append(obj)

        all_meshes = {obj.name: obj for obj in (selected_meshes + collection_meshes)}.values()

        total_removed = 0
        for light in selected_lights:
            expected_name = f"BB_Shadow Linking for {light.name}"
            linking_group = bpy.data.collections.get(expected_name)
            if not linking_group:
                self.report({'INFO'}, f"No shadow linking group '{expected_name}' found for light '{light.name}'")
                continue

            removed = 0
            for obj in list(linking_group.objects):
                if obj.name in [m_obj.name for m_obj in all_meshes]:
                    try:
                        linking_group.objects.unlink(obj)
                        removed += 1
                    except Exception as e:
                        print(f"DEBUG: Error unlinking {obj.name}: {e}")

            total_removed += removed
            if "shadow_linking_blocker_collection" in light:
                del light["shadow_linking_blocker_collection"]

        self.report({'INFO'}, f"Shadow Unlinked objects from {len(selected_lights)} light(s); removed {total_removed} object(s)")
        return {'FINISHED'}

# -------------------------------------------------------------------
#   UIList Classes for Scrollable Lists
# -------------------------------------------------------------------
class LL_UL_LightList_UI(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        self.use_filter_show = True
        row = layout.row(align=True)
        row.prop(item, "selected", text="")
        row.label(text=item.name)

class LL_UL_MeshList_UI(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        self.use_filter_show = True
        row = layout.row(align=True)
        row.prop(item, "selected", text="")
        row.label(text=item.name)

class LL_UL_CollectionList_UI(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        self.use_filter_show = True
        row = layout.row(align=True)
        row.prop(item, "selected", text="")
        row.label(text=item.name)

# -------------------------------------------------------------------
#   Panel – UI Layout
# -------------------------------------------------------------------
class LL_PT_Panel(bpy.types.Panel):
    bl_label = "Light Link"
    bl_idname = "LL_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Light Editor"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return context.scene.render.engine == 'CYCLES'

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        scene = context.scene
        main_row = layout.row(align=True)
        
        col_lights = main_row.column(align=True)
        col_lights.label(text="Lights")
        col_lights.template_list("LL_UL_LightList_UI", "", scene, "ll_light_items", scene, "ll_light_index", rows=scene.ll_list_rows)
        
        col_meshes = main_row.column(align=True)
        col_meshes.label(text="Meshes")
        col_meshes.template_list("LL_UL_MeshList_UI", "", scene, "ll_mesh_items", scene, "ll_mesh_index", rows=scene.ll_list_rows)
        
        col_colls = main_row.column(align=True)
        col_colls.label(text="Collections")
        col_colls.template_list("LL_UL_CollectionList_UI", "", scene, "ll_collection_items", scene, "ll_collection_index", rows=scene.ll_list_rows)
        
        layout.separator()
        layout.prop(scene, "ll_list_rows", text="List Height")
        layout.separator()
        
        op_row = layout.row(align=True)
        col_light_ops = op_row.column(align=True)
        col_light_ops.operator("ll_editor.refresh_selected_lights", text="Selected Lights")
        col_light_ops.operator("ll_editor.refresh_all_lights", text="All Visible Lights")
        col_light_ops.operator("ll_editor.reset_lights", text="Deselect All")
        
        col_mesh_ops = op_row.column(align=True)
        col_mesh_ops.operator("ll_editor.refresh_selected_meshes", text="Selected Meshes")
        col_mesh_ops.operator("ll_editor.refresh_all_meshes", text="All Meshes")
        col_mesh_ops.operator("ll_editor.reset_meshes", text="Deselect All")
        
        col_coll_ops = op_row.column(align=True)
        col_coll_ops.operator("ll_editor.refresh_selected_collections", text="Selected Collections")
        col_coll_ops.operator("ll_editor.refresh_all_collections", text="All Collections")
        col_coll_ops.operator("ll_editor.reset_collections", text="Deselect All")
        
        layout.separator()
        link_row = layout.row(align=True)
        link_row.operator("ll_editor.link", text="Light Link")
        link_row.operator("ll_editor.unlink", text="Light Unlink")
        
        shadow_link_row = layout.row(align=True)
        shadow_link_row.operator("ll_editor.shadow_link", text="Shadow Link")
        shadow_link_row.operator("ll_editor.shadow_unlink", text="Shadow Unlink")

@persistent
def LL_clear_handler(dummy):
    update_light_items(bpy.context.scene, bpy.context)
    update_mesh_items(bpy.context.scene, bpy.context)
    update_collection_items(bpy.context.scene, bpy.context)

# -------------------------------------------------------------------
#   Registration
# -------------------------------------------------------------------
classes = [
    LL_LightItem,
    LL_MeshItem,
    LL_CollectionItem,
    LL_OT_ToggleSelection,
    LL_OT_RefreshSelectedLights,
    LL_OT_RefreshSelectedMeshes,
    LL_OT_RefreshSelectedCollections,
    LL_OT_RefreshAllLights,
    LL_OT_ResetLights,
    LL_OT_RefreshAllMeshes,
    LL_OT_ResetMeshes,
    LL_OT_RefreshAllCollections,
    LL_OT_ResetCollections,
    LL_OT_Link,
    LL_OT_Unlink,
    LL_OT_ShadowLink,
    LL_OT_ShadowUnlink,
    LL_UL_LightList_UI,
    LL_UL_MeshList_UI,
    LL_UL_CollectionList_UI,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.utils.register_class(LL_PT_Panel)

    bpy.types.Scene.ll_light_items = bpy.props.CollectionProperty(type=LL_LightItem)
    bpy.types.Scene.ll_mesh_items = bpy.props.CollectionProperty(type=LL_MeshItem)
    bpy.types.Scene.ll_collection_items = bpy.props.CollectionProperty(type=LL_CollectionItem)

    bpy.types.Scene.ll_light_index = bpy.props.IntProperty(default=-1)
    bpy.types.Scene.ll_mesh_index = bpy.props.IntProperty(default=-1)
    bpy.types.Scene.ll_collection_index = bpy.props.IntProperty(default=-1)
    bpy.types.Scene.ll_list_rows = bpy.props.IntProperty(
        name="List Height",
        description="Number of rows to display in each list",
        default=10,
        min=1,
        max=50
    )

    bpy.app.handlers.load_post.append(LL_clear_handler)


def unregister():
    del bpy.types.Scene.ll_light_items
    del bpy.types.Scene.ll_mesh_items
    del bpy.types.Scene.ll_collection_items
    del bpy.types.Scene.ll_light_index
    del bpy.types.Scene.ll_mesh_index
    del bpy.types.Scene.ll_collection_index
    del bpy.types.Scene.ll_list_rows

    bpy.utils.unregister_class(LL_PT_Panel)

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

    bpy.app.handlers.load_post.remove(LL_clear_handler)




if __name__ == "__main__":
    register()
