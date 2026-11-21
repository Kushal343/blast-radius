"""Recognize function definitions whose only edits change literal defaults."""

import ast

from operands import NO_VALUE, constant, pack


FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


def default_edits(before, after):
    pending = [(before, after)]
    changed = []
    while pending:
        old, new = pending.pop()
        if type(old) is not type(new):
            return None
        if isinstance(old, FUNCTIONS):
            old_args, new_args = old.args, new.args
            old_defaults = (*old_args.defaults, *old_args.kw_defaults)
            new_defaults = (*new_args.defaults, *new_args.kw_defaults)
            fields = ('defaults', 'kw_defaults')
            if len(old_args.defaults) != len(new_args.defaults) or len(old_args.kw_defaults) != len(new_args.kw_defaults):
                return None
            if any(changed_default(a, b) for a, b in zip(old_defaults, new_defaults)):
                if old.decorator_list or not all(compatible_default(a, b) for a, b in zip(old_defaults, new_defaults)):
                    return None
                changed.append(old)
                pending.extend((getattr(old, field), getattr(new, field)) for field in old._fields if field != 'args')
                pending.extend((getattr(old_args, field), getattr(new_args, field))
                               for field in old_args._fields if field not in fields)
                continue
        if isinstance(old, ast.AST):
            pending.extend((getattr(old, field), getattr(new, field)) for field in old._fields)
        elif isinstance(old, list):
            if len(old) != len(new):
                return None
            pending.extend(zip(old, new))
        elif old != new:
            return None
    return changed


def changed_default(before, after):
    if before is None or after is None:
        return before is not after
    return ast.dump(before) != ast.dump(after)


def compatible_default(before, after):
    if not changed_default(before, after):
        return True
    if before is None or after is None:
        return False
    old, new = constant(before), constant(after)
    return type(old) is type(new) and pack(old) is not NO_VALUE and pack(new) is not NO_VALUE
