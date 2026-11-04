"""Every event kind the code records is registered (homelab#2618).

The events table no longer checks its kinds (migration 23), so the registry in
:mod:`music_fetch.event_kinds` is the only list.  :func:`events.record` and
:func:`events.insert` refuse a kind it lacks at run time; this test finds the
same mistake before it ships, by reading every call site of either in the
three packages.

The scan follows a kind from the call to where it is spelled: a string literal,
a module constant (``SET_KIND = "recording_set"``, also imported), a parameter
of a wrapper (``albums._event(kind, …)``: every call of the wrapper in its
module), a local assignment, a ``for`` over a tuple of tuples, or a conditional
expression.  It follows a wrapper's ``**data`` the same way, so the ``data``
keys a call site spells out are checked against the kind's registered fields.
A ``data`` part it can't read (a dict built elsewhere) is skipped; a kind it
can't read fails the test, so that call site must be made readable or listed in
:data:`OPAQUE` with the kinds it records.
"""

from __future__ import annotations

import ast
import importlib.util
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from music_fetch import db, event_kinds

PACKAGES = ("music_fetch", "music_scan", "music_service")

# Call sites whose kind the scan can't follow, with the kinds they record: each
# must also appear as a string literal in the same module.
OPAQUE = {
    # resolve.Pass.record_events replays the (kind, …) tuples a pass collected.
    ("music_fetch.resolve", "record_events"): {"album_merged", "spotify_id_resolved", "conflict"},
}

ITEM_FIELDS = ("artist", "title", "album", "path")  # events.item_fields


class Unresolved(Exception):
    pass


@dataclass
class Module:
    name: str
    tree: ast.Module
    source: str
    constants: dict[str, str] = field(default_factory=dict)
    imported: dict[str, tuple[str, str]] = field(default_factory=dict)  # local name → (module, name)
    modules: dict[str, str] = field(default_factory=dict)  # local alias → module
    parents: dict[ast.AST, ast.AST] = field(default_factory=dict)


@dataclass(frozen=True)
class Site:
    module: str
    line: int
    function: str
    kind: str
    keys: frozenset[str]


def _package_dir(name: str) -> Path:
    spec = importlib.util.find_spec(name)
    assert spec and spec.submodule_search_locations, f"{name} is not importable"
    return Path(next(iter(spec.submodule_search_locations)))


def load(packages: dict[str, Path]) -> dict[str, Module]:
    """Every module of *packages* (package name → its directory), parsed."""
    modules = {}
    for package, root in packages.items():
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(root).with_suffix("")
            parts = [p for p in rel.parts if p != "__init__"]
            name = ".".join([package, *parts])
            modules[name] = _parse(name, path.read_text())
    return modules


def _parse(name: str, source: str) -> Module:
    mod = Module(name, ast.parse(source), source)
    for node in mod.tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            mod.constants[node.targets[0].id] = node.value.value
    for node in ast.walk(mod.tree):
        for child in ast.iter_child_nodes(node):
            mod.parents[child] = node
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                mod.imported[alias.asname or alias.name] = (node.module, alias.name)
                mod.modules[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                mod.modules[alias.asname or alias.name.split(".")[0]] = alias.name
    return mod


def _function(mod: Module, node: ast.AST) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    while node in mod.parents:
        node = mod.parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node
    return None


def _params(fn) -> list[str]:
    return [a.arg for a in (*fn.args.posonlyargs, *fn.args.args)]


def _calls_of(mod: Module, fn) -> list[ast.Call]:
    out = []
    for node in ast.walk(mod.tree):
        if isinstance(node, ast.Call):
            f = node.func
            if (isinstance(f, ast.Name) and f.id == fn.name) or (isinstance(f, ast.Attribute) and f.attr == fn.name):
                out.append(node)
    return out


def _argument(call: ast.Call, fn, param: str) -> ast.AST | None:
    for kw in call.keywords:
        if kw.arg == param:
            return kw.value
    params = _params(fn)
    index = params.index(param)
    if params and params[0] in ("self", "cls") and isinstance(call.func, ast.Attribute):
        index -= 1
    if index < len(call.args) and not any(isinstance(a, ast.Starred) for a in call.args[:index + 1]):
        return call.args[index]
    return None


def _constant(modules: dict[str, Module], module: str, name: str) -> str | None:
    mod = modules.get(module)
    if mod is None:
        return None
    if name in mod.constants:
        return mod.constants[name]
    if name in mod.imported:
        return _constant(modules, *mod.imported[name])
    return None


# A resolved value: each string it may be, with the extra data keys that path adds.
Values = list[tuple[str, frozenset[str]]]


def resolve(modules: dict[str, Module], mod: Module, expr: ast.AST, at: ast.AST, depth: int = 0) -> Values:
    """The strings *expr* (seen at node *at* of *mod*) may be."""
    if depth > 6:
        raise Unresolved("too deep")
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return [(expr.value, frozenset())]
    if isinstance(expr, ast.IfExp):
        return resolve(modules, mod, expr.body, at, depth) + resolve(modules, mod, expr.orelse, at, depth)
    if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name) and expr.value.id in mod.modules:
        value = _constant(modules, mod.modules[expr.value.id], expr.attr)
        if value is not None:
            return [(value, frozenset())]
    if isinstance(expr, ast.Name):
        fn = _function(mod, at)
        if fn is not None:
            found = _local(modules, mod, fn, expr.id, depth)
            if found is not None:
                return found
            if expr.id in _params(fn):
                return _through_wrapper(modules, mod, fn, expr.id, depth)
        value = _constant(modules, mod.name, expr.id)
        if value is not None:
            return [(value, frozenset())]
    raise Unresolved(ast.unparse(expr))


def _local(modules, mod, fn, name: str, depth: int) -> Values | None:
    found: Values = []
    seen = False
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            seen = True
            found += resolve(modules, mod, node.value, node, depth + 1)
        elif isinstance(node, ast.For) and isinstance(node.iter, (ast.Tuple, ast.List)):
            target = node.target
            if isinstance(target, ast.Name) and target.id == name:
                seen = True
                for elt in node.iter.elts:
                    found += resolve(modules, mod, elt, node, depth + 1)
            elif isinstance(target, ast.Tuple):
                names = [t.id if isinstance(t, ast.Name) else None for t in target.elts]
                if name in names:
                    seen = True
                    index = names.index(name)
                    for elt in node.iter.elts:
                        if not isinstance(elt, ast.Tuple):
                            raise Unresolved(ast.unparse(elt))
                        found += resolve(modules, mod, elt.elts[index], node, depth + 1)
    return found if seen else None


def _through_wrapper(modules, mod, fn, param: str, depth: int) -> Values:
    calls = _calls_of(mod, fn)
    if not calls:
        raise Unresolved(f"{fn.name}({param}) has no callers in {mod.name}")
    found: Values = []
    kwargs = fn.args.kwarg.arg if fn.args.kwarg else None
    for call in calls:
        arg = _argument(call, fn, param)
        if arg is None:
            raise Unresolved(f"{fn.name}() call at {mod.name}:{call.lineno}")
        extra = frozenset(_call_keys(call, fn)) if kwargs else frozenset()
        found += [(kind, keys | extra) for kind, keys in resolve(modules, mod, arg, call, depth + 1)]
    return found


def _call_keys(call: ast.Call, fn) -> set[str]:
    """The keys a wrapper call adds to the wrapper's ``**data``."""
    named = set(_params(fn)) | {a.arg for a in fn.args.kwonlyargs}
    keys = set()
    for kw in call.keywords:
        if kw.arg is None:
            keys |= _dict_keys(kw.value)
        elif kw.arg not in named:
            keys.add(kw.arg)
    return keys


def _dict_keys(expr: ast.AST) -> set[str]:
    """The keys a ``data`` expression spells out; what it doesn't is left out."""
    if isinstance(expr, ast.Dict):
        keys = set()
        for k, v in zip(expr.keys, expr.values):
            if k is None:
                keys |= _dict_keys(v)
            elif isinstance(k, ast.Constant) and isinstance(k.value, str):
                keys.add(k.value)
        return keys
    if isinstance(expr, ast.IfExp):
        return _dict_keys(expr.body) | _dict_keys(expr.orelse)
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute) and expr.func.attr == "item_fields":
        return set(ITEM_FIELDS)
    if isinstance(expr, ast.DictComp) and isinstance(expr.generators[0].iter, (ast.Tuple, ast.List)):
        return {e.value for e in expr.generators[0].iter.elts if isinstance(e, ast.Constant)}
    return set()


def _kind_arg(call: ast.Call) -> ast.AST | None:
    for kw in call.keywords:
        if kw.arg == "kind":
            return kw.value
    index = 1 if call.func.attr == "insert" else 0  # insert(conn, kind, …)
    return call.args[index] if len(call.args) > index else None


def _is_event_call(node: ast.AST) -> bool:
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("record", "insert")
            and isinstance(node.func.value, ast.Name) and node.func.value.id == "events")


def sites(modules: dict[str, Module]) -> tuple[list[Site], list[str]]:
    """Every (kind, data keys) the code records, and the call sites it couldn't read."""
    found, unread = [], []
    for mod in modules.values():
        for node in ast.walk(mod.tree):
            if not _is_event_call(node):
                continue
            fn = _function(mod, node)
            fname = fn.name if fn else "<module>"
            data = next((kw.value for kw in node.keywords if kw.arg == "data"), None)
            keys = frozenset(_dict_keys(data)) if data is not None else frozenset()
            arg = _kind_arg(node)
            try:
                if arg is None:
                    raise Unresolved("no kind argument")
                values = resolve(modules, mod, arg, node)
            except Unresolved as exc:
                if (mod.name, fname) in OPAQUE:
                    values = [(k, frozenset()) for k in OPAQUE[mod.name, fname]]
                    for kind in OPAQUE[mod.name, fname]:
                        if f'"{kind}"' not in mod.source:
                            unread.append(f"{mod.name}:{node.lineno}: OPAQUE lists {kind}, not in the module")
                else:
                    unread.append(f"{mod.name}:{node.lineno} ({fname}): {exc}")
                    continue
            found += [Site(mod.name, node.lineno, fname, kind, keys | extra) for kind, extra in values]
    return found, unread


@pytest.fixture(scope="module")
def recorded():
    return sites(load({name: _package_dir(name) for name in PACKAGES}))


def test_the_scan_reads_every_call_site(recorded):
    found, unread = recorded
    assert not unread, "event call sites whose kind the scan can't follow: " + "; ".join(unread)
    assert len({s.module for s in found}) > 20, "the scan found too few call sites: is it reading the packages?"


def test_every_recorded_kind_is_registered(recorded):
    found, _ = recorded
    unknown = sorted({f"{s.kind} ({s.module}:{s.line})" for s in found if s.kind not in event_kinds.KINDS})
    assert not unknown, f"kinds recorded but not in music_fetch.event_kinds.KINDS: {unknown}"


def test_every_recorded_data_key_is_registered(recorded):
    found, _ = recorded
    missing = defaultdict(set)
    for s in found:
        if s.kind in event_kinds.KINDS:
            for key in s.keys - set(event_kinds.KINDS[s.kind].data):
                missing[s.kind].add(f"{key} ({s.module}:{s.line})")
    assert not missing, f"data keys recorded but not registered: {dict(missing)}"


def test_every_registered_kind_is_recorded(recorded):
    """A kind nothing records any more stays registered (old rows still carry it)
    only when it says so."""
    found, _ = recorded
    recorded_kinds = {s.kind for s in found}
    idle = sorted(k for k, v in event_kinds.KINDS.items() if k not in recorded_kinds and not v.retired)
    assert not idle, f"registered kinds nothing records (mark them retired=True if that is deliberate): {idle}"


def test_event_kinds_is_the_registry():
    assert db.EVENT_KINDS == tuple(event_kinds.KINDS)
    for kind, spec in event_kinds.KINDS.items():
        assert spec.subject in event_kinds.SUBJECTS, kind
        assert spec.description, kind
        assert len(set(spec.data)) == len(spec.data), kind


def test_the_scan_follows_wrappers_loops_and_constants():
    source = '''
from music_fetch import events
KIND = "c"

def _event(kind, key, **data):
    events.record(kind, "m", source="s", data={"key": key, **data})

def one():
    _event("a", 1, why="x")
    _event("b" if 1 else KIND, 2, **{"n": 1})

def two(conn):
    for kind, sign in (("d", "+"), (KIND, "-")):
        events.insert(conn, kind, "m", source="s", data={"count": 1})
'''
    found, unread = sites({"pkg.mod": _parse("pkg.mod", source)})
    assert not unread
    assert {(s.kind, s.keys) for s in found} == {
        ("a", frozenset({"key", "why"})), ("b", frozenset({"key", "n"})), ("c", frozenset({"key", "n"})),
        ("d", frozenset({"count"})), ("c", frozenset({"count"}))}
