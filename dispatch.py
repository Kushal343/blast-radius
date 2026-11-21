"""Locate deferred dictionary callbacks and class configuration read sites."""

import ast
import dis
from functools import lru_cache
from types import FunctionType


def site(path, node):
    return f'{path}:{node.lineno}:{node.col_offset}'


def constant_value(node):
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        node = node.operand
    return isinstance(node, ast.Constant) and type(node.value) in (str, int, float, bool, type(None))


def declared_scalar(node):
    if not isinstance(node, (ast.AnnAssign, ast.Assign)) or not constant_value(node.value):
        return None
    targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
    if len(targets) == 1 and isinstance(targets[0], ast.Name) and targets[0].id.isupper():
        return targets[0].id
    return None


def scalar_inventory(root):
    candidates = {}
    blocked = set()
    for folder in ('sqlglot', 'tests'):
        for file in (root / folder).rglob('*.py'):
            path = file.relative_to(root).as_posix()
            tree = ast.parse(file.read_bytes())
            for node in ast.walk(tree):
                # A bare name or a string-based access can bypass an attribute
                # expression. Leave settings with such uses on line coverage.
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id.isupper():
                    blocked.add(node.id)
                if folder == 'tests' and isinstance(node, ast.Attribute):
                    blocked.add(node.attr)
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.isupper():
                    blocked.add(node.value)
            if folder == 'sqlglot':
                for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
                    for statement in cls.body:
                        if name := declared_scalar(statement):
                            candidates[path, cls.name, name] = site(path, statement)
    return {key: value for key, value in candidates.items() if key[2] not in blocked}


def positional_call(node):
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and not any(isinstance(n, ast.Starred) for n in node.args)
            and all(kw.arg is not None for kw in node.keywords))


class DispatchTransform(ast.NodeTransformer):
    def __init__(self, path):
        self.path = path

    # Restrict callbacks to dictionaries constructed outside function bodies.
    def visit_FunctionDef(self, node):
        return node

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_Lambda = visit_FunctionDef

    def visit_Dict(self, node):
        self.generic_visit(node)
        for i, value in enumerate(node.values):
            if isinstance(value, ast.Lambda):
                hook, args, kwargs = '_blast_bind_lambda', [value], []
            elif positional_call(value):
                hook, args, kwargs = '_blast_bind_factory', [value.func, *value.args], value.keywords
            else:
                continue
            node.values[i] = ast.copy_location(ast.Call(
                ast.Name(hook, ast.Load()), [ast.Constant(site(self.path, value)), *args], kwargs), value)
        return node


@lru_cache(maxsize=None)
def closure_factory(code):
    # Only binding arguments, creating a closure and returning it are allowed.
    # Defaults, conversions, conditional branches and global reads fall back.
    operations = {
        'RESUME', 'MAKE_CELL', 'COPY_FREE_VARS', 'LOAD_CLOSURE', 'LOAD_FAST',
        'LOAD_FAST_LOAD_FAST', 'LOAD_CONST', 'BUILD_TUPLE', 'MAKE_FUNCTION',
        'SET_FUNCTION_ATTRIBUTE', 'STORE_FAST', 'RETURN_VALUE',
    }
    return all(i.opname in operations for i in dis.get_instructions(code))


class DispatchProfile:
    def __init__(self, tracer, inventory):
        self.tracer = tracer
        self.scalar_sites = inventory
        self.words = {name for _, _, name in inventory}
        self.owners = {}
        self.calls = {}
        self.hits = {value: set() for value in inventory.values()}

    def bind_lambda(self, key, function):
        # Different factory invocations can share the same inner code object.
        # Give each dictionary entry an identity while preserving its bytecode.
        code = function.__code__.replace()
        function.__code__ = code
        self.calls[id(code)] = (code, key)
        self.hits.setdefault(key, set())
        return function

    def bind_factory(self, key, factory, /, *args, **kwargs):
        value = factory(*args, **kwargs)
        if (type(factory) is FunctionType and type(value) is FunctionType
                and closure_factory(factory.__code__)
                and any(item is value.__code__ for item in factory.__code__.co_consts)):
            return self.bind_lambda(key, value)
        return value

    def read(self, receiver, word):
        kind = receiver if isinstance(receiver, type) else type(receiver)
        cache = self.owners
        token = (id(kind), word)
        if token not in cache:
            key = None
            for base in kind.__mro__:
                if word in vars(base):
                    path = base.__module__.replace('.', '/') + '.py'
                    key = self.scalar_sites.get((path, base.__qualname__, word))
                    break
            cache[token] = (kind, key)
        key = cache[token][1]
        if key is not None:
            self.hits[key].add('' if self.tracer.import_depth else self.tracer.context)
        return receiver


def changed_sites(path, before, after, table):
    """Only prune when every structural difference belongs to a measured site."""
    sites = set()
    pairs = [(before, after)]
    while pairs:
        a, b = pairs.pop()
        if type(a) is not type(b):
            return None
        if isinstance(a, ast.AST):
            fields = a._fields
            if hasattr(a, 'lineno') and (key := site(path, a)) in table:
                if isinstance(a, ast.Lambda):
                    if ast.dump(a.body) != ast.dump(b.body):
                        sites.add(key)
                    fields = ('args',)
                elif declared_scalar(a) and declared_scalar(b) == declared_scalar(a):
                    if ast.dump(a.value) != ast.dump(b.value):
                        sites.add(key)
                    fields = tuple(field for field in fields if field != 'value')
                elif positional_call(a) and positional_call(b):
                    # A changed literal has no side effects while constructing
                    # the closure. Require the same callable and signature.
                    if (len(a.args) != len(b.args)
                            or [kw.arg for kw in a.keywords] != [kw.arg for kw in b.keywords]):
                        return None
                    pairs.append((a.func, b.func))
                    for x, y in zip([*a.args, *(k.value for k in a.keywords)],
                                    [*b.args, *(k.value for k in b.keywords)]):
                        if constant_value(x) and constant_value(y):
                            if ast.dump(x) != ast.dump(y):
                                sites.add(key)
                        else:
                            pairs.append((x, y))
                    continue
            pairs.extend((getattr(a, f), getattr(b, f)) for f in fields)
        elif isinstance(a, list):
            if len(a) != len(b):
                return None
            pairs.extend(zip(a, b))
        elif a != b:
            return None
    return sites
