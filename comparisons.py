"""Identify simple identity tests that can be checked against baseline values."""

import ast


def eligible(node):
    return (isinstance(node, ast.Compare) and len(node.ops) == 1
            and isinstance(node.ops[0], (ast.Is, ast.IsNot))
            and isinstance(node.left, ast.Attribute)
            and isinstance(node.left.value, ast.Name) and node.left.value.id == 'self'
            and isinstance(node.comparators[0], (ast.Constant, ast.Attribute)))


def site_for(path, node):
    return f'{path}:{node.lineno}:{node.col_offset}'


def equality_edits(path, before, after):
    """Recognize ASTs whose only edits swap identity for matching equality operators."""
    pending = [(before, after)]
    sites = set()
    while pending:
        old, new = pending.pop()
        if type(old) is not type(new):
            return None
        if isinstance(old, ast.AST):
            if eligible(old) and isinstance(new.ops[0],
                    ast.Eq if isinstance(old.ops[0], ast.Is) else ast.NotEq):
                if len(new.ops) != 1:
                    return None
                sites.add(site_for(path, old))
                pending.extend(((old.left, new.left), (old.comparators, new.comparators)))
            else:
                pending.extend((getattr(old, field), getattr(new, field)) for field in old._fields)
        elif isinstance(old, list):
            if len(old) != len(new):
                return None
            pending.extend(zip(old, new))
        elif old != new:
            return None
    return sites
