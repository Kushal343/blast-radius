"""Select regression tests using dependencies observed on the pinned revision."""

import argparse
import ast
import bisect
import gzip
import json
import multiprocessing
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from branches import edited_branches
from comparisons import equality_edits, site_for
from declarations import default_edits
from dispatch import changed_sites
from mapflow import dict_value_edits
from operands import edited_predicates, same_result, unpack
from patches import read_patch


FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
DIALECT_FAMILIES = {'dialects', 'generators', 'parsers', 'typing', 'tokens'}
CONFIG = {'pytest.ini', 'tox.ini', 'setup.cfg', 'pyproject.toml'}
BRANCHES = (ast.If, ast.While, ast.For, ast.AsyncFor, ast.Match, ast.With, ast.AsyncWith)
FRONTEND_MODULES = {'sqlglot/parser.py', 'sqlglot/generator.py', 'sqlglot/tokens.py',
                    'sqlglot/tokenizer_core.py'}


@dataclass(frozen=True)
class Statement:
    node: ast.AST
    runtime: bool
    classes: tuple[str, ...]
    functions: tuple[str, ...]
    first: int
    last: int


def statements_by_line(tree, wanted=None):
    index = {}
    wanted = sorted(wanted) if wanted is not None else None

    def relevant(start, stop):
        if wanted is None:
            return range(start, stop + 1)
        return wanted[bisect.bisect_left(wanted, start):bisect.bisect_right(wanted, stop)]

    def walk(node, classes=(), functions=()):
        if isinstance(node, ast.Lambda):
            body = node.body
            first = max(node.lineno + 1, body.lineno)
            statement = Statement(body, True, classes, functions, body.lineno, body.end_lineno)
            for line in relevant(first, body.end_lineno):
                index[line] = statement
            return
        if isinstance(node, ast.stmt):
            first = min([node.lineno, *(n.lineno for n in getattr(node, 'decorator_list', ()))])
            selected = relevant(first, node.end_lineno)
            if not selected:
                return
            children = [c.lineno for c in ast.iter_child_nodes(node) if isinstance(c, ast.stmt)]
            last = min(children) if children else node.end_lineno
            statement = Statement(node, bool(functions), classes, functions, first, last)
            for line in selected:
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
        numbers = [self.by_name.get(name) for name in measured]
        self.groups = CatalogGroups(artifact['groups'], numbers)
        initial = measured.index('') if '' in measured else -1
        self.initialization = {i for i, group in enumerate(artifact['groups']) if initial in group}
        self.lines = {name: {int(line): group for line, group in lines.items()}
                      for name, lines in artifact['lines'].items()}
        self.readers = artifact['files']
        self.dialects = artifact['dialects']
        self.symbol_groups = artifact.get('symbols')
        self.comparisons = artifact.get('comparisons', {})
        self.relations = artifact.get('relations', {})
        self.operands = artifact.get('operands', {})
        self.dictionary_values = artifact.get('dictionary_values', {})
        self.dispatch = artifact.get('dispatch', {})
        self.branches = artifact.get('branches', {})
        self.dispatch_lines = defaultdict(set)
        for location in self.dispatch:
            path, line, _ = location.rsplit(':', 2)
            self.dispatch_lines[path].add(int(line))
        self.checkpoints = artifact.get('executor_checkpoints', {})
        self.executor_lines = {name: {path: set(lines) for path, lines in files.items()}
                               for name, files in artifact.get('executor_lines', {}).items()}
        self.executor_cases = artifact.get('executor_cases', {})
        self.executor_shared = {name: {path: set(lines) for path, lines in files.items()}
                                for name, files in artifact.get('executor_shared', {}).items()}
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

    def python_tests(self, change, replacements, equality_sites, predicates, body_edits, dictionaries, bounds):
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
        focus = changed_statement(old_tree, new_tree)
        if focus is None:
            return direct_readers

        replacements[path] = after
        if self.branches:
            guards = edited_branches(path, *focus, self.branches)
            if guards and all(self.branches[key] not in self.initialization for key in guards):
                bounds.append(set(direct_readers).union(self.external, self.unmeasured,
                    *(self.groups[self.branches[key]] for key in guards)))
        if self.dispatch_lines[path] and any(
                stmt and any(stmt.first <= line <= stmt.node.end_lineno for line in self.dispatch_lines[path])
                for stmt in (old_index.get(n) for n in removed)):
            sites = changed_sites(path, *focus, self.dispatch)
            if sites and all(self.dispatch[key] not in self.initialization for key in sites):
                bounds.append(set(direct_readers).union(self.external, self.unmeasured,
                    *(self.groups[self.dispatch[key]] for key in sites)))
        if (self.comparisons or self.relations) and path.startswith('sqlglot/'):
            equality_sites[path] = equality_edits(path, *focus)
        if self.operands and path.startswith('sqlglot/'):
            predicates[path] = edited_predicates(path, *focus, site_for)

        new_index = statements_by_line(new_tree, added)
        if path.startswith('sqlglot/') and any(isinstance(old_index[line].node, FUNCTIONS)
                                                for line in removed if line in old_index):
            defaults = default_edits(old_tree, new_tree)
            if defaults:
                calls = set(direct_readers) | self.unmeasured | self.external
                during_import = False
                for function in defaults:
                    lines = range(function.body[0].lineno, function.end_lineno + 1)
                    covered, initial = self.line_tests(path, lines)
                    calls.update(covered | self.symbol_tests(function.name))
                    during_import |= initial
                if not during_import:
                    return calls
        if any(name.lower() in self.dictionary_values for line in removed if line in old_index
               for name in bindings(old_index[line].node)):
            dictionaries[path] = dict_value_edits(*focus)
        tests = set(direct_readers)
        initialization = set()
        affected_lines = set()
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
            affected_lines.update(run_lines)
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
        frontend = path in FRONTEND_MODULES or path.startswith((
            'sqlglot/optimizer/', 'sqlglot/parsers/', 'sqlglot/generators/', 'sqlglot/typing/', 'sqlglot/dialects/'))
        if self.checkpoints and frontend and not initialization and not inserts and removed and added:
            old_scopes = self.runtime_scopes(old_index, removed)
            new_scopes = self.runtime_scopes(new_index, added)
            if old_scopes and old_scopes == new_scopes:
                body_edits[path] = affected_lines
        return tests | self.unmeasured

    @staticmethod
    def runtime_scopes(index, changed):
        scopes = set()
        for line in changed:
            stmt = index.get(line)
            if (not stmt or not stmt.functions or not stmt.runtime
                    or isinstance(stmt.node, (ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal))):
                return set()
            scopes.add((stmt.classes, stmt.functions))
        return scopes

    def select(self, patch):
        tests = set()
        replacements = {}
        equality_sites = {}
        predicates = {}
        body_edits = {}
        dictionaries = {}
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
                    bounds = []
                    affected = self.python_tests(change, replacements, equality_sites, predicates,
                                                 body_edits, dictionaries, bounds)
                    tests.update(affected.intersection(*bounds) if bounds else affected)
        except (OSError, UnicodeError, SyntaxError, ValueError) as error:
            print(f'{patch.name}: {error}', file=sys.stderr)
            return self.all, {}, {}
        if can_probe and len(replacements) == 1:
            path = next(iter(replacements))
            safe = self.unchanged_comparisons(tests, equality_sites.get(path), predicates.get(path))
            protected = self.unmeasured | self.external | self.file_tests(path)
            tests.difference_update(safe - protected)
            words = dictionaries.get(path)
            if words and words.keys() <= self.dictionary_values.keys():
                readers = set()
                for word, keys in words.items():
                    uses = set()
                    boot = False
                    for module, lines in self.dictionary_values[word].items():
                        observed, initial = self.line_tests(module, lines)
                        boot |= initial
                        uses.update(observed)
                    if boot:
                        readers.update(tests)
                        break
                    if f'lookup:{word}' in self.operands:
                        unknown, values = self.operands[f'lookup:{word}']
                        if unknown in self.initialization or any(group in self.initialization for _, group in values):
                            readers.update(tests)
                            break
                        unchanged = set()
                        affected = set(self.groups[unknown])
                        for value, group in values:
                            (affected if unpack(value) in keys else unchanged).update(self.groups[group])
                        uses.difference_update(unchanged - affected)
                    readers.update(uses)
                tests.intersection_update(readers | protected)
        if not can_probe:
            return tests, {}, {}
        return tests, replacements, body_edits if body_edits.keys() == replacements.keys() else {}

    def unchanged_comparisons(self, tests, identity, predicates):
        affected = set()
        if identity and identity <= self.relations.keys():
            for site in identity:
                for relation, group in self.relations[site].items():
                    if relation in ('identical', 'distinct'):
                        continue
                    if group in self.initialization:
                        return set()
                    affected.update(self.groups[group])
            return tests - affected
        if identity and identity <= self.comparisons.keys():
            for site in identity:
                _, uncertain = self.comparisons[site]
                if uncertain in self.initialization:
                    return set()
                affected.update(self.groups[uncertain])
            return tests - affected
        if not predicates or not predicates.keys() <= self.operands.keys():
            return set()
        for site, (before, after) in predicates.items():
            unknown, values = self.operands[site]
            if unknown in self.initialization:
                return set()
            affected.update(self.groups[unknown])
            for value, group in values:
                if not same_result(value, before, after):
                    if group in self.initialization:
                        return set()
                    affected.update(self.groups[group])
        # An unvisited predicate cannot introduce a new path by itself. The
        # structural match above established that the rest of the code is equal.
        return tests - affected

    def run_probe(self, replacements, names, timeout, checkpoints=None):
        request = {'files': replacements, 'tests': names, 'checkpoints': checkpoints}
        try:
            if sys.platform == 'linux':
                result = fork_probe(self.root, request, timeout)
            else:
                process = subprocess.run(
                    [sys.executable, str(Path(__file__).with_name('probe.py')), str(self.root)],
                    input=json.dumps(request), encoding='utf-8', stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, timeout=timeout, check=True,
                )
                result = json.loads(process.stdout)
            self.probe_cpu += result['cpu_seconds']
            return {self.by_name[name] for name in result['passed'] if name in self.by_name}
        except subprocess.TimeoutExpired:
            # Windows has no RUSAGE_CHILDREN. Charge the timeout conservatively there.
            self.probe_cpu += timeout
            return set()
        except (EOFError, OSError, ValueError, subprocess.SubprocessError):
            return set()

    def eligible_executor_cases(self, tests, body_edits):
        cache = {}
        for name, paths in self.executor_cases.items():
            number = self.by_name.get(name)
            if number not in tests or number in self.unmeasured or name not in self.checkpoints:
                continue
            executor = self.executor_lines.get(name, {})
            shared = self.executor_shared.get(name, {})
            if any((executor.get(path, set()) | shared.get(path, set())) & lines
                   for path, lines in body_edits.items()):
                continue
            indices = {index for path, lines in body_edits.items() for line in lines
                       for index in paths.get(path, {}).get(str(line), ())}
            if 0 < len(indices) <= 30:
                cache[name] = {**self.checkpoints[name], 'cases': sorted(indices)}
        return cache

    def refine(self, tests, replacements, durations, body_edits):
        remaining = self.probe_deadline - time.monotonic()
        if not tests or not replacements or remaining < 1:
            return tests
        if body_edits and remaining > 4:
            cache = self.eligible_executor_cases(tests, body_edits)
            eligible = {self.by_name[name] for name in cache}
            case_count = sum(len(item['cases']) for item in cache.values())
            if (eligible and scheduled_cost(tests, durations) - scheduled_cost(tests - eligible, durations)
                    > 4500 + 150 * case_count):
                names = sorted(cache, key=lambda name: len(cache[name]['cases']))
                tests = tests - (eligible & self.run_probe(replacements, names, min(5, remaining), cache))
                remaining = self.probe_deadline - time.monotonic()
                if remaining < 1:
                    return tests
        multiprocessing = self.symbol_tests('ProcessPoolExecutor') - self.by_file['tests/test_optimizer.py']
        protected = self.external | self.unmeasured | multiprocessing
        protected.update(*(self.file_tests(path) for path in replacements))
        candidates = tests - protected
        expected = sum(durations[number] for number in candidates)
        if not candidates or expected > 2000:
            return tests
        retained = tests & protected
        if retained:
            savings = scheduled_cost(tests, durations) - scheduled_cost(retained, durations)
            if savings <= expected + 250:
                return tests
        names = [self.catalog[number]['id'] for number in sorted(candidates)]
        return tests - (candidates & self.run_probe(replacements, names, min(4, remaining)))


def fork_worker(output, root, request):
    from probe import execute_request

    with output, open(os.devnull, 'w') as sink:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
        output.send(execute_request(root, request))


def fork_probe(root, request, timeout):
    # Preload third-party dependencies once. Each child imports SQLGlot and
    # the tests from scratch, after installing its own private patch overlay.
    if any(name.startswith(('tests/test_optimizer.py', 'tests/test_executor.py')) for name in request['tests']):
        import pandas.testing
    ctx = multiprocessing.get_context('fork')
    reader, writer = ctx.Pipe(duplex=False)
    child = ctx.Process(target=fork_worker, args=(writer, root, request))
    with reader, writer:
        child.start()
        writer.close()
        try:
            if reader.poll(timeout):
                return reader.recv()
            return {'passed': [], 'cpu_seconds': 0}
        finally:
            if child.is_alive():
                child.kill()
            child.join()
            child.close()


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


class CatalogGroups:
    def __init__(self, groups, numbers):
        self.groups = groups
        self.numbers = numbers

    @lru_cache(maxsize=None)
    def __getitem__(self, group):
        return frozenset(n for i in self.groups[group] if (n := self.numbers[i]) is not None)


def changed_statement(before, after):
    """Find a statement containing every AST difference, or use the whole module."""
    whole = (before, after)
    focus = None
    pairs = [(before, after, whole)]
    while pairs:
        a, b, parent = pairs.pop()
        if type(a) is type(b):
            if isinstance(a, ast.AST):
                if isinstance(a, ast.stmt):
                    parent = (a, b)
                pairs.extend((getattr(a, f), getattr(b, f), parent) for f in a._fields)
                continue
            if isinstance(a, list):
                if len(a) == len(b):
                    pairs.extend((x, y, parent) for x, y in zip(a, b))
                    continue
            elif a == b and (not isinstance(a, (float, complex)) or repr(a) == repr(b)):
                continue
        if focus is not None and focus != parent:
            return whole
        focus = parent
    return focus


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
        numbers, replacements, body_edits = selector.select(patch)
        if not args.coverage_only:
            numbers = selector.refine(numbers, replacements, durations, body_edits)
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
