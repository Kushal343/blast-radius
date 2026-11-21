"""Collect line, file, and subprocess dependencies from unmodified SQLGlot."""

import ast
import gzip
import importlib.machinery
import json
import os
import pickle
import re
import sys
from collections import defaultdict
from concurrent.futures import Future
from functools import lru_cache
from pathlib import Path

import pytest

from comparisons import eligible, site_for


class LineTrace:
    """Remember each line once per pytest context, even inside large loops."""

    def __init__(self, root):
        self.root = os.path.normcase(str(root)) + os.sep
        self.tool = sys.monitoring.COVERAGE_ID
        self.context = ''
        self.paths = {}
        self.instrumented = {}
        self.hits = defaultdict(lambda: defaultdict(set))

    def relative(self, filename):
        if filename not in self.paths:
            normalized = os.path.normcase(filename)
            path = normalized[len(self.root):].replace(os.sep, '/') if normalized.startswith(self.root) else ''
            self.paths[filename] = path if path.startswith(('sqlglot/', 'tests/', 'sqlglotc/')) else ''
        return self.paths[filename]

    def entered(self, code, offset):
        if id(code) not in self.instrumented and self.relative(code.co_filename):
            sys.monitoring.set_local_events(self.tool, code, sys.monitoring.events.LINE)
            self.instrumented[id(code)] = code
        return sys.monitoring.DISABLE

    def line(self, code, number):
        self.hits[self.relative(code.co_filename)][number].add(self.context)
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

    def pytest_sessionstart(self, session):
        from sqlglot.dialects.dialect import Dialect
        from tests import test_executor, test_optimizer

        # Run executor jobs in the traced process so their lines are associated
        # with the owning pytest item, including jobs using the spawn context.
        test_executor.ProcessPoolExecutor = TracedExecutor
        test_optimizer.ProcessPoolExecutor = TracedExecutor

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


class TracedExecutor:
    def __init__(self, *, mp_context, initializer=None, initargs=()):
        if initializer is not None:
            initializer(*initargs)

    def __enter__(self):
        return self

    def __exit__(self, *exception):
        return False

    def shutdown(self):
        pass

    def submit(self, task, *args, **kwargs):
        result = Future()
        try:
            task, args, kwargs = pickle.loads(pickle.dumps((task, args, kwargs)))
            value = pickle.loads(pickle.dumps(task(*args, **kwargs)))
            result.set_result(value)
        except Exception as error:
            result.set_exception(error)
        return result


class ComparisonTransform(ast.NodeTransformer):
    def __init__(self, path):
        self.path = path
        self.changed = False

    def visit_Compare(self, node):
        self.generic_visit(node)
        if not eligible(node):
            return node
        self.changed = True
        replacement = ast.Call(ast.Name('_blast_sample_compare', ast.Load()),
                               [ast.Constant(site_for(self.path, node)), node.left, node.comparators[0],
                                ast.Constant(isinstance(node.ops[0], ast.IsNot))], [])
        return ast.copy_location(replacement, node)


class ComparisonSource(importlib.machinery.SourceFileLoader):
    def __init__(self, original, path, observe):
        super().__init__(original.name, original.path)
        self.relative_path = path
        self.observe = observe

    def exec_module(self, module):
        module.__dict__['_blast_sample_compare'] = self.observe
        super().exec_module(module)

    def get_code(self, fullname):
        data = self.get_data(self.path)
        if b'self.' in data and b' is ' in data:
            tree = ast.parse(data, self.path)
            transformer = ComparisonTransform(self.relative_path)
            transformer.visit(tree)
            if transformer.changed:
                return compile(ast.fix_missing_locations(tree), self.path, 'exec', dont_inherit=True)
        return super().get_code(fullname)


class ComparisonFinder(importlib.machinery.PathFinder):
    def __init__(self, tracer):
        self.tracer = tracer
        self.observations = defaultdict(dict)

    def find_spec(self, fullname, path=None, target=None):
        if fullname != 'sqlglot' and not fullname.startswith('sqlglot.'):
            return None
        spec = super().find_spec(fullname, path, target)
        if spec and isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            relative = self.tracer.relative(spec.origin)
            if relative:
                spec.loader = ComparisonSource(spec.loader, relative, self.observe)
        return spec

    def observe(self, site, left, right, inverted):
        identical = left is right
        old_result = not identical if inverted else identical
        contexts = self.observations[site]
        context = self.tracer.context
        if contexts.get(context) is not False:
            safe = pure_equality(type(left)) and pure_equality(type(right))
            if safe:
                new_result = left != right if inverted else left == right
                safe = new_result is old_result
            contexts[context] = safe
        return old_result


@lru_cache(maxsize=None)
def pure_equality(kind):
    builtins = (object, str, bytes, int, float, type(None))
    return (type.__getattribute__(kind, '__eq__') in {base.__eq__ for base in builtins}
            and type.__getattribute__(kind, '__ne__') in {base.__ne__ for base in builtins})


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


def save_map(output, tracer, plugin, comparisons):
    tests = sorted(set(plugin.status) | {''})
    ids = {name: number for number, name in enumerate(tests)}
    setups = defaultdict(list)
    for name in tests:
        setups['setup:' + name.rpartition('::')[0]].append(name)
    groups = []
    group_ids = {}

    def group(names):
        expanded = {item for name in names for item in setups.get(name, [name])}
        members = tuple(sorted(ids[n] for n in expanded if n in ids))
        if members not in group_ids:
            group_ids[members] = len(groups)
            groups.append(members)
        return group_ids[members]

    lines = {path: {line: group(names) for line, names in sorted(points.items())}
             for path, points in sorted(tracer.hits.items())}

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
        'comparisons': {site: [group(name for name, safe in contexts.items() if safe),
                               group(name for name, safe in contexts.items() if not safe)]
                        for site, contexts in sorted(comparisons.items())},
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
    comparison_finder = ComparisonFinder(tracer)
    sys.meta_path.insert(0, comparison_finder)
    plugin = Dependencies(root, tracer)
    sys.addaudithook(plugin.audit)
    tracer.start()
    result = pytest.main(['-q', '--tb=no', '-p', 'no:cacheprovider', '-p', 'pytest_timeout',
                          '--timeout=240', *args], plugins=[plugin])
    tracer.stop()
    if int(result) not in (0, 1) or not plugin.status:
        raise SystemExit(f'Coverage collection did not finish: pytest exit {result}')
    save_map(output, tracer, plugin, comparison_finder.observations)


if __name__ == '__main__':
    main()
