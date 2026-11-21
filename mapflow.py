"""Locate value reads of literal dictionaries with auditable name flow."""

import ast
from collections import defaultdict


def spelling(node):
    if isinstance(node, ast.Attribute):
        return node.attr.lower()
    if isinstance(node, ast.Name):
        return node.id.lower()
    return ''


def literal_dict(node):
    if not isinstance(node, ast.Dict):
        return False
    try:
        return type(ast.literal_eval(node)) is dict
    except (ValueError, TypeError, SyntaxError):
        return False


def dict_value_edits(before, after):
    """Accept only literal value replacements with the same ordered keys."""
    todo = [(before, after)]
    names = defaultdict(set)
    while todo:
        old, new = todo.pop()
        if type(old) is not type(new):
            return set()
        if (isinstance(old, (ast.Assign, ast.AnnAssign)) and literal_dict(old.value)
                and literal_dict(new.value) and ast.dump(old.value) != ast.dump(new.value)):
            old_keys = [ast.dump(key) for key in old.value.keys]
            new_keys = [ast.dump(key) for key in new.value.keys]
            targets = old.targets if isinstance(old, ast.Assign) else [old.target]
            if old_keys != new_keys or len(targets) != 1 or not isinstance(targets[0], ast.Name):
                return {}
            for key, old_value, new_value in zip(old.value.keys, old.value.values, new.value.values):
                if ast.dump(old_value) != ast.dump(new_value):
                    names[spelling(targets[0])].add(ast.literal_eval(key))
            todo.extend((getattr(old, field), getattr(new, field)) for field in old._fields if field != 'value')
        elif isinstance(old, ast.AST):
            todo.extend((getattr(old, field), getattr(new, field)) for field in old._fields)
        elif isinstance(old, list):
            if len(old) != len(new):
                return {}
            todo.extend(zip(old, new))
        elif old != new:
            return {}
    return dict(names)


def truthiness_only(node, parents):
    parent = parents.get(node)
    while isinstance(parent, ast.BoolOp):
        node, parent = parent, parents.get(parent)
    if isinstance(parent, (ast.If, ast.While, ast.IfExp, ast.Assert)) and node is parent.test:
        return True
    if isinstance(parent, ast.UnaryOp) and isinstance(parent.op, ast.Not):
        return True
    return (isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name)
            and parent.func.id in ('bool', 'len') and parent.args == [node] and not parent.keywords)


def find_flows(root):
    files = [*sorted((root / 'sqlglot').rglob('*.py')), *sorted((root / 'tests').rglob('*.py'))]
    trees = {file.relative_to(root).as_posix(): ast.parse(file.read_bytes()) for file in files}
    names = set()
    for path, tree in trees.items():
        if not path.startswith('sqlglot/'):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and literal_dict(node.value):
                names.update(target.id.lower() for target in node.targets
                             if isinstance(target, ast.Name) and target.id.isupper())

    unknown = set()
    value_reads = defaultdict(lambda: defaultdict(set))
    for path, tree in trees.items():
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node, parent in parents.items():
            if isinstance(node, ast.alias) and node.asname and node.name.lower() != node.asname.lower():
                unknown.add(node.name.lower())
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name)
                    and parent.func.id in ('getattr', 'setattr', 'hasattr', 'delattr')):
                unknown.add(node.value.lower())
            word = spelling(node)
            if word not in names or word in unknown or not isinstance(node.ctx, ast.Load):
                continue
            if isinstance(parent, ast.Attribute) and parent.value is node and parent.attr == 'get':
                call = parents.get(parent)
                if isinstance(call, ast.Call) and call.func is parent and call.args:
                    value_reads[word][path].update(range(call.lineno, call.end_lineno + 1))
                    continue
            if isinstance(parent, ast.Subscript) and parent.value is node and isinstance(parent.ctx, ast.Load):
                value_reads[word][path].update(range(parent.lineno, parent.end_lineno + 1))
                continue
            if (isinstance(parent, ast.Compare) and len(parent.ops) == 1
                    and isinstance(parent.ops[0], (ast.In, ast.NotIn)) and parent.comparators[0] is node):
                continue
            if ((isinstance(parent, ast.Assign) and parent.value is node
                 and all(spelling(target) == word for target in parent.targets))
                    or isinstance(parent, ast.keyword) and parent.arg and parent.arg.lower() == word
                    or truthiness_only(node, parents)):
                continue
            unknown.add(word)
    return {symbol: {path: sorted(lines) for path, lines in sorted(uses.items())}
            for symbol, uses in sorted(value_reads.items()) if symbol not in unknown}
