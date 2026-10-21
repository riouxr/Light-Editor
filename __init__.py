bl_info = {
    "name": "Light Editor",
    "author": "Robert Rioux aka Blender Bob, Rombout Versluijs",
    "location": "3Dview > Light Editor",
    "version": (2, 4, 7),
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
    # Unregister in reverse order (best practice). Each module is unregistered
    # independently: if one raises, the others must still be torn down, or the
    # classes they left registered break the next enable with
    # "already registered as a subclass".
    import traceback
    for module in (LightGroup, Linking, LightEditor):
        try:
            module.unregister()
        except Exception:
            traceback.print_exc()

if __name__ == "__main__":
    register()
