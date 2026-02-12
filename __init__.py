bl_info = {
    "name": "Light Editor",
    "author": "Robert Rioux aka Blender Bob, Rombout Versluijs",
    "location": "3Dview > Light Editor",
    "version": (2, 4, 1),
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

def register():
    LightEditor.register()
    Linking.register()
    LightGroup.register()

def unregister():
    # Unregister in reverse order (best practice)
    LightGroup.unregister()
    Linking.unregister()
    LightEditor.unregister()

if __name__ == "__main__":
    register()
