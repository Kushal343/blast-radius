"""Select regression tests using dependencies observed on the pinned revision."""

import argparse
import ast
import gzip
import json
import re
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from comparisons import equality_edits
from patches import read_patch


FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
DIALECT_FAMILIES = {'dialects', 'generators', 'parsers', 'typing', 'tokens'}
CONFIG = {'pytest.ini', 'tox.ini', 'setup.cfg', 'pyproject.toml'}
BRANCHES = (ast.If, ast.While, ast.For, ast.AsyncFor, ast.Match, ast.With, ast.AsyncWith)


@dataclass(frozen=True)
class Statement:
    node: ast.AST
    runtime: bool
    classes: tuple[str, ...]
    functions: tuple[str, ...]
    first: int
    last: int


def statements_by_line(tree):
    index = {}

    def walk(node, classes=(), functions=()):
        if isinstance(node, ast.Lambda):
            body = node.body
            first = max(node.lineno + 1, body.lineno)
            statement = Statement(body, True, classes, functions, body.lineno, body.end_lineno)
            for line in range(first, body.end_lineno + 1):
                index[line] = statement
            return
        if isinstance(node, ast.stmt):
            first = min([node.lineno, *(n.lineno for n in getattr(node, 'decorator_list', ()))])
            children = [c.lineno for c in ast.iter_child_nodes(node) if isinstance(c, ast.stmt)]
            last = min(children) if children else node.end_lineno
            statement = Statement(node, bool(functions), classes, functions, first, last)
            for line in range(first, node.end_lineno + 1):
                index[line] = statement
        nested_classes = (*classes, node.name) if isinstance(node, ast.ClassDef) else classes
        nested_functions = (*functions, node.name) if isinstance(node, FUNCTIONS) else functions
        for child in ast.iter_child_nodes(node):
            walk(child, nested_classes, nested_functions)

    walk(tree)
    return index


def bindings(node):
    if isinstance(node, (*FUNCTIONS, ast.ClassDef)):
        return {node.name}
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        targets = [node.target]
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        return {n.asname or n.name.split('.')[0] for n in node.names}
    else:
        return set()
    return {n.id for target in targets for n in ast.walk(target) if isinstance(n, ast.Name)}


def scalar_assignment(node):
    if not isinstance(node, (ast.Assign, ast.AnnAssign)):
        return False
    value = node.value
    if isinstance(value, ast.UnaryOp) and isinstance(value.op, (ast.USub, ast.UAdd)):
        value = value.operand
    return isinstance(value, ast.Constant)


class Selector:
    def __init__(self, catalog, artifact, root):
        self.root = root
        self.catalog = {record['number']: record for record in catalog}
        self.all = set(self.catalog)
        self.by_file = defaultdict(set)
        self.by_name = {record['id']: number for number, record in self.catalog.items()}
        for number, record in self.catalog.items():
            self.by_file[record['file']].add(number)
        measured = artifact['tests']
        self.groups = [frozenset(self.by_name[measured[i]] for i in group if measured[i] in self.by_name)
                       for group in artifact['groups']]
        self.initialization = {i for i, group in enumerate(artifact['groups'])
                               if any(not measured[member] for member in group)}
        self.lines = {name: {int(line): group for line, group in lines.items()}
                      for name, lines in artifact['lines'].items()}
        self.readers = artifact['files']
        self.dialects = artifact['dialects']
        self.symbol_groups = artifact.get('symbols')
        self.comparisons = artifact.get('comparisons', {})
        self.external = {self.by_name[name] for name in artifact['external'] if name in self.by_name}
        self.unmeasured = {number for name, number in self.by_name.items()
                           if artifact['status'].get(name) != 'passed'}
        self.ref_cache = {}
        self.sources = {}
        self.base_trees = {}
        self.probe_deadline = time.monotonic() + 45
        self.probe_cpu = 0.0

    def file_tests(self, path):
        return self.groups[self.readers[path]] if path in self.readers else frozenset()

    def source(self, path):
        if path not in self.sources:
            self.sources[path] = (self.root / path).read_text(encoding='utf-8')
        return self.sources[path]

    def line_tests(self, path, lines):
        coverage = self.lines.get(path, {})
        groups = {coverage[line] for line in lines if line in coverage}
        return set().union(*(self.groups[group] for group in groups)), bool(groups & self.initialization)

    def symbol_tests(self, name):
        if name not in self.ref_cache:
            if self.symbol_groups is not None:
                self.ref_cache[name] = set().union(*(self.groups[group] for group in self.symbol_groups.get(name, ())))
                return self.ref_cache[name]
            token = re.compile(r'\b' + re.escape(name) + r'\b')
            tests = set()
            for path in self.lines:
                if not path.endswith('.py'):
                    continue
                source = self.source(path)
                if name not in source:
                    continue
                lines = (number for number, line in enumerate(source.split('\n'), 1)
                         if token.search(line) and not line.lstrip().startswith('#'))
                readers, _ = self.line_tests(path, lines)
                tests.update(readers)
            self.ref_cache[name] = tests
        return self.ref_cache[name]

    def module_tests(self, path):
        seen = self.lines.get(path, {}).values()
        result = set().union(*(self.groups[group] for group in set(seen)))
        result.update(self.groups[self.readers[path]] if path in self.readers else ())
        result.update(self.by_file[path])
        if path in self.dialects:
            result.update(self.groups[self.dialects[path]])
        return result

    def initial_tests(self, path, old, new):
        if path in self.by_file:
            return self.by_file[path]
        statements = [statement for statement in (old, new) if statement is not None]
        names = set().union(*(bindings(statement.node) for statement in statements))
        observed = set().union(*(self.symbol_tests(name) for name in names))
        if path in self.dialects and self.groups[self.dialects[path]] != self.all:
            affinity = self.module_tests(path)
            owners = set().union(*(set(statement.classes) for statement in statements))
            affinity.update(*(self.symbol_tests(name) for name in owners))
            if path.split('/')[1] in DIALECT_FAMILIES and path.count('/') == 2:
                if observed and all(s.classes and scalar_assignment(s.node) for s in statements):
                    observed &= affinity
                    return observed | self.unmeasured
                return affinity | self.unmeasured
        if path.startswith('sqlglot/expressions/') and statements and statements[0].classes:
            owner = statements[0].classes[-1]
            return self.symbol_tests(owner) | self.unmeasured
        if observed and all(isinstance(s.node, (ast.Assign, ast.AnnAssign)) for s in statements):
            if all(all(isinstance(child, (ast.Constant, ast.Name, ast.Load, ast.Store, ast.Dict,
                                         ast.Tuple, ast.Set, ast.List, ast.UnaryOp, ast.USub, ast.UAdd))
                       for child in ast.walk(s.node.value)) for s in statements if s.node.value is not None):
                return observed | self.module_tests(path) | self.unmeasured
        return self.all

    def test_method(self, path, statement):
        names = (*statement.classes, *statement.functions)
        if isinstance(statement.node, FUNCTIONS):
            names = (*names, statement.node.name)
        prefix = '::'.join((path, *names))
        return {number for number in self.by_file[path]
                if (test_id := self.catalog[number]['id']) == prefix or test_id.startswith(prefix + '[')}

    def python_tests(self, change, replacements, equality_sites):
        path = change.path
        if change.before != change.after or not change.hunks:
            return self.by_file[path] if path in self.by_file else self.all

        before = self.source(path)
        after, removed, added, inserts = change.apply(before)
        direct_readers = self.file_tests(path)
        if path not in self.base_trees:
            tree = ast.parse(before, filename=path)
            self.base_trees[path] = (tree, statements_by_line(tree))
        old_tree, old_index = self.base_trees[path]
        try:
            new_tree = ast.parse(after, filename=path)
            compile(new_tree, path, 'exec')
        except (SyntaxError, ValueError):
            return self.by_file[path] if path in self.by_file else self.all
        if ast.dump(old_tree) == ast.dump(new_tree):
            return direct_readers

        replacements[path] = after
        if self.comparisons and path.startswith('sqlglot/'):
            equality_sites[path] = equality_edits(path, old_tree, new_tree)

        new_index = statements_by_line(new_tree)
        tests = set(direct_readers)
        initialization = set()
        for line in removed:
            stmt = old_index.get(line)
            if stmt is None:
                continue
            if not stmt.runtime:
                initialization.add((stmt, None))
                continue
            tests.update(self.test_method(path, stmt) if path in self.by_file else ())
            lines = self.lines.get(path, {})
            run_lines = {line} if line in lines else set(range(stmt.first, stmt.last + 1))
            if isinstance(stmt.node, BRANCHES):
                run_lines.add(stmt.first)
                condition = getattr(stmt.node, 'test', None)
                if condition is not None:
                    run_lines.add(condition.lineno)
            elif isinstance(stmt.node, ast.Return) and isinstance(stmt.node.value, ast.BoolOp):
                run_lines.add(stmt.node.value.lineno)
            covered, during_import = self.line_tests(path, run_lines)
            tests.update(covered)
            if during_import:
                initialization.add((stmt, None))

        for line in added:
            stmt = new_index.get(line)
            if stmt is None:
                continue
            if not stmt.runtime:
                initialization.add((None, stmt))
            elif path in self.by_file:
                tests.update(self.test_method(path, stmt))

        if inserts:
            neighbors = {line for position in inserts.values() for line in (position, position + 1)}
            covered, during_import = self.line_tests(path, neighbors)
            tests.update(covered)
            if during_import and not initialization:
                tests.update(self.module_tests(path))

        for old, new in initialization:
            statement = old or new
            if path in self.by_file:
                method = self.test_method(path, statement)
                if method:
                    tests.update(method)
                    continue
            tests.update(self.initial_tests(path, old, new))
            if tests == self.all:
                break
        return tests | self.unmeasured

    def select(self, patch):
        tests = set()
        replacements = {}
        equality_sites = {}
        can_probe = True
        try:
            for change in read_patch(patch.read_text(encoding='utf-8')):
                can_probe &= bool(change.before == change.after and change.path
                                  and change.path.startswith('sqlglot/') and change.path.endswith('.py')
                                  and change.hunks)
                for path in {change.before, change.after} - {None}:
                    if path in self.readers:
                        tests.update(self.file_tests(path))
                        if self.readers[path] in self.initialization:
                            tests.update(self.all)
                    if path == 'conftest.py' or path in CONFIG:
                        tests.update(self.all)
                    elif path.endswith('/conftest.py'):
                        prefix = path.removesuffix('conftest.py')
                        tests.update(number for number, t in self.catalog.items() if t['file'].startswith(prefix))
                if change.path and change.path.endswith('.py') and change.path.startswith(('sqlglot/', 'tests/')):
                    tests.update(self.python_tests(change, replacements, equality_sites))
        except (OSError, UnicodeError, SyntaxError, ValueError) as error:
            print(f'{patch.name}: {error}', file=sys.stderr)
            return self.all, {}
        if can_probe and len(replacements) == 1 and equality_sites:
            path, sites = next(iter(equality_sites.items()))
            if sites and sites <= self.comparisons.keys():
                safe = tests.copy()
                for site in sites:
                    kept, uncertain = self.comparisons[site]
                    if kept in self.initialization or uncertain in self.initialization:
                        break
                    safe.intersection_update(self.groups[kept] - self.groups[uncertain])
                else:
                    protected = self.unmeasured | self.external | self.file_tests(path)
                    tests.difference_update(safe - protected)
        return tests, replacements if can_probe else {}

    def refine(self, tests, replacements, durations):
        remaining = self.probe_deadline - time.monotonic()
        if not tests or not replacements or remaining < 1:
            return tests
        protected = self.external | self.unmeasured | self.symbol_tests('ProcessPoolExecutor')
        protected.update(*(self.file_tests(path) for path in replacements))
        candidates = tests - protected
        expected = sum(durations[number] for number in candidates)
        if len(candidates) < 2 or expected > 1300:
            return tests
        retained = tests & protected
        if retained:
            savings = scheduled_cost(tests, durations) - scheduled_cost(retained, durations)
            if savings <= expected + 250:
                return tests
        names = [self.catalog[number]['id'] for number in sorted(candidates)]
        request = json.dumps({'files': replacements, 'tests': names})
        try:
            process = subprocess.run(
                [sys.executable, str(Path(__file__).with_name('probe.py')), str(self.root)],
                input=request, encoding='utf-8', stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, timeout=min(4, remaining), check=True,
            )
            result = json.loads(process.stdout)
            self.probe_cpu += result['cpu_seconds']
            passed = result['passed']
        except (OSError, ValueError, subprocess.SubprocessError):
            return tests
        return tests - (candidates & {self.by_name[name] for name in passed if name in self.by_name})


def schedule(numbers, durations):
    if not numbers:
        return []
    ordered = sorted(numbers, key=lambda number: (-durations[number], number))
    total = sum(durations[number] for number in ordered)
    best_cost = total + 2000
    best = [ordered]
    for size in range(2, min(8, len(ordered)) + 1):
        floor = max(durations[ordered[0]], (total + size - 1) // size)
        if floor + 2000 * size >= best_cost:
            continue
        workers = [[] for _ in range(size)]
        loads = [0] * size
        for index, number in enumerate(ordered):
            worker = index if index < size else loads.index(min(loads))
            workers[worker].append(number)
            loads[worker] += durations[number]
        cost = 2000 * size + max(loads)
        if cost < best_cost:
            best, best_cost = workers, cost
    return best


def scheduled_cost(numbers, durations):
    workers = schedule(numbers, durations)
    return 2000 * len(workers) + max((sum(durations[num] for num in worker) for worker in workers), default=0)


def main():
    started = time.process_time()
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', type=Path, default=Path('/data'))
    parser.add_argument('--source', type=Path, default=here / 'source')
    parser.add_argument('--map', type=Path, default=here / 'coverage.json.gz')
    parser.add_argument('--coverage-only', action='store_true')
    parser.add_argument('--report-cpu', action='store_true', help='emit local CPU seconds to stderr')
    args = parser.parse_args()
    catalog = json.loads((args.data / 'tests.json').read_bytes())['tests']
    with gzip.open(args.map, 'rb') as file:
        artifact = json.load(file)
    selector = Selector(catalog, artifact, args.source.resolve())
    durations = {test['number']: round(test['seconds'] * 1000) for test in catalog}
    for patch in sorted((args.data / 'commits').glob('*.patch')):
        numbers, replacements = selector.select(patch)
        if not args.coverage_only:
            numbers = selector.refine(numbers, replacements, durations)
        workers = schedule(numbers, durations)
        print(json.dumps({'commit': patch.stem, 'workers': workers}, separators=(',', ':')))
    if args.report_cpu:
        try:
            import resource
        except ImportError:
            child = selector.probe_cpu
        else:
            usage = resource.getrusage(resource.RUSAGE_CHILDREN)
            child = usage.ru_utime + usage.ru_stime
        print(f'CPU_SECONDS={time.process_time() - started + child:.6f}', file=sys.stderr)


if __name__ == '__main__':
    main()
