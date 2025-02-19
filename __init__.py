bl_info = {
    "name": "Light Editor",
    "author": "Robert Rioux aka Blender Bob, Rombout Versluijs",
    "location": "3Dview > Light Editor",
    "version": (1, 9, 7),
    "blender": (4, 2, 0),
    "description": "A Light Editor and Light Linking addon",
    "category": "Object",
}

# __init__.py
import bpy

# Import your submodules:
from . import LightEditor
from . import Linking
from . import LightGroup
from . icons import initialize_icons_collection, unload_icons

def register():
    LightEditor.register()
    Linking.register()
    LightGroup.register()
    initialize_icons_collection()

def unregister():
    # Unregister in reverse order (best practice)
    LightGroup.unregister()
    Linking.unregister()
    LightEditor.unregister()
    unload_icons()


if __name__ == "__main__":
    register()
