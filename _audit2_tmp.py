import sys, os, django, pkgutil, importlib, inspect
sys.path.insert(0, ".")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
django.setup()

from rest_framework import serializers as S
from rest_framework.relations import RelatedField, ManyRelatedField, PrimaryKeyRelatedField

import apps
seen = set()
for mod in pkgutil.walk_packages(apps.__path__, "apps."):
    name = mod.name
    if "migrations" in name or name.endswith(".tests"):
        continue
    try:
        m = importlib.import_module(name)
    except Exception:
        continue
    for attr, obj in vars(m).items():
        if not inspect.isclass(obj) or not issubclass(obj, S.BaseSerializer):
            continue
        if obj.__module__ != name or obj in seen:
            continue
        seen.add(obj)
        try:
            fields = obj().fields
        except Exception as exc:
            continue
        for fname, f in fields.items():
            if "session" not in fname:
                continue
            if isinstance(f, (RelatedField, ManyRelatedField)):
                print(f"RELATED  {name}.{obj.__name__}.{fname} -> {type(f).__name__}")
print("audited serializers:", len(seen))
