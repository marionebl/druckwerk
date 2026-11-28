import ast
import importlib.util
import os

HUB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def test_hub_uses_only_names_the_slicer_module_has():
    # hub.py runs only in the image; a name removed from the slicer makes the app exit at start.
    spec = importlib.util.spec_from_file_location("slicer_names", os.path.join(HUB, "slicer", "server.py"))
    s = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(s)
    tree = ast.parse(open(os.path.join(HUB, "hub.py")).read())
    used = {n.attr for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "s"}
    assert used and [a for a in sorted(used) if not hasattr(s, a)] == []
