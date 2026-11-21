"""Measure expression reachability for short-circuit and conditional operands."""

import ast

from dispatch import site


class BranchTransform(ast.NodeTransformer):
    def __init__(self, path, tracer):
        self.path = path
        self.tracer = tracer

    def mark(self, value):
        # Give the guard a unique location. sys.monitoring disables that line
        # event after its first hit in each test. Evaluation precedes the
        # operand, so even an exception raised by the operand counts as a hit.
        key = site(self.path, value)
        line = self.tracer.virtual_line(self.path, key)
        flag = ast.Name('_blast_branch_enabled', ast.Load(), lineno=line,
                        end_lineno=line, col_offset=0, end_col_offset=1)
        return ast.copy_location(ast.IfExp(flag, value, ast.Constant(None)), value)

    def visit_BoolOp(self, node):
        self.generic_visit(node)
        node.values[1:] = [self.mark(value) for value in node.values[1:]]
        return node

    def visit_IfExp(self, node):
        self.generic_visit(node)
        node.body = self.mark(node.body)
        node.orelse = self.mark(node.orelse)
        return node


def edited_branches(path, before, after, reach):
    """Return readers' guard sites only if they enclose every difference."""
    changed = set()
    pending = [(before, after, None)]
    while pending:
        old, new, guard = pending.pop()
        if type(old) is type(new):
            if isinstance(old, ast.AST):
                if isinstance(old, ast.BoolOp) and len(old.values) == len(new.values):
                    pending.append((old.op, new.op, guard))
                    for i, (a, b) in enumerate(zip(old.values, new.values)):
                        key = site(path, a) if i else None
                        pending.append((a, b, key if key in reach else guard))
                elif isinstance(old, ast.IfExp):
                    pending.append((old.test, new.test, guard))
                    for a, b in ((old.body, new.body), (old.orelse, new.orelse)):
                        key = site(path, a)
                        pending.append((a, b, key if key in reach else guard))
                else:
                    pending.extend((getattr(old, f), getattr(new, f), guard) for f in old._fields)
                continue
            if isinstance(old, list):
                if len(old) == len(new):
                    pending.extend((a, b, guard) for a, b in zip(old, new))
                    continue
            elif old == new and (not isinstance(old, (float, complex)) or repr(old) == repr(new)):
                continue
        if guard is None:
            return None
        changed.add(guard)
    return changed
