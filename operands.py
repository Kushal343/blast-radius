"""Baseline operands for comparing simple predicates on previously exercised inputs."""

import ast
import math
import operator


OPERATORS = {
    ast.Eq: operator.eq, ast.NotEq: operator.ne,
    ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
    ast.In: lambda value, collection: value in collection,
    ast.NotIn: lambda value, collection: value not in collection,
}
ATOMS = (bool, int, float, str, type(None))
NO_VALUE = object()


def pack(value):
    kind = type(value)
    if kind in ATOMS:
        if (kind is str and len(value) > 80 or kind is int and value.bit_length() > 256
                or kind is float and not math.isfinite(value)):
            return NO_VALUE
        return (kind.__name__, value)
    if kind is tuple and len(value) <= 12:
        items = tuple(pack(item) for item in value)
        if all(item is not NO_VALUE for item in items):
            return ('tuple', items)
    return NO_VALUE


def unpack(item):
    kind, value = item
    return tuple(map(unpack, value)) if kind == 'tuple' else value


def constant(node):
    if isinstance(node, ast.Constant):
        return node.value if pack(node.value) is not NO_VALUE else NO_VALUE
    if isinstance(node, (ast.Tuple, ast.List, ast.Set, ast.UnaryOp)):
        try:
            value = ast.literal_eval(node)
        except (SyntaxError, TypeError, ValueError):
            return NO_VALUE
        if type(value) in (list, set):
            return value if all(pack(item) is not NO_VALUE for item in value) else NO_VALUE
        if pack(value) is not NO_VALUE:
            return value
    return NO_VALUE


def describe(node):
    if not isinstance(node, ast.Compare) or len(node.ops) != 1 or type(node.ops[0]) not in OPERATORS:
        return None
    left, right = node.left, node.comparators[0]
    # Observe one runtime operand. The other side is evaluated by Python as usual.
    for position, dynamic, fixed in ((0, left, right), (1, right, left)):
        if not isinstance(dynamic, (ast.Attribute, ast.Call, ast.Name, ast.Subscript)):
            continue
        value = constant(fixed)
        if value is NO_VALUE:
            continue
        if type(value) not in (list, set) or position == 0 and isinstance(node.ops[0], (ast.In, ast.NotIn)):
            return position, dynamic, type(node.ops[0]), value
    return None


def edited_predicates(path, before, after, site_for):
    """Return edited predicates only when the rest of the module is unchanged."""
    stack = [(before, after)]
    edits = {}
    while stack:
        old, new = stack.pop()
        if type(old) is not type(new):
            return None
        if isinstance(old, ast.Compare):
            old_desc, new_desc = describe(old), describe(new)
            if (old_desc and new_desc and old_desc[0] == new_desc[0]
                    and ast.dump(old_desc[1]) == ast.dump(new_desc[1])):
                if ast.dump(old) != ast.dump(new):
                    edits[site_for(path, old)] = (old_desc, new_desc)
                continue
        if isinstance(old, ast.AST):
            stack.extend((getattr(old, field), getattr(new, field)) for field in old._fields)
        elif isinstance(old, list):
            if len(old) != len(new):
                return None
            stack.extend(zip(old, new))
        elif old != new:
            return None
    return edits


def same_result(item, before, after):
    value = unpack(item)

    def evaluate(description):
        position, _, kind, fixed = description
        operation = OPERATORS[kind]
        return operation(value, fixed) if position == 0 else operation(fixed, value)

    try:
        return evaluate(before) is evaluate(after)
    except (TypeError, ValueError, OverflowError):
        return False
