bl_info = {
    "name": "Light Editor",
    "author": "Robert Rioux aka Blender Bob, Rombout Versluijs",
    "location": "3Dview > Light Editor",
    "version": (1, 9, 7),
    "blender": (4, 2, 0),
    "description": "A Light Editor and Light linking addon",
    "category": "Object",
}

# __init__.py
import bpy

# Import your submodules:
from . import lighteditor
from . import linking
from . import lightgroup
from . icons import initialize_icons_collection, unload_icons

def register():
    lighteditor.register()
    linking.register()
    lightgroup.register()
    initialize_icons_collection()

def unregister():
    # Unregister in reverse order (best practice)
    lightgroup.unregister()
    linking.unregister()
    lighteditor.unregister()
    unload_icons()


if __name__ == "__main__":
    register()
