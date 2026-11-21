"""Collect line, file, and subprocess dependencies from unmodified SQLGlot."""

import ast
import gzip
import importlib.machinery
import json
import os
import re
import sys
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

import pytest

from branches import BranchTransform
from comparisons import eligible, site_for
from dispatch import DispatchProfile, DispatchTransform, scalar_inventory
from mapflow import find_flows, spelling
from operands import NO_VALUE, describe, pack
from replay_cache import EXECUTOR_TESTS, InlineExecutor, checkpoint_key, digest


def case_of(context):
    if context.startswith('case:'):
        _, index, test = context.split(':', 2)
        return test, int(index)
    return context, None


class LineTrace:
    """Remember each line once per pytest context, even inside large loops."""

    def __init__(self, root):
        self.root = os.path.normcase(str(root)) + os.sep
        self.tool = sys.monitoring.COVERAGE_ID
        self.context = ''
        self.paths = {}
        self.instrumented = {}
        self.hits = defaultdict(lambda: defaultdict(set))
        self.dispatch = None
        self.import_depth = 0
        self.virtual = defaultdict(dict)
        self.branch_hits = {}

    def virtual_line(self, path, key):
        by_line = self.virtual[path]
        number = 100000 + len(by_line)
        by_line[number] = key
        self.branch_hits.setdefault(key, set())
        return number

    def relative(self, filename):
        if filename not in self.paths:
            normalized = os.path.normcase(filename)
            path = normalized[len(self.root):].replace(os.sep, '/') if normalized.startswith(self.root) else ''
            self.paths[filename] = path if path.startswith(('sqlglot/', 'tests/', 'sqlglotc/')) else ''
        return self.paths[filename]

    def entered(self, code, offset):
        if self.dispatch and (callback := self.dispatch.calls.get(id(code))):
            self.dispatch.hits[callback[1]].add('' if self.import_depth else self.context)
        if id(code) not in self.instrumented and self.relative(code.co_filename):
            sys.monitoring.set_local_events(self.tool, code, sys.monitoring.events.LINE)
            self.instrumented[id(code)] = code
        return sys.monitoring.DISABLE

    def line(self, code, number):
        path = self.relative(code.co_filename)
        if number >= 100000 and number in self.virtual.get(path, ()):
            self.branch_hits[self.virtual[path][number]].add('' if self.import_depth else self.context)
        else:
            self.hits[path][number].add(self.context)
        return sys.monitoring.DISABLE

    def switch_context(self, context):
        self.context = context
        sys.monitoring.restart_events()

    def start(self):
        sys.monitoring.use_tool_id(self.tool, 'sqlglot-dependencies')
        sys.monitoring.register_callback(self.tool, sys.monitoring.events.PY_START, self.entered)
        sys.monitoring.register_callback(self.tool, sys.monitoring.events.LINE, self.line)
        sys.monitoring.set_events(self.tool, sys.monitoring.events.PY_START)

    def stop(self):
        sys.monitoring.set_events(self.tool, 0)
        sys.monitoring.register_callback(self.tool, sys.monitoring.events.LINE, None)
        sys.monitoring.register_callback(self.tool, sys.monitoring.events.PY_START, None)
        sys.monitoring.free_tool_id(self.tool)


class Dependencies:
    def __init__(self, root, tracer):
        self.root = os.path.normcase(str(root)) + os.sep
        self.tracer = tracer
        self.current = ''
        self.files = defaultdict(set)
        self.dialects = defaultdict(set)
        self.external = set()
        self.status = {}
        self.collection_errors = []
        self.checkpoints = defaultdict(lambda: defaultdict(set))
        self.reference_sql = defaultdict(dict)

    def trace_executor(self):
        from sqlglot.executor.python import PythonExecutor
        from tests import test_executor

        original_init = PythonExecutor.__init__
        original_execute = PythonExecutor.execute
        original_transpile = test_executor.transpile
        original_parser = test_executor.parse_one
        original_subtest = test_executor.TestExecutor.subtestHelper
        parse_counts = defaultdict(int)

        def in_case(test, index, func, *args, **kwargs):
            context = self.tracer.context
            self.tracer.switch_context(f'case:{index}:{test}')
            try:
                return func(*args, **kwargs)
            finally:
                self.tracer.switch_context(context)

        class IndexedExecutor(InlineExecutor):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                parse_counts[self_test.current] = 0
                self.next_index = 0

            def submit(self, task, *args, **kwargs):
                active = self_test.current
                if active not in EXECUTOR_TESTS or task is not test_executor.mp_execute:
                    return super().submit(task, *args, **kwargs)
                index = self.next_index
                self.next_index += 1
                return in_case(active, index, super().submit, task, *args, **kwargs)

        self_test = self

        def traced_parser(*args, **kwargs):
            test = self.current
            caller = sys._getframe(1).f_code.co_qualname
            if test not in EXECUTOR_TESTS or not caller.startswith('TestExecutor._mp_execute'):
                return original_parser(*args, **kwargs)
            index = parse_counts[test]
            parse_counts[test] += 1
            return in_case(test, index, original_parser, *args, **kwargs)

        def traced_subtest(instance, index, *args, **kwargs):
            if self.current in EXECUTOR_TESTS:
                return in_case(self.current, index, original_subtest, instance, index, *args, **kwargs)
            return original_subtest(instance, index, *args, **kwargs)

        def traced_init(executor, *args, **kwargs):
            context = self.tracer.context
            if case_of(context)[0] not in EXECUTOR_TESTS:
                return original_init(executor, *args, **kwargs)
            self.tracer.switch_context('executor:' + context)
            try:
                return original_init(executor, *args, **kwargs)
            finally:
                self.tracer.switch_context(context)

        def traced_execute(executor, plan, outer_scope=None):
            context = self.tracer.context
            test, index = case_of(context)
            if test not in EXECUTOR_TESTS or index is None or outer_scope is not None:
                return original_execute(executor, plan, outer_scope)
            self.tracer.switch_context('checkpoint-metadata')
            try:
                key = checkpoint_key(executor, plan)
            except (ValueError, TypeError, AttributeError, RecursionError):
                key = None
            self.tracer.switch_context('executor:' + context)
            try:
                table = original_execute(executor, plan, outer_scope)
                if key is not None:
                    self.checkpoints[test][index].add(key)
                return table
            finally:
                self.tracer.switch_context(context)

        def traced_transpile(sql, *args, **kwargs):
            result = original_transpile(sql, *args, **kwargs)
            test, _ = case_of(self.tracer.context)
            if test in EXECUTOR_TESTS and kwargs.get('write') == 'duckdb':
                self.reference_sql[test][digest(sql)] = digest(result[0])
            return result

        PythonExecutor.__init__ = traced_init
        PythonExecutor.execute = traced_execute
        test_executor.ProcessPoolExecutor = IndexedExecutor
        test_executor.parse_one = traced_parser
        test_executor.transpile = traced_transpile
        test_executor.TestExecutor.subtestHelper = traced_subtest

    def pytest_sessionstart(self, session):
        from sqlglot.dialects.dialect import Dialect
        from tests import test_executor, test_optimizer

        # Run executor jobs in the traced process so their lines are associated
        # with the owning pytest item, including jobs using the spawn context.
        test_optimizer.ProcessPoolExecutor = InlineExecutor
        self.trace_executor()

        original = Dialect.get_or_raise.__func__
        families = {}

        @classmethod
        def record(cls, dialect):
            instance = original(cls, dialect)
            if self.current:
                kind = type(instance)
                paths = families.get(kind)
                if paths is None:
                    components = (kind, kind.parser_class, kind.generator_class, kind.tokenizer_class)
                    paths = families[kind] = {
                        base.__module__.replace('.', '/') + '.py'
                        for component in components
                        for base in component.__mro__
                        if base.__module__.startswith('sqlglot.')
                    }
                for path in paths:
                    self.dialects[path].add(self.current)
            return instance

        Dialect.get_or_raise = record

    def audit(self, event, args):
        if event in ('subprocess.Popen', 'os.posix_spawn', 'os.system'):
            if self.current:
                self.external.add(self.current)
            return
        if event != 'open' or not isinstance(args[0], str):
            return
        path = args[0]
        normalized = os.path.normcase(path)
        if normalized.startswith(self.root):
            path = normalized[len(self.root):].replace(os.sep, '/')
        elif not os.path.isabs(path):
            path = normalized.replace(os.sep, '/').removeprefix('./')
        else:
            return
        if not path.startswith(('tests/', 'sqlglot/', 'sqlglotc/')) or '__pycache__' in path:
            return
        if path.endswith('.py') and sys._getframe(1).f_code.co_filename.startswith('<frozen importlib'):
            return
        self.files[path].add(self.current)

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_setup(self, item):
        current = self.current
        self.current = 'setup:' + item.nodeid.rpartition('::')[0]
        self.tracer.switch_context(self.current)
        try:
            return (yield)
        finally:
            self.current = current
            self.tracer.switch_context(current)

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_protocol(self, item, nextitem):
        self.current = item.nodeid
        self.status.setdefault(item.nodeid, 'passed')
        self.tracer.switch_context(self.current)
        try:
            return (yield)
        finally:
            self.current = ''
            self.tracer.switch_context('')

    def pytest_runtest_logreport(self, report):
        if report.failed:
            self.status[report.nodeid] = 'failed'
        elif report.skipped and self.status.get(report.nodeid) != 'failed':
            self.status[report.nodeid] = 'skipped'

    def pytest_collectreport(self, report):
        if report.failed:
            self.collection_errors.append(report.nodeid)


class ComparisonTransform(ast.NodeTransformer):
    def __init__(self, path, lookups, scalar_words):
        self.path = path
        self.lookups = lookups
        self.scalar_words = scalar_words
        self.changed = False

    def watch(self, key, operand):
        self.changed = True
        return ast.copy_location(ast.Call(ast.Name('_blast_observe_operand', ast.Load()),
                                         [ast.Constant(key), operand], []), operand)

    def visit_Call(self, node):
        self.generic_visit(node)
        function = node.func
        if isinstance(function, ast.Attribute) and function.attr == 'get' and node.args:
            word = spelling(function.value)
            if word in self.lookups and not isinstance(node.args[0], ast.Starred):
                node.args[0] = self.watch('lookup:' + word, node.args[0])
        return node

    def visit_Subscript(self, node):
        self.generic_visit(node)
        word = spelling(node.value)
        if word in self.lookups and isinstance(node.ctx, ast.Load) and not isinstance(node.slice, ast.Slice):
            node.slice = self.watch('lookup:' + word, node.slice)
        return node

    def visit_Attribute(self, node):
        self.generic_visit(node)
        if isinstance(node.ctx, ast.Load) and node.attr in self.scalar_words:
            node.value = ast.copy_location(ast.Call(ast.Name('_blast_scalar', ast.Load()),
                [node.value, ast.Constant(node.attr)], []), node.value)
            self.changed = True
        return node

    def visit_Compare(self, node):
        self.generic_visit(node)
        if description := describe(node):
            position, operand, _, _ = description
            watch = self.watch(site_for(self.path, node), operand)
            if position == 0:
                node.left = watch
            else:
                node.comparators[0] = watch
            return node
        if not eligible(node):
            return node
        self.changed = True
        replacement = ast.Call(ast.Name('_blast_sample_compare', ast.Load()),
                               [ast.Constant(site_for(self.path, node)), node.left, node.comparators[0],
                                ast.Constant(isinstance(node.ops[0], ast.IsNot))], [])
        return ast.copy_location(replacement, node)


class ComparisonSource(importlib.machinery.SourceFileLoader):
    def __init__(self, original, path, observers, lookups, tracer):
        super().__init__(original.name, original.path)
        self.relative_path = path
        self.observers = observers
        self.lookups = lookups
        self.tracer = tracer

    def exec_module(self, module):
        module.__dict__.update(self.observers)
        self.tracer.import_depth += 1
        try:
            super().exec_module(module)
        finally:
            self.tracer.import_depth -= 1

    def get_code(self, fullname):
        data = self.get_data(self.path)
        tree = ast.parse(data, self.path)
        # Bind callbacks before rewriting expressions so their source positions
        # and factory identities refer to the untouched baseline.
        DispatchTransform(self.relative_path).visit(tree)
        ComparisonTransform(self.relative_path, self.lookups, self.tracer.dispatch.words).visit(tree)
        BranchTransform(self.relative_path, self.tracer).visit(tree)
        return compile(ast.fix_missing_locations(tree), self.path, 'exec', dont_inherit=True)


class ComparisonFinder(importlib.machinery.PathFinder):
    def __init__(self, tracer, flows):
        self.tracer = tracer
        self.observations = defaultdict(lambda: defaultdict(set))
        self.operands = defaultdict(dict)
        self.flows = flows
        self.lookups = defaultdict(set)
        for word, paths in flows.items():
            for path in paths:
                self.lookups[path].add(word)
        self.observers = {'_blast_branch_enabled': True,
                          '_blast_bind_lambda': tracer.dispatch.bind_lambda,
                          '_blast_bind_factory': tracer.dispatch.bind_factory,
                          '_blast_scalar': tracer.dispatch.read,
                          '_blast_sample_compare': self.observe,
                          '_blast_observe_operand': self.observe_operand}

    def find_spec(self, fullname, path=None, target=None):
        if fullname != 'sqlglot' and not fullname.startswith('sqlglot.'):
            return None
        spec = super().find_spec(fullname, path, target)
        if spec and isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            relative = self.tracer.relative(spec.origin)
            if relative:
                spec.loader = ComparisonSource(spec.loader, relative, self.observers, self.lookups.get(relative, ()), self.tracer)
        return spec

    def observe(self, site, left, right, inverted):
        identical = left is right
        if left is None or right is None:
            kind = type(right) if left is None else type(left)
            relation = ('identical' if identical else
                        'distinct' if distinct_from_none(kind) else 'unknown')
        elif pure_equality(type(left)) and pure_equality(type(right)):
            if left == right:
                relation = 'identical' if identical else 'equal_distinct'
            else:
                relation = 'nonreflexive' if identical else 'distinct'
        else:
            relation = 'unknown'
        self.observations[site][relation].add('' if self.tracer.import_depth else self.tracer.context)
        return not identical if inverted else identical

    def observe_operand(self, site, value):
        contexts = self.operands[site]
        context = self.tracer.context
        members = contexts.get(context, NO_VALUE)
        if members is None:
            return value
        datum = pack(value)
        if datum is NO_VALUE:
            contexts[context] = None
        elif members is NO_VALUE:
            contexts[context] = {datum}
        else:
            members.add(datum)
            if len(members) > 32:
                contexts[context] = None
        return value


@lru_cache(maxsize=None)
def pure_equality(kind):
    builtins = (object, str, bytes, int, float, type(None))
    return (type.__getattribute__(kind, '__eq__') in {base.__eq__ for base in builtins}
            and type.__getattribute__(kind, '__ne__') in {base.__ne__ for base in builtins})


@lru_cache(maxsize=None)
def distinct_from_none(kind):
    if pure_equality(kind) or kind in (list, tuple, dict, set, frozenset, bytearray, complex, range):
        return True
    # The pinned Expr comparison returns False for unlike concrete types
    # before hashing. Verify both inherited implementations by identity.
    core = sys.modules.get('sqlglot.expressions.core')
    expr = getattr(core, 'Expression', None)
    return (expr is not None and type.__getattribute__(kind, '__eq__') is expr.__eq__
            and type.__getattribute__(kind, '__ne__') is expr.__ne__)


def symbol_groups(root, files):
    tokens = re.compile(r'\b[A-Za-z_]\w*\b')
    groups = defaultdict(set)
    for path, points in files.items():
        if not path.endswith('.py'):
            continue
        source = (root / path).read_text(encoding='utf-8').split('\n')
        for line, group_id in points.items():
            number = int(line) - 1
            if not 0 <= number < len(source) or source[number].lstrip().startswith('#'):
                continue
            for token in tokens.findall(source[number]):
                groups[token].add(group_id)
    return {token: sorted(ids) for token, ids in groups.items()}


def save_map(output, tracer, plugin, finder):
    tests = sorted(set(plugin.status) | {''})
    ids = {name: number for number, name in enumerate(tests)}
    setups = defaultdict(list)
    for name in tests:
        setups['setup:' + name.rpartition('::')[0]].append(name)
    groups = []
    group_ids = {}

    def group(names):
        expanded = {case_of(item.removeprefix('executor:'))[0] for name in names for item in setups.get(name, [name])}
        members = tuple(sorted(ids[n] for n in expanded if n in ids))
        if members not in group_ids:
            group_ids[members] = len(groups)
            groups.append(members)
        return group_ids[members]

    lines = {path: {line: group(names) for line, names in sorted(points.items())
                    if names - {'checkpoint-metadata'}}
             for path, points in sorted(tracer.hits.items())}

    def operand_groups(contexts):
        by_value = defaultdict(set)
        unknown = set()
        for name, values in contexts.items():
            if values is None:
                unknown.add(name)
                continue
            for value in values:
                by_value[value].add(name)
        return [group(unknown), [(value, group(names)) for value, names in
                                 sorted(by_value.items(), key=lambda item: repr(item[0]))]]

    executor_tests = EXECUTOR_TESTS & plugin.status.keys()
    executor_lines = {test: defaultdict(list) for test in executor_tests}
    case_lines = {test: defaultdict(dict) for test in executor_tests}
    shared_lines = {test: defaultdict(list) for test in executor_tests}
    for path, points in sorted(tracer.hits.items()):
        if not path.startswith('sqlglot/'):
            continue
        for line, names in sorted(points.items()):
            by_test = defaultdict(set)
            for name in names:
                executor = name.startswith('executor:')
                test, index = case_of(name.removeprefix('executor:'))
                if test in executor_tests:
                    if executor:
                        executor_lines[test][path].append(line)
                    if index is None:
                        shared_lines[test][path].append(line)
                    else:
                        by_test[test].add(index)
                elif name.startswith('setup:'):
                    for member in setups.get(name, ()):
                        if member in executor_tests:
                            shared_lines[member][path].append(line)
            for test, indices in by_test.items():
                case_lines[test][path][line] = sorted(indices)
    artifact = {
        'tests': tests,
        'groups': groups,
        'lines': lines,
        'files': {path: group(names) for path, names in sorted(plugin.files.items())},
        'dialects': {path: group(names) for path, names in sorted(plugin.dialects.items())},
        'external': sorted({item for name in plugin.external for item in setups.get(name, [name])}),
        'status': plugin.status,
        'collection_errors': plugin.collection_errors,
        'symbols': symbol_groups(Path.cwd(), lines),
        'relations': {site: {relation: group(names) for relation, names in values.items()}
                      for site, values in finder.observations.items()},
        'operands': {site: operand_groups(contexts) for site, contexts in sorted(finder.operands.items())},
        'dictionary_values': finder.flows,
        'dispatch': {site: group(names) for site, names in tracer.dispatch.hits.items()},
        'branches': {site: group(names) for site, names in tracer.branch_hits.items()},
        'executor_checkpoints': {name: {'plans': {index: sorted(keys) for index, keys in plugin.checkpoints[name].items()},
                                       'reference_sql': plugin.reference_sql[name]}
                                 for name in sorted(executor_tests)},
        'executor_lines': executor_lines,
        'executor_cases': case_lines,
        'executor_shared': shared_lines,
    }
    payload = json.dumps(artifact, separators=(',', ':'), sort_keys=True).encode()
    output.write_bytes(gzip.compress(payload, compresslevel=6, mtime=0))
    print(f'Mapped {len(tests) - 1} tests, {len(lines)} source files and {len(groups)} test groups.')


def main():
    root = Path(sys.argv[1]).resolve()
    output = Path(sys.argv[2]).resolve()
    args = sys.argv[3:] or ['tests']
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ.update(SKIP_INTEGRATION='0', PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
    tracer = LineTrace(root)
    tracer.dispatch = DispatchProfile(tracer, scalar_inventory(root))
    comparison_finder = ComparisonFinder(tracer, find_flows(root))
    sys.meta_path.insert(0, comparison_finder)
    plugin = Dependencies(root, tracer)
    sys.addaudithook(plugin.audit)
    tracer.start()
    result = pytest.main(['-q', '--tb=no', '-p', 'no:cacheprovider', '-p', 'pytest_timeout',
                          '--timeout=240', *args], plugins=[plugin])
    tracer.stop()
    if int(result) not in (0, 1) or not plugin.status:
        raise SystemExit(f'Coverage collection did not finish: pytest exit {result}')
    save_map(output, tracer, plugin, comparison_finder)


if __name__ == '__main__':
    main()
